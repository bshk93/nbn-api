"""Regression tests for backup_to_drive.py — the weekly off-site tarball
(dev-deploy spec, Phase 2 item 11).

Everything here is about what must **not** be in the archive. The tarball leaves
the box for a third-party provider, so a credential in it is a credential
published; and the guard has to survive members.json changing shape, which is
why the redaction is asserted after the fact rather than trusted.

The other property worth pinning is that `members.json` is redacted rather than
dropped. A member's token is one rotation to replace; their tenure history is
not replaceable at all, and dropping the file to protect the token would throw
away the irreplaceable half to protect the cheap one.

Nothing here touches Drive, the live data directory, or the network.

    venv/bin/python -m tests.test_drive_backup
"""
from __future__ import annotations

import json
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_to_drive as bd  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


MEMBERS = {
    "kim":  {"token": "deadbeef", "roles": ["admin"], "tenures": [{"team": "PHX", "position": "owner"}]},
    "dave": {"token": "cafebabe", "roles": ["phx"], "tenures": []},
    "nobody": {"roles": [], "tenures": []},
}

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    work = tmp / "data"
    work.mkdir()
    (work / "members.json").write_text(json.dumps(MEMBERS))
    (work / "google-oauth.json").write_text('{"refresh_token": "SECRET"}')
    (work / "sessions.json").write_text('{"sid": "SECRET"}')
    (work / "tokens.json").write_text('{"legacy": "SECRET"}')
    (work / "transactions.json").write_text('[{"type": "trade"}]')
    (work / "allstats-25-26.csv").write_text("TEAM,DATE\nPHX,2026-01-01\n")

    tracked = ["members.json", "google-oauth.json", "sessions.json", "tokens.json",
               "transactions.json", "allstats-25-26.csv", "deleted-since-snapshot.json"]
    bd.WORK_TREE = work
    bd.tracked_files = lambda: tracked

    dest = tmp / "nbs-backup-2026-08-19.tar.gz"
    count, raw = bd.build_tarball(dest)
    bd.verify(dest)

    with tarfile.open(dest) as tar:
        names = set(tar.getnames())
        members = json.loads(tar.extractfile("members.json").read())

    check("the data files are in", {"transactions.json", "allstats-25-26.csv"} <= names)
    check("the Drive credential is not", "google-oauth.json" not in names)
    check("session ids are not", "sessions.json" not in names)
    check("the legacy token file is not", "tokens.json" not in names)
    check("a file deleted since the last snapshot is skipped, not fatal",
          "deleted-since-snapshot.json" not in names and count == 3, count)

    check("members.json is kept", "members.json" in names)
    check("every token in it is redacted",
          all(v.get("token") in (None, "REDACTED") for v in members.values()), members)
    check("roles survive", members["kim"]["roles"] == ["admin"])
    check("tenures survive", members["kim"]["tenures"][0]["team"] == "PHX")
    check("a member with no token is untouched", "token" not in members["nobody"])

    # ── The verification must actually catch a leak ───────────────────────────
    bad = tmp / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(work / "google-oauth.json", arcname="google-oauth.json")
    try:
        bd.verify(bad)
        check("verify() rejects a tarball containing a credential", False, "no SystemExit")
    except SystemExit as exc:
        check("verify() rejects a tarball containing a credential", "google-oauth.json" in str(exc))

    unredacted = tmp / "unredacted.tar.gz"
    with tarfile.open(unredacted, "w:gz") as tar:
        tar.add(work / "members.json", arcname="members.json")
    try:
        bd.verify(unredacted)
        check("verify() rejects members.json with live tokens", False, "no SystemExit")
    except SystemExit as exc:
        check("verify() rejects members.json with live tokens", "token" in str(exc))

    # ── A shape change must fail loudly, not ship tokens ──────────────────────
    (work / "members.json").write_text(json.dumps({"kim": {"secret": "moved-field"}}))
    try:
        bd.redacted_members(work / "members.json")
        check("a members.json with no recognisable token field is refused", False, "no SystemExit")
    except SystemExit as exc:
        check("a members.json with no recognisable token field is refused", "redact" in str(exc))

# ── Pruning may only ever touch this script's own files ───────────────────────
check("the prune pattern matches a real backup name",
      bool(bd.NAME_RE.match("nbs-backup-2026-08-19.tar.gz")))
for other in ["trade-PHX-LAL.xlsx", "nbs-backup-notes.txt", "nbs-backup-2026-08-19.tar.gz.bak",
              "my nbs-backup-2026-08-19.tar.gz"]:
    check(f"and not {other!r}", not bd.NAME_RE.match(other))

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_drive_backup: all pass")

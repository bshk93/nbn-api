#!/usr/bin/env python3
"""Weekly off-site tarball of the league data (dev-deploy spec, Phase 2 item 11).

This is the **third** tier of the backup story and the only one in a different
failure domain from the other two:

  1. `nbs-snapshot` — history, locally, every 10 minutes (threat: logical corruption)
  2. push to `bshk93/nbn-data` — off this box (threat: disk or VPS loss)
  3. **here** — off GitHub too (threat: provider or account loss)

The set is exactly what tier 1 tracks (`git ls-files` against the snapshot repo),
so the tarball and the git backup can never describe different things. Ignored
paths — `derived/`, `public/`, the `.rds` bulk — are ignored here for the same
reason they are ignored there: the build regenerates them, and a restore drill
proved it (86/86 derived CSVs byte-identical from a bare clone, 2026-08-19).

**Credentials are the one exclusion, and it is not all-or-nothing.**
`google-oauth.json` (this script's own credential) is dropped outright. So are
`sessions.json` and `tokens.json` if they ever reappear. `members.json` is
**redacted, not dropped**: every `token` value becomes `"REDACTED"`, and roles,
tenures and names — the half that is genuinely irreplaceable — are kept. A lost
token is one rotation; a lost tenure history is gone. The redaction is asserted
before upload, so a shape change in members.json fails the run rather than
silently shipping tokens.

Uploads use the existing `drive.file` credential, which can only see files it
created itself — it cannot read the rest of the Drive. **Nothing here is
shared**: unlike the trade-sheet export, no permission is granted, so the
tarball is visible only to the account that owns the Drive.

Old backups are trashed (recoverable for 30 days), never permanently deleted,
only when they match this script's own name pattern, and only beyond the newest
`--keep`.

    venv/bin/python backup_to_drive.py                # tar, upload, prune
    venv/bin/python backup_to_drive.py --dry-run      # build the tarball, upload nothing
    venv/bin/python backup_to_drive.py --keep 24
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from routers.google_sheets import (  # noqa: E402
    FILES_URL, UPLOAD_URL, _access_token_for, _load_creds,
)

GIT_DIR = Path(os.environ.get("NBS_GIT_DIR", "/var/lib/nbs-backup.git"))
WORK_TREE = Path(os.environ.get("NBS_WORK_TREE", "/var/lib/nothing-but-stats"))

# Never leaves this box, under any circumstances.
EXCLUDE = {"google-oauth.json", "sessions.json", "tokens.json"}
# Kept, with its credentials stripped — see the module docstring.
REDACT = "members.json"

NAME_RE = re.compile(r"^nbs-backup-\d{4}-\d{2}-\d{2}\.tar\.gz$")
TARBALL_MIME = "application/gzip"


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", f"--git-dir={GIT_DIR}", f"--work-tree={WORK_TREE}", "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def redacted_members(path: Path) -> bytes:
    """members.json with every token blanked and everything else intact."""
    data = json.loads(path.read_text())
    redacted = 0
    for entry in data.values():
        if isinstance(entry, dict) and entry.get("token"):
            entry["token"] = "REDACTED"
            redacted += 1
    if not redacted:
        # Either the file is empty or its shape changed under us. Refuse rather
        # than upload something whose credentials we can no longer locate.
        raise SystemExit(f"FATAL: no tokens found to redact in {path} — check its shape "
                         f"before letting this leave the box")
    return (json.dumps(data, indent=2) + "\n").encode()


def build_tarball(dest: Path) -> tuple[int, int]:
    """Returns (files written, uncompressed bytes)."""
    files = tracked_files()
    count = 0
    raw = 0
    with tarfile.open(dest, "w:gz") as tar:
        for rel in files:
            if Path(rel).name in EXCLUDE:
                continue
            src = WORK_TREE / rel
            if not src.is_file():
                continue        # deleted since the last snapshot; the repo has it
            if Path(rel).name == REDACT:
                blob = redacted_members(src)
                info = tarfile.TarInfo(rel)
                info.size = len(blob)
                info.mtime = int(src.stat().st_mtime)
                info.mode = 0o600
                import io
                tar.addfile(info, io.BytesIO(blob))
                count += 1
                raw += len(blob)
                continue
            tar.add(src, arcname=rel)
            count += 1
            raw += src.stat().st_size
    return count, raw


def verify(dest: Path) -> None:
    """Open the tarball back up and prove the exclusions held. A backup nobody
    verified is the whole failure mode this phase exists to close."""
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        leaked = [n for n in names if Path(n).name in EXCLUDE]
        if leaked:
            raise SystemExit(f"FATAL: excluded file(s) present in the tarball: {leaked}")
        if REDACT in names:
            body = json.loads(tar.extractfile(REDACT).read())
            live = [k for k, v in body.items()
                    if isinstance(v, dict) and v.get("token") not in (None, "", "REDACTED")]
            if live:
                raise SystemExit(f"FATAL: {REDACT} in the tarball still carries {len(live)} token(s)")


def upload(dest: Path, creds: dict, token: str) -> str:
    metadata = {"name": dest.name, "mimeType": TARBALL_MIME,
                "description": "NBN league data backup — see dev-deploy-setup-spec.md Phase 2"}
    if creds.get("folder_id"):
        metadata["parents"] = [creds["folder_id"]]
    with httpx.Client(timeout=300) as client:
        # No permissions call afterwards, deliberately: this is the opposite of
        # the trade-sheet export, which shares publicly on purpose.
        resp = client.post(
            UPLOAD_URL,
            params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,name,size"},
            headers={"Authorization": f"Bearer {token}"},
            files={
                "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
                "file": (dest.name, dest.read_bytes(), TARBALL_MIME),
            },
        )
    if resp.status_code not in (200, 201):
        raise SystemExit(f"FATAL: Drive upload failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json()["id"]


def prune(creds: dict, token: str, keep: int) -> list[str]:
    """Trash all but the newest `keep` backups. Trash, not delete — 30 days of
    grace on a mistake here, and the alternative is unrecoverable."""
    params = {
        "q": "name contains 'nbs-backup-' and trashed = false",
        "orderBy": "name desc",
        "fields": "files(id,name,createdTime)",
        "pageSize": "200",
        "supportsAllDrives": "true",
    }
    if creds.get("folder_id"):
        params["q"] += f" and '{creds['folder_id']}' in parents"
    with httpx.Client(timeout=60) as client:
        resp = client.get(FILES_URL, params=params,
                          headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            print(f"WARN: could not list backups to prune ({resp.status_code})", file=sys.stderr)
            return []
        # The name pattern is re-checked per file: `name contains` is a substring
        # match, and nothing else in this folder may ever be trashed by accident.
        ours = [f for f in resp.json().get("files", []) if NAME_RE.match(f["name"])]
        trashed = []
        for f in ours[keep:]:
            r = client.patch(f"{FILES_URL}/{f['id']}",
                             params={"supportsAllDrives": "true"},
                             headers={"Authorization": f"Bearer {token}"},
                             json={"trashed": True})
            if r.status_code == 200:
                trashed.append(f["name"])
            else:
                print(f"WARN: could not trash {f['name']} ({r.status_code})", file=sys.stderr)
    return trashed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keep", type=int, default=12, help="backups to retain on Drive (default 12)")
    ap.add_argument("--dry-run", action="store_true", help="build and verify the tarball, upload nothing")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--out", type=Path, default=None, help="keep the tarball at this path too")
    args = ap.parse_args()

    if not GIT_DIR.is_dir():
        print(f"FATAL: {GIT_DIR} does not exist — tier 1 of the backup is missing", file=sys.stderr)
        return 2

    name = f"nbs-backup-{datetime.now(timezone.utc):%Y-%m-%d}.tar.gz"
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / name
        count, raw = build_tarball(dest)
        verify(dest)
        size = dest.stat().st_size
        print(f"{name}: {count} file(s), {raw/1e6:.1f}MB -> {size/1e6:.1f}MB gzipped")
        if args.out:
            args.out.write_bytes(dest.read_bytes())
            print(f"  copy kept at {args.out}")
        if args.dry_run:
            print("  dry run — nothing uploaded")
            return 0

        creds = _load_creds()
        token = _access_token_for(creds)
        file_id = upload(dest, creds, token)
        print(f"  uploaded to Drive as {file_id}")
        if not args.no_prune:
            trashed = prune(creds, token, args.keep)
            print(f"  pruned {len(trashed)}: {', '.join(trashed) if trashed else 'nothing older than the newest ' + str(args.keep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

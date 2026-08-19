#!/usr/bin/env python3
"""Weekly integrity check on the raw box score CSVs (dev-deploy spec, Phase 2 item 10).

The threat this answers is **logical corruption**, which is first on the spec's
list for a reason: a bad parse, a bad manual fix, or a script that rewrites where
it meant to append. A mirroring backup copies that damage faithfully, and the
snapshot timer pushes it off-box within ten minutes. Nothing notices. This is
what turns "discovered never" into "discovered within a week".

Two properties, both cheap and both absolute:

  * **Row counts only ever go up.** These files are appended to, one game at a
    time, and never edited. A count that fell is a truncation.
  * **A closed season never changes at all.** Once a season is over, its file is
    finished; its `sha256` must match byte for byte, forever. Only the current
    season's two files are allowed to differ from the last check, and only by
    growing.

State lives in `stats-integrity.json` in the data directory, which the snapshot
timer tracks — so the manifest's own history is off-box alongside the files it
describes.

**A violation is not accepted into the manifest.** The stored entry stays as it
was, so the next run compares against the last *good* state and alerts again.
A check that quietly re-baselined itself would report each corruption exactly
once and then call it the new normal. Re-baseline deliberately with `--accept`
once a human has resolved the finding.

Alerting is Discord (`DISCORD_ALERT_CHANNEL`, inert if unset) **and** a non-zero
exit, so a violation is visible in `systemctl status` / the journal even with no
channel configured — the same argument `nbs-snapshot` makes about a failed push.

    venv/bin/python check_stats_integrity.py            # check, alert, exit 1 on violation
    venv/bin/python check_stats_integrity.py --accept   # re-baseline after resolving one
    venv/bin/python check_stats_integrity.py --data-dir /tmp/restore-drill
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routers import discord_transport as transport   # noqa: E402
from routers.storage import _current_season_str      # noqa: E402

DISCORD_ALERT_CHANNEL = os.environ.get("DISCORD_ALERT_CHANNEL", "").strip()
DISCORD_ADMIN_ID = os.environ.get("DISCORD_ADMIN_ID", "").strip()

# One scheduled run a week, and at most a couple of messages from it. Sized to
# catch a loop, not to shape a feed.
MAX_BURST = 5
BURST_WINDOW = 3600

MANIFEST_NAME = "stats-integrity.json"


def _season_files(season: str) -> set[str]:
    return {f"allstats-{season}.csv", f"allstats-playoffs-{season.split('-')[-1]}.csv"}


def _end_year(name: str) -> int:
    """The calendar year a file's season ends in — `allstats-25-26.csv` and
    `allstats-playoffs-26.csv` are both 26, which is what makes them one season."""
    stem = name[len("allstats-"):-len(".csv")].replace("playoffs-", "")
    try:
        return int(stem.split("-")[-1])
    except ValueError:
        return -1


def _live_files(season: str, names) -> set[str]:
    """The files still legitimately being appended to. Everything else is closed
    and must not change at all.

    Two seasons can qualify, and deliberately so. The stats clock rolls over on
    **July 1** (`_current_season_str`), while a playoff run can finish well after
    it — the 25-26 finals were played 2026-06-18, and a slower postseason would
    have landed in July against a clock already reading 26-27. Freezing last
    season the moment the calendar turned would alert on a real game. So the
    newest season on disk is live alongside the current one; every season behind
    it is frozen, which is the property that matters.
    """
    live = _season_files(season)
    newest = max((_end_year(n) for n in names), default=-1)
    if newest >= 0:
        live |= {n for n in names if _end_year(n) == newest}
    return live


def scan(data_dir: Path) -> dict[str, dict]:
    """`{filename: {rows, sha256, bytes}}` for every raw box score file.

    Rows are counted with the csv reader rather than by counting newlines, so a
    quoted field containing one can never be miscounted as a missing game.
    """
    out: dict[str, dict] = {}
    paths = sorted(data_dir.glob("allstats-*.csv"))
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        with path.open(newline="") as fh:
            rows = sum(1 for _ in csv.reader(fh)) - 1     # minus the header
        out[path.name] = {
            "rows": max(rows, 0),
            "sha256": digest.hexdigest(),
            "bytes": path.stat().st_size,
        }
    return out


def compare(current: dict[str, dict], previous: dict[str, dict], season: str
            ) -> tuple[list[str], list[str]]:
    """`(violations, notes)`. Violations are alert-worthy; notes are the normal
    week-to-week movement, reported so a silent run is distinguishable from a
    run that saw nothing."""
    violations: list[str] = []
    notes: list[str] = []
    live = _live_files(season, set(current) | set(previous))

    for name, prev in sorted(previous.items()):
        cur = current.get(name)
        if cur is None:
            violations.append(f"{name}: GONE — was {prev['rows']} rows, no file on disk now")
            continue
        if cur["rows"] < prev["rows"]:
            violations.append(
                f"{name}: LOST {prev['rows'] - cur['rows']} rows "
                f"({prev['rows']} → {cur['rows']})"
            )
        elif name not in live and cur["sha256"] != prev["sha256"]:
            # Grew or was edited in place, but the season is over: either way the
            # file should have been finished. Row count alone would miss an edit.
            violations.append(
                f"{name}: CHANGED but the season is closed "
                f"({prev['rows']} → {cur['rows']} rows, sha256 "
                f"{prev['sha256'][:12]} → {cur['sha256'][:12]})"
            )
        elif cur["rows"] > prev["rows"]:
            notes.append(f"{name}: +{cur['rows'] - prev['rows']} rows ({cur['rows']} total)")

    for name in sorted(set(current) - set(previous)):
        notes.append(f"{name}: new to the manifest ({current[name]['rows']} rows)")

    return violations, notes


def alert(violations: list[str], data_dir: Path) -> bool:
    """Post the violations to Discord. Returns False when nothing was sent."""
    if not transport.configured(DISCORD_ALERT_CHANNEL):
        return False
    mention = f"<@{DISCORD_ADMIN_ID}> " if DISCORD_ADMIN_ID else ""
    body = "\n".join(f"• {v}" for v in violations[:15])
    if len(violations) > 15:
        body += f"\n• …and {len(violations) - 15} more"
    text = (
        f"{mention}**Box score integrity check FAILED** — {len(violations)} "
        f"violation(s) in `{data_dir}`\n{body}\n"
        f"These files are append-only and unrebuildable. Restore from `nbs-backup` "
        f"(`git --git-dir=/var/lib/nbs-backup.git log -- <file>`) before anything "
        f"else writes over them. Re-baseline with `check_stats_integrity.py --accept` "
        f"once resolved."
    )
    sent = transport.send(DISCORD_ALERT_CHANNEL, {"content": text[:1990]},
                          max_burst=MAX_BURST, burst_window=BURST_WINDOW)
    transport.flush()
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default="/var/lib/nothing-but-stats", type=Path)
    ap.add_argument("--manifest", default=None, type=Path,
                    help=f"defaults to <data-dir>/{MANIFEST_NAME}")
    ap.add_argument("--accept", action="store_true",
                    help="re-baseline the manifest, including files that violate")
    ap.add_argument("--no-alert", action="store_true", help="check and report, post nothing")
    args = ap.parse_args()

    data_dir: Path = args.data_dir
    manifest_path: Path = args.manifest or (data_dir / MANIFEST_NAME)
    if not data_dir.is_dir():
        print(f"FATAL: {data_dir} is not a directory", file=sys.stderr)
        return 2

    current = scan(data_dir)
    if not current:
        print(f"FATAL: no allstats-*.csv found in {data_dir}", file=sys.stderr)
        return 2

    stored = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    previous = stored.get("files", {})
    season = _current_season_str()

    violations, notes = compare(current, previous, season)
    if args.accept:
        violations, notes = [], notes + [f"re-baselined {len(current)} file(s) with --accept"]

    total = sum(f["rows"] for f in current.values())
    print(f"{len(current)} file(s), {total:,} rows, current season {season}"
          + ("" if previous else " — first run, seeding the manifest"))
    for n in notes:
        print(f"  {n}")
    for v in violations:
        print(f"  VIOLATION  {v}")

    # A violating file keeps its last good entry, so the next run compares against
    # that same known-good state and alerts again until a human uses --accept.
    bad = {v.split(":")[0] for v in violations}
    files = dict(previous)
    for name, info in current.items():
        if name not in bad:
            files[name] = info
    manifest_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_season": season,
        "files": files,
    }, indent=2) + "\n")

    if violations and not args.no_alert:
        posted = alert(violations, data_dir)
        print(f"  alert: {'posted to Discord' if posted else 'DISCORD_ALERT_CHANNEL unset — journal only'}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

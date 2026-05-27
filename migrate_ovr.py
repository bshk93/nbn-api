#!/usr/bin/env python3
"""
Migrate OVR out of roster CSVs into ovr-history.json.

Dry run by default. Pass --apply to write changes.

For each new-format roster CSV (has SLUG, no PLAYER):
  - Reads SLUG + OVR pairs
  - Appends {date, ovr} entries to ovr-history.json (today's date)
  - Rewrites the CSV without the OVR column

Legacy-format CSVs (has PLAYER column) are skipped.
"""

import csv
import io
import json
import sys
from datetime import date
from pathlib import Path

DATA_DIR = Path("/var/lib/nothing-but-stats")
OVR_FILE = DATA_DIR / "ovr-history.json"
TODAY = date.today().isoformat()
APPLY = "--apply" in sys.argv

TEAMS = [
    "atl","bkn","bos","cha","chi","cle","dal","den","det","gsw",
    "hou","ind","lac","lal","mem","mia","mil","min","nop","nyk",
    "okc","orl","phi","phx","por","sac","sas","tor","uta","was",
]


def read_csv(path):
    text = path.read_text()
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)
    return headers, rows


def write_csv(path, headers, rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(out.getvalue())


def main():
    ovr_history = {}
    if OVR_FILE.exists():
        ovr_history = json.loads(OVR_FILE.read_text())

    for team in TEAMS:
        path = DATA_DIR / f"{team}-roster.csv"
        if not path.exists():
            print(f"  SKIP {team}: file not found")
            continue

        headers, rows = read_csv(path)

        if "PLAYER" in headers or "SLUG" not in headers:
            print(f"  SKIP {team}: legacy format")
            continue

        if "OVR" not in headers:
            print(f"  SKIP {team}: no OVR column (already migrated?)")
            continue

        new_headers = [h for h in headers if h != "OVR"]
        collected = 0
        for row in rows:
            slug = row.get("SLUG", "").strip()
            ovr_raw = row.get("OVR", "").strip()
            if not slug or not ovr_raw:
                continue
            try:
                ovr = int(ovr_raw)
            except ValueError:
                print(f"    WARN {team}: non-integer OVR for {slug!r}: {ovr_raw!r}")
                continue

            entries = ovr_history.setdefault(slug, [])
            # Don't duplicate if there's already an entry for today
            if not any(e["date"] == TODAY for e in entries):
                entries.append({"date": TODAY, "ovr": ovr})
                collected += 1

        print(f"  {team}: {collected} OVR entries collected, CSV will drop OVR column")

        if APPLY:
            write_csv(path, new_headers, rows)

    if APPLY:
        OVR_FILE.write_text(json.dumps(ovr_history, indent=2))
        print(f"\nWrote {OVR_FILE}")
        print("Done.")
    else:
        print(f"\nDry run — pass --apply to write changes")
        print(f"Would write {OVR_FILE} with {sum(len(v) for v in ovr_history.values())} total entries")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Record where every team sat today. Run by `nbn-cap-history.timer`.

    venv/bin/python snapshot_cap_history.py            # today, skip if recorded
    venv/bin/python snapshot_cap_history.py --force    # re-record today
    venv/bin/python snapshot_cap_history.py --date 2026-08-25
    venv/bin/python snapshot_cap_history.py --dry-run  # print, write nothing

A thin wrapper so the timer does not have to know the module layout, and so a
person can run the same code by hand. The work is in routers/cap_history.py.
"""
import argparse
import json
import sys

from routers import cap_history


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ISO date to record as (default: today)")
    ap.add_argument("--force", action="store_true",
                    help="re-record a date that is already on file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the rows that would be written")
    args = ap.parse_args()

    if args.dry_run:
        rows = cap_history.build_rows(on_date=args.date)
        for row in rows:
            print(json.dumps(row, separators=(",", ":")))
        print(f"-- {len(rows)} rows, nothing written", file=sys.stderr)
        return 0

    result = cap_history.snapshot(on_date=args.date, force=args.force)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

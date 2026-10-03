#!/usr/bin/env python3
"""Refresh the IRL feed's game logs. Run by `nbn-irl.timer`.

    venv/bin/python fetch_irl.py              # every sport in irl-rosters.json
    venv/bin/python fetch_irl.py --sport nfl  # one sport
    venv/bin/python fetch_irl.py --force      # refetch closed seasons too

A thin wrapper so the timer does not have to know the module layout. The work
is in routers/irl.py. Writes only under the data dir's `irl/`, which is a cache
of ESPN's data and safe to delete and refetch.
"""
import argparse
import json
import sys

from routers import irl


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", action="append", choices=sorted(irl.SPORTS),
                    help="limit to this sport (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="refetch seasons that are already closed")
    args = ap.parse_args()
    summary = irl.fetch_all(sports=args.sport, force=args.force)
    print(json.dumps(summary))
    return 1 if any(s["failed"] for s in summary.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

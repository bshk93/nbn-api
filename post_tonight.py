"""Daily, 11:00 ET: post the "Tonight" card to #leaguetalk.

All of it is `routers.tonight_card.post_tonight`; this is the timer's entry
point (`nbn-tonight.timer`). A day with no games posts nothing, and a date is
posted at most once.

    venv/bin/python post_tonight.py                      # today's game day
    venv/bin/python post_tonight.py --date 2026-11-11    # a given date
    venv/bin/python post_tonight.py --dry-run            # print the message, post nothing
"""
import argparse
import json
import sys

from routers import tonight_card


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default="", help="YYYY-MM-DD; default today's game day")
    ap.add_argument("--dry-run", action="store_true", help="print the message, post nothing")
    args = ap.parse_args()
    done = tonight_card.post_tonight(args.date or None, dry_run=args.dry_run)
    print(json.dumps(done, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

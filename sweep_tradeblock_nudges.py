"""Daily: the weekly "still on the block?" nudge.

Every team with a player on its trading block for a week or more, not marked
to stay until traded, gets one inbox message a week naming them. All of it is
`routers.tradeblock_nudge.sweep_tradeblock_nudges`; this is the timer's entry
point (`nbn-tradeblock-nudge.timer`).

    venv/bin/python sweep_tradeblock_nudges.py            # run it
    venv/bin/python sweep_tradeblock_nudges.py --dry-run  # list who'd be nudged, write nothing
"""
import argparse
import json
import sys

from routers import tradeblock_nudge as N


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list who'd be nudged, write nothing")
    args = ap.parse_args()

    done = N.sweep_tradeblock_nudges(dry_run=args.dry_run)
    if args.dry_run or done["nudged"] or done["stamped"]:
        print(json.dumps(done, indent=1 if args.dry_run else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())

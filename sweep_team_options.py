"""Hourly: § 6.1's team option windows.

Sends each team its 48- and 24-hour reminders before a window closes (the
rookie-scale deadline in the fall, June 30 for every other team option), and
once one has closed, declines every option in it still undecided. All of it is
`routers.transactions.sweep_team_options`; this is the timer's entry point
(`nbn-team-options.timer`).

    venv/bin/python sweep_team_options.py            # run it
    venv/bin/python sweep_team_options.py --dry-run  # list what's undecided, write nothing
"""
import argparse
import json
import sys
from collections import Counter

from routers import discord_transport
from routers import transactions as T


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list what's undecided, write nothing")
    args = ap.parse_args()

    if args.dry_run:
        undecided = T._undecided_team_options(T.load_player_bios())
        by_close = Counter((o["closes"], o["kind"]) for o in undecided)
        print(json.dumps({"undecided": len(undecided),
                          "by_close_date": [{"closes": c, "kind": k, "options": n}
                                            for (c, k), n in sorted(by_close.items(), key=lambda x: str(x[0]))]},
                         indent=1))
        return 0

    done = T.sweep_team_options()
    # Posts go out on a daemon thread; without this the process exits
    # before they're sent and Discord never hears about the run.
    discord_transport.flush()
    if done["notices"] or done["declined"]:
        print(json.dumps(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())

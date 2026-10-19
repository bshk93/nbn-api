"""Hourly: § 6.1's rookie-scale team option deadline.

Sends each team its 48- and 24-hour reminders before the deadline, and once
it passes, declines every rookie-scale option still undecided. All of it is
`routers.transactions.sweep_rookie_team_options`; this is the timer's entry
point (`nbn-team-options.timer`).

    venv/bin/python sweep_team_options.py            # run it
    venv/bin/python sweep_team_options.py --dry-run  # list what's due, write nothing
"""
import argparse
import json
import sys

from routers import transactions as T
from routers import season_calendar


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list what's due, write nothing")
    args = ap.parse_args()

    if args.dry_run:
        season = T._current_league_year()
        due = T._due_rookie_options(season, T.load_player_bios())
        print(json.dumps({"season": season, "deadline": season_calendar.team_option_deadline(season),
                          "undecided": len(due), "by_team": sorted({t for _, t, _ in due}),
                          "players": sorted(s for s, _, _ in due)}, indent=1))
        return 0

    done = T.sweep_rookie_team_options()
    if done["notices"] or done["declined"]:
        print(json.dumps(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Hourly: § 3.10's opening-night renounce.

A UFA/RFA hold still on a roster once opening night arrives (midnight league
time) is renounced automatically. All of it is
`routers.transactions.sweep_fa_holds`; this is the timer's entry point
(`nbn-fa-holds.timer`).

    venv/bin/python sweep_fa_holds.py            # run it
    venv/bin/python sweep_fa_holds.py --dry-run  # list the holds it would renounce, write nothing
"""
import argparse
import json
import sys

from routers import season_calendar
from routers import discord_transport
from routers import transactions as T
from routers.league_time import league_today_str


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list the holds, write nothing")
    args = ap.parse_args()

    if args.dry_run:
        today = league_today_str()
        season = T._season_for_date(today)
        print(json.dumps({"today": today, "opening_night": season_calendar.opening_night(season),
                          "holds": T._lapsed_fa_holds(T.load_player_bios(), season)}, indent=1))
        return 0

    done = T.sweep_fa_holds()
    # Posts go out on a daemon thread; without this the process exits
    # before they're sent and Discord never hears about the run.
    discord_transport.flush()
    if done["renounced"]:
        print(json.dumps(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())

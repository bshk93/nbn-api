#!/usr/bin/env python3
"""Recompute every day of this season's fair value history with the current model.

    venv/bin/python rebuild_fair_value.py                 # every day from opening night to today
    venv/bin/python rebuild_fair_value.py --from 2026-10-24
    venv/bin/python rebuild_fair_value.py --dry-run       # print the days, write nothing

Run it after any change to the model (routers/fair_value.py or
stats_build/finances.py), so the change does not show up as a day's move.
Each day counts the games played by its end (game date). Rosters and contracts
are today's; see `fair_value.rebuild_history`.
"""
import argparse
import json
import sys
from datetime import date, timedelta

from routers import fair_value, playoff_odds
from routers.league_time import league_today_str
from season_clock import current_season


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="first day (default: opening night)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    season = current_season()
    schedule = playoff_odds.load_schedule(season)
    opening = min((g["date"] for g in schedule if g.get("date")), default=None)
    start = args.start or opening
    if not start:
        print(f"no schedule for {season}", file=sys.stderr)
        return 1
    today = league_today_str()
    days, d = [], date.fromisoformat(start)
    while d.isoformat() <= today:
        days.append(d.isoformat())
        d += timedelta(days=1)
    if args.dry_run:
        print(json.dumps({"season": season, "days": days}))
        return 0
    rows = fair_value.rebuild_history(season, days)
    print(json.dumps({"season": season, "rebuilt": [r["date"] for r in rows]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

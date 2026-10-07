#!/usr/bin/env python3
"""Recompute playoff odds. Run by `nbn-playoff-odds.timer`.

    venv/bin/python snapshot_playoff_odds.py             # compute and save
    venv/bin/python snapshot_playoff_odds.py --dry-run   # print, write nothing

Saves playoff-odds.json (what GET /api/playoff-odds serves) and the day's row in
playoff-odds-history.jsonl. The model is in routers/playoff_odds.py. Skips the
offseason: nothing runs until the season's schedule file exists.
"""
import argparse
import json
import sys
import time

from routers import playoff_odds
from routers.constants import DATA_DIR
from routers.league_time import league_today_str
from season_clock import current_season


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the odds, write nothing")
    args = ap.parse_args()

    season = current_season()
    if not (DATA_DIR / f"schedule-{season}.json").exists():
        print(json.dumps({"skipped": f"no schedule for {season}"}))
        return 0
    t0 = time.time()
    run = playoff_odds.compute(season, league_today_str())
    took = round(time.time() - t0, 1)
    if args.dry_run:
        for r in sorted(run["teams"], key=lambda r: (r["conf"], -r["playoffs"])):
            print(f'{r["conf"]:4} {r["team"]}  prior {r["prior"]:+5.1f}  proj {r["proj_w"]:4.1f}  '
                  f'po {r["playoffs"]:.0%}  top6 {r["top6"]:.0%}  #1 {r["seed1"]:.0%}  title {r["title"]:.1%}')
        print(f"-- {run['games_played']} played, {run['games_left']} left, {took}s, nothing written", file=sys.stderr)
        return 0
    playoff_odds.save_run(run)
    print(json.dumps({"date": run["date"], "season": season, "games_played": run["games_played"], "seconds": took}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

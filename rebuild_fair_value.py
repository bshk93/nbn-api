#!/usr/bin/env python3
"""Recompute every day of this season's fair value history with the current model.

    venv/bin/python rebuild_fair_value.py                 # every day from opening night to today
    venv/bin/python rebuild_fair_value.py --from 2026-10-24
    venv/bin/python rebuild_fair_value.py --dry-run       # compute and report, write nothing
    venv/bin/python rebuild_fair_value.py --check         # same model: the rebuild must match the rows

Run it after any change to the model (routers/fair_value.py or
stats_build/finances.py), so the change does not show up as a day's move.
Each day is priced on the league as it stood at its end, from the inputs the
hourly run recorded that day (`fair_value.recorded_inputs`), and counts the
games played by its end (game date). A day with no record stops the rebuild
before anything is written.

Every run prints each team's biggest one-day moves in roster strength, so a
trade or a ratings update shows up on its own day before anything is saved.
Read them: a rebuild where no team moves on the day of a trade is wrong.
`--check` recomputes the days the current model already priced and fails if
any team's strength, injured strength or payroll differs from its row, or its
price by more than CHECK_PRICE_TOLERANCE.
"""
import argparse
import fcntl
import json
import sys
from datetime import date, timedelta

from routers import fair_value, playoff_odds
from routers.league_time import league_today_str
from season_clock import current_season

MOVES_SHOWN = 3
# A same-model rebuild can differ from the live row by a game's gate: the live
# run read the ledger as last built, the rebuild recounts it from the games.
CHECK_PRICE_TOLERANCE = 0.002


def strength_moves(rows: list[dict]) -> dict[str, list[tuple[str, float]]]:
    """team -> its biggest one-day strength moves, as (date, change)."""
    rows = sorted(rows, key=lambda r: r["date"])
    out: dict[str, list[tuple[str, float]]] = {}
    for prev, row in zip(rows, rows[1:]):
        for t, x in row["teams"].items():
            a, b = (prev["teams"].get(t) or {}).get("strength"), x.get("strength")
            if a is not None and b is not None and a != b:
                out.setdefault(t, []).append((row["date"], round(b - a, 2)))
    return {t: sorted(v, key=lambda m: -abs(m[1]))[:MOVES_SHOWN] for t, v in sorted(out.items())}


def check(rebuilt: list[dict], season: str) -> list[str]:
    """Differences between rebuilt rows and the rows the same model wrote."""
    old = {r["date"]: r for r in fair_value.read_history(season) if r.get("model") == fair_value.MODEL}
    bad = []
    for row in rebuilt:
        was = old.get(row["date"])
        if not was:
            continue
        for t, x in row["teams"].items():
            y = was["teams"].get(t) or {}
            for k in ("strength", "strength_injured", "payroll"):
                if x.get(k) != y.get(k):
                    bad.append(f'{row["date"]} {t} {k}: row {y.get(k)}, rebuilt {x.get(k)}')
            if y.get("price") and abs(x["price"] / y["price"] - 1) > CHECK_PRICE_TOLERANCE:
                bad.append(f'{row["date"]} {t} price: row {y["price"]}, rebuilt {x["price"]}')
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="first day (default: opening night)")
    ap.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    ap.add_argument("--check", action="store_true", help="compare with the rows the same model wrote; write nothing")
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
    if not (args.dry_run or args.check):
        # The hourly run (snapshot_playoff_odds.py) writes the same history;
        # wait for it rather than both writing at once.
        lock = open(fair_value.DATA_DIR / ".playoff-odds.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        rows = fair_value.rebuild_history(season, days, write=not (args.dry_run or args.check))
    except fair_value.MissingInputs as e:
        print(f"refusing to rebuild: {e}", file=sys.stderr)
        return 1
    report = {"season": season, "days": [r["date"] for r in rows], "written": not (args.dry_run or args.check),
              "strength_moves": strength_moves(rows)}
    if args.check:
        report["mismatches"] = check(rows, season)
    print(json.dumps(report, indent=1))
    return 1 if report.get("mismatches") else 0


if __name__ == "__main__":
    sys.exit(main())

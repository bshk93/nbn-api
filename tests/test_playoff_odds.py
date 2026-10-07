"""Playoff odds — the simulation's bookkeeping, not its forecasts.

Pins: every simulated season fills exactly 6 direct spots, 8 playoff spots and
one #1 seed per conference, one champion; a played game is matched to its
schedule slot and never simulated again; projected wins add up to the games
there are; a far stronger team is nearly certain; the roster prior is centred
on the league. Synthetic inputs only; nothing reads the data dir.

    venv/bin/python -m tests.test_playoff_odds
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import playoff_odds as po  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TEAMS = sorted(po.EAST | po.WEST)
priors = {t: 0.0 for t in TEAMS}
priors["BOS"] = 25.0          # absurdly strong, so the outcome is near-certain

# A small schedule: every team plays its neighbour twice, home and away.
schedule = []
for i, t in enumerate(TEAMS):
    u = TEAMS[(i + 1) % len(TEAMS)]
    schedule.append({"date": "2026-10-20", "home_team": t, "away_team": u})
    schedule.append({"date": "2026-10-22", "home_team": u, "away_team": t})
# One of them already played: the away side won.
first = schedule[0]
results = [{"date": first["date"], "home": first["home_team"], "away": first["away_team"],
            "home_pts": 90, "away_pts": 101}]

SIMS = 400
out = po.simulate(results, schedule, priors, sims=SIMS, seed=7)
rows = {r["team"]: r for r in out["teams"]}

print("bookkeeping")
check("a played game is not simulated again", out["games_left"] == len(schedule) - 1)
check("its result is on the record", rows[first["away_team"]]["w"] == 1 and rows[first["home_team"]]["l"] == 1)
total_w = sum(r["proj_w"] for r in out["teams"])
check("projected wins add up to the games there are", abs(total_w - len(schedule)) < 0.5)
for conf in ("East", "West"):
    cr = [r for r in out["teams"] if r["conf"] == conf]
    check(f"{conf}: eight playoff spots a season", abs(sum(r["playoffs"] for r in cr) - 8) < 1e-6)
    check(f"{conf}: six direct spots", abs(sum(r["top6"] for r in cr) - 6) < 1e-6)
    check(f"{conf}: four play-in spots", abs(sum(r["play_in"] for r in cr) - 4) < 1e-6)
    check(f"{conf}: one #1 seed", abs(sum(r["seed1"] for r in cr) - 1) < 1e-6)
    check(f"{conf}: one conference champion", abs(sum(r["finals"] for r in cr) - 1) < 1e-6)
check("one champion", abs(sum(r["title"] for r in out["teams"]) - 1) < 1e-6)

print("forecasts")
check("a far stronger team nearly always makes it", rows["BOS"]["playoffs"] > 0.97)
check("and is the title favourite", max(out["teams"], key=lambda r: r["title"])["team"] == "BOS")
check("the same seed gives the same answer", po.simulate(results, schedule, priors, sims=50, seed=3)
      == po.simulate(results, schedule, priors, sims=50, seed=3))

print("roster prior")
pri = po.roster_priors({"AAA": 80.0, "BBB": 76.0, "CCC": 78.0, "DDD": None})
check("centred on the league", abs(pri["AAA"] + pri["BBB"] + pri["CCC"]) < 1e-9)
check("stronger roster, higher prior", pri["AAA"] > pri["CCC"] > pri["BBB"])
check("an unknown roster is average", pri["DDD"] == 0.0)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

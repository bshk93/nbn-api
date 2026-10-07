"""`GET /api/players/{slug}/insights` — feats, streaks and teammates.

Pins: feat counts and ranks (ties share a rank), streaks run over the regular
season only, in date order, across seasons, and report whether they are still
running; teammates are the players on the same team on the same date, with the
record from the score. Synthetic rows only; nothing here reads the data dir.

    venv/bin/python -m tests.test_player_insights
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import game_highs as gh  # noqa: E402
from routers import player_insights as pi  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def game(player, team, date, season="24-25", playoff=False, us=110, them=100, **stats):
    r = {"PLAYER": player, "TEAM": team, "OPP": "XXX", "DATE": date, "SEASON": season,
         "gametype": "PLAYOFF" if playoff else "REG", "WL": "NA",
         "TEAM_PTS": str(us), "OPP_TEAM_PTS": str(them), "ROUND": "", "GAME": ""}
    for k in gh.STATS:
        r[k] = str(stats.get(k, 0))
    return r


A, B, C = "ALPHA, AL", "BETA, BO", "GAMMA, GUS"
ROWS = [
    # Alpha: 20+ in four straight regular-season games across two seasons,
    # with a playoff dud in between that must not break the run.
    game(A, "GSW", "2024-03-01", season="23-24", P=25),
    game(A, "GSW", "2024-03-03", season="23-24", P=22),
    game(A, "GSW", "2024-05-01", season="23-24", playoff=True, P=5),
    game(A, "GSW", "2024-11-01", P=30),
    game(A, "GSW", "2024-11-03", P=21, us=90, them=99),
    game(A, "GSW", "2024-11-05", P=8),
    game(A, "GSW", "2024-11-07", P=24),           # a new run, still going
    # Beta: Alpha's teammate in three games; one 50-point game.
    game(B, "GSW", "2024-03-01", season="23-24", P=50),
    game(B, "GSW", "2024-11-03", P=10, us=90, them=99),
    game(B, "GSW", "2024-11-07", P=10),
    # Gamma: another team on the same date — never Alpha's teammate.
    game(C, "BOS", "2024-03-01", season="23-24", P=50),
]
ins = pi.Insights(gh.GameIndex(iter(ROWS)))
a = ins.payload("alpha-al")
streak = {s["key"]: s for s in a["streaks"]}

print("streaks")
check("a run carries across seasons", streak["P20"]["length"] == 4)
check("a playoff game neither extends nor breaks it",
      streak["P20"]["start"] == "2024-03-01" and streak["P20"]["end"] == "2024-11-03")
check("a run that has since ended is not active", streak["P20"]["active"] is False)
check("the best run ranks first", streak["P20"]["rank"] == 1)
check("no qualifying game means length 0", streak["DD"]["length"] == 0)
running = ins.payload("beta-bo")
check("rank against every player's longest", {s["key"]: s for s in running["streaks"]}["P20"]["rank"] == 2)

print("feats")
fifty = {f["key"]: f for f in running["feats"]}["P50"]
check("a feat is counted", fifty["count"] == 1)
check("level counts share a rank", fifty["rank"] == 1 and fifty["tied"] is True)
check("no feat means no rank", "rank" not in {f["key"]: f for f in a["feats"]}["P50"])

print("teammates")
mates = {t["slug"]: t for t in a["teammates"]}
check("same team, same date is a teammate", mates.get("beta-bo", {}).get("games") == 3)
check("another team on the same date is not", "gamma-gus" not in mates)
check("the record comes from the score", (mates["beta-bo"]["w"], mates["beta-bo"]["l"]) == (2, 1))

print("unknown player")
check("no games → None", ins.payload("nobody-here") is None)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

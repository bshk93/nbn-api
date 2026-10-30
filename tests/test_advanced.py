"""`stats_build/advanced.py` — pace, ratings and Four Factors.

What would make it lie is pinned:

- **The formulas**, on a hand-built game whose answers are worked out here.
- **A game is counted once per side.** Both teams get the same possessions,
  and a game with no opponent rows is dropped rather than half-counted.
- **The league row averages, it doesn't add.** Its game count is games
  played, and its ratings are each team's possessions counted once.
- **Player context joins `player_seasons*.csv`**: every stint has a row with
  the same SLUG, season, team and game count, or the page can't line them up.
- **An every-game player carries his team's whole season** of minutes, so
  the shares are taken of the right games.
- **Live data is plausible**: pace 90-110 and ratings 95-125 over 20+ games, and points per
  possession across the league equal total points over total possessions.

Reads live data; writes nothing.

    venv/bin/python -m tests.test_advanced
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.constants import DATA_DIR  # noqa: E402
from stats_build import advanced as a  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def line(team, opp, **stats):
    base = {"SEASON": "25-26", "DATE": "2026-01-01", "TEAM": team, "OPP": opp,
            "M": "240", "P": "0", "FGM": "0", "FGA": "0", "3PM": "0", "3PA": "0",
            "FTM": "0", "FTA": "0", "OR": "0", "DR": "0", "TO": "0"}
    base.update({k: str(v) for k, v in stats.items()})
    return base


print("formulas on a hand-built game")
rows = [
    # Two players for AAA so the sums are exercised; one for BBB, away (`@`).
    line("AAA", "BBB", M=120, P=60, FGM=22, FGA=45, **{"3PM": 6}, FTM=10, FTA=12, OR=5, DR=15, TO=7),
    line("AAA", "BBB", M=120, P=50, FGM=20, FGA=40, **{"3PM": 4}, FTM=6, FTA=8, OR=5, DR=15, TO=6),
    line("BBB", "@AAA", P=100, FGM=40, FGA=90, **{"3PM": 10}, FTM=10, FTA=16, OR=12, DR=33, TO=15),
]
games = a.team_games(rows)
check("both sides of the game found", len(games) == 2)
aaa = next(g for g in games if g["TEAM"] == "AAA")
bbb = next(g for g in games if g["TEAM"] == "BBB")
# AAA: 85 + 0.4*20 - 1.07*(10/43)*43 + 13 = 85 + 8 - 10.7 + 13 = 95.3
# BBB: 90 + 0.4*16 - 1.07*(12/42)*50 + 15 = 111.4 - 15.2857... = 96.1143
expect = (95.3 + (90 + 6.4 - 1.07 * 12 / 42 * 50 + 15)) / 2
check(f"possessions are the two sides' average ({aaa['POSS']:.3f})", abs(aaa["POSS"] - expect) < 1e-9)
check("both teams get the same possessions", aaa["POSS"] == bbb["POSS"])

row = dict(zip(a.HEADER, a._summarize("AAA", "25-26", "REG", [aaa])))
check("pace is per 48 minutes", row["PACE"] == round(expect, 1))
check("ORtg is points per 100", row["ORTG"] == round(100 * 110 / expect, 1))
check("DRtg is allowed per 100", row["DRTG"] == round(100 * 100 / expect, 1))
check("eFG% credits threes", row["EFG"] == round((42 + 5) / 85, 3))
check("TOV% per play", row["TOV_PCT"] == round(13 / (85 + 0.44 * 20 + 13), 3))
check("ORB% against their DRB", row["ORB_PCT"] == round(10 / (10 + 33), 3))
check("DRB% against their ORB", row["DRB_PCT"] == round(30 / (30 + 12), 3))
check("FT rate is FTM/FGA", row["FT_RATE"] == round(16 / 85, 3))
check("opponent eFG%", row["OPP_EFG"] == round((40 + 5) / 90, 3))

print("a game without an opponent is dropped")
lone = [line("CCC", "DDD", P=100, FGA=80)]
check("no half games", a.team_games(lone) == [])

print("live data")
out = a.team_advanced(DATA_DIR)
col = {c: i for i, c in enumerate(a.HEADER)}
teams = [r for r in out if r[col["TEAM"]] != a.LEAGUE]
league = [r for r in out if r[col["TEAM"]] == a.LEAGUE]
seasons = sorted({r[col["SEASON"]] for r in out})
kinds = sorted({(r[col["SEASON"]], r[col["TYPE"]]) for r in out})
check(f"one league row per season and type ({len(kinds)})", len(league) == len(kinds))
check("30 teams in every regular season", all(
    sum(1 for r in teams if r[col["SEASON"]] == s and r[col["TYPE"]] == "REG") == 30
    for s in seasons))
check("16 teams in every finished postseason", all(
    sum(1 for r in teams if r[col["SEASON"]] == s and r[col["TYPE"]] == "PLAYOFF") == 16
    for s, k in kinds if k == "PLAYOFF"))
# A four-game sweep, or four games into a season, can sit outside these for
# real; the ranges are for a real sample.
check("pace between 90 and 110 (20+ games)", all(
    90 <= r[col["PACE"]] <= 110 for r in out if r[col["G"]] >= 20))
check("ratings between 95 and 125 (20+ games)", all(
    95 <= r[col["ORTG"]] <= 125 and 95 <= r[col["DRTG"]] <= 125
    for r in teams if r[col["G"]] >= 20))
for lg in league:
    s, k = lg[col["SEASON"]], lg[col["TYPE"]]
    rs = [r for r in teams if r[col["SEASON"]] == s and r[col["TYPE"]] == k]
    s = f"{s} {k}"
    check(f"{s}: league plays half the team-games ({lg[col['G']]})",
          lg[col["G"]] * 2 == sum(r[col["G"]] for r in rs))
    check(f"{s}: league ORtg equals league DRtg", lg[col["ORTG"]] == lg[col["DRTG"]])
    check(f"{s}: net ratings sum to about zero",
          abs(sum(r[col["NRTG"]] * r[col["POSS"]] for r in rs)) / lg[col["POSS"]] < 1)

print("player context")
ctx = a.player_context(DATA_DIR)
pcol = {c: i for i, c in enumerate(a.PLAYER_HEADER)}
by_key = {(r[0], r[1], r[2], r[3]): r for r in ctx}
derived = DATA_DIR / "derived" / "players"
for fname, kind in (("player_seasons.csv", "REG"), ("player_seasons_playoffs.csv", "PLAYOFF")):
    with (derived / fname).open(newline="") as fh:
        seasons_rows = [r for r in csv.DictReader(fh) if r["G"] not in ("", "NA")]
    lost = [r for r in seasons_rows
            if by_key.get((r["SLUG"], r["SEASON"][:5], r["TEAM"], kind), [None] * 5)[4] != int(r["G"])]
    check(f"{fname}: all {len(seasons_rows)} stints join with the same G ({len(lost)} don't)", not lost)

# Each team-game's minutes reach TM_MIN once per player who played in it, so
# the sum over players is minutes times players-per-game, not five. What does
# hold exactly: a player who played every game carries the team's minutes.
team_rows = {(r[col["TEAM"]], r[col["SEASON"]], r[col["TYPE"]]): r for r in teams}
full = [r for r in ctx if (r[2], r[1], r[3]) in team_rows
        and r[pcol["G"]] == team_rows[(r[2], r[1], r[3])][col["G"]]]
check(f"an every-game player carries his team's minutes ({len(full)} stints)", full and all(
    r[pcol["TM_MIN"]] == team_rows[(r[2], r[1], r[3])][col["MIN"]] for r in full))
check("possessions are never more than a game's worth per game",
      all(r[pcol["POSS"]] <= 130 * r[pcol["G"]] for r in ctx))

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall checks passed")

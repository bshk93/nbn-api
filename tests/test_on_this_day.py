"""Regression tests for routers/on_this_day.py — the homepage's On This Day.

What must keep being true:

  * only earlier years count: the requested day itself, and any later year,
    never show up as history;
  * one line per earlier season, the day's best by Game Score, with a feat
    named when the line clears one;
  * a series counts as clinched on its last game's date, and a Finals series
    is a title;
  * only trades, never voided ones.

    venv/bin/python -m tests.test_on_this_day
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import game_highs as gh  # noqa: E402
from routers import on_this_day as otd  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def g(player, team, opp, date, season, P=10, R=5, A=5, playoff=False, home=True):
    return {"PLAYER": player, "TEAM": team, "OPP": opp if home else "@" + opp, "DATE": date,
            "SEASON": season, "gametype": "PLAYOFF" if playoff else "REG",
            "M": 36, "P": P, "R": R, "A": A, "S": 1, "B": 1, "TO": 2, "PF": 2,
            "FGM": P // 2, "FGA": P, "3PM": 1, "3PA": 3, "FTM": 0, "FTA": 0, "OR": 1, "DR": R - 1,
            "WL": "W", "TEAM_PTS": 110, "OPP_TEAM_PTS": 100}


GAMES = [
    g("STAR, A", "AAA", "BBB", "2024-12-25", "24-25", P=52),
    g("ROLE, B", "BBB", "AAA", "2024-12-25", "24-25", P=20, home=False),
    g("TRIP, C", "CCC", "DDD", "2023-12-25", "23-24", P=20, R=12, A=11),
    g("TONIGHT, D", "AAA", "CCC", "2026-12-25", "26-27", P=70),   # the day itself
    g("OTHER, E", "AAA", "CCC", "2024-12-24", "24-25", P=60),     # another day
    # A first-round series: game 1 on 4-20, the clincher on 4-28.
    g("PO, F", "AAA", "BBB", "2025-04-20", "24-25", playoff=True),
    g("PO, F", "AAA", "BBB", "2025-04-28", "24-25", playoff=True),
    g("FIN, G", "CCC", "AAA", "2025-06-15", "24-25", playoff=True),
]
index = gh.GameIndex(iter(GAMES))
BRACKETS = [
    {"SEASON": "24-25", "ROUND": "1", "T1": "AAA", "T2": "BBB", "T1_W": "4", "T2_W": "1", "WINNER": "AAA"},
    {"SEASON": "24-25", "ROUND": "4", "T1": "CCC", "T2": "AAA", "T1_W": "4", "T2_W": "3", "WINNER": "CCC"},
]

print("best game per earlier season")
games = otd.best_games(index, "2026-12-25")
check("one line per earlier season, newest first", [x["date"] for x in games] == ["2024-12-25", "2023-12-25"])
check("the day's best by Game Score", games[0]["player"] == "STAR, A")
check("a 50-point game is named", games[0]["feat"] == "50-point game")
check("a triple-double is named", games[1]["feat"] == "triple-double")
check("the requested day itself is not history", all(x["date"] != "2026-12-25" for x in games))
check("asked from 2024, 2024's own day is excluded", [x["date"] for x in otd.best_games(index, "2024-12-25")] == ["2023-12-25"])

print("\nclinches")
c = otd.clinches(index, "2026-04-28", BRACKETS)
check("clinched on the last game's date", len(c) == 1 and c[0]["winner"] == "AAA" and (c[0]["wins"], c[0]["losses"]) == (4, 1))
check("not on game 1's date", otd.clinches(index, "2026-04-20", BRACKETS) == [])
t = otd.clinches(index, "2026-06-15", BRACKETS)
check("a Finals clincher is a title", t and t[0]["title"] is True and t[0]["loser"] == "AAA")

print("\ntrades")
TR = [{"type": "trade", "date": "2022-12-25", "details": {}},
      {"type": "trade", "date": "2023-12-25", "voided": True, "details": {}},
      {"type": "trade", "date": "2026-12-25", "details": {}},
      {"type": "trade", "date": "2022-12-24", "details": {}}]
got = otd.trades("2026-12-25", TR)
check("earlier years' trades that day, not voided", [x["date"] for x in got] == ["2022-12-25"])

print("\nroute")
otd.gh.get_index = lambda: (index, ())
otd._load_brackets = lambda: BRACKETS
otd._load_trades = lambda: TR
r = otd.get_on_this_day("2026-12-25")
check("route returns all three", set(r) == {"date", "games", "clinches", "trades"} and len(r["games"]) == 2)
try:
    otd.get_on_this_day("12-25")
    check("a malformed date is refused", False)
except Exception as exc:
    check("a malformed date is refused", getattr(exc, "status_code", None) == 422)

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

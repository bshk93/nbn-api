"""Regression tests for routers/stream_sim.py — /stats/stream-sim.

What must keep being true:

  * a box score is matched to its scheduled game on date + both teams,
    whichever side is home;
  * the `stream` flag decides the side; a game with no schedule entry is
    counted as unmatched and lands on neither;
  * possessions, margins, home wins and overtime are counted per game;
  * a rate is pooled (sum over sum), not an average of nightly rates;
  * a gap only reads as a real one when the t-test says so.

    venv/bin/python -m tests.test_stream_sim
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import stream_sim as ss  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def line(player, team, opp, date, team_pts, opp_pts, home=True, M=48, P=0, FGA=0, OR=0, DR=0, TO=0, FTA=0):
    return {"PLAYER": player, "TEAM": team, "OPP": opp if home else "@" + opp, "OPP_RAW": opp,
            "DATE": date, "M": M, "P": P, "R": OR + DR, "OR": OR, "DR": DR, "A": 0, "S": 0, "B": 0,
            "TO": TO, "PF": 0, "FGM": 0, "FGA": FGA, "3PM": 0, "3PA": 0, "FTM": 0, "FTA": FTA,
            "TEAM_PTS": team_pts, "OPP_TEAM_PTS": opp_pts, "gametype": "REG"}


def side(team, opp, date, pts, opp_pts, home, star, minutes=(48, 48, 48, 48, 48)):
    """Five players; `star` scores everything."""
    return [line(f"{team}{i}, P" if i else star, team, opp, date, pts, opp_pts, home=home,
                 M=m, P=pts if i == 0 else 0, FGA=20 if i == 0 else 0, TO=2 if i == 0 else 0)
            for i, m in enumerate(minutes)]


ROWS = (
    # Streamed: AAA (home) beats BBB 90-80.
    side("AAA", "BBB", "2026-10-20", 90, 80, True, "STAR, A")
    + side("BBB", "AAA", "2026-10-20", 80, 90, False, "STAR, B")
    # Simmed: BBB (home) beats AAA 120-100, in overtime.
    + side("BBB", "AAA", "2026-10-22", 120, 100, True, "STAR, B", minutes=(53,) * 5)
    + side("AAA", "BBB", "2026-10-22", 100, 120, False, "STAR, A", minutes=(53,) * 5)
    # Not on the schedule.
    + side("CCC", "DDD", "2026-10-23", 99, 98, True, "STAR, C")
)
SCHED = [
    {"date": "2026-10-20", "home_team": "AAA", "away_team": "BBB", "stream": True, "streamer": "kim"},
    {"date": "2026-10-22", "home_team": "BBB", "away_team": "AAA"},
    {"date": "2026-10-23", "home_team": "EEE", "away_team": "FFF"},
]

out = ss.compare(ROWS, SCHED)
M = {m["key"]: m for m in out["metrics"]}

print("splitting games")
check("one streamed game, one simmed", out["games"] == {"stream": 1, "sim": 1})
check("two team-games a side", out["team_games"] == {"stream": 2, "sim": 2})
check("a game off the schedule is unmatched, on neither side", out["unmatched"] == 1)
check("the streamed game is listed with its score",
      out["stream_games"] == [{"date": "2026-10-20", "home_team": "AAA", "away_team": "BBB",
                               "home_score": 90, "away_score": 80, "streamer": "kim", "youtube_url": None}])

print("\nmeasures")
check("points per team-game", M["pts"]["stream"] == 85 and M["pts"]["sim"] == 110)
check("possessions: FGA - OREB + TOV + 0.44 FTA", M["pace"]["stream"] == 22)
check("margin per game", M["margin"]["stream"] == 10 and M["margin"]["sim"] == 20)
check("combined score per game", M["total"]["stream"] == 170)
check("home team won both", M["home_won"]["stream"] == 1 and M["home_won"]["sim"] == 1)
check("over 240 player-minutes a side is overtime", M["ot"]["stream"] == 0 and M["ot"]["sim"] == 1)
check("the top scorer's share of his team's points", M["top_pts"]["stream"] == 1)
check("difference and relative difference", M["pts"]["diff"] == -25 and abs(M["pts"]["rel"] + 25 / 110) < 1e-9)
check("a 90-point night is a 40-point game", M["p40"]["stream"] == 1)
check("every measure has a group, a family and a signal", all(m["group"] and m["family"] and m["signal"] for m in out["metrics"]))

print("\npooling")
fg = next(m for m in ss.METRICS if m["key"] == "fg_pct")
v, per = ss._side(fg, [{"FGM": 1, "FGA": 1}, {"FGM": 0, "FGA": 9}])
check("FG% is makes over attempts (1 of 10), not the mean of 100% and 0%", v == 0.1 and per == [1.0, 0.0])

print("\nsignal")
check("|t| of 3+ is clear", ss.signal(-3.2) == "clear")
check("|t| of 2-3 is likely", ss.signal(2.4) == "likely")
check("under 2 is noise", ss.signal(1.1) == "noise")
check("no spread at all says nothing", ss.signal(None) == "none")

print()
if FAILS:
    print(f"{len(FAILS)} failed")
    sys.exit(1)
print("all passed")

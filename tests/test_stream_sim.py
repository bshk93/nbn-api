"""Regression tests for routers/stream_sim.py — /stats/stream-sim.

What must keep being true:

  * a box score is matched to its scheduled game on date + both teams,
    whichever side is home;
  * the `stream` flag decides the side; a game with no schedule entry is
    counted as unmatched and lands on neither;
  * possessions, margins, home wins and overtime are counted per game;
  * the player table only holds players with real minutes on both sides.

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
st, sm = out["summary"]["stream"], out["summary"]["sim"]

print("splitting games")
check("one streamed game, one simmed", (st["games"], sm["games"]) == (1, 1))
check("a game off the schedule is unmatched, on neither side", out["unmatched"] == 1)
check("the streamed game is listed with its score",
      out["stream_games"] == [{"date": "2026-10-20", "home_team": "AAA", "away_team": "BBB",
                               "home_score": 90, "away_score": 80, "streamer": "kim", "youtube_url": None}])

print("\nper team-game numbers")
check("points per team-game", st["pts"] == 85 and sm["pts"] == 110)
check("possessions: FGA - OREB + TOV + 0.44 FTA", st["pace"] == 22)
check("margin per game", st["margin"] == 10 and sm["margin"] == 20)
check("home team won both", st["home_win_pct"] == 1 and sm["home_win_pct"] == 1)
check("over 240 player-minutes a side is overtime", st["ot_pct"] == 0 and sm["ot_pct"] == 1)
check("the top scorer's share of his team's points", st["top_pts_share"] == 1)

print("\nplayers")
slugs = {p["slug"] for p in out["players"]}
check("players on both sides are listed", {"star-a", "star-b"} <= slugs)
check("a player on one side only is not", "star-c" not in slugs)
a = next(p for p in out["players"] if p["slug"] == "star-a")
check("each side's per-game line", a["stream"]["pts"] == 90 and a["sim"]["pts"] == 100)

print()
if FAILS:
    print(f"{len(FAILS)} failed")
    sys.exit(1)
print("all passed")

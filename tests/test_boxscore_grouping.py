"""`GET /api/boxscores` — rows into games.

19 regular-season games have an `@` on both sides or on neither. Keyed by
(home, away), each came out as two half games with an empty side (PHX-SAS on
2026-03-19 showed twice, 0 home players each time). Pinned here: a game is
its two teams, whatever the `@` says, and attendance joins on the same pair.

Reads live data; writes nothing.

    venv/bin/python -m tests.test_boxscore_grouping
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.boxscores as b  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def row(team, opp, pts, opp_pts, player):
    return {"TEAM": team, "OPP": opp, "TEAM_PTS": str(pts), "OPP_TEAM_PTS": str(opp_pts),
            "PLAYER": player, "gametype": "REG", "M": "30"}


print("a clean game")
gs = b._group_games([row("NYK", "BOS", 110, 100, "A"), row("BOS", "@NYK", 100, 110, "B")], {})
check("one game", len(gs) == 1)
g = gs[0]
check("home is the side without the @", g["home_team"] == "NYK" and g["away_team"] == "BOS")
check("and known", g["home_known"])
check("scores per side", (g["home_score"], g["away_score"]) == (110, 100))
check("players per side", len(g["home_players"]) == 1 and len(g["away_players"]) == 1)

for label, rows in (
    ("no @ on either side", [row("SAS", "PHX", 115, 121, "A"), row("PHX", "SAS", 121, 115, "B")]),
    ("an @ on both sides", [row("SAS", "@PHX", 115, 121, "A"), row("PHX", "@SAS", 121, 115, "B")]),
):
    print(label)
    gs = b._group_games(rows, {})
    check("still one game, not two halves", len(gs) == 1)
    g = gs[0]
    check("teams go alphabetically", (g["home_team"], g["away_team"]) == ("PHX", "SAS"))
    check("home is not claimed", g["home_known"] is False)
    check("each side keeps its own score and players",
          (g["home_score"], g["away_score"]) == (121, 115)
          and len(g["home_players"]) == 1 and len(g["away_players"]) == 1)

print("a side with no rows still has a score")
gs = b._group_games([row("NYK", "BOS", 110, 100, "A")], {})
check("from the other side's OPP_TEAM_PTS", gs[0]["away_score"] == 100)

print("on live data")
live = b.get_boxscores(date="2026-03-19", season="25-26")
pair = [g for g in live if {g["home_team"], g["away_team"]} == {"PHX", "SAS"}]
check("PHX-SAS on 2026-03-19 is one game", len(pair) == 1)
check("with both sides' players", pair and pair[0]["home_players"] and pair[0]["away_players"])

print("attendance joins on the pair")
tmp = Path(tempfile.mkdtemp(prefix="nbn-bx-test-")) / "att.csv"
tmp.write_text("SEASON,DATE,gametype,ROUND,HOME,AWAY,HOME_KNOWN,ATTENDANCE,SELLOUT,ARENA\n"
               "25-26,2026-01-01,REG,NA,NYK,BOS,TRUE,18204,FALSE,The Garden\n")
real = b.ATTENDANCE_CSV
try:
    b.ATTENDANCE_CSV = tmp
    g = b._with_attendance({"home_team": "BOS", "away_team": "NYK"}, "2026-01-01")
    miss = b._with_attendance({"home_team": "BOS", "away_team": "NYK"}, "2026-01-02")
finally:
    b.ATTENDANCE_CSV = real
check("found whichever way round", g.get("attendance") == {"count": 18204, "sellout": False, "arena": "The Garden"})
check("a game the ledger hasn't seen has none", "attendance" not in miss)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall checks passed")

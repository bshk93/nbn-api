"""`GET /api/teams/{team}/season-lines` — one team's per-game lines this season.

The homepage team card used to fetch a full-career gamelog per starter, each
one parsing every allstats file. This endpoint reads one season's file, once
per change, and answers for the whole team.

    venv/bin/python -m tests.test_team_season_lines
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import boxscores  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


HEADER = "PLAYER,TEAM,DATE,M,P,R,A,S,B,FGM,FGA\n"
ROWS = [
    "CURRY, STEPHEN,GSW,2026-10-20,30,20,5,8,1,0,7,15",
    "CURRY, STEPHEN,GSW,2026-10-22,34,30,4,6,2,0,10,20",
    "GREEN, DRAYMOND,GSW,2026-10-22,28,6,9,7,1,1,3,5",
    "JAMES, LEBRON,LAL,2026-10-22,35,25,8,9,1,1,10,18",
    # Traded: his LAL game is not GSW's.
    "NOBODY, AT ALL,GSW,2026-10-22,10,2,1,0,0,0,1,4",
]

tmp = Path(tempfile.mkdtemp())
path = tmp / "allstats-26-27.csv"
# Names carry a comma, so quote the PLAYER field.
path.write_text(HEADER + "".join(
    '"' + r.split(",", 2)[0] + "," + r.split(",", 2)[1] + '",' + r.split(",", 2)[2] + "\n" for r in ROWS))

boxscores.allstats_path = lambda season, gt: path
boxscores.player_bios_shared = lambda: {
    "curry-stephen": {"name": "CURRY, STEPHEN"},
    "green-draymond": {"name": "GREEN, DRAYMOND"},
    "james-lebron": {"name": "JAMES, LEBRON"},
}

res = boxscores.get_team_season_lines("gsw", season="26-27")
p = res["players"]

print("only this team's players, keyed by slug")
check("team upper-cased", res["team"] == "GSW")
check("curry and green", set(p) == {"curry-stephen", "green-draymond"})
check("unknown name left out", "nobody-at-all" not in p)

print("averages per game")
c = p["curry-stephen"]
check("gp", c["gp"] == 2)
check("pts", c["pts"] == 25.0)
check("min", c["min"] == 32.0)
check("fg%", c["fg_pct"] == round(17 / 35, 3))
check("last5 newest first", c["last5_pts"] == [30, 20])

print("the file is read once per change")
calls = []
real = boxscores.read_csv
boxscores.read_csv = lambda pth: (calls.append(pth), real(pth))[1]
boxscores._TEAM_LINES_CACHE.clear()
boxscores.get_team_season_lines("GSW", season="26-27")
boxscores.get_team_season_lines("LAL", season="26-27")
check("one read for two teams", len(calls) == 1)

print("a season with no file is empty, not an error")
boxscores.allstats_path = lambda season, gt: tmp / "missing.csv"
check("empty", boxscores.get_team_season_lines("GSW", season="27-28")["players"] == {})

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

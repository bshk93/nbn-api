"""`GET /api/home/team/{team}` — the homepage team card in one request.

It bundles what the card used to ask a dozen endpoints for, so what matters is
that each part is what its own endpoint would say, that it adds no rule of its
own, and that one request parses bios and OVR once and hands the same objects
to every helper.

    venv/bin/python -m tests.test_home_team
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
from routers import boxscores, cap_history, discord, home, injuries  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


BIOS = {
    "pg-a": {"name": "GUARD, ALPHA", "pos": ["PG"], "type": "player"},
    "sg-b": {"name": "GUARD, BETA", "pos": ["SG"], "type": "player"},
    "sf-c": {"name": "WING, CHARLIE", "pos": ["SF"], "type": "player"},
    "pf-d": {"name": "FORWARD, DELTA", "pos": ["PF"], "type": "player"},
    "c-e": {"name": "CENTER, ECHO", "pos": ["C"], "type": "player"},
    "c-f": {"name": "CENTER, FOXTROT", "pos": ["C"], "type": "player"},
    "opp-pg": {"name": "OTHER, GUARD", "pos": ["PG"], "type": "player"},
}
OVR = {"pg-a": [{"ovr": 80}], "sg-b": [{"ovr": 79}], "sf-c": [{"ovr": 78}],
       "pf-d": [{"ovr": 77}], "c-e": [{"ovr": 76}], "c-f": [{"ovr": 85}], "opp-pg": [{"ovr": 90}]}
ROSTERS = {"was": ["pg-a", "sg-b", "sf-c", "pf-d", "c-e", "c-f"], "cle": ["opp-pg"]}
GAMES = [
    {"date": "2026-10-20", "home_team": "WAS", "away_team": "BOS"},
    {"date": "2026-10-28", "home_team": "CLE", "away_team": "WAS", "time_et": "7:00p"},
    {"date": "2026-10-30", "home_team": "WAS", "away_team": "CHA"},
    {"date": "2027-04-12", "home_team": "LAL", "away_team": "GSW"},
]

tmp = Path(tempfile.mkdtemp())
standings = tmp / "standings-history.csv"
standings.write_text("SEASON,TEAM,W,L,PCT\n26-27,WAS,1,3,0.25\n26-27,CLE,3,0,1\n25-26,WAS,30,52,0.366\n24-25,BOS,60,22,0.732\n")

calls = {"bios": [], "build_rows": []}
home._current_league_year = lambda: "26-27"
home.league_today_str = lambda: "2026-10-27"
home.player_bios_shared = lambda: BIOS
home.ovr_shared = lambda: OVR
home._load_json_shared = lambda path, default: {"games": GAMES}
home.STANDINGS_CSV = standings
discord._load_csv = lambda path: [{"SLUG": s} for s in ROSTERS.get(path.name.split("-")[0], [])]
discord._team_rotation = lambda team: None
discord._current_league_year = lambda: "26-27"
boxscores.get_boxscore_games = lambda season=None, team=None: [
    {"date": "2026-10-20", "home_team": "WAS", "away_team": "BOS", "home_score": 99, "away_score": 90},
    {"date": "2026-10-24", "home_team": "CHI", "away_team": "WAS", "home_score": 124, "away_score": 103},
]


def fake_lines(team, season=None, bios=None):
    calls["bios"].append(bios)
    return {"pg-a": {"gp": 3, "pts": 16.3}} if team == "WAS" else {"opp-pg": {"gp": 3, "pts": 25.0}}


boxscores.team_season_lines = fake_lines


def fake_build_rows(on_date=None, season=None, teams=None, bios=None):
    calls["build_rows"].append((teams, bios))
    return [{"team": t, "season": season, "salary": 1} for t in teams]


cap_history.build_rows = fake_build_rows
injuries.load_injuries = lambda: {"last_report": {"date": "2026-10-26"}, "players": {
    "pf-d": {"slug": "pf-d", "team": "WAS", "status": "Out"},
    "opp-pg": {"slug": "opp-pg", "team": "CLE", "status": "Hurt"},
}}

res = home.get_home_team("was")

print("shape")
check("team upper-cased", res["team"] == "WAS")
check("season", res["season"] == "26-27")
check("schedule range is the league's", (res["schedule_first"], res["schedule_last"]) == ("2026-10-20", "2027-04-12"))
check("only this team's games", len(res["games"]) == 3)
check("games carry the public shape", all("stream" in g and "streamer" in g for g in res["games"]))

print("results newest first")
check("order", [g["date"] for g in res["results"]] == ["2026-10-24", "2026-10-20"])

print("standings: this season for all, every season for this team")
check("rows", sorted((r["SEASON"], r["TEAM"]) for r in res["standings"])
      == [("25-26", "WAS"), ("26-27", "CLE"), ("26-27", "WAS")])

print("five: computed by OVR when no rotation is saved")
five = res["five"]
check("not set", five["set"] is False)
check("slots", [p and p["slug"] for p in five["players"]] == ["pg-a", "sg-b", "sf-c", "pf-d", "c-f"])
check("player fields", five["players"][0] == {"slug": "pg-a", "name": "GUARD, ALPHA", "pos": ["PG"], "ovr": 80, "type": "player"})

print("five: the saved rotation wins")
discord._team_rotation = lambda team: {"PG": "pg-a", "SG": "sg-b", "SF": "sf-c", "PF": "pf-d", "C": "c-e"} if team == "WAS" else None
res2 = home.get_home_team("WAS")
check("set", res2["five"]["set"] is True)
check("C from the rotation", res2["five"]["players"][4]["slug"] == "c-e")

print("opponent is the next game's")
check("team", res["opponent"]["team"] == "CLE")
check("five", res["opponent"]["five"]["players"][0]["slug"] == "opp-pg")
check("lines", "opp-pg" in res["opponent"]["lines"])

print("cap row: one team, computed by build_rows, not here")
check("cap", res["cap"] == {"team": "WAS", "season": "26-27", "salary": 1})
check("asked for one team", calls["build_rows"][0][0] == ["WAS"])

print("injuries: this team's only")
check("rows", [p["slug"] for p in res["injuries"]["players"]] == ["pf-d"])
check("report date", res["injuries"]["last_report"] == {"date": "2026-10-26"})

print("bios are read once and handed to every helper")
check("lines got the shared bios", all(b is BIOS for b in calls["bios"]))
check("build_rows got the shared bios", all(b is BIOS for _, b in calls["build_rows"]))

print("no next game, no opponent")
home._load_json_shared = lambda path, default: {"games": GAMES[:1]}
check("opponent is None", home.get_home_team("WAS")["opponent"] is None)

print("an unknown team is a 422")
try:
    home.get_home_team("XYZ")
    check("raised", False)
except HTTPException as e:
    check("422", e.status_code == 422)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

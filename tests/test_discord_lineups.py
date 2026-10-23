"""/lineups and /roster's starting five — the saved rotation first, else computed.

Mirrors /preview and the team page (teams/lineup.js: rotationFive, then
computeStartingFive). Nothing here touches disk: rosters, rotations and the
schedule are stubbed.

    venv/bin/python3 tests/test_discord_lineups.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routers.discord as d  # noqa: E402
import routers.coaching_settings as cs  # noqa: E402
import routers.schedule as sch  # noqa: E402
import routers.league_time as lt  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


def P(slug, pos, ovr, typ="player"):
    return {"slug": slug, "name_raw": slug.upper().replace("-", ", "), "pos_list": pos,
            "ovr": ovr, "type": typ, "hold_type": None}


ROSTERS = {
    "PHX": [P("a-pg", ["PG"], 85), P("b-sg", ["SG"], 84), P("c-sf", ["SF"], 83),
            P("d-pf", ["PF"], 82), P("e-c", ["C"], 81), P("f-pg", ["PG"], 70),
            P("g-tw", ["SG"], 65, "two-way")],
    "LAL": [P("h-pg", ["PG"], 90), P("i-sg", ["SG"], 80), P("j-sf", ["SF"], 79),
            P("k-pf", ["PF"], 78), P("l-c", ["C"], 77)],
}
ROTATIONS = {}
GAMES = []
d._roster_augmented = lambda team: [dict(p) for p in ROSTERS.get(team, [])]
cs.get_rotations = lambda: ROTATIONS
sch._load = lambda season: {"games": GAMES}
lt.league_today_str = lambda: "2026-10-23"


def slugs(five):
    return [p["slug"] if p else None for p in five]


print("_starting_five")
five, rot = d._starting_five("PHX", d._roster_augmented("PHX"))
check("no rotation -> computed", not rot and slugs(five) == ["a-pg", "b-sg", "c-sf", "d-pf", "e-c"], slugs(five))

ROTATIONS["PHX"] = {"PG": "f-pg", "SG": "g-tw", "SF": "c-sf", "PF": "d-pf", "C": "e-c"}
five, rot = d._starting_five("PHX", d._roster_augmented("PHX"))
check("saved rotation wins, two-way eligible", rot and slugs(five) == ["f-pg", "g-tw", "c-sf", "d-pf", "e-c"], slugs(five))

ROTATIONS["PHX"] = {"PG": "traded-away", "SG": "b-sg", "SF": "b-sg"}
five, rot = d._starting_five("PHX", d._roster_augmented("PHX"))
check("off-roster and repeat slots read empty", rot and slugs(five) == [None, "b-sg", None, None, None], slugs(five))

ROTATIONS["PHX"] = {"PG": "nobody", "C": "also-nobody"}
five, rot = d._starting_five("PHX", d._roster_augmented("PHX"))
check("rotation naming no one -> computed", not rot and slugs(five)[0] == "a-pg", slugs(five))

print("/roster")
ROTATIONS["PHX"] = {"PG": "f-pg", "SG": "g-tw", "SF": "c-sf", "PF": "d-pf", "C": "e-c"}
d.TEAM_COLORS.setdefault("PHX", 0)
emb = d.roster_response("PHX")["data"]["embeds"][0]
desc = emb["description"]
pg_block = desc.split("\n\n")[0].split("\n")
check("rotation starter bolded at PG", pg_block[1] == "**Pg F — 70**", pg_block)
check("benched best PG listed under PG", pg_block[2:] == ["Pg A — 85"], pg_block)
check("two-way starter not repeated in Two-Way block", "Two-Way" not in desc, desc)
check("footer says saved rotation", "saved rotation" in emb["footer"]["text"])
ROTATIONS.clear()
emb = d.roster_response("PHX")["data"]["embeds"][0]
check("footer says projected without one", "projected" in emb["footer"]["text"])
check("two-way back in its block without a rotation", "Two-Way" in emb["description"])

print("/lineups")
r = d.lineups_response("phx", "lal")["data"]["embeds"][0]
check("no game: order given", r["title"].startswith("PHX vs LAL"), r["title"])
check("no game: says so", "No upcoming game" in r["description"])
check("two fields", [f["name"] for f in r["fields"]] == ["PHX projected starters", "LAL projected starters"],
      [f["name"] for f in r["fields"]])
check("avg OVR shown", "Avg OVR 80.8" in r["fields"][1]["value"], r["fields"][1]["value"])

GAMES[:] = [
    {"date": "2026-10-20", "away_team": "PHX", "home_team": "LAL"},   # past
    {"date": "2026-11-02", "time_et": "7:30p", "away_team": "LAL", "home_team": "PHX"},
    {"date": "2026-12-01", "away_team": "PHX", "home_team": "LAL"},
]
ROTATIONS["LAL"] = {"PG": "h-pg"}
r = d.lineups_response("PHX", "LAL")["data"]["embeds"][0]
check("oriented off next game", r["title"].startswith("LAL @ PHX"), r["title"])
check("links preview", r["url"] == "https://nbn.today/preview/?away=LAL&home=PHX&date=2026-11-02", r["url"])
check("tip-off shown", "7:30p ET" in r["description"], r["description"])
check("away field first, marked as saved", r["fields"][0]["name"] == "LAL starters", r["fields"][0]["name"])
check("empty rotation slot shows a dash", "`SG` —" in r["fields"][0]["value"], r["fields"][0]["value"])

check("unknown team", "Unknown team" in d.lineups_response("XXX", "LAL")["data"]["content"])
check("same team twice", "different" in d.lineups_response("LAL", "lal")["data"]["content"])

print("dispatch")
out = d.dispatch({"name": "lineups", "options": [{"name": "team1", "value": "PHX"},
                                                 {"name": "team2", "value": "LAL"}]}, {})
check("dispatch routes /lineups", "embeds" in out["data"])

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)

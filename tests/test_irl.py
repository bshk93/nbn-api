"""`routers/irl.py` — the IRL feed's ESPN normalizer and its read routes.

What's worth pinning is what would go wrong silently, since ESPN's shapes are
undocumented and nothing else checks them:

- **A game lands in the right season type.** Football has one category per
  season type; basketball splits it by month or playoff round. Preseason is
  dropped. A game with no stat line (a lineman's) still counts as played, and
  its type comes from the event note.
- **The score is the player's team's first**, whichever side was home.
- **A closed season is never refetched; the current one always is.**
- **`max_games` trims newest first across seasons**, and `has_stats` is false
  only for a player with no stat columns at all.
- **An NBN-sourced roster (NBA) is read live from `{abbr}-roster.csv`**, and
  auto-mapping a slug to an ESPN id needs an exact name AND date of birth —
  one match only, and never for unsigned draft rights.
- **A run skips a player whose file is fresher than `refresh_minutes`.**

No network and no live data: requests are faked, files go to a temp dir.

    venv/bin/python -m tests.test_irl
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.irl as irl  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-irl-test-"))
irl.IRL_DIR = TMP / "irl"
irl.IRL_ROSTERS_FILE = TMP / "irl-rosters.json"
irl.REQUEST_GAP_SECONDS = 0
irl.DATA_DIR = TMP
irl.PLAYER_BIOS_FILE = TMP / "player-bios.json"
irl.IRL_IDS_FILE = TMP / "irl-ids.json"


def ev(eid, date, home_id, away_id, hs, as_, team_id, note=None):
    e = {"id": eid, "gameDate": date, "atVs": "vs" if team_id == home_id else "@",
         "homeTeamId": home_id, "awayTeamId": away_id,
         "homeTeamScore": str(hs), "awayTeamScore": str(as_), "gameResult": "W",
         "team": {"id": team_id, "abbreviation": "CIN"},
         "opponent": {"abbreviation": "PIT"}}
    if note:
        e["eventNote"] = note
    return e


FOOTBALL = {
    "names": ["receptions", "receivingYards", "fumbles"],
    "labels": ["REC", "YDS\t", "FUM"],
    "categories": [{"displayName": "Receiving", "count": 2}, {"displayName": "Fumbles", "count": 1}],
    "events": {
        "1": ev("1", "2025-09-07T17:00Z", "4", "23", 24, 20, "4"),
        "2": ev("2", "2025-09-14T17:00Z", "23", "4", 10, 31, "4"),
        "3": ev("3", "2026-01-10T17:00Z", "4", "23", 17, 14, "4"),
        "9": ev("9", "2025-08-10T17:00Z", "4", "23", 3, 7, "4", note="Preseason Week 1"),
    },
    "seasonTypes": [
        {"displayName": "2025 Preseason", "categories": [
            {"type": "event", "events": [{"eventId": "9", "stats": ["1", "5", "0"]}]}]},
        {"displayName": "2025 Postseason", "categories": [
            {"type": "event", "events": [{"eventId": "3", "stats": ["8", "120", "0"]}]}]},
        {"displayName": "2025 Regular Season", "categories": [
            {"type": "event", "events": [{"eventId": "1", "stats": ["5", "64", "1"]},
                                         {"eventId": "2", "stats": ["7", "98", "0"]}]}],
         "summary": {"stats": [{"displayName": "Totals", "stats": ["12", "162", "1"]}]}},
    ],
}

print("normalize_gamelog — football")
s = irl.normalize_gamelog(FOOTBALL, "nfl", 2025, closed=True)
check("columns carry their group and a stripped label",
      s["columns"][1] == {"key": "receivingYards", "label": "YDS", "group": "Receiving"}
      and s["columns"][2]["group"] == "Fumbles")
check("preseason game dropped", [g["id"] for g in s["games"]] == ["3", "2", "1"])
check("season types", [g["type"] for g in s["games"]] == ["post", "reg", "reg"])
check("stats keyed by name", s["games"][1]["stats"] == {"receptions": "7", "receivingYards": "98", "fumbles": "0"})
check("score is own team's first when away", (s["games"][1]["team_score"], s["games"][1]["opp_score"]) == (31, 10))
check("score is own team's first when home", (s["games"][2]["team_score"], s["games"][2]["opp_score"]) == (24, 20))
check("totals kept with their type", s["totals"] == [{"type": "reg", "label": "Totals",
                                                     "stats": {"receptions": "12", "receivingYards": "162", "fumbles": "1"}}])
check("label", s["label"] == "2025" and s["closed"] is True)

print("normalize_gamelog — a lineman: games, no stats")
lineman = {"events": {"5": ev("5", "2026-01-11T17:00Z", "4", "23", 19, 23, "4", note="NFC Wild Card Playoffs"),
                      "6": ev("6", "2025-12-28T17:00Z", "4", "23", 19, 23, "4")},
           "seasonTypes": [{"displayName": "2025 Regular Season", "categories": [{"type": "event", "events": []}]}]}
s = irl.normalize_gamelog(lineman, "nfl", 2025, closed=True)
check("games kept with empty stats", [(g["id"], g["type"], g["stats"]) for g in s["games"]]
      == [("5", "post", {}), ("6", "reg", {})])
check("no columns", s["columns"] == [])

print("normalize_gamelog — basketball months and play-in")
BASKETBALL = {
    "names": ["points"], "labels": ["PTS"],
    "events": {"a": ev("a", "2026-04-15T23:00Z", "1", "2", 110, 100, "1"),
               "b": ev("b", "2026-03-02T23:00Z", "1", "2", 110, 100, "1"),
               "c": ev("c", "2026-04-02T23:00Z", "1", "2", 110, 100, "1")},
    "seasonTypes": [
        {"displayName": "2025-26 Play In Regular Season", "categories": [
            {"type": "event", "displayName": "april", "events": [{"eventId": "a", "stats": ["30"]}]}]},
        {"displayName": "2025-26 Regular Season", "categories": [
            {"type": "event", "displayName": "april", "events": [{"eventId": "c", "stats": ["12"]}]},
            {"type": "event", "displayName": "march", "events": [{"eventId": "b", "stats": ["20"]}]},
            {"type": "total", "displayName": "Regular Season", "events": [{"eventId": "x", "stats": ["99"]}]}]},
    ],
}
s = irl.normalize_gamelog(BASKETBALL, "nba", 2026, closed=False)
check("months merged, total category ignored", [(g["id"], g["type"], g["stats"]["points"]) for g in s["games"]]
      == [("a", "playin", "30"), ("c", "reg", "12"), ("b", "reg", "20")])
check("nba season label", s["label"] == "2025-26" and irl.season_label("nba", 2027) == "2026-27")
check("no categories → no group", s["columns"] == [{"key": "points", "label": "PTS", "group": None}])

print("normalize_bio")
b = irl.normalize_bio({"athlete": {
    "displayName": "Ricky Pearsall", "position": {"abbreviation": "WR"},
    "team": {"abbreviation": "SF", "displayName": "San Francisco 49ers"},
    "status": {"name": "Day-To-Day"}, "headshot": {"href": "h.png"},
    "injuries": [{"status": "Injured Reserve", "shortComment": "Surgery.", "date": "2026-08-13T15:11Z",
                  "type": {"abbreviation": "IR"},
                  "details": {"type": "Knee - PCL", "detail": "Surgery", "returnDate": "2027-02-15T00:00Z"}}]}})
check("bio fields", (b["name"], b["pos"], b["team"], b["headshot"]) == ("Ricky Pearsall", "WR", "SF", "h.png"))
check("injury", b["injury"] == {"status": "Injured Reserve", "abbr": "IR", "detail": "Knee - PCL — Surgery",
                                "return_date": "2027-02-15T00:00Z", "comment": "Surgery.", "date": "2026-08-13T15:11Z"})
d = irl.normalize_bio({"athlete": {"injuries": [{"details": {"type": "Concussion", "detail": "Concussion"}}]}})
check("repeated injury detail collapsed", d["injury"]["detail"] == "Concussion")
d = irl.normalize_bio({"athlete": {"injuries": [{"details": {"type": "Ribs", "detail": "Not Specified"}}]}})
check("'Not Specified' dropped", d["injury"]["detail"] == "Ribs")
check("no injury → None", irl.normalize_bio({"athlete": {}})["injury"] is None)

print("fetch_player — closed seasons are kept, the current one refetched")
calls = []


def fake_get(client, url, **params):
    calls.append((url.rsplit("/", 1)[-1], params.get("season")))
    if url.endswith("/gamelog"):
        return FOOTBALL
    return {"athlete": {"displayName": "Ja'Marr Chase"}}


irl._get = fake_get
irl.fetch_player(None, "nfl", "100", current=2026)
check("first fetch asks for both seasons and the bio",
      calls == [("gamelog", 2026), ("gamelog", 2025), ("100", None)])
saved = json.loads(irl.player_path("nfl", "100").read_text())
check("closed flag", saved["seasons"]["2025"]["closed"] is True and saved["seasons"]["2026"]["closed"] is False)
calls.clear()
irl.fetch_player(None, "nfl", "100", current=2026)
check("second fetch skips the closed season", calls == [("gamelog", 2026), ("100", None)])
calls.clear()
irl.fetch_player(None, "nfl", "100", current=2027)
check("a rollover refetches the season that was current", calls == [("gamelog", 2027), ("gamelog", 2026), ("100", None)])
check("a season past the window is dropped",
      sorted(json.loads(irl.player_path("nfl", "100").read_text())["seasons"]) == ["2026", "2027"])
calls.clear()
irl.fetch_player(None, "nfl", "100", current=2027, force=True)
check("--force refetches closed seasons", ("gamelog", 2026) in calls)

print("routes")
irl.IRL_ROSTERS_FILE.write_text(json.dumps({
    "nfl": {"CIN": {"league": "NBNFL", "label": "Bengals",
                    "players": [{"name": "Chase", "espn_id": "100"}, {"name": "Lineman", "espn_id": "200"},
                                {"name": "Unfetched", "espn_id": "300"}]}},
    "cricket": {"X": {"players": []}},
}))
lineman_season = irl.normalize_gamelog(lineman, "nfl", 2025, closed=True)
irl.player_path("nfl", "200").write_text(json.dumps({"fetched_at": "2026-10-03T00:00:00+00:00", "bio": {},
                                                     "seasons": {"2025": lineman_season}}))
app = FastAPI()
app.include_router(irl.router)
c = TestClient(app)
r = c.get("/api/irl/rosters").json()
check("rosters list skips unknown sports and empty NBN rosters", list(r) == ["nfl"])
check("roster summary", r["nfl"]["rosters"] == [{"key": "CIN", "league": "NBNFL", "label": "Bengals", "count": 3}])
r = c.get("/api/irl/nfl/cin")
check("roster key is case-insensitive", r.status_code == 200 and r.json()["roster"] == "CIN")
players = r.json()["players"]
check("roster order kept", [p["name"] for p in players] == ["Chase", "Lineman", "Unfetched"])
check("has_stats", [p["has_stats"] for p in players] == [True, False, False])
check("seasons newest first", [s["year"] for s in players[0]["seasons"]] == [2027, 2026])
check("unfetched player is empty, not an error", players[2]["bio"] is None and players[2]["seasons"] == [])
check("unknown roster 404s", c.get("/api/irl/nfl/XYZ").status_code == 404)
check("unknown sport 404s", c.get("/api/irl/mlb/CIN").status_code == 404)

print("NBN-sourced rosters (NBA)")
(TMP / "bos-roster.csv").write_text("SLUG\noubre-kelly\nmartin-cody\nnew-guy\nrights-only\nno-dob\n")
irl.PLAYER_BIOS_FILE.write_text(json.dumps({
    "oubre-kelly": {"name": "OUBRE, KELLY", "dob": "1995-12-09", "type": "player"},
    "martin-cody": {"name": "MARTIN, CODY", "dob": "1995-09-28", "type": "player"},
    "new-guy":     {"name": "GUY, NEW", "dob": "2005-01-01", "type": "two-way"},
    "rights-only": {"name": "ONLY, RIGHTS", "dob": "2006-01-01", "type": "draft-rights"},
    "no-dob":      {"name": "DOB, NO", "type": "player"},
}))
irl.IRL_IDS_FILE.write_text(json.dumps({"nba": {"oubre-kelly": "3133603"}}))
ESPN_ROSTER = {"athletes": [
    {"id": 1, "fullName": "Caleb Martin", "dateOfBirth": "1995-09-28T07:00Z"},   # Cody's twin
    {"id": 2, "fullName": "New Guy Jr.", "dateOfBirth": "2005-01-01T07:00Z"},
    {"id": 3, "fullName": "Rights Only", "dateOfBirth": "2006-01-01T07:00Z"},
    {"id": 4, "fullName": "No Dob", "dateOfBirth": "2001-01-01T07:00Z"},
]}
calls.clear()


def fake_espn(client, url, **params):
    calls.append(url)
    if url.endswith("/teams"):
        return {"sports": [{"leagues": [{"teams": [{"team": {"id": "2"}}]}]}]}
    if url.endswith("/roster"):
        return ESPN_ROSTER
    return fake_get(client, url, **params)


irl._get = fake_espn
m = irl.auto_map(None, "nba")
check("exact name + DOB is mapped (suffix ignored)", m["added"] == {"new-guy": "2"})
check("a twin with the same birthday is not", "martin-cody" in m["unmapped"])
check("no DOB on our side → not mapped", "no-dob" in m["unmapped"])
check("draft rights are skipped, not reported", "rights-only" not in m["unmapped"] and "rights-only" not in m["added"])
check("ids file keeps the old id and gains the new",
      json.loads(irl.IRL_IDS_FILE.read_text())["nba"] == {"oubre-kelly": "3133603", "new-guy": "2"})
calls.clear()
irl.IRL_IDS_FILE.write_text(json.dumps({"nba": {"oubre-kelly": "3133603", "new-guy": "2", "martin-cody": "3138161",
                                                "no-dob": "9"}}))
check("nothing unmapped → no ESPN roster requests", irl.auto_map(None, "nba") == {"added": {}, "unmapped": []}
      and calls == [])

r = c.get("/api/irl/nba/bos")
check("NBA roster read from the roster CSV", r.status_code == 200 and r.json()["roster"] == "BOS")
ps = r.json()["players"]
check("every rostered slug listed, in roster order", [p["slug"] for p in ps]
      == ["oubre-kelly", "martin-cody", "new-guy", "rights-only", "no-dob"])
check("display name from the bio", ps[0]["name"] == "Kelly Oubre")
check("unmapped player has no id and no stats", ps[3]["espn_id"] == "" and ps[3]["has_stats"] is False)
check("NBA rosters listed only where a roster file exists",
      [x["key"] for x in c.get("/api/irl/rosters").json()["nba"]["rosters"]] == ["BOS"])
check("a team that isn't one 404s", c.get("/api/irl/nba/XYZ").status_code == 404)
check("sport_ids: mapped ids only, once each", irl.sport_ids("nba") == ["3133603", "3138161", "2", "9"])

print("refresh_minutes")
from datetime import datetime, timedelta, timezone  # noqa: E402
now = datetime.now(timezone.utc)
irl.player_path("nba", "3133603").parent.mkdir(parents=True, exist_ok=True)
irl.player_path("nba", "3133603").write_text(json.dumps({"fetched_at": now.isoformat()}))
irl.player_path("nba", "3138161").write_text(json.dumps({"fetched_at": (now - timedelta(hours=5)).isoformat()}))
check("a fresh file is fresh", irl._fresh("nba", "3133603"))
check("an old file is stale", not irl._fresh("nba", "3138161"))
check("no file is stale", not irl._fresh("nba", "2"))
irl.current_season = lambda client, sport: 2027
summary = irl.fetch_all(sports=["nba"])["nba"]
check("fetch_all refetches only the stale three", (summary["ok"], summary["fresh"]) == (3, 1))
summary = irl.fetch_all(sports=["nba"], force=True)["nba"]
check("--force refetches everyone", (summary["ok"], summary["fresh"]) == (4, 0))

print("_trim")
seasons = [{"year": 2, "games": [1, 2, 3]}, {"year": 1, "games": [4, 5, 6]}]
check("newest N across seasons", irl._trim(seasons, 4) == [{"year": 2, "games": [1, 2, 3]}, {"year": 1, "games": [4]}])
check("None keeps all", irl._trim(seasons, None) == seasons)
check("exact fit drops the older season", irl._trim(seasons, 3) == [{"year": 2, "games": [1, 2, 3]}])

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall ok")

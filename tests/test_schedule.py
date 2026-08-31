"""`routers/schedule.py` — the league game schedule.

The schedule is seeded once from the real NBA schedule and hand-edited
thereafter, which is the whole reason these endpoints exist: NBN's in-season cup
does not follow the NBA's, so the 60 group games the seed carries are the
league's to reassign. That makes the edit paths, not the read path, the thing
worth pinning:

- **A team cannot play twice on one date.** This is the only structural rule the
  file has, and it is what would let a submitted box score be checked against a
  real fixture later. It has to survive a *move*, not just an insert — a PATCH
  that walks a game onto a date where one of its teams is already playing is the
  same error arriving by a different door.
- **...but a game must not conflict with itself.** The obvious implementation of
  the rule above rejects every PATCH that leaves the date alone, because the
  game is already on that date playing those teams. Retiming a game is the
  common edit; it must not need an override.
- **An id addresses a game without naming its season.** Seasons live in separate
  files, and a caller holding an id from a list should not have to know which
  one it came out of.
- **Editing never disturbs the rest of the file.** Seeded rows carry a
  `source_id` back to Basketball-Reference; a nearby edit must leave it alone,
  and the file has to stay in date/time order so its diffs stay readable.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_schedule
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.schedule as sched  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

BOD_TOKEN  = "b" * 64
PLAIN_TOKEN = "n" * 64
MEMBERS = {
    "Boss":    {"token": BOD_TOKEN,   "roles": ["bod"], "tenures": []},
    "Nobody":  {"token": PLAIN_TOKEN, "roles": [],      "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-schedule-test-"))
sched.DATA_DIR = TMP
sched._current_league_year = lambda: "26-27"
sched.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(sched.router)
c = TestClient(app)

BOD   = {"Authorization": "Bearer " + BOD_TOKEN}
PLAIN = {"Authorization": "Bearer " + PLAIN_TOKEN}


def seed(season, games, source="test"):
    (TMP / f"schedule-{season}.json").write_text(json.dumps(
        {"season": season, "source": source, "games": games}, indent=2))


def g(gid, date, away, home, time_et="7:00p", source_id=""):
    return {"id": gid, "date": date, "time_et": time_et, "away_team": away,
            "home_team": home, "arena": "", "note": "", "source_id": source_id}


seed("26-27", [
    g("aaa", "2026-10-20", "BOS", "DET", "3:00p", source_id="202610200DET"),
    g("bbb", "2026-10-20", "PHI", "NYK", "7:00p", source_id="202610200NYK"),
    g("ccc", "2026-10-22", "LAL", "GSW", "10:00p", source_id="202610220GSW"),
])
seed("25-26", [g("old", "2025-12-25", "MIA", "BOS", "2:30p")])


def games(season=None):
    q = f"?season={season}" if season else ""
    return c.get("/api/schedule" + q).json()["games"]


def ids(season=None):
    return [x["id"] for x in games(season)]


# ── reading ───────────────────────────────────────────────────────────────────

print("reading")
body = c.get("/api/schedule").json()
check("defaults to the current season", body["season"] == "26-27")
check("carries the seed's provenance", body["source"] == "test")
check("count matches the games", body["count"] == len(body["games"]) == 3)
check("another season is reachable by name", len(games("25-26")) == 1)
check("a season with no file reads as empty, not 404",
      c.get("/api/schedule?season=30-31").json()["games"] == [])
check("a nonsense season is a 422", c.get("/api/schedule?season=2026").status_code == 422)

check("team filter catches home and away",
      {x["id"] for x in c.get("/api/schedule?team=DET").json()["games"]} == {"aaa"})
check("team filter is case-insensitive",
      len(c.get("/api/schedule?team=det").json()["games"]) == 1)
check("unknown team is a 422", c.get("/api/schedule?team=XXX").status_code == 422)
check("from is inclusive",
      {x["id"] for x in c.get("/api/schedule?from=2026-10-22").json()["games"]} == {"ccc"})
check("to is inclusive",
      {x["id"] for x in c.get("/api/schedule?to=2026-10-20").json()["games"]} == {"aaa", "bbb"})

seasons = {s["season"]: s["games"] for s in c.get("/api/schedule/seasons").json()}
check("seasons lists every file with a count", seasons == {"25-26": 1, "26-27": 3})

# ── auth ──────────────────────────────────────────────────────────────────────

print("auth")
NEW = {"date": "2026-11-01", "away_team": "MIA", "home_team": "ORL"}
check("reading needs no token", c.get("/api/schedule").status_code == 200)
check("a member without bod cannot add", c.post("/api/schedule", json=NEW, headers=PLAIN).status_code == 403)
check("no token at all cannot add", c.post("/api/schedule", json=NEW).status_code in (401, 403))
check("nothing was written", len(games()) == 3)

# ── adding ────────────────────────────────────────────────────────────────────

print("adding")
r = c.post("/api/schedule", json=NEW, headers=BOD)
check("bod can add", r.status_code == 200)
added = r.json()
check("a hand-added game gets an id", bool(added["id"]) and added["id"] not in ("aaa", "bbb", "ccc"))
check("and no source_id, since it came from nobody", added["source_id"] == "")
check("it is in the file", added["id"] in ids())

check("24h time is normalized to the NBA's spelling",
      c.post("/api/schedule", json={"date": "2026-11-02", "away_team": "UTA",
                                    "home_team": "POR", "time_et": "19:30"},
             headers=BOD).json()["time_et"] == "7:30p")
check("'7:00PM' normalizes too",
      c.post("/api/schedule", json={"date": "2026-11-03", "away_team": "UTA",
                                    "home_team": "SAC", "time_et": "7:00PM"},
             headers=BOD).json()["time_et"] == "7:00p")
check("midnight-hour maths does not wrap 12:30p to 12:30a",
      c.post("/api/schedule", json={"date": "2026-11-04", "away_team": "UTA",
                                    "home_team": "PHX", "time_et": "12:30"},
             headers=BOD).json()["time_et"] == "12:30p")
check("garbage time is a 422",
      c.post("/api/schedule", json={"date": "2026-11-05", "away_team": "UTA",
                                    "home_team": "LAC", "time_et": "tea time"},
             headers=BOD).status_code == 422)
check("bad date is a 422",
      c.post("/api/schedule", json={"date": "Nov 5", "away_team": "UTA",
                                    "home_team": "LAC"}, headers=BOD).status_code == 422)
check("unknown team is a 422",
      c.post("/api/schedule", json={"date": "2026-11-05", "away_team": "SEA",
                                    "home_team": "LAC"}, headers=BOD).status_code == 422)
check("a team cannot host itself",
      c.post("/api/schedule", json={"date": "2026-11-05", "away_team": "LAC",
                                    "home_team": "LAC"}, headers=BOD).status_code == 422)

# ── the one structural rule ───────────────────────────────────────────────────

print("a team plays once a day")
r = c.post("/api/schedule", json={"date": "2026-10-20", "away_team": "DET", "home_team": "MIA"},
           headers=BOD)
check("double-booking a team is a 409", r.status_code == 409)
check("and the message names the game in the way", "DET" in r.json()["detail"])
check("the conflicting game was not written", len([x for x in games() if x["date"] == "2026-10-20"]) == 2)
r = c.post("/api/schedule", json={"date": "2026-10-20", "away_team": "DET", "home_team": "MIA",
                                  "allow_conflict": True}, headers=BOD)
check("allow_conflict lets a doubleheader through", r.status_code == 200)
dh_id = r.json()["id"]
check("two teams on the same day are fine otherwise",
      c.post("/api/schedule", json={"date": "2026-10-20", "away_team": "UTA",
                                    "home_team": "SAS"}, headers=BOD).status_code == 200)
c.delete(f"/api/schedule/{dh_id}", headers=BOD)

# ── editing ───────────────────────────────────────────────────────────────────

print("editing")
check("retiming a game does not conflict with itself",
      c.patch("/api/schedule/aaa", json={"time_et": "8:00p"}, headers=BOD).status_code == 200)
check("the new time stuck", [x for x in games() if x["id"] == "aaa"][0]["time_et"] == "8:00p")
check("a seeded row keeps its source_id through an edit",
      [x for x in games() if x["id"] == "aaa"][0]["source_id"] == "202610200DET")
check("a partial patch leaves the other fields alone",
      [x for x in games() if x["id"] == "aaa"][0]["away_team"] == "BOS")

# ccc is LAL @ GSW on the 22nd. Park LAL on the 27th, then try to walk ccc onto
# it: the clash is on the *away* team, and via a date change rather than an
# insert, which is the case a naive rule check misses.
c.post("/api/schedule", json={"date": "2026-10-27", "away_team": "LAL", "home_team": "SAC"},
       headers=BOD)
check("moving a game onto a date one of its teams already plays is a 409",
      c.patch("/api/schedule/ccc", json={"date": "2026-10-27"}, headers=BOD).status_code == 409)
check("the move was not applied",
      [x for x in games() if x["id"] == "ccc"][0]["date"] == "2026-10-22")
check("allow_conflict overrides a move too",
      c.patch("/api/schedule/ccc", json={"date": "2026-10-27", "allow_conflict": True},
              headers=BOD).status_code == 200)
c.patch("/api/schedule/ccc", json={"date": "2026-10-22"}, headers=BOD)
check("moving it somewhere free works",
      c.patch("/api/schedule/ccc", json={"date": "2026-10-25", "note": "Cup replacement"},
              headers=BOD).status_code == 200)
moved = [x for x in games() if x["id"] == "ccc"][0]
check("date and note both applied", (moved["date"], moved["note"]) == ("2026-10-25", "Cup replacement"))
check("patching to itself is a 422",
      c.patch("/api/schedule/ccc", json={"home_team": "LAL"}, headers=BOD).status_code == 422)
check("a member without bod cannot edit",
      c.patch("/api/schedule/ccc", json={"note": "no"}, headers=PLAIN).status_code == 403)
check("patching an unknown id is a 404",
      c.patch("/api/schedule/nope", json={"note": "x"}, headers=BOD).status_code == 404)

# ── ids address a game across seasons ─────────────────────────────────────────

print("an id finds its own season")
check("a past season's game is reachable without naming the season",
      c.patch("/api/schedule/old", json={"note": "Christmas"}, headers=BOD).status_code == 200)
check("and it was written to that season's file, not the current one",
      [x for x in games("25-26") if x["id"] == "old"][0]["note"] == "Christmas")
check("the current season did not gain a row", "old" not in ids())

# ── deleting ──────────────────────────────────────────────────────────────────

print("deleting")
check("a member without bod cannot delete",
      c.delete("/api/schedule/bbb", headers=PLAIN).status_code == 403)
check("bod can delete", c.delete("/api/schedule/bbb", headers=BOD).status_code == 200)
check("it is gone", "bbb" not in ids())
check("deleting it twice is a 404", c.delete("/api/schedule/bbb", headers=BOD).status_code == 404)
check("the rest of the season is untouched", "aaa" in ids() and "ccc" in ids())

# ── the file stays readable ───────────────────────────────────────────────────

print("the file on disk")
raw = json.loads((TMP / "schedule-26-27.json").read_text())
dates = [x["date"] for x in raw["games"]]
check("kept in date order", dates == sorted(dates))
same_day = [x["time_et"] for x in raw["games"] if x["date"] == "2026-10-20"]
check("and in tip-off order within a date", same_day == sorted(same_day, key=sched._time_key))
check("the season and source survived every write",
      raw["season"] == "26-27" and raw["source"] == "test")
check("every game has the full shape",
      all(set(x) == {"id", "date", "time_et", "away_team", "home_team",
                     "arena", "note", "source_id"} for x in raw["games"]))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

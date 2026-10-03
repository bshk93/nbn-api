"""`routers/nbnfl.py` — the NBNFL stat log's backend.

Deliberately the smallest subsystem in this API: one file, no roster/contract
model, no build step. What's worth pinning is the part that would be easy to
get wrong silently since nothing else checks it:

- **Only `MY_TEAM`'s games and stat lines are accepted.** A game that doesn't
  involve it, or a stat line for the opponent, is refused.
- **An edit re-validates the whole merged game**, and replacing `stats`
  replaces every line rather than appending.
- **Writes need the `stats` role (or `admin`); reads are open.** A role
  that isn't `stats`, `bod` included, is refused.

Writes go to a temp file; nothing here touches live data.

    venv/bin/python -m tests.test_nbnfl
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.nbnfl as nbnfl  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


ADMIN_TOKEN = "a" * 64
PLAIN_TOKEN = "n" * 64
STATS_TOKEN = "s" * 64
BOD_TOKEN = "b" * 64
MEMBERS = {
    "Stats":  {"token": STATS_TOKEN, "roles": ["stats"], "tenures": []},
    "Board":  {"token": BOD_TOKEN, "roles": ["bod"], "tenures": []},
    "Boss":   {"token": ADMIN_TOKEN, "roles": ["admin"], "tenures": []},
    "Nobody": {"token": PLAIN_TOKEN, "roles": [], "tenures": []},
}
auth.load_members = lambda: MEMBERS

nbnfl.NBNFL_FILE = Path(tempfile.mkdtemp(prefix="nbn-nbnfl-test-")) / "nbnfl.json"
nbnfl.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(nbnfl.router)
c = TestClient(app)

ADMIN = {"Authorization": "Bearer " + ADMIN_TOKEN}
PLAIN = {"Authorization": "Bearer " + PLAIN_TOKEN}
STATS = {"Authorization": "Bearer " + STATS_TOKEN}
BOD = {"Authorization": "Bearer " + BOD_TOKEN}


def base_game(**over):
    g = {"season": 2026, "week": 1, "date": "2026-09-07", "home": "CIN", "away": "BUF",
         "home_score": 27, "away_score": 20, "stats": []}
    g.update(over)
    return g


# ── teams ────────────────────────────────────────────────────────────────────

r = c.get("/api/nbnfl/teams")
check("teams: 200", r.status_code == 200)
check("teams: all 32 present", len(r.json()["teams"]) == 32)
check("teams: names the tracked team", r.json()["my_team"] == nbnfl.MY_TEAM == "CIN")
check("teams: KC is AFC West", any(t["abbr"] == "KC" and t["conference"] == "AFC" and t["division"] == "West"
                                    for t in r.json()["teams"]))

# ── write auth ───────────────────────────────────────────────────────────────

r = c.post("/api/nbnfl/games", json=base_game())
check("post: 401 with no token", r.status_code == 401)

r = c.post("/api/nbnfl/games", json=base_game(), headers=PLAIN)
check("post: 403 for a token with no role", r.status_code == 403)
r = c.post("/api/nbnfl/games", json=base_game(), headers=BOD)
check("post: 403 for bod (not stats)", r.status_code == 403)

# ── validation ───────────────────────────────────────────────────────────────

r = c.post("/api/nbnfl/games", json=base_game(home="CIN", away="CIN"), headers=ADMIN)
check("post: rejects home == away", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(away="ZZZ"), headers=ADMIN)
check("post: rejects an unknown team abbr", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(date="09-07-2026"), headers=ADMIN)
check("post: rejects a non-ISO date", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(away_score=-3), headers=ADMIN)
check("post: rejects a negative score", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(home="KC", away="BUF"), headers=ADMIN)
check("post: rejects a game the tracked team isn't in", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(
    stats=[{"player": "Josh Allen", "team": "BUF", "category": "passing", "stats": {"yds": 100}}]
), headers=ADMIN)
check("post: rejects a stat line for the opponent", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(
    stats=[{"player": "Some Guy", "team": "CIN", "category": "juggling", "stats": {}}]
), headers=ADMIN)
check("post: rejects an unknown stat category", r.status_code == 400)

# ── round trip: create, edit, delete ────────────────────────────────────────

r = c.post("/api/nbnfl/games", json=base_game(
    week=1, home="CIN", away="BUF", home_score=27, away_score=20,
    stats=[{"player": "J. Burrow", "team": "CIN", "category": "passing", "stats": {"yds": 305, "td": 3}}]
), headers=ADMIN)
check("post: 200 on a legal game", r.status_code == 200)
game_id = r.json()["id"]
check("post: assigns an id", bool(game_id))

r = c.post("/api/nbnfl/games", json=base_game(week=2, home="KC", away="CIN", home_score=17, away_score=24),
           headers=STATS)
check("post: 200 with the tracked team away, from the stats role", r.status_code == 200)

r = c.put(f"/api/nbnfl/games/{game_id}", json={"home_score": 30}, headers=PLAIN)
check("put: 403 for a token with no role", r.status_code == 403)

r = c.put(f"/api/nbnfl/games/{game_id}", json={"home_score": 30}, headers=ADMIN)
check("put: 200 on a partial edit", r.status_code == 200)
check("put: only the given field changed", r.json()["home_score"] == 30 and r.json()["away"] == "BUF"
      and len(r.json()["stats"]) == 1)

r = c.put(f"/api/nbnfl/games/{game_id}", json={"stats": [
    {"player": "J. Burrow", "team": "CIN", "category": "passing", "stats": {"yds": 310, "td": 3}},
    {"player": "C. Brown", "team": "CIN", "category": "rushing", "stats": {"yds": 90}},
    {"player": "T. Hendrickson", "team": "CIN", "category": "defense", "stats": {"tkl": 4, "sack": 1.5}},
]}, headers=ADMIN)
check("put: stats are replaced, not appended",
      r.status_code == 200 and len(r.json()["stats"]) == 3 and r.json()["stats"][0]["stats"]["yds"] == 310)

r = c.put(f"/api/nbnfl/games/{game_id}", json={"home": "ZZZ"}, headers=ADMIN)
check("put: re-validates the merged game", r.status_code == 400)

r = c.put(f"/api/nbnfl/games/{game_id}", json={"stats": [
    {"player": "Josh Allen", "team": "BUF", "category": "passing", "stats": {"yds": 1}}]}, headers=ADMIN)
check("put: rejects an opponent stat line on edit", r.status_code == 400)

r = c.delete(f"/api/nbnfl/games/{game_id}", headers=PLAIN)
check("delete: 403 for a token with no role", r.status_code == 403)
r = c.delete(f"/api/nbnfl/games/{game_id}", headers=STATS)
check("delete: 200 for the stats role", r.status_code == 200)
r = c.delete(f"/api/nbnfl/games/{game_id}", headers=ADMIN)
check("delete: 404 the second time", r.status_code == 404)

remaining = c.get("/api/nbnfl/games").json()
check("games: one left after the delete", len(remaining) == 1)


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
else:
    print("all checks passed")
sys.exit(1 if FAILS else 0)

"""`routers/nbnfl.py` — the NBNFL sister-league dashboard's backend.

Deliberately the smallest subsystem in this API: one file, no roster/contract
model, no build step. What's worth pinning is the part that would be easy to
get wrong silently since nothing else checks it:

- **Standings are computed from the raw game list, not stored.** Wins/losses/
  ties and PF/PA have to come out right for both the home and away side of
  every game, including a tie.
- **A stat line must belong to one of the two teams actually in its game.**
  Nothing else would catch a stat line attributed to a team that didn't play.
- **Leaders sum a category's primary stat across every stat line for a
  player**, not just the most recent one — a QB's week 2 line has to add to
  week 1, not replace it.
- **Writes are admin-gated; reads are not.**

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
MEMBERS = {
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


def base_game(**over):
    g = {"season": 2026, "week": 1, "date": "2026-09-07", "home": "KC", "away": "BUF",
         "home_score": 27, "away_score": 20, "stats": []}
    g.update(over)
    return g


# ── teams ────────────────────────────────────────────────────────────────────

r = c.get("/api/nbnfl/teams")
check("teams: 200", r.status_code == 200)
check("teams: all 32 present", len(r.json()["teams"]) == 32)
check("teams: KC is AFC West", any(t["abbr"] == "KC" and t["conference"] == "AFC" and t["division"] == "West"
                                    for t in r.json()["teams"]))

# ── write auth ───────────────────────────────────────────────────────────────

r = c.post("/api/nbnfl/games", json=base_game())
check("post: 401 with no token", r.status_code == 401)

r = c.post("/api/nbnfl/games", json=base_game(), headers=PLAIN)
check("post: 403 for a non-admin token", r.status_code == 403)

# ── validation ───────────────────────────────────────────────────────────────

r = c.post("/api/nbnfl/games", json=base_game(home="KC", away="KC"), headers=ADMIN)
check("post: rejects home == away", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(home="ZZZ"), headers=ADMIN)
check("post: rejects an unknown team abbr", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(date="09-07-2026"), headers=ADMIN)
check("post: rejects a non-ISO date", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(away_score=-3), headers=ADMIN)
check("post: rejects a negative score", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(
    stats=[{"player": "Some Guy", "team": "DAL", "category": "passing", "stats": {"yds": 100}}]
), headers=ADMIN)
check("post: rejects a stat line for a team not in the game", r.status_code == 400)

r = c.post("/api/nbnfl/games", json=base_game(
    stats=[{"player": "Some Guy", "team": "KC", "category": "juggling", "stats": {}}]
), headers=ADMIN)
check("post: rejects an unknown stat category", r.status_code == 400)

# ── round trip: create, standings, leaders, edit, delete ────────────────────

r = c.post("/api/nbnfl/games", json=base_game(
    week=1, home="KC", away="BUF", home_score=27, away_score=20,
    stats=[
        {"player": "Pat Mahomes", "team": "KC", "category": "passing", "stats": {"yds": 305, "td": 3}},
        {"player": "Josh Allen", "team": "BUF", "category": "passing", "stats": {"yds": 280, "td": 2}},
    ]
), headers=ADMIN)
check("post: 200 on a legal game", r.status_code == 200)
game_id = r.json()["id"]
check("post: assigns an id", bool(game_id))

r = c.post("/api/nbnfl/games", json=base_game(
    week=2, home="BUF", away="KC", home_score=17, away_score=24,
    stats=[{"player": "Pat Mahomes", "team": "KC", "category": "passing", "stats": {"yds": 260, "td": 1}}]
), headers=ADMIN)
check("post: 200 on the rematch", r.status_code == 200)

st = c.get("/api/nbnfl/standings").json()
kc = next(row for row in st["AFC"]["West"] if row["abbr"] == "KC")
buf = next(row for row in st["AFC"]["East"] if row["abbr"] == "BUF")
check("standings: KC is 2-0", kc["wins"] == 2 and kc["losses"] == 0)
check("standings: BUF is 0-2", buf["wins"] == 0 and buf["losses"] == 2)
check("standings: KC PF/PA sums both games from both sides", kc["pf"] == 27 + 24 and kc["pa"] == 20 + 17)
check("standings: diff is pf - pa", kc["diff"] == kc["pf"] - kc["pa"])

ld = c.get("/api/nbnfl/leaders").json()
check("leaders: Mahomes leads passing", ld["passing"][0]["player"] == "Pat Mahomes")
check("leaders: passing yards sum across both games, not just the last one",
      ld["passing"][0]["value"] == 305 + 260)

r = c.put(f"/api/nbnfl/games/{game_id}", json={"home_score": 30}, headers=ADMIN)
check("put: 200 on a partial edit", r.status_code == 200)
check("put: only the given field changed", r.json()["home_score"] == 30 and r.json()["away"] == "BUF")

r = c.put(f"/api/nbnfl/games/{game_id}", json={"home": "ZZZ"}, headers=ADMIN)
check("put: re-validates the merged game", r.status_code == 400)

r = c.delete(f"/api/nbnfl/games/{game_id}", headers=ADMIN)
check("delete: 200", r.status_code == 200)
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

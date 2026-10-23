"""`routers/tetris.py` — the homepage Tetris card's leaderboard. Pins:

  * the leaderboard is public; posting a score needs a member token
  * only a member's best game is kept, and a worse one doesn't replace it
  * the board is one row per member, best first, ties to whoever got there first
  * a score no game with that many lines could reach is a 422 and writes nothing
  * an honest game's score (every line a tetris, every piece hard-dropped) passes

Writes go to a temp file; nothing here touches $DATA_DIR.

    venv/bin/python -m tests.test_tetris
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.tetris as tetris  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


A_TOKEN, B_TOKEN = "a" * 64, "b" * 64
auth.load_members = lambda: {
    "Alpha": {"token": A_TOKEN, "roles": [], "tenures": []},
    "Bravo": {"token": B_TOKEN, "roles": [], "tenures": []},
}
tetris.TETRIS_SCORES_FILE = Path(tempfile.mkdtemp(prefix="nbn-tetris-test-")) / "tetris-scores.json"
tetris.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(tetris.router)
c = TestClient(app)
A = {"Authorization": "Bearer " + A_TOKEN}
B = {"Authorization": "Bearer " + B_TOKEN}

print("auth")
check("leaderboard is public and starts empty", c.get("/api/tetris/leaderboard").json() == {"leaderboard": []})
check("posting without a token is a 401", c.post("/api/tetris/scores", json={"score": 100, "lines": 1}).status_code == 401)

print("best game only")
r = c.post("/api/tetris/scores", json={"score": 4000, "lines": 12}, headers=A).json()
check("first game is a new best at rank 1", r["new_best"] and r["rank"] == 1)
check("level is the server's, from lines", r["best"]["level"] == 2)
r = c.post("/api/tetris/scores", json={"score": 900, "lines": 4}, headers=A).json()
check("a worse game isn't a new best", not r["new_best"] and r["best"]["score"] == 4000)
c.post("/api/tetris/scores", json={"score": 4000, "lines": 15}, headers=B)
board = c.get("/api/tetris/leaderboard").json()["leaderboard"]
check("one row per member", [row["member"] for row in board] == ["Alpha", "Bravo"])
check("a tie goes to whoever got there first", board[0]["member"] == "Alpha" and board[1]["rank"] == 2)
r = c.post("/api/tetris/scores", json={"score": 6000, "lines": 20}, headers=B).json()
check("beating the leader takes rank 1", r["new_best"] and r["rank"] == 1)
games = tetris._load_json(tetris.TETRIS_SCORES_FILE, {})
check("every finished game is counted", games["Alpha"]["games"] == 2 and games["Bravo"]["games"] == 2)

print("plausibility")
r = c.post("/api/tetris/scores", json={"score": 9_999_999, "lines": 3}, headers=A)
check("an unreachable score is a 422", r.status_code == 422)
check("and writes nothing", tetris._load_json(tetris.TETRIS_SCORES_FILE, {})["Alpha"]["games"] == 2)
check("negative scores are refused", c.post("/api/tetris/scores", json={"score": -1, "lines": 0}, headers=A).status_code == 422)
# The best a game can do: every 4 lines a tetris at its level, every piece hard-dropped 20 rows.
for lines in (0, 4, 40, 400):
    best = sum(800 * ((n * 4) // 10 + 1) for n in range(lines // 4)) + ((lines * 10 + 200) // 4) * 40
    check(f"a perfect {lines}-line game passes", tetris._plausible(best, lines))

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")

"""`routers/parse_request.py` — the "uploads done, parse the night" button. Pins:

  * unconfigured (no fire URL/token) is a 503 and fires nothing
  * needs the stats or rosters role
  * an empty queue (no game with both sides, no injury report) is a 409
  * a ready game fires once, with the date in the text, and returns the session URL
  * a second request inside the cooldown is a 409 naming the first run; force fires
  * a failed fire is a 502 and is recorded, and doesn't start the cooldown

Temp dirs only; the fire POST is stubbed, so no network.

    venv/bin/python -m tests.test_parse_request
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.injuries as injuries  # noqa: E402
import routers.parse_request as pr  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


S_TOKEN, X_TOKEN = "s" * 64, "x" * 64
auth.load_members = lambda: {
    "Stats": {"token": S_TOKEN, "roles": ["stats"], "tenures": []},
    "Fan": {"token": X_TOKEN, "roles": [], "tenures": []},
}
tmp = Path(tempfile.mkdtemp(prefix="nbn-parse-req-"))
pr.STATE_FILE = tmp / "parse-requests.json"
pr.PENDING_BOXSCORES_DIR = tmp / "pending"
injuries.PENDING_DIR = tmp / "pending-inj"
pr.PENDING_BOXSCORES_DIR.mkdir()

fired = []
fail_next = [False]


def fake_fire(url, token, text):
    if fail_next[0]:
        fail_next[0] = False
        raise RuntimeError("boom")
    fired.append(text)
    return {"claude_code_session_id": f"session_{len(fired)}",
            "claude_code_session_url": f"https://claude.ai/code/session_{len(fired)}"}


pr._post_fire = fake_fire

app = FastAPI()
app.include_router(pr.router)
c = TestClient(app)
S = {"Authorization": f"Bearer {S_TOKEN}"}
X = {"Authorization": f"Bearer {X_TOKEN}"}


def add_game(gid, date, home_imgs=1, away_imgs=1):
    d = pr.PENDING_BOXSCORES_DIR / gid
    d.mkdir()
    imgs = {"home": [{"file": f"home-{i}.jpg"} for i in range(home_imgs)],
            "away": [{"file": f"away-{i}.jpg"} for i in range(away_imgs)]}
    (d / "meta.json").write_text(json.dumps({"id": gid, "date": date, "home_team": "PHI",
                                             "away_team": "TOR", "images": imgs}))


os.environ.pop("NBN_PARSE_ROUTINE_URL", None)
os.environ.pop("NBN_PARSE_ROUTINE_TOKEN", None)
print("config and auth")
check("unconfigured is 503", c.post("/api/boxscores/parse-request", headers=S).status_code == 503)
os.environ["NBN_PARSE_ROUTINE_URL"] = "https://example.invalid/fire"
os.environ["NBN_PARSE_ROUTINE_TOKEN"] = "t"
check("no role is 403", c.post("/api/boxscores/parse-request", headers=X).status_code == 403)
check("GET says configured", c.get("/api/boxscores/parse-request", headers=S).json()["configured"] is True)

print("queue")
add_game("aaaaaaaa", "2026-11-12", away_imgs=0)
r = c.post("/api/boxscores/parse-request", headers=S)
check("one side only is 409", r.status_code == 409)
check("nothing fired", fired == [])

add_game("bbbbbbbb", "2026-11-12")
fail_next[0] = True
r = c.post("/api/boxscores/parse-request", headers=S)
check("failed fire is 502", r.status_code == 502)
check("failure recorded", pr._load()[-1]["ok"] is False)

r = c.post("/api/boxscores/parse-request", headers=S)
check("ready game fires", r.status_code == 200 and len(fired) == 1)
check("returns the session url", r.json().get("session_url") == "https://claude.ai/code/session_1")
check("text names the date", "2026-11-12: 1 game(s)" in fired[0])
check("text notes the half-uploaded game", "only one side" in fired[0])

print("cooldown")
r = c.post("/api/boxscores/parse-request", headers=S)
check("second request is 409", r.status_code == 409 and len(fired) == 1)
check("409 names the running session", r.json()["detail"]["session_url"] == "https://claude.ai/code/session_1")
r = c.post("/api/boxscores/parse-request", headers=S, json={"force": True})
check("force fires again", r.status_code == 200 and len(fired) == 2)
check("GET shows the last run", c.get("/api/boxscores/parse-request", headers=S).json()["last"]["session_id"] == "session_2")

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

"""The updates committee's tick on /ratings-changes.

- GET /api/ratings-changes carries `updated` per change: null until ticked.
- PUT /api/ratings-changes/updated needs `updates` (or admin); anyone else 403.
- A tick on a (slug, date) that isn't a real change is a 404.
- Unticking removes it.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_ratings_updated
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.players as players  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


UPD_TOKEN, ADMIN_TOKEN, PLAIN_TOKEN = "u" * 64, "d" * 64, "n" * 64
MEMBERS = {
    "Updater": {"token": UPD_TOKEN,   "roles": ["updates"], "tenures": []},
    "Admin":   {"token": ADMIN_TOKEN, "roles": ["admin"],   "tenures": []},
    "Nobody":  {"token": PLAIN_TOKEN, "roles": [],          "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-ratings-updated-test-"))
players.RATINGS_UPDATED_FILE = TMP / "ratings-updated.json"
players.log_write = lambda info, msg: None
players.load_player_bios = lambda: {"doe-john": {"name": "DOE, JOHN"}}
players.load_attributes = lambda: {"doe-john": [
    {"date": "2026-11-01", "2k_ovr": 80, "2k_pos": ["SF"], "attributes": {}, "badges": []},
    {"date": "2026-11-05", "2k_ovr": 82, "2k_pos": ["SF"], "attributes": {}, "badges": []},
]}

app = FastAPI()
app.include_router(players.router)
client = TestClient(app)


def put(token, **body):
    return client.put("/api/ratings-changes/updated", json=body,
                      headers={"Authorization": f"Bearer {token}"})


def entry():
    return client.get("/api/ratings-changes").json()["entries"][0]


print("ratings updated")
check("unticked change reads null", entry()["updated"] is None)
check("no role is 403", put(PLAIN_TOKEN, slug="doe-john", date="2026-11-05", updated=True).status_code == 403)
check("no such change is 404", put(UPD_TOKEN, slug="doe-john", date="2026-11-01", updated=True).status_code == 404)
r = put(UPD_TOKEN, slug="doe-john", date="2026-11-05", updated=True)
check("updates role can tick", r.status_code == 200 and r.json()["updated"]["by"] == "Updater")
check("GET shows who ticked it", (entry()["updated"] or {}).get("by") == "Updater")
check("admin can untick", put(ADMIN_TOKEN, slug="doe-john", date="2026-11-05", updated=False).status_code == 200)
check("unticked again reads null", entry()["updated"] is None)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")

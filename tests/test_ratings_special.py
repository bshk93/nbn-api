"""The updates committee's special-request flag on /ratings-changes.

- GET/PUT /api/ratings-changes/special need `updates` (or admin); anyone
  else 403, no token 401.
- The public GET /api/ratings-changes never carries the flag.
- A flag on a (slug, date) that isn't a real change is a 404.
- Unflagging removes it.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_ratings_special
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

TMP = Path(tempfile.mkdtemp(prefix="nbn-ratings-special-test-"))
players.RATINGS_UPDATED_FILE = TMP / "ratings-updated.json"
players.RATINGS_SPECIAL_FILE = TMP / "ratings-special.json"
players.log_write = lambda info, msg: None
players.load_player_bios = lambda: {"doe-john": {"name": "DOE, JOHN"}}
players.load_attributes = lambda: {"doe-john": [
    {"date": "2026-11-01", "2k_ovr": 80, "2k_pos": ["SF"], "attributes": {}, "badges": []},
    {"date": "2026-11-05", "2k_ovr": 82, "2k_pos": ["SF"], "attributes": {}, "badges": []},
]}

app = FastAPI()
app.include_router(players.router)
client = TestClient(app)


def auth_hdr(token):
    return {"Authorization": f"Bearer {token}"}


def put(token, **body):
    return client.put("/api/ratings-changes/special", json=body, headers=auth_hdr(token))


def flags(token=UPD_TOKEN):
    return client.get("/api/ratings-changes/special", headers=auth_hdr(token))


print("ratings special request")
check("no token can't read flags", client.get("/api/ratings-changes/special").status_code == 401)
check("no role can't read flags", flags(PLAIN_TOKEN).status_code == 403)
check("nothing flagged reads empty", flags().json() == {})
check("no role is 403", put(PLAIN_TOKEN, slug="doe-john", date="2026-11-05", special=True).status_code == 403)
check("no such change is 404", put(UPD_TOKEN, slug="doe-john", date="2026-11-01", special=True).status_code == 404)
r = put(UPD_TOKEN, slug="doe-john", date="2026-11-05", special=True)
check("updates role can flag", r.status_code == 200 and r.json()["special"]["by"] == "Updater")
check("admin reads who flagged it", flags(ADMIN_TOKEN).json().get("doe-john|2026-11-05", {}).get("by") == "Updater")
pub = client.get("/api/ratings-changes").json()["entries"][0]
check("public list doesn't carry the flag", "special" not in pub and "special_request" not in pub)
check("admin can unflag", put(ADMIN_TOKEN, slug="doe-john", date="2026-11-05", special=False).status_code == 200)
check("unflagged reads empty again", flags().json() == {})

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")

"""`routers/donations.py` — the league funding tracker.

Read is public (the site's /donations/ page has no auth gate), writes are
`bod`-only. Pins:

  * add and edit both validate team/member/amount/date the same way
  * an edit stamps `updated_by`/`updated_at` without touching `added_by`/`added_at`
  * a plain member (no `bod`) can read but not write
  * editing a nonexistent id is a 404, not a silent no-op

Writes go to a temp directory; nothing here touches donations.json in
NBS_DATA_DIR.

    venv/bin/python -m tests.test_donations
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
import routers.donations as donations  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


BOD_TOKEN   = "b" * 64
PLAIN_TOKEN = "n" * 64
MEMBERS = {
    "Boss":   {"token": BOD_TOKEN,   "roles": ["bod"], "tenures": []},
    "Nobody": {"token": PLAIN_TOKEN, "roles": [],      "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-donations-test-"))
donations.DONATIONS_FILE = TMP / "donations.json"
donations.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(donations.router)
c = TestClient(app)

BOD   = {"Authorization": "Bearer " + BOD_TOKEN}
PLAIN = {"Authorization": "Bearer " + PLAIN_TOKEN}

VALID = {"team": "LAL", "member": "RJ", "amount": 100, "date": "2026-09-07"}

# ── reading ───────────────────────────────────────────────────────────────────

print("reading")
check("starts empty", c.get("/api/donations").json() == [])
check("no auth required to read", c.get("/api/donations").status_code == 200)

# ── adding ────────────────────────────────────────────────────────────────────

print("adding")
check("plain member can't add", c.post("/api/donations", json=VALID, headers=PLAIN).status_code == 403)
check("no token can't add", c.post("/api/donations", json=VALID).status_code == 401)

r = c.post("/api/donations", json=VALID, headers=BOD)
check("bod can add", r.status_code == 201)
added = r.json()
check("gets an id", bool(added.get("id")))
check("carries the fields", (added["team"], added["member"], added["amount"], added["date"])
      == ("LAL", "RJ", 100, "2026-09-07"))
check("stamps who added it", added["added_by"] == "Boss")
check("shows up on read", c.get("/api/donations").json() == [added])

check("unknown team rejected", c.post("/api/donations",
      json={**VALID, "team": "XXX"}, headers=BOD).status_code == 422)
check("blank member rejected", c.post("/api/donations",
      json={**VALID, "member": "  "}, headers=BOD).status_code == 422)
check("non-positive amount rejected", c.post("/api/donations",
      json={**VALID, "amount": 0}, headers=BOD).status_code == 422)
check("malformed date rejected", c.post("/api/donations",
      json={**VALID, "date": "9/7/2026"}, headers=BOD).status_code == 422)

# ── editing ───────────────────────────────────────────────────────────────────

print("editing")
donation_id = added["id"]
edit_body = {"team": "ATL", "member": "KVL", "amount": 50, "date": "2026-09-08"}

check("plain member can't edit",
      c.put(f"/api/donations/{donation_id}", json=edit_body, headers=PLAIN).status_code == 403)

r = c.put(f"/api/donations/{donation_id}", json=edit_body, headers=BOD)
check("bod can edit", r.status_code == 200)
edited = r.json()
check("fields updated", (edited["team"], edited["member"], edited["amount"], edited["date"])
      == ("ATL", "KVL", 50, "2026-09-08"))
check("id unchanged", edited["id"] == donation_id)
check("original added_by/added_at survive the edit",
      edited["added_by"] == "Boss" and edited["added_at"] == added["added_at"])
check("stamps who edited it", edited["updated_by"] == "Boss")

check("editing a nonexistent id is a 404",
      c.put("/api/donations/nope", json=edit_body, headers=BOD).status_code == 404)

# ── the file on disk ────────────────────────────────────────────────────────

print("the file on disk")
raw = json.loads(donations.DONATIONS_FILE.read_text())
check("exactly one row", len(raw) == 1)
check("the edit persisted", raw[0]["team"] == "ATL" and raw[0]["amount"] == 50)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

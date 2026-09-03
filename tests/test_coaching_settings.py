"""`routers/coaching_settings.py` — per-team 2K coach profile.

This router is deliberately schema-blind: `values`/`minutes` are stored as
opaque dicts, so these tests pin the parts the server *does* own —

- **A team's own role writes its own blob**, same predicate as trading-block
  (`has_role(team.lower())` or `admin`) — a different team's role is a 403.
- **A save always marks `pending`.** Every PUT is a full replace, and every
  replace is unentered until a streamer says otherwise.
- **A save does not clobber the entered trail.** `entered_at`/`entered_by`
  survive a PUT that only flips `pending` back to true.
- **Only `streamer` (or admin) can mark a team entered**, and marking stamps
  `entered_at`/`entered_by` and clears `pending`.
- **A stale `expected_updated_at` on `/enter` is a 409, not a silent clear** —
  the whole reason that field exists is to stop a streamer's click from
  marking a *newer*, unseen save as entered.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_coaching_settings
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
import routers.coaching_settings as cs  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

ATL_TOKEN    = "a" * 64
BOS_TOKEN    = "b" * 64
ADMIN_TOKEN  = "d" * 64
STREAM_TOKEN = "s" * 64
PLAIN_TOKEN  = "n" * 64
MEMBERS = {
    "AtlOwner": {"token": ATL_TOKEN,    "roles": ["atl"],      "tenures": []},
    "BosOwner": {"token": BOS_TOKEN,    "roles": ["bos"],      "tenures": []},
    "Admin":    {"token": ADMIN_TOKEN,  "roles": ["admin"],    "tenures": []},
    "Streamy":  {"token": STREAM_TOKEN, "roles": ["streamer"], "tenures": []},
    "Nobody":   {"token": PLAIN_TOKEN,  "roles": [],           "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-coaching-settings-test-"))
cs.COACHING_SETTINGS_FILE = TMP / "coaching-settings.json"
cs.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(cs.router)
c = TestClient(app)

ATL    = {"Authorization": "Bearer " + ATL_TOKEN}
BOS    = {"Authorization": "Bearer " + BOS_TOKEN}
ADMIN  = {"Authorization": "Bearer " + ADMIN_TOKEN}
STREAM = {"Authorization": "Bearer " + STREAM_TOKEN}
PLAIN  = {"Authorization": "Bearer " + PLAIN_TOKEN}


def rec(team):
    return c.get("/api/coaching-settings").json().get(team)


# ── reading ───────────────────────────────────────────────────────────────────

print("reading")
check("an empty store reads as {}", c.get("/api/coaching-settings").json() == {})
check("reading needs no token", c.get("/api/coaching-settings").status_code == 200)

# ── writing ───────────────────────────────────────────────────────────────────

print("writing")
BODY = {"values": {"offensive_focus": "Get To The Basket"}, "minutes": {"PG": {"slug": "mann-tre", "minutes": 36}}}
check("no token cannot save", c.put("/api/coaching-settings/ATL", json=BODY).status_code in (401, 403))
check("a member without the role cannot save", c.put("/api/coaching-settings/ATL", json=BODY, headers=BOS).status_code == 403)
check("unknown team is a 404", c.put("/api/coaching-settings/XXX", json=BODY, headers=ATL).status_code == 404)
check("nothing was written yet", rec("ATL") is None)

r = c.put("/api/coaching-settings/ATL", json=BODY, headers=ATL)
check("the team's own role can save", r.status_code == 200)
saved = r.json()
check("the blob round-trips", saved["values"] == BODY["values"] and saved["minutes"] == BODY["minutes"])
check("who/when are stamped", saved["updated_by"] == "AtlOwner" and bool(saved["updated_at"]))
check("a fresh save is pending", saved["pending"] is True)
check("a fresh team has no entered trail yet", saved["entered_at"] is None and saved["entered_by"] is None)
check("lowercase team abbr works too",
      c.put("/api/coaching-settings/atl", json=BODY, headers=ATL).status_code == 200)
check("admin can save any team's blob",
      c.put("/api/coaching-settings/ATL", json=BODY, headers=ADMIN).status_code == 200)

# ── entering ──────────────────────────────────────────────────────────────────

print("entering")
check("a member without the streamer role cannot mark entered",
      c.post("/api/coaching-settings/ATL/enter", headers=ATL).status_code == 403)
check("no token cannot mark entered",
      c.post("/api/coaching-settings/ATL/enter").status_code in (401, 403))
check("entering an unknown team is a 404",
      c.post("/api/coaching-settings/XXX/enter", headers=STREAM).status_code == 404)
check("entering a team with no saved settings is a 404",
      c.post("/api/coaching-settings/BOS/enter", headers=STREAM).status_code == 404)

before = rec("ATL")
r = c.post("/api/coaching-settings/ATL/enter", json={"expected_updated_at": before["updated_at"]}, headers=STREAM)
check("a streamer can mark entered", r.status_code == 200)
entered = r.json()
check("pending clears", entered["pending"] is False)
check("entered_by/entered_at are stamped", entered["entered_by"] == "Streamy" and bool(entered["entered_at"]))
check("re-entering with no expected_updated_at is not an error",
      c.post("/api/coaching-settings/ATL/enter", json={}, headers=STREAM).status_code == 200)

print("a save after entering goes back to pending")
r2 = c.put("/api/coaching-settings/ATL", json=BODY, headers=ATL)
check("the new save is pending again", r2.json()["pending"] is True)
check("the entered trail survives the save",
      r2.json()["entered_by"] == "Streamy" and bool(r2.json()["entered_at"]))

print("stale enter")
stale_updated_at = before["updated_at"]  # the *first* save, now superseded
r3 = c.post("/api/coaching-settings/ATL/enter",
             json={"expected_updated_at": stale_updated_at}, headers=STREAM)
check("entering against a stale updated_at is a 409", r3.status_code == 409)
check("pending was not cleared by the stale attempt", rec("ATL")["pending"] is True)

# ── the file on disk ─────────────────────────────────────────────────────────

print("the file on disk")
raw = json.loads(cs.COACHING_SETTINGS_FILE.read_text())
check("only teams that were saved appear", set(raw.keys()) == {"ATL"})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

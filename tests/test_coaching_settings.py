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
# The routes under test send game-day inbox messages. Stub them: the real
# ones read the live members.json and write the live inboxes.json.
import routers.game_day_notify as gdn  # noqa: E402
for _fn in ('coaching_saved', 'coaching_entered', 'day_done', 'maybe_day_ready'):
    setattr(gdn, _fn, lambda *a, **k: None)
# The starter check reads 2K positions and bios; give it a fixed world
# instead of the live files.
import routers.players as players  # noqa: E402
PRIMARY = {"mann-tre": "PG", "big-man": "C", "wing-guy": "SF", "combo-sf": "SF"}
BIOS = {"big-man": {"name": "MAN, BIG"}, "wing-guy": {"name": "GUY, WING"},
        "combo-sf": {"name": "SF, COMBO", "secondary_pos": "PF"}}
players._primary_pos = lambda slug: PRIMARY.get(slug)
players.load_player_bios = lambda: BIOS

app = FastAPI()
app.include_router(cs.router)
c = TestClient(app)

ATL    = {"Authorization": "Bearer " + ATL_TOKEN}
BOS    = {"Authorization": "Bearer " + BOS_TOKEN}
ADMIN  = {"Authorization": "Bearer " + ADMIN_TOKEN}
STREAM = {"Authorization": "Bearer " + STREAM_TOKEN}
PLAIN  = {"Authorization": "Bearer " + PLAIN_TOKEN}


def rec(team):
    return c.get("/api/coaching-settings", headers=ADMIN).json().get(team)


# ── reading ───────────────────────────────────────────────────────────────────

print("reading")
check("an empty store reads as {}", c.get("/api/coaching-settings").json() == {})
check("reading with no token is a 200, not a 401", c.get("/api/coaching-settings").status_code == 200)

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

# ── who can read ──────────────────────────────────────────────────────────────

print("reading is scoped to the team")
c.put("/api/coaching-settings/BOS", json=BODY, headers=BOS)
def read(h=None):
    return c.get("/api/coaching-settings", headers=h or {}).json()
check("no token sees nothing", read() == {})
check("a member with no team role sees nothing", read(PLAIN) == {})
check("a bad token sees nothing", read({"Authorization": "Bearer " + "z" * 64}) == {})
check("a team role sees only its own team", set(read(ATL)) == {"ATL"})
check("the other team likewise", set(read(BOS)) == {"BOS"})
check("a streamer sees every team", set(read(STREAM)) == {"ATL", "BOS"})
check("admin sees every team", set(read(ADMIN)) == {"ATL", "BOS"})

# ── the public rotation ──────────────────────────────────────────────────────

print("the rotation order is public, the minutes are not")
c.put("/api/coaching-settings/BOS", json={"values": {"x": 1}, "minutes": {
    "PG": {"slug": "white-derrick", "minutes": 34, "res": False},
    "SG": {"slug": "", "minutes": 0, "res": False},
    "6":  {"slug": "brown-jaylen", "minutes": 20, "res": True},
}}, headers=BOS)
r = c.get("/api/rotations")
check("no token is a 200", r.status_code == 200)
rot = r.json()
check("each team maps slot -> slug, empty slots dropped", rot.get("BOS") == {"PG": "white-derrick", "6": "brown-jaylen"})
check("ATL's rotation is there too", rot.get("ATL") == {"PG": "mann-tre"})
check("no minutes, res or values leak", all(k not in r.text for k in ('"minutes"', '"res"', '"values"', '34', '20')))
c.put("/api/coaching-settings/BOS", json={"values": {}, "minutes": {}}, headers=BOS)
check("a team with no slots set is left out", "BOS" not in c.get("/api/rotations").json())
c.put("/api/coaching-settings/BOS", json=BODY, headers=BOS)

# ── starters must play near their slot ───────────────────────────────────────

print("starters")
check("where a C may start", cs.starter_positions("C", None) == ["PF", "C"])
check("where an SF may start", cs.starter_positions("SF", None) == ["SG", "SF", "PF"])
check("a secondary is added (already within one step today)",
      cs.starter_positions("SF", "PF") == ["SG", "SF", "PF"])
check("an unknown primary has no list", cs.starter_positions(None, "PF") == [])


def five(**slots):
    return {"values": {}, "minutes": {s: {"slug": slug, "minutes": 30} for s, slug in slots.items()}}


r = c.put("/api/coaching-settings/BOS", json=five(PG="big-man"), headers=BOS)
check("a C can't start at PG", r.status_code == 422 and "can't start at PG" in r.json()["detail"])
check("...and nothing was saved", (rec("BOS") or {}).get("minutes", {}).get("PG", {}).get("slug") != "big-man")
check("nor can admin put him there",
      c.put("/api/coaching-settings/BOS", json=five(PG="big-man"), headers=ADMIN).status_code == 422)
check("one step off is fine: a C at PF",
      c.put("/api/coaching-settings/BOS", json=five(PF="big-man"), headers=BOS).status_code == 200)
check("an SF at SG and PF",
      c.put("/api/coaching-settings/BOS", json=five(SG="wing-guy", PF="combo-sf"), headers=BOS).status_code == 200)
check("a player with no 2K position yet isn't blocked",
      c.put("/api/coaching-settings/BOS", json=five(C="rookie-unknown"), headers=BOS).status_code == 200)
check("the bench is never checked",
      c.put("/api/coaching-settings/BOS", json=five(**{"6": "big-man", "7": "mann-tre"}), headers=BOS).status_code == 200)

# A rotation saved before the rule: a slot left as it was keeps saving.
data = json.loads(cs.COACHING_SETTINGS_FILE.read_text())
data["BOS"]["minutes"] = five(PG="big-man")["minutes"]
cs.COACHING_SETTINGS_FILE.write_text(json.dumps(data))
check("an already-saved illegal starter doesn't block an unrelated save",
      c.put("/api/coaching-settings/BOS", json={"values": {"tempo": 5}, "minutes": five(PG="big-man")["minutes"]},
            headers=BOS).status_code == 200)
check("but moving a different illegal player in is refused",
      c.put("/api/coaching-settings/BOS", json=five(PG="big-man", SG="big-man"), headers=BOS).status_code == 422)

# ── the file on disk ─────────────────────────────────────────────────────────

print("the file on disk")
raw = json.loads(cs.COACHING_SETTINGS_FILE.read_text())
check("only teams that were saved appear", set(raw.keys()) == {"ATL", "BOS"})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

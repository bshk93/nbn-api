"""Regression tests for NB¥ theme unlocks (routers/themes.py).

What these pin, and why each one is here rather than assumed:

  * **The catalog is the price list.** The page never composes a price, so an
    id present in the catalog with the wrong price is a silent overcharge.
  * **Free themes cannot be bought.** They are in the catalog so the picker
    can render them; posting one is a 400, not a 5,000 NB¥ donation.
  * **Buying twice charges once.** The ownership check and the charge are
    under one lock; a double-clicked button must not pay twice.
  * **A member's own team's theme is free, and cannot be bought.** It is an
    entitlement derived from tenure, not a grant written into the owned list —
    so it must not be charged for, must not appear in `cosmetics.themes`, and
    must lapse when the tenure ends.
  * **Insufficient funds is a 402 that names the shortfall**, matching the
    other two NB¥ sinks (cosmetics, avatar).
  * **The unlock is additive.** It must not clobber `name_color` /
    `status_text` in the same `cosmetics` dict, and a second unlock must not
    drop the first.
  * **Every charge lands in the ledger**, which is the only record of the
    purchase — there is no receipt anywhere else.
  * **A failed grant refunds.** Charging for a theme the member does not end
    up owning is the one outcome with no way back for them.

Balances, ledger and members are all in-memory.

    venv/bin/python -m tests.test_themes
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.bets as bets  # noqa: E402
import routers.themes as themes  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

RICH_TOKEN = "r" * 64
POOR_TOKEN = "p" * 64

OWNER_TOKEN = "o" * 64

MEMBERS = {
    "Rich": {"token": RICH_TOKEN, "roles": [], "tenures": [],
             "cosmetics": {"name_color": "#a855f7", "status_text": "Kachow"}},
    "Poor": {"token": POOR_TOKEN, "roles": [], "tenures": []},
    # Holds ORL today, held BOS once and left. Only the open tenure counts.
    "Owner": {"token": OWNER_TOKEN, "roles": [], "tenures": [
        {"team": "BOS", "start": "2020-07-01", "end": "2022-07-25", "position": "owner"},
        {"team": "ORL", "start": "2026-04-26", "end": None, "position": "owner"},
    ]},
}
BALANCES = {"Rich": 12000.0, "Poor": 250.0, "Owner": 12000.0}
LEDGER = []

auth.load_members = lambda: MEMBERS
auth.save_members = lambda m: None      # load_members hands back MEMBERS itself
themes.load_members = auth.load_members
themes.save_members = auth.save_members
themes.log_write = lambda info, msg: None

bets._load_balances = lambda: BALANCES
bets._save_balances = lambda b: None    # same dict, mutated in place
bets._append_ledger = lambda entries: LEDGER.extend(entries)

app = FastAPI()
app.include_router(themes.router)
c = TestClient(app)


def bearer(token):
    return {"Authorization": "Bearer " + token}


PAID_ID = "lavender-rose"
TEAM_ID = "team-phx"
OWN_TEAM_ID = "team-orl"


# ── the catalog ───────────────────────────────────────────────────────────────

print("\n-- the catalog --")

r = c.get("/api/themes")
check("GET /api/themes is public", r.status_code == 200)
cat = r.json()["themes"]
by_id = {t["id"]: t for t in cat}

check("the two NBN Today themes are free",
      by_id["nbn-today"]["free"] and by_id["nbn-today-light"]["free"]
      and by_id["nbn-today"]["price"] == 0)
check("Lavender Rose is paid", not by_id[PAID_ID]["free"])
check("every paid theme is the one flat price",
      all(t["price"] == themes.THEME_PRICE for t in cat if not t["free"]))
check("the flat price is 5,000 NB¥", themes.THEME_PRICE == 5000.0)
check("PHX is listed and carries its abbreviation",
      by_id.get(TEAM_ID, {}).get("team") == "PHX")
check("every listed team theme has a live CSS block",
      all(t["team"] in themes.LIVE_TEAM_THEMES for t in cat if t.get("team")))
check("every catalog entry has a label and an icon",
      all(t.get("label") and t.get("icon") for t in cat))


# ── buying ────────────────────────────────────────────────────────────────────

print("\n-- buying --")

r = c.post(f"/api/members/me/themes/{PAID_ID}")
check("no token → 401", r.status_code == 401)

r = c.post("/api/members/me/themes/not-a-theme", headers=bearer(RICH_TOKEN))
check("unknown theme → 404", r.status_code == 404)

r = c.post("/api/members/me/themes/nbn-today", headers=bearer(RICH_TOKEN))
check("a free theme cannot be bought → 400", r.status_code == 400)
check("...and nothing was charged for it", BALANCES["Rich"] == 12000.0)

r = c.post(f"/api/members/me/themes/{PAID_ID}", headers=bearer(RICH_TOKEN))
check("buying a paid theme → 200", r.status_code == 200)
check("charged exactly the price", BALANCES["Rich"] == 7000.0)
check("the response reports the new balance", r.json()["new_balance"] == 7000.0)
check("the theme is owned", PAID_ID in r.json()["owned"])
check("ownership is stored under cosmetics.themes",
      MEMBERS["Rich"]["cosmetics"]["themes"] == [PAID_ID])
check("the existing cosmetics survived the unlock",
      MEMBERS["Rich"]["cosmetics"]["name_color"] == "#a855f7"
      and MEMBERS["Rich"]["cosmetics"]["status_text"] == "Kachow")
check("the charge is in the ledger, naming the theme",
      LEDGER[-1]["member"] == "Rich" and LEDGER[-1]["delta"] == -5000.0
      and "Lavender Rose" in LEDGER[-1]["reason"])

before = len(LEDGER)
r = c.post(f"/api/members/me/themes/{PAID_ID}", headers=bearer(RICH_TOKEN))
check("buying the same theme again → 200, flagged already owned",
      r.status_code == 200 and r.json()["already_owned"] is True)
check("...and is not charged a second time", BALANCES["Rich"] == 7000.0)
check("...and writes no second ledger row", len(LEDGER) == before)

r = c.post(f"/api/members/me/themes/{TEAM_ID}", headers=bearer(RICH_TOKEN))
check("a second, different theme is charged", BALANCES["Rich"] == 2000.0)
check("...and both are owned now",
      sorted(MEMBERS["Rich"]["cosmetics"]["themes"]) == sorted([PAID_ID, TEAM_ID]))


# ── your own team's theme is free ─────────────────────────────────────────────

print("\n-- your own team's theme --")

check("the current team's theme is free", themes.own_team_theme(MEMBERS["Owner"]) == OWN_TEAM_ID)
check("...and the team they left is not",
      themes.free_theme_ids(MEMBERS["Owner"]) == [OWN_TEAM_ID])
check("a member with no team gets nothing free", themes.free_theme_ids(MEMBERS["Rich"]) == [])
check("a closed tenure alone gets nothing free",
      themes.free_theme_ids({"tenures": [
          {"team": "ORL", "start": "2020-07-01", "end": "2022-07-25", "position": "owner"}]}) == [])
check("an open tenure with no position gets nothing free",
      themes.free_theme_ids({"tenures": [
          {"team": "ORL", "start": "2026-04-26", "end": None, "position": "none"}]}) == [])
check("a GM's team counts the same as an owner's",
      themes.free_theme_ids({"tenures": [
          {"team": "SAC", "start": "2026-04-26", "end": None, "position": "gm"}]}) == ["team-sac"])

before = len(LEDGER)
r = c.post(f"/api/members/me/themes/{OWN_TEAM_ID}", headers=bearer(OWNER_TOKEN))
check("buying your own team's theme → 400", r.status_code == 400)
check("...and nothing was charged", BALANCES["Owner"] == 12000.0)
check("...and nothing reached the ledger", len(LEDGER) == before)
check("...and it was not written into the owned list",
      "themes" not in MEMBERS["Owner"].get("cosmetics", {}))

r = c.post(f"/api/members/me/themes/{TEAM_ID}", headers=bearer(OWNER_TOKEN))
check("another team's theme is still charged in full",
      r.status_code == 200 and BALANCES["Owner"] == 7000.0)


# ── not enough NB¥ ────────────────────────────────────────────────────────────

print("\n-- not enough NB¥ --")

before = len(LEDGER)
r = c.post(f"/api/members/me/themes/{PAID_ID}", headers=bearer(POOR_TOKEN))
check("too poor → 402", r.status_code == 402)
check("the refusal names the price and the balance",
      "5,000" in r.json()["detail"] and "250" in r.json()["detail"])
check("nothing was charged", BALANCES["Poor"] == 250.0)
check("nothing was granted", "themes" not in MEMBERS["Poor"].get("cosmetics", {}))
check("nothing reached the ledger", len(LEDGER) == before)


# ── a failed grant refunds ────────────────────────────────────────────────────

print("\n-- a failed grant refunds --")


def _boom(_members):
    raise RuntimeError("disk full")


themes.save_members = _boom
before_balance = BALANCES["Poor"]
BALANCES["Poor"] = 5000.0
before = len(LEDGER)
try:
    c.post(f"/api/members/me/themes/{PAID_ID}", headers=bearer(POOR_TOKEN))
    raised = False
except RuntimeError:
    raised = True
check("the write failure propagates rather than being swallowed", raised)
check("the member was refunded", BALANCES["Poor"] == 5000.0)
check("the refund is in the ledger as its own row",
      len(LEDGER) == before + 1 and LEDGER[-1]["delta"] == 5000.0
      and "Refund" in LEDGER[-1]["reason"])
themes.save_members = auth.save_members
BALANCES["Poor"] = before_balance


print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_themes: ALL PASS")

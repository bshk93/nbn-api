"""A ratings update loaded into 2K (the DNA) flags the player's positions for
a streamer to check, since the load can overwrite what his team set.

- Ticking a change Updated on /ratings-changes opens a check on his team,
  rostered players only, and tells the next streamer once.
- The check shows on GET /api/position-checks/{team} as due, with the date.
- Mark entered closes it, recording whether 2K had been overwritten and with
  what. A check for a newer update than the one shown stays open.
- Unticking closes a check still open for that update.
- A newer tick re-opens a closed one; a tick for a traded player moves it.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_dna_position_check
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.coaching_settings as cs  # noqa: E402
import routers.game_day_notify as gdn  # noqa: E402
import routers.players as players  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


UPD_TOKEN, STREAM_TOKEN, BOS_TOKEN = "u" * 64, "s" * 64, "b" * 64
MEMBERS = {
    "Updater": {"token": UPD_TOKEN,    "roles": ["updates"],  "tenures": []},
    "Streamy": {"token": STREAM_TOKEN, "roles": ["streamer"], "tenures": []},
    "BosOwner": {"token": BOS_TOKEN,   "roles": ["bos"],      "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-dna-check-test-"))
players.RATINGS_UPDATED_FILE = TMP / "ratings-updated.json"
cs.POSITION_CHECKS_FILE = TMP / "position-checks.json"
cs.COACHING_SETTINGS_FILE = TMP / "coaching-settings.json"
players.log_write = cs.log_write = lambda info, msg: None
cs.league_today_str = lambda: "2026-11-11"

# Inbox messages go to stubs: the real ones read live members and inboxes.
NOTIFIED, RECEIPTS = [], []
gdn.dna_updated = lambda team, by: NOTIFIED.append(team)
gdn.positions_set = lambda team, by: RECEIPTS.append(team)
gdn.positions_changed = lambda *a, **k: None


def snap(date, ovr, pos):
    return {"date": date, "2k_ovr": ovr, "2k_pos": pos, "attributes": {}, "badges": []}


ATTRS = {
    "wing-guy": [snap("2026-11-01", 80, ["SF", "PF"]), snap("2026-11-05", 82, ["SF", "PF"]),
                 snap("2026-11-09", 83, ["SF", "PF"])],
    "big-man":  [snap("2026-11-01", 75, ["C"]), snap("2026-11-05", 77, ["C"])],
    "free-guy": [snap("2026-11-01", 70, ["PG"]), snap("2026-11-05", 71, ["PG"])],
}
BIOS = {
    "wing-guy": {"name": "GUY, WING", "position": {"primary": "SF", "secondary": "PF", "team": "BOS"},
                 "secondary_pos": "SG"},
    "big-man":  {"name": "MAN, BIG", "position": {"primary": "C", "secondary": None, "team": "BOS"}},
    "free-guy": {"name": "GUY, FREE"},
}
ROSTERS = {"BOS": ["wing-guy", "big-man"]}
players.load_attributes = lambda: ATTRS
players.load_player_bios = lambda: BIOS
players._build_team_map = lambda: {s: t for t, slugs in ROSTERS.items() for s in slugs}
cs._roster_slugs = lambda team: ROSTERS.get(team, [])

app = FastAPI()
app.include_router(players.router)
app.include_router(cs.router)
c = TestClient(app)
UPD = {"Authorization": "Bearer " + UPD_TOKEN}
STREAM = {"Authorization": "Bearer " + STREAM_TOKEN}


def tick(slug, date, on=True):
    return c.put("/api/ratings-changes/updated", json={"slug": slug, "date": date, "updated": on}, headers=UPD)


def items(team="BOS"):
    return {i["slug"]: i for i in c.get(f"/api/position-checks/{team}").json()["items"]}


def confirm(dna, team="BOS"):
    return c.post(f"/api/position-checks/{team}/confirm", json={"seen": {}, "dna": dna}, headers=STREAM)


def stored(team="BOS"):
    return cs.load_position_checks().get(team) or {}


print("dna position checks")
# The team's SG secondary was never entered, so wing-guy is already on the
# list for that. Confirm it first so the DNA check is the only thing listed.
c.post("/api/position-checks/BOS/confirm", json={"seen": {"wing-guy": "SG"}, "primaries": {"wing-guy": "SF"}},
       headers=STREAM)
check("nothing waiting to start", items() == {})
RECEIPTS.clear()

check("tick works", tick("wing-guy", "2026-11-05").status_code == 200)
it = items().get("wing-guy")
check("the tick flags his positions", it is not None and it["due"] and it["dna"]["date"] == "2026-11-05")
check("...showing the team's positions to check", it and it["primary"] == "SF" and it["secondary"] == "SG")
check("...and who ticked it", it and it["dna"]["by"] == "Updater")
check("the next streamer is told", NOTIFIED == ["BOS"])

tick("big-man", "2026-11-05")
check("a second player on a team already waiting tells no one again", NOTIFIED == ["BOS"])
check("both are listed", set(items()) == {"wing-guy", "big-man"})

tick("free-guy", "2026-11-05")
check("a free agent is not flagged", all("free-guy" not in (r.get("dna") or {})
                                         for r in cs.load_position_checks().values()))

tick("big-man", "2026-11-05", on=False)
check("unticking closes an open check", set(items()) == {"wing-guy"})

tick("wing-guy", "2026-11-09")
check("a newer update moves the check to its date", items()["wing-guy"]["dna"]["date"] == "2026-11-09")
check("confirming an older update than is open is refused quietly",
      confirm({"wing-guy": {"date": "2026-11-05"}}).status_code == 200 and "wing-guy" in items())

check("a made-up position is a 422",
      confirm({"wing-guy": {"date": "2026-11-09", "had": {"primary": "QB"}}}).status_code == 422)
r = confirm({"wing-guy": {"date": "2026-11-09", "had": {"primary": "PF", "secondary": "C"}}})
check("the streamer closes it", r.status_code == 200 and items() == {})
done = stored()["dna_done"]["wing-guy"]
check("...recording that 2K had been overwritten, and with what",
      done["overwritten"] and done["had"] == {"primary": "PF", "secondary": "C"} and done["checked_by"] == "Streamy")
check("the team gets its receipt", RECEIPTS == ["BOS"])

tick("wing-guy", "2026-11-05", on=False)
check("unticking an update already checked changes nothing", items() == {}
      and stored()["dna_done"]["wing-guy"]["date"] == "2026-11-09")

NOTIFIED.clear()
tick("wing-guy", "2026-11-05")
check("re-ticking an update older than the last check flags him again", "wing-guy" in items())
r = confirm({"wing-guy": {"date": "2026-11-05"}})
check("a check where 2K still matched", r.status_code == 200
      and stored()["dna_done"]["wing-guy"]["overwritten"] is False)

# He's traded to MIA, then ticked again: the check follows him.
tick("wing-guy", "2026-11-09")
ROSTERS["BOS"].remove("wing-guy")
ROSTERS["MIA"] = ["wing-guy"]
check("a traded player's old check isn't shown on the old team", items() == {})
tick("wing-guy", "2026-11-09", on=False)
tick("wing-guy", "2026-11-09")
check("the next tick opens it on his new team", "wing-guy" in items("MIA")
      and "wing-guy" not in (stored().get("dna") or {}))

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")

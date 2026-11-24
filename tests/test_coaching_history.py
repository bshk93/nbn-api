"""`routers/coaching_history.py` — every version of a team's coaching settings.

What it pins:

- **Each save, entry, note and clear adds one whole line**, with the exact
  values and minutes, so any past version can be shown as it was.
- **An entry's `at` is the record's `entered_at`**, the same stamp the games
  played on it carry, and its `version` is the save it entered.
- **Private like the settings.** The team's own role reads its own history;
  another team's role is a 403, no token a 401; streamers and admin read any.
- **A failed history write never fails the save.**
- **The seed writes each team once**, and only from what the records hold.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_coaching_history
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
import routers.coaching_history as ch  # noqa: E402
import routers.coaching_settings as cs  # noqa: E402
import routers.game_starters as gs  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


MEMBERS = {
    "WasOwner": {"token": "w" * 64, "roles": ["was"]},
    "BosOwner": {"token": "b" * 64, "roles": ["bos"]},
    "Admin":    {"token": "d" * 64, "roles": ["admin"]},
    "Streamy":  {"token": "s" * 64, "roles": ["streamer"]},
    "Nobody":   {"token": "n" * 64, "roles": []},
}
auth.load_members = lambda: MEMBERS
H = {name: {"Authorization": "Bearer " + m["token"]} for name, m in MEMBERS.items()}

TMP = Path(tempfile.mkdtemp(prefix="nbn-coaching-history-test-"))
cs.COACHING_SETTINGS_FILE = TMP / "coaching-settings.json"
cs.COACHING_NOTES_FILE = TMP / "coaching-notes.json"
cs.POSITION_CHECKS_FILE = TMP / "position-checks.json"
ch.COACHING_HISTORY_FILE = TMP / "coaching-history.jsonl"
gs.STARTERS_FILE = TMP / "game-starters.jsonl"
cs.log_write = lambda info, msg: None
cs._roster_slugs = lambda team: []
import routers.game_day_notify as gdn  # noqa: E402
for _fn in ("coaching_saved", "coaching_entered", "note_left", "note_cleared"):
    setattr(gdn, _fn, lambda *a, **k: None)
import routers.players as players  # noqa: E402
players._primary_pos = lambda slug, attrs=None, bio=None: None
players.load_player_bios = lambda: {}

app = FastAPI()
app.include_router(cs.router)
app.include_router(ch.router)
c = TestClient(app)


def hist(who="WasOwner", team="WAS"):
    return c.get(f"/api/coaching-history/{team}", headers=H[who])


def lines():
    p = ch.COACHING_HISTORY_FILE
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


V1 = {"active_system": "Pace & Space", "off_def_ratings": {"offense": 90, "defense": 70}}
M1 = {"PG": {"slug": "young-trae", "minutes": 34, "res": False}}
V2 = {"active_system": "Seven Seconds or Less", "off_def_ratings": {"offense": 85, "defense": 75}}
M2 = {"PG": {"slug": "young-trae", "minutes": 36, "res": False}}

print("recording")
check("no history reads as empty", hist().json()["events"] == [])
c.put("/api/coaching-settings/WAS", json={"values": V1, "minutes": M1}, headers=H["WasOwner"])
L = lines()
check("a save adds one line", len(L) == 1 and L[0]["kind"] == "saved")
check("the line holds the whole version", L[0]["values"] == V1 and L[0]["minutes"] == M1)
check("and who saved it, when", L[0]["by"] == "WasOwner" and L[0]["at"] == cs.load_coaching_settings()["WAS"]["updated_at"])

rec = c.post("/api/coaching-settings/WAS/enter", json={}, headers=H["Streamy"]).json()
L = lines()
check("entering adds an entered line", L[-1]["kind"] == "entered" and L[-1]["by"] == "Streamy")
check("its at is the record's entered_at", L[-1]["at"] == rec["entered_at"])
check("its version is the save it entered", L[-1]["version"] == L[0]["at"])
check("with what was entered", L[-1]["values"] == V1 and L[-1]["minutes"] == M1)

c.put("/api/coaching-settings/WAS", json={"values": V2, "minutes": M2}, headers=H["WasOwner"])
check("a second save is its own line; the first stays as it was",
      len(lines()) == 3 and lines()[0]["values"] == V1 and lines()[2]["values"] == V2)

c.put("/api/coaching-notes/WAS", json={"text": "Sarr plays through fouls"}, headers=H["WasOwner"])
check("a note is recorded", lines()[-1]["kind"] == "note" and lines()[-1]["text"] == "Sarr plays through fouls")
c.post("/api/coaching-notes/WAS/clear", json={}, headers=H["Streamy"])
check("clearing it is recorded", lines()[-1]["kind"] == "note_cleared" and lines()[-1]["by"] == "Streamy")
c.put("/api/coaching-notes/WAS", json={"text": ""}, headers=H["WasOwner"])
check("deleting it is recorded as an empty note", lines()[-1]["kind"] == "note" and lines()[-1]["text"] == "")
n = len(lines())
c.put("/api/coaching-notes/WAS", json={"text": ""}, headers=H["WasOwner"])
check("deleting a note that isn't there records nothing", len(lines()) == n)

print("reading")
check("no token is a 401", c.get("/api/coaching-history/WAS").status_code == 401)
check("another team is a 403", hist("BosOwner").status_code == 403)
check("a member with no role is a 403", hist("Nobody").status_code == 403)
check("unknown team is a 404", hist(team="XXX").status_code == 404)
r = hist()
check("the team reads its own", r.status_code == 200 and len(r.json()["events"]) == n)
check("newest first", [e["at"] for e in r.json()["events"]] == sorted((e["at"] for e in lines()), reverse=True))
check("a streamer reads any team", hist("Streamy").status_code == 200)
check("admin reads any team", hist("Admin").status_code == 200)
check("lowercase works", c.get("/api/coaching-history/was", headers=H["WasOwner"]).status_code == 200)
c.put("/api/coaching-settings/BOS", json={"values": {"x": 1}, "minutes": {}}, headers=H["BosOwner"])
check("one team's history never shows another's",
      all(e["team"] == "WAS" for e in hist().json()["events"]) and len(hist("BosOwner", "BOS").json()["events"]) == 1)
check("limit keeps the newest",
      [e["at"] for e in c.get("/api/coaching-history/WAS?limit=2", headers=H["WasOwner"]).json()["events"]]
      == [e["at"] for e in hist().json()["events"]][:2])

print("games")
gs.STARTERS_FILE.write_text(json.dumps({
    "date": "2026-11-02", "season": "26-27", "game_type": "REG", "home_team": "WAS", "away_team": "CHA",
    "teams": {"WAS": {"starters": ["young-trae"], "source": "entered", "entered_at": rec["entered_at"]},
              "CHA": {"starters": None, "source": None, "entered_at": None}}}) + "\n")
g = hist().json()["games"]
check("the team's games come back with the entry they were played on",
      len(g) == 1 and g[0]["entered_at"] == rec["entered_at"] and g[0]["opp"] == "CHA" and g[0]["home"] is True)

print("never breaks a save")
real = ch.COACHING_HISTORY_FILE
ch.COACHING_HISTORY_FILE = TMP / "no-such-dir" / "h.jsonl"
r = c.put("/api/coaching-settings/WAS", json={"values": V1, "minutes": M1}, headers=H["WasOwner"])
check("a save still succeeds when the history can't be written", r.status_code == 200)
ch.COACHING_HISTORY_FILE = real

print("seed")
import seed_coaching_history as seed  # noqa: E402
ch.COACHING_HISTORY_FILE = TMP / "seeded.jsonl"
settings = {
    "ATL": {"values": V2, "minutes": M2, "updated_at": "2026-11-10T00:00:00+00:00", "updated_by": "A", "pending": True,
            "entered_at": "2026-11-05T00:00:00+00:00", "entered_by": "S",
            "entered_values": V1, "entered_minutes": M1},
    "BOS": {"values": V1, "minutes": M1, "updated_at": "2026-11-01T00:00:00+00:00", "updated_by": "B", "pending": False,
            "entered_at": "2026-11-02T00:00:00+00:00", "entered_by": "S"},
}
notes = {"ATL": {"text": "hi", "updated_at": "2026-11-11T00:00:00+00:00", "updated_by": "A",
                 "cleared_at": "2026-11-12T00:00:00+00:00", "cleared_by": "S"}}
seed.load_coaching_settings = lambda: settings
seed.load_coaching_notes = lambda: notes
sys.argv = ["seed"]
seed.main()
S = lines()
atl = [e for e in S if e["team"] == "ATL"]
check("a pending team gets its entered and its saved version",
      [e["kind"] for e in atl] == ["entered", "saved", "note", "note_cleared"]
      and atl[0]["values"] == V1 and atl[1]["values"] == V2)
bos = [e for e in S if e["team"] == "BOS"]
check("an entered team gets one entered line, from its current settings",
      [e["kind"] for e in bos] == ["entered"] and bos[0]["values"] == V1 and bos[0]["version"] == "2026-11-01T00:00:00+00:00")
check("every seeded line says so", all(e.get("seeded") for e in S))
seed.main()
check("running it again writes nothing", len(lines()) == len(S))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

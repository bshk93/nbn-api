"""Game-day inbox messages — routers/game_day_notify.py, driven through the real
routes that call it (coaching settings, streaming days, screenshot uploads).

Pinned: who each handoff reaches, and the two rules that keep it quiet — a
coaching save notifies only on the change to pending, and "ready to parse"
goes once per date.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_game_day_notify
"""
from __future__ import annotations

import io
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.inbox as inbox  # noqa: E402
import routers.schedule as schedule  # noqa: E402
import routers.game_day_notify as gdn  # noqa: E402
import routers.coaching_settings as cs  # noqa: E402
import routers.streaming_days as sd  # noqa: E402
import routers.boxscores as bx  # noqa: E402
import routers.boxscore_shots as shots  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── world ─────────────────────────────────────────────────────────────────────

TOK = {n: (n[0] * 64) for n in ("phxowner", "streamy", "kim", "otherstreamer")}
MEMBERS = {
    "phxowner":      {"token": TOK["phxowner"], "roles": ["phx"], "tenures": []},
    "streamy":       {"token": TOK["streamy"], "roles": ["streamer"], "tenures": []},
    "kim":           {"token": TOK["kim"], "roles": ["admin", "stats"], "tenures": []},
    "otherstreamer": {"token": TOK["otherstreamer"], "roles": ["streamer"], "tenures": []},
}
H = {n: {"Authorization": "Bearer " + t} for n, t in TOK.items()}
auth.load_members = lambda: MEMBERS

SENT = []  # (kind, to, text)
inbox.notify_member = lambda to, text, link=None: SENT.append(("member", to, text))
inbox.notify_role = lambda role, text, link=None: SENT.append(("role", role, text))
inbox.notify_team = lambda team, text, link=None: SENT.append(("team", team, text))

TODAY = date(2026, 10, 24)
gdn.league_today = lambda: TODAY
gdn._current_league_year = lambda: "26-27"

GAMES = [
    {"id": "g1", "date": "2026-10-23", "home_team": "PHX", "away_team": "LAL"},
    {"id": "g2", "date": "2026-10-23", "home_team": "NYK", "away_team": "BOS"},
    {"id": "g3", "date": "2026-10-25", "home_team": "PHX", "away_team": "DEN", "streamer": "streamy"},
    {"id": "g4", "date": "2026-10-24", "home_team": "MIA", "away_team": "ORL"},
    {"id": "g5", "date": "2026-10-30", "home_team": "UTA", "away_team": "SAC"},
]
schedule._load = lambda season: {"season": season, "games": GAMES}

TMP = Path(tempfile.mkdtemp(prefix="nbn-gameday-test-"))
cs.COACHING_SETTINGS_FILE = TMP / "coaching.json"
sd.STREAMING_DAYS_FILE = TMP / "streaming-days.json"
cs.log_write = sd.log_write = lambda *a, **k: None
bx.DATA_DIR = TMP
bx.PENDING_BOXSCORES_DIR = shots.PENDING_BOXSCORES_DIR = TMP / "pending-boxscores"
shots.KEPT_BOXSCORES_DIR = TMP / "boxscore-screenshots"
bx.MANUAL_QUEUE_FILE = TMP / "q.json"
bx._award_amount = lambda *a: (0.0, 0.0)
bx._award_submission_reward = lambda *a: (0.0, 0.0)

app = FastAPI()
for r in (cs.router, sd.router, bx.router):
    app.include_router(r)
c = TestClient(app)


def last(n=1):
    return SENT[-n:]


# ── 1. coaching saved ─────────────────────────────────────────────────────────

print("a team saves coaching settings")
SENT.clear()
c.put("/api/coaching-settings/PHX", json={"values": {"a": 1}}, headers=H["phxowner"])
check("the claimed streamer of the team's next game hears about it",
      SENT == [("member", "streamy", SENT[0][2])] if SENT else False, SENT)
check("the message names the game", SENT and "Oct 25" in SENT[0][2] and "DEN" in SENT[0][2])
c.put("/api/coaching-settings/PHX", json={"values": {"a": 2}}, headers=H["phxowner"])
check("a second save while still pending sends nothing", len(SENT) == 1)

print("the streamer enters them")
c.post("/api/coaching-settings/PHX/enter", json={}, headers=H["streamy"])
check("the team gets a receipt", last() == [("team", "PHX", last()[0][2])] and "streamy" in last()[0][2])
c.post("/api/coaching-settings/PHX/enter", json={}, headers=H["streamy"])
check("entering again sends no second receipt", len(SENT) == 2)
c.put("/api/coaching-settings/PHX", json={"values": {"a": 3}}, headers=H["phxowner"])
check("a save after entry notifies again", len(SENT) == 3 and SENT[-1][1] == "streamy")

print("an unclaimed game")
SENT.clear()
MEMBERS["miaowner"] = {"token": "m" * 64, "roles": ["mia"], "tenures": []}
MEMBERS["utaowner"] = {"token": "u" * 64, "roles": ["uta"], "tenures": []}
c.put("/api/coaching-settings/MIA", json={"values": {}}, headers={"Authorization": "Bearer " + "m" * 64})
check("today's unclaimed game pings every streamer", SENT and SENT[0][:2] == ("role", "streamer"))
check("and says nobody has claimed it", SENT and "no streamer" in SENT[0][2])
SENT.clear()
c.put("/api/coaching-settings/UTA", json={"values": {}}, headers={"Authorization": "Bearer " + "u" * 64})
check("an unclaimed game a week out pings nobody", SENT == [])

# ── 3. day done ───────────────────────────────────────────────────────────────

print("a streamer marks a day done")
SENT.clear()
c.post("/api/streaming-days/2026-10-23/done", headers=H["streamy"])
check("Stats hears about it", SENT and SENT[0][:2] == ("role", "stats"))
check("with the game count", SENT and "2 games" in SENT[0][2])
c.post("/api/streaming-days/2026-10-23/done", headers=H["streamy"])
check("marking it done again sends nothing", len(SENT) == 1)

# ── 4. ready to parse ─────────────────────────────────────────────────────────

print("screenshots come in")


def png():
    b = io.BytesIO()
    Image.new("RGB", (8, 8)).save(b, "PNG")
    return b.getvalue()


def up(team, home, away):
    return c.post("/api/boxscore/upload/side", headers=H["kim"],
                  data={"team": team, "date": "2026-10-23", "home_team": home, "away_team": away,
                        "season": "26-27", "game_type": "REG"},
                  files={"image": ("s.png", png(), "image/png")})


SENT.clear()
up("PHX", "PHX", "LAL"); up("LAL", "PHX", "LAL")
check("one of two games ready: nothing yet", SENT == [])
up("BOS", "NYK", "BOS")
check("half of the last game: nothing yet", SENT == [])
up("NYK", "NYK", "BOS")
check("the whole day ready: the parser hears", SENT and SENT[0][:2] == ("role", "admin"), SENT)
check("naming the day and count", SENT and "Fri Oct 23" in SENT[0][2] and "2 games" in SENT[0][2])
up("NYK", "NYK", "BOS")
check("a further upload that day sends nothing", len(SENT) == 1)

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_game_day_notify: all pass")

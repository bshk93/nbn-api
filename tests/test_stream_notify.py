"""`POST /api/twitch/eventsub` — the #leaguetalk go-live post.

What it promises: an unsigned, mis-signed or stale request is a 403; the
verification challenge is echoed; a `stream.online` posts a link to nbn.today
once, pinging both teams of each stream game on the day being streamed and no
one on a day of box scores; a redelivery, a restart inside the gap, an old
stream or a rerun posts nothing. Nothing reaches Discord: the transport's
enqueue is swapped out, and state goes to a temp dir.

    venv/bin/python -m tests.test_stream_notify
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.boxscores as bx  # noqa: E402
import routers.discord_transport as transport  # noqa: E402
import routers.game_day_notify as gdn  # noqa: E402
import routers.schedule as schedule  # noqa: E402
import routers.stream_notify as sn  # noqa: E402
import routers.streaming_days as sd  # noqa: E402
from routers.discord_notify import TEAM_ROLE_IDS  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── world ─────────────────────────────────────────────────────────────────────

SECRET = "s3cret-for-tests"
os.environ["TWITCH_EVENTSUB_SECRET"] = SECRET
sn.STATE_FILE = Path(tempfile.mkdtemp()) / "stream-announce.json"
sn.DISCORD_LEAGUETALK_CHANNEL = "111"
transport.DISCORD_BOT_TOKEN = "bot"
SENT = []
transport._enqueue = lambda msg: SENT.append(msg)

TODAY = date(2026, 10, 24)
sn.league_today = lambda: TODAY
sn._current_league_year = lambda: "26-27"
GAMES = []
schedule._load = lambda season: {"season": season, "games": GAMES}
DONE = {}
sd.load_streaming_days = lambda: DONE
BOXED = set()
gdn._boxed_games = lambda season: BOXED
bx._game_in_pending_screenshots = lambda d, h, a: False

app = FastAPI()
app.include_router(sn.router)
client = TestClient(app)

N = [0]


def post(kind, payload, *, secret=SECRET, ts=None, msg_id=None):
    N[0] += 1
    msg_id = msg_id or f"msg-{N[0]}"
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), msg_id.encode() + ts.encode() + body, hashlib.sha256).hexdigest()
    return client.post("/api/twitch/eventsub", content=body, headers={
        "Twitch-Eventsub-Message-Id": msg_id, "Twitch-Eventsub-Message-Timestamp": ts,
        "Twitch-Eventsub-Message-Signature": sig, "Twitch-Eventsub-Message-Type": kind,
        "Content-Type": "application/json"})


def online(started=None, kind="live"):
    started = started or datetime.now(timezone.utc) - timedelta(seconds=5)
    return {"subscription": {"type": "stream.online"},
            "event": {"type": kind, "broadcaster_user_login": "nothingbutnet",
                      "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ")}}


def reset():
    SENT.clear()
    sn._seen_ids.clear()
    sn.STATE_FILE.unlink(missing_ok=True)
    transport._recent_sends.clear()


def game(d, away, home, stream=False):
    g = {"id": f"{d}-{away}-{home}", "date": d, "away_team": away, "home_team": home}
    if stream:
        g["stream"] = True
    return g


# ── signature ─────────────────────────────────────────────────────────────────

print("signature")
reset()
check("wrong secret is 403", post("notification", online(), secret="nope").status_code == 403)
r = client.post("/api/twitch/eventsub", content=b"{}")
check("no headers is 403", r.status_code == 403)
old = (datetime.now(timezone.utc) - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
check("an 11-minute-old message is 403", post("notification", online(), ts=old).status_code == 403)
saved = os.environ.pop("TWITCH_EVENTSUB_SECRET")
check("no secret configured is 403", post("notification", online()).status_code == 403)
os.environ["TWITCH_EVENTSUB_SECRET"] = saved
check("nothing posted", SENT == [])

print("challenge")
r = post("webhook_callback_verification", {"challenge": "abc123", "subscription": {"type": "stream.online"}})
check("echoes the challenge as plain text", r.status_code == 200 and r.text == "abc123", r.text)
check("revocation is acknowledged", post("revocation", {"subscription": {"type": "stream.online",
                                                                         "status": "authorization_revoked"}}).status_code == 204)

# ── the post ──────────────────────────────────────────────────────────────────

print("a game day with a stream game")
reset()
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True), game("2026-10-24", "BOS", "MIA"),
            game("2026-10-25", "LAL", "GSW", stream=True)]
r = post("notification", online())
check("204", r.status_code == 204)
check("one post", len(SENT) == 1)
p = SENT[0]["payload"] if SENT else {}
check("to #leaguetalk", SENT and SENT[0]["channel"] == "111")
check("reads NBN is live, linking nbn.today",
      p.get("content", "").startswith("🔴 NBN is live: https://nbn.today"), p)
check("pings both teams of the stream game",
      sorted(p.get("allowed_mentions", {}).get("roles", [])) == sorted([TEAM_ROLE_IDS["CLE"], TEAM_ROLE_IDS["SAC"]]), p)
check("no @everyone", p.get("allowed_mentions", {}).get("parse") == [])

print("redelivery and restarts")
SENT.clear()
post("notification", online())
check("a restart inside the gap posts nothing", SENT == [])


def expire_gap():
    state = json.loads(sn.STATE_FILE.read_text())
    state["posted_at"] = (datetime.now(timezone.utc) - timedelta(hours=sn.RESTART_GAP_HOURS + 1)).isoformat()
    sn.STATE_FILE.write_text(json.dumps(state))


expire_gap()
post("notification", online(), msg_id="dup")
check("the next day's stream posts", len(SENT) == 1)
expire_gap()
post("notification", online(), msg_id="dup")
check("a redelivered message id posts nothing, even past the gap", len(SENT) == 1)

print("box scores, behind schedule")
reset()
GAMES[:] = [game("2026-10-22", "NYK", "PHI"), game("2026-10-22", "DEN", "UTA"),
            game("2026-10-23", "OKC", "HOU", stream=True), game("2026-10-24", "CLE", "SAC", stream=True)]
post("notification", online())
p = SENT[0]["payload"] if SENT else {}
check("the oldest unplayed day has no stream game: pings no one",
      "roles" not in p.get("allowed_mentions", {}) and "<@&" not in p.get("content", ""), p)
check("reads Box Scores Live, linking nbn.today",
      p.get("content") == "🔴 Box Scores Live: https://nbn.today", p)

reset()
DONE["2026-10-22"] = {"done": True}
post("notification", online())
p = SENT[0]["payload"] if SENT else {}
check("once that day is done, the next day's stream game is pinged",
      p.get("allowed_mentions", {}).get("roles") == [TEAM_ROLE_IDS["OKC"], TEAM_ROLE_IDS["HOU"]], p)
DONE.clear()

reset()
GAMES[:] = []
post("notification", online())
check("an empty schedule is a box-score day",
      len(SENT) == 1 and SENT[0]["payload"]["content"].startswith("🔴 Box Scores Live"))

reset()
real_load = schedule._load
schedule._load = lambda season: (_ for _ in ()).throw(FileNotFoundError(season))
post("notification", online())
check("an unreadable schedule still posts, as NBN is live with no pings",
      len(SENT) == 1 and SENT[0]["payload"]["content"] == "🔴 NBN is live: https://nbn.today", SENT)
schedule._load = real_load

print("not worth announcing")
reset()
post("notification", online(kind="rerun"))
check("a rerun posts nothing", SENT == [])
post("notification", online(started=datetime.now(timezone.utc) - timedelta(minutes=20)))
check("a stream that began 20 minutes ago posts nothing", SENT == [])
post("notification", {"subscription": {"type": "channel.update"}, "event": {}})
check("another subscription type posts nothing", SENT == [])
sn.DISCORD_LEAGUETALK_CHANNEL = ""
post("notification", online())
check("no channel configured posts nothing", SENT == [])
check("and leaves the restart gap unstamped", not sn.STATE_FILE.exists())
sn.DISCORD_LEAGUETALK_CHANNEL = "111"

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

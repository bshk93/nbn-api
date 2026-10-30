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
saved_emoji, sn.LIVE_EMOJI = sn.LIVE_EMOJI, "🔴"   # the checks below read 🔴
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
RECORDS = {"CLE": "5-2", "SAC": "3-4", "LAL": "6-1"}
real_records = sn.team_records
sn.team_records = lambda: RECORDS
# No player line unless a section asks for one, so the matchup checks stay exact.
real_player = sn.random_player
sn.random_player = lambda team: None
real_front_office = sn.random_front_office
sn.random_front_office = lambda team: None
# Twitch is never asked: the title is whatever a section sets.
TITLE = ["NBN Game Night"]
real_fetch_title = sn.fetch_title
sn.fetch_title = lambda broadcaster_id: TITLE[0]

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


def game(d, away, home, stream=False, time_et=""):
    g = {"id": f"{d}-{away}-{home}", "date": d, "away_team": away, "home_team": home, "time_et": time_et}
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
      p.get("content", "").startswith("🔴 NBN is [live](<https://nbn.today>)"), p)
check("pings both teams of the stream game",
      sorted(p.get("allowed_mentions", {}).get("roles", [])) == sorted([TEAM_ROLE_IDS["CLE"], TEAM_ROLE_IDS["SAC"]]), p)
check("no @everyone", p.get("allowed_mentions", {}).get("parse") == [])
check("the matchup line is away at home, each tag with its record",
      p.get("content", "").split("\n")[1:] ==
      [f"<@&{TEAM_ROLE_IDS['CLE']}> (5-2) at <@&{TEAM_ROLE_IDS['SAC']}> (3-4)"], p)

print("two stream games, records missing")
reset()
GAMES[:] = [game("2026-10-24", "BOS", "MIA", stream=True, time_et="10:00p"),
            game("2026-10-24", "CLE", "SAC", stream=True, time_et="7:30p")]
post("notification", online())
lines = SENT[0]["payload"]["content"].split("\n") if SENT else []
check("one line per stream game, in tip-off order",
      lines[1:] == [f"<@&{TEAM_ROLE_IDS['CLE']}> (5-2) at <@&{TEAM_ROLE_IDS['SAC']}> (3-4)",
                    f"<@&{TEAM_ROLE_IDS['BOS']}> (0-0) at <@&{TEAM_ROLE_IDS['MIA']}> (0-0)"], lines)
check("all four teams pinged", len(SENT[0]["payload"]["allowed_mentions"]["roles"]) == 4 if SENT else False)

reset()
sn.team_records = lambda: (_ for _ in ()).throw(FileNotFoundError("standings"))
post("notification", online())
lines = SENT[0]["payload"]["content"].split("\n") if SENT else []
check("unreadable standings: the matchup posts without records",
      lines[1:] == [f"<@&{TEAM_ROLE_IDS['CLE']}> at <@&{TEAM_ROLE_IDS['SAC']}>",
                    f"<@&{TEAM_ROLE_IDS['BOS']}> at <@&{TEAM_ROLE_IDS['MIA']}>"], lines)
sn.team_records = lambda: RECORDS
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True), game("2026-10-24", "BOS", "MIA"),
            game("2026-10-25", "LAL", "GSW", stream=True)]

print("a random-player line under each matchup")
STARS = {"CLE": "Darius Garland", "SAC": "Domantas Sabonis", "BOS": "Jayson Tatum", "MIA": "Bam Adebayo"}
sn.random_player = lambda team: STARS.get(team)
reset()
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True, time_et="7:30p"),
            game("2026-10-24", "BOS", "MIA", stream=True, time_et="10:00p")]
post("notification", online())
lines = SENT[0]["payload"]["content"].split("\n") if SENT else []
first = next((b for b in sn.BLURBS if b.format(away="Darius Garland", home="Domantas Sabonis") == lines[2]), None) \
    if len(lines) > 2 else None
second = next((b for b in sn.BLURBS if b.format(away="Jayson Tatum", home="Bam Adebayo") == lines[4]), None) \
    if len(lines) > 4 else None
check("each matchup gets a line naming a player from each roster, away then home",
      first is not None and second is not None, lines)
check("two games on one post never share a line", first != second)
check("the first line's template is remembered", json.loads(sn.STATE_FILE.read_text()).get("blurb") == first)

repeats = 0
for _ in range(40):
    lines_out, template = sn.pick_blurbs([("CLE", "SAC")], last=sn.BLURBS[0])
    repeats += template == sn.BLURBS[0]
check("never the same line as the last post", repeats == 0)

sn.random_player = lambda team: None if team == "MIA" else STARS.get(team)
lines_out, _ = sn.pick_blurbs([("CLE", "SAC"), ("BOS", "MIA")], last=None)
check("a side with no players drops only its game's line", lines_out[0] and lines_out[1] is None, lines_out)
sn.random_player = lambda team: (_ for _ in ()).throw(RuntimeError("roster gone"))
lines_out, _ = sn.pick_blurbs([("CLE", "SAC")], last=None)
check("a roster that won't read drops the line, not the post", lines_out == [None])
sn.random_player = lambda team: None

print("random_player off the whole roster")
import routers.discord as discord  # noqa: E402
real_aug = discord._roster_augmented
discord._roster_augmented = lambda team: [
    {"name_raw": "Garland, Darius", "type": "player", "hold_type": None},
    {"name_raw": "Deep, Bench", "type": "player", "hold_type": "NON_GTD"},
    {"name_raw": "Way, Two", "type": "two-way", "hold_type": None},
    {"name_raw": "Rights, Draft", "type": "draft-rights", "hold_type": None},
    {"name_raw": "Gone, Dead", "type": "dead", "hold_type": None},
    {"name_raw": "Hold, Unsigned", "type": "player", "hold_type": "UFA"},
]
picked = {real_player("CLE") for _ in range(200)}
check("anyone under contract, bench and two-way included, by display name",
      picked == {"Darius Garland", "Bench Deep", "Two Way"}, picked)
discord._roster_augmented = lambda team: []
check("an empty roster is None", real_player("CLE") is None)
discord._roster_augmented = real_aug

print("box scores: one game, a front-office line")
TITLE[0] = "Box Scores"
FRONT = {"NYK": "Kim", "PHI": "Darth Awn", "DEN": "jo_bo", "UTA": "Tee"}
sn.random_front_office = lambda team: FRONT.get(team)
reset()
GAMES[:] = [game("2026-10-24", "NYK", "PHI"), game("2026-10-24", "DEN", "UTA"),
            game("2026-10-25", "CLE", "SAC", stream=True)]
seen_games = set()
for _ in range(30):
    reset()
    post("notification", online())
    p = SENT[0]["payload"]
    lines = p["content"].split("\n")
    seen_games.add(lines[1])
check("only ever one game, picked from that night", len(lines) == 3 and len(seen_games) == 2, seen_games)
fills = {f"<@&{TEAM_ROLE_IDS['NYK']}> (0-0) at <@&{TEAM_ROLE_IDS['PHI']}> (0-0)": ("Kim", "Darth Awn"),
         f"<@&{TEAM_ROLE_IDS['DEN']}> (0-0) at <@&{TEAM_ROLE_IDS['UTA']}> (0-0)": ("jo_bo", "Tee")}
away, home = fills[lines[1]]
box = next((b for b in sn.BOX_BLURBS if b.format(away=away, home=home) == lines[2]), None)
check("the line is a box-score one, naming each side's front office", box is not None, lines)
check("still pings no one", p["allowed_mentions"] == {"parse": []})
state = json.loads(sn.STATE_FILE.read_text())
check("remembered as the box-score opener", state.get("box_blurb") == box and state.get("kind") == "box", state)
check("no front office on a side drops only the line",
      sn.pick_blurbs([("NYK", "BOS")], None, "box")[0] == [None])
sn.random_front_office = lambda team: None

TITLE[0] = "NBN Game Night"

print("random_front_office off members.json")
import routers.auth as auth  # noqa: E402
real_members = auth.load_members
auth.load_members = lambda: {
    "Kim":     {"tenures": [{"team": "nyk", "position": "gm", "start": "2024-01-01", "end": None}]},
    "Old GM":  {"tenures": [{"team": "NYK", "position": "gm", "start": "2020-01-01", "end": "2023-01-01"}]},
    "Fan":     {"tenures": [{"team": "NYK", "position": "none", "start": "2024-01-01"}]},
    "jo_bo":   {"tenures": [{"team": "NYK", "position": "co-owner", "start": "2025-01-01"}]},
    "Elsewhere": {"tenures": [{"team": "PHI", "position": "gm", "start": "2025-01-01"}]},
}
picked = {real_front_office("NYK") for _ in range(100)}
check("current front office only, names escaped for Discord", picked == {"Kim", "jo\\_bo"}, picked)
check("a team with nobody is None", real_front_office("BOS") is None)
auth.load_members = real_members

print("the server's own emoji")
check("defaults to :minionannouncement:, as a bot has to send it",
      saved_emoji == "<:minionannouncement:1564839468823744572>", saved_emoji)
sn.LIVE_EMOJI = "<:other:1234567890>"
check("an override opens the post",
      sn.build_payload("box", [])["content"] == "<:other:1234567890> Box Scores [live](<https://nbn.today>)")
sn.LIVE_EMOJI = "🔴"

print("team_records off the standings file")
tmp = Path(tempfile.mkdtemp()) / "standings-history.csv"
tmp.write_text("SEASON,TEAM,W,L\n25-26,CLE,50,32\n26-27,CLE,5,2\n26-27,SAC,3,4\n")
import routers.home as home  # noqa: E402
real_csv, home.STANDINGS_CSV = home.STANDINGS_CSV, tmp
check("this season's W-L only", real_records() == {"CLE": "5-2", "SAC": "3-4"}, real_records())
home.STANDINGS_CSV = real_csv

print("redelivery and restarts")
reset()
post("notification", online())
check("first stream posts", len(SENT) == 1)
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

print("offline, then back: restart or the box-score stream")
OFFLINE = {"subscription": {"type": "stream.offline"}, "event": {"broadcaster_user_login": "nothingbutnet"}}


def offline_ago(minutes):
    """The last post went out an hour before the stream went offline, `minutes` ago."""
    state = json.loads(sn.STATE_FILE.read_text())
    state["offline_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    state["posted_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes + 60)).isoformat()
    sn.STATE_FILE.write_text(json.dumps(state))


reset()
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True), game("2026-10-24", "BOS", "MIA"),
            game("2026-10-24", "NYK", "PHI")]
post("notification", online())
check("the game stream posts", len(SENT) == 1 and "NBN is" in SENT[0]["payload"]["content"])
r = post("notification", OFFLINE)
check("stream.offline is acknowledged and stamped",
      r.status_code == 204 and json.loads(sn.STATE_FILE.read_text()).get("offline_at"))
SENT.clear()
post("notification", online())
check("back within the grace: a restart, silent", SENT == [])

offline_ago(sn.RESTART_GRACE_MINUTES + 10)
TITLE[0] = "Box Scores!"
post("notification", online())
p = SENT[0]["payload"] if SENT else {}
lines = p.get("content", "").split("\n")
check("a box-score title later that night posts, as the box scores", lines[0].endswith("Box Scores [live](<https://nbn.today>)"), lines)
check("its game is one of the night's non-stream games",
      len(lines) > 1 and f"<@&{TEAM_ROLE_IDS['CLE']}>" not in lines[1], lines)
check("and pings no one", p.get("allowed_mentions") == {"parse": []}, p)

SENT.clear()
offline_ago(sn.RESTART_GRACE_MINUTES + 10)
post("notification", online())
check("a third stream that night is box scores again, not the stream games",
      SENT and "Box Scores" in SENT[0]["payload"]["content"].split("\n")[0])

print("box scores right after the game stream")
reset()
TITLE[0] = "NBN Game Night"
post("notification", online())
check("the game stream posts", len(SENT) == 1)
offline_ago(2)
TITLE[0] = "NBN box scores"
SENT.clear()
post("notification", online())
check("two minutes later with a box-score title: posts at once, not a restart",
      SENT and "Box Scores" in SENT[0]["payload"]["content"].split("\n")[0], SENT)
offline_ago(3)
SENT.clear()
post("notification", online())
check("the box-score stream dropping and coming back is a restart", SENT == [])

print("no title from Twitch: the night's second stream is box scores")
reset()
TITLE[0] = None
post("notification", online())
check("first stream of the night: the stream games", SENT and "NBN is" in SENT[0]["payload"]["content"])
offline_ago(5)
SENT.clear()
post("notification", online())
check("next stream that night: box scores", SENT and "Box Scores" in SENT[0]["payload"]["content"].split("\n")[0])
TITLE[0] = "NBN Game Night"

print("box scores first, then the game stream")
reset()
DONE.clear()
state = {"posted_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
         "offline_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
         "kind": "box", "day": "2026-10-24"}
sn.STATE_FILE.write_text(json.dumps(state))
post("notification", online())
check("the stream games still get their post",
      SENT and SENT[0]["payload"]["content"].split("\n")[0].endswith("NBN is [live](<https://nbn.today>)"), SENT)

print("plan_post")
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True)]
kind, games, day = sn.plan_post({"stream_day": "2026-10-24"})
check("no title, a night of only stream games already posted: box scores of those",
      kind == "box" and games == [("CLE", "SAC")])
kind, _, _ = sn.plan_post({"stream_day": "2026-10-23"})
check("a stream post from another night doesn't count", kind == "stream")
check("a box-score title wins, even on a night with no stream post yet",
      sn.plan_post({}, "Box Score Night")[0] == "box")
check("any other title is the stream, even after the night's stream post",
      sn.plan_post({"stream_day": "2026-10-24"}, "NBN Game Night")[0] == "stream")
check("'boxscores' and 'BOX-SCORES' both count",
      all(sn.BOX_TITLE_RE.search(t) for t in ("boxscores", "BOX-SCORES", "box score")))
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True), game("2026-10-24", "BOS", "MIA"),
            game("2026-10-25", "LAL", "GSW", stream=True)]

print("fetch_title against a mocked Twitch")
import httpx  # noqa: E402
import routers.stream_live as sl  # noqa: E402
saved_env = {k: os.environ.get(k) for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")}
os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"] = "cid", "secret"
sl._token.update(value=None, expires=0.0)
RealClient = httpx.Client


def twitch(handler):
    sn.httpx.Client = lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw)


def ok(req):
    if req.url.host == "id.twitch.tv":
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    check("asks /channels for the broadcaster", req.url.path == "/helix/channels"
          and req.url.params.get("broadcaster_id") == "156572449")
    return httpx.Response(200, json={"data": [{"title": "Box Scores 10/24"}]})


twitch(ok)
check("reads the title", real_fetch_title("156572449") == "Box Scores 10/24")
twitch(lambda req: httpx.Response(500))
sl._token.update(value=None, expires=0.0)
check("a Twitch error is None", real_fetch_title("156572449") is None)
check("no broadcaster id is None", real_fetch_title("") is None)
sn.httpx.Client = RealClient
for k, v in saved_env.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

print("the night is the date the stream started (10-28 / 10-29, as it happened)")
check("7 PM EDT is that night", sn.night_of(datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc)) == "2026-10-29")
check("12:40 AM is still the night before",
      sn.night_of(datetime(2026, 10, 30, 4, 40, tzinfo=timezone.utc)) == "2026-10-29")
check("7 AM is the new day", sn.night_of(datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc)) == "2026-10-29")

# 10-28 was never marked done and still had games without box scores, and
# 10-29 was marked done before its stream. The old earliest-unplayed rule
# called the 10-29 stream 10-28's box scores.
sn.league_today = lambda: date(2026, 10, 29)
real_night_of = sn.night_of
sn.night_of = lambda started: "2026-10-29"
GAMES[:] = [game("2026-10-28", "CHA", "DET", stream=True), game("2026-10-28", "MIA", "ATL"),
            game("2026-10-29", "CLE", "ATL", time_et="7:00p"), game("2026-10-29", "MEM", "POR", stream=True, time_et="10:00p"),
            game("2026-10-29", "SAC", "NOP", time_et="8:00p")]
BOXED.add(("2026-10-28", bx._game_teams("DET", "CHA")))
DONE["2026-10-29"] = {"done": True}
reset()
TITLE[0] = "NBN Game Night"
post("notification", online())
p = SENT[0]["payload"] if SENT else {}
check("the 10-29 stream is that night's stream game, not 10-28's box scores",
      p.get("content", "").split("\n")[:2] ==
      [f"{sn.LIVE_EMOJI} NBN is [live](<https://nbn.today>)",
       f"<@&{TEAM_ROLE_IDS['MEM']}> (0-0) at <@&{TEAM_ROLE_IDS['POR']}> (0-0)"], p)
check("and pings MEM and POR", sorted(p.get("allowed_mentions", {}).get("roles", [])) ==
      sorted([TEAM_ROLE_IDS["MEM"], TEAM_ROLE_IDS["POR"]]), p)
offline_ago(2)
TITLE[0] = "Box Scores"
SENT.clear()
post("notification", online())
lines = SENT[0]["payload"]["content"].split("\n") if SENT else []
import re  # noqa: E402
matchup = re.sub(r" \(\d+-\d+\)", "", lines[1]) if len(lines) > 1 else ""
check("the box scores right after are 10-29's, from a non-stream game",
      matchup in (f"<@&{TEAM_ROLE_IDS['CLE']}> at <@&{TEAM_ROLE_IDS['ATL']}>",
                  f"<@&{TEAM_ROLE_IDS['SAC']}> at <@&{TEAM_ROLE_IDS['NOP']}>"), lines)

print("a night with no games falls back to the earliest unplayed day")
sn.night_of = lambda started: "2026-10-30"
kind, games, day = sn.plan_post({}, "Box Scores", "2026-10-30")
check("catch-up box scores: 10-28's unplayed game", kind == "box" and day == "2026-10-28"
      and games == [("MIA", "ATL")], (kind, games, day))
kind, games, _ = sn.plan_post({}, "NBN Game Night", "2026-10-30")
check("catch-up under a game-night title: NBN is live, no stream game to name", kind == "stream" and games == [])
sn.night_of = real_night_of
sn.league_today = lambda: TODAY
BOXED.clear()
DONE.clear()
TITLE[0] = "NBN Game Night"
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True), game("2026-10-24", "BOS", "MIA"),
            game("2026-10-25", "LAL", "GSW", stream=True)]

print("is_restart without an offline notice")
now = datetime.now(timezone.utc)
check("no post yet is never a restart", not sn.is_restart({}, now))
check("an offline from before the last post is ignored; the 12h fallback rules",
      sn.is_restart({"posted_at": (now - timedelta(hours=3)).isoformat(),
                     "offline_at": (now - timedelta(hours=5)).isoformat()}, now))
check("a different kind than the last post is never a restart",
      not sn.is_restart({"posted_at": (now - timedelta(minutes=5)).isoformat(), "kind": "stream"}, now, "box"))
check("past the fallback, a new stream",
      not sn.is_restart({"posted_at": (now - timedelta(hours=13)).isoformat()}, now))

print("box scores, behind schedule (no title: the schedule decides)")
TITLE[0] = None
reset()
GAMES[:] = [game("2026-10-22", "NYK", "PHI"), game("2026-10-22", "DEN", "UTA"),
            game("2026-10-23", "OKC", "HOU", stream=True), game("2026-10-24", "CLE", "SAC", stream=True)]
post("notification", online())
p = SENT[0]["payload"] if SENT else {}
check("the oldest unplayed day has no stream game: pings no one",
      p.get("allowed_mentions") == {"parse": []}, p)
lines = p.get("content", "").split("\n")
check("reads Box Scores live, linking nbn.today", lines[0] == "🔴 Box Scores [live](<https://nbn.today>)", lines)
check("one of that night's games, tags shown, with records",
      lines[1:] in ([f"<@&{TEAM_ROLE_IDS['NYK']}> (0-0) at <@&{TEAM_ROLE_IDS['PHI']}> (0-0)"],
                    [f"<@&{TEAM_ROLE_IDS['DEN']}> (0-0) at <@&{TEAM_ROLE_IDS['UTA']}> (0-0)"]), lines)

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
      len(SENT) == 1 and SENT[0]["payload"]["content"].startswith("🔴 Box Scores [live]"))

reset()
real_load = schedule._load
schedule._load = lambda season: (_ for _ in ()).throw(FileNotFoundError(season))
post("notification", online())
check("an unreadable schedule still posts, as NBN is live with no pings",
      len(SENT) == 1 and SENT[0]["payload"]["content"] == "🔴 NBN is [live](<https://nbn.today>)", SENT)
schedule._load = real_load

print("the title decides the kind")
TITLE[0] = "NBN Game Night"
reset()
GAMES[:] = [game("2026-10-24", "BOS", "MIA")]
post("notification", online())
check("a game-night title with no stream game found is still NBN is live, with no matchup",
      SENT and SENT[0]["payload"] == {"content": "🔴 NBN is [live](<https://nbn.today>)",
                                      "allowed_mentions": {"parse": []}}, SENT)
reset()
TITLE[0] = "box scores"
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True)]
post("notification", online())
check("a box-score title on a night of only stream games is box scores",
      SENT and "Box Scores" in SENT[0]["payload"]["content"].split("\n")[0], SENT)
TITLE[0] = "NBN Game Night"
GAMES[:] = [game("2026-10-24", "CLE", "SAC", stream=True), game("2026-10-24", "BOS", "MIA"),
            game("2026-10-25", "LAL", "GSW", stream=True)]

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

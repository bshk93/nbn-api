"""Post to #leaguetalk the moment twitch.tv/nothingbutnet goes live —
`POST /api/twitch/eventsub`.

`stream_live.py` only finds out the channel is live when someone loads the
site, and Helix's `/streams` itself runs a minute or two behind. Twitch
EventSub pushes `stream.online` to this endpoint within seconds of the stream
starting instead, and the post goes out on the shared Discord queue.

The post is a link to nbn.today (the homepage carries the player while live),
plus a ping for each team in a game flagged as a stream (`stream` on the
schedule game, set by a streamer or a bought stream). A day of box scores
with no stream game pings no one. Which day is being streamed is the earliest
date in the last week with a game not yet played, since the league can run
behind — the same rule `game_day_notify` uses for a team's next game.

Three gates, so a restart or a redelivery never posts twice:

1. **Signature and age.** Every request is HMAC-verified against
   `TWITCH_EVENTSUB_SECRET` and refused when its timestamp is over 10 minutes
   old (Twitch's own replay rule). Until the secret is set, everything 403s.
2. **Message id.** Twitch redelivers a notification it isn't sure arrived; a
   message id already seen is acknowledged and dropped.
3. **Restart gap.** No post within `RESTART_GAP_HOURS` of the last one, stamped
   in `stream-announce.json`, so a dropped stream that comes back is silent and
   so is an API restart in between. A stream that began more than
   `MAX_STREAM_AGE` ago isn't announced at all.

Requires (in .env):
  TWITCH_EVENTSUB_SECRET       (10-100 chars; the same value given to
                                subscribe_twitch_eventsub.py)
  DISCORD_LEAGUETALK_CHANNEL   (unset, nothing posts)

Subscribe once with `subscribe_twitch_eventsub.py`.

    venv/bin/python -m tests.test_stream_notify
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from . import discord_transport as transport
from .constants import DATA_DIR
from .discord_notify import with_team_pings
from .league_time import league_today
from .storage import _current_league_year, _load_json, _save_json

router = APIRouter()
log = logging.getLogger(__name__)

DISCORD_LEAGUETALK_CHANNEL = os.environ.get("DISCORD_LEAGUETALK_CHANNEL", "").strip()
SITE_URL = "https://nbn.today"
STATE_FILE = DATA_DIR / "stream-announce.json"

MAX_MESSAGE_AGE   = 600   # seconds; Twitch says to drop anything older
MAX_STREAM_AGE    = 900   # seconds; a stream this old when we hear of it isn't news
RESTART_GAP_HOURS = 12    # a game day is one stream; anything inside this is it coming back
SEEN_IDS_KEEP     = 3600  # seconds a message id is remembered for

# One post a game day. Three an hour still stops a loop dead.
MAX_BURST    = 3
BURST_WINDOW = 3600

_lock = threading.Lock()
_seen_ids: dict[str, float] = {}


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Twitch stamps nanoseconds ("…:51.634234626Z"); datetime takes six digits."""
    if not ts:
        return None
    ts = re.sub(r"(\.\d{6})\d+", r"\1", str(ts)).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def verify(secret: str, msg_id: str, ts: str, body: bytes, signature: str) -> bool:
    if not (secret and msg_id and ts and signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), msg_id.encode() + ts.encode() + body,
                                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _first_sighting(msg_id: str) -> bool:
    now = time.time()
    with _lock:
        for k in [k for k, t in _seen_ids.items() if now - t > SEEN_IDS_KEEP]:
            del _seen_ids[k]
        if msg_id in _seen_ids:
            return False
        _seen_ids[msg_id] = now
        return True


def streamed_teams() -> list[str]:
    """Both teams of every stream game on the day being streamed; empty for a
    day of box scores. Imported at call time, like `game_day_notify` does."""
    from .game_day_notify import LOOKBACK_DAYS, _boxed_games, _played
    from .schedule import _load
    from .streaming_days import load_streaming_days
    today = league_today()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    season = _current_league_year()
    games = [g for g in _load(season)["games"] if since <= g["date"] <= today.isoformat()]
    done = {d for d, rec in load_streaming_days().items() if rec.get("done")}
    boxed = _boxed_games(season)
    unplayed = [g for g in games if not _played(g, done, boxed)]
    if not unplayed:
        return []
    day = min(g["date"] for g in unplayed)
    teams: list[str] = []
    for g in unplayed:
        if g["date"] == day and g.get("stream"):
            teams += [g["away_team"], g["home_team"]]
    return list(dict.fromkeys(teams))


def build_payload(teams: list[str]) -> dict:
    return with_team_pings(f"🔴 NBN is live: {SITE_URL}", teams)


def announce(event: dict, now: Optional[datetime] = None) -> bool:
    """Post a `stream.online` event, unless a gate says not to. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        if event.get("type") != "live":   # reruns, premieres, watch parties
            return False
        started = _parse_ts(event.get("started_at"))
        if not started or (now - started).total_seconds() > MAX_STREAM_AGE:
            log.info("stream_notify: stream started %s, too long ago to announce", event.get("started_at"))
            return False
        if not transport.configured(DISCORD_LEAGUETALK_CHANNEL):
            return False
        with _lock:
            state = _load_json(STATE_FILE, {})
            last = _parse_ts(state.get("posted_at"))
            if last and now - last < timedelta(hours=RESTART_GAP_HOURS):
                log.info("stream_notify: posted at %s already; treating this as a restart", state["posted_at"])
                return False
            try:
                teams = streamed_teams()
            except Exception as e:  # noqa: BLE001 — a missing schedule still gets the link out
                log.warning("stream_notify: couldn't work out the stream game: %s", e)
                teams = []
            sent = transport.send(DISCORD_LEAGUETALK_CHANNEL, lambda: build_payload(teams),
                                  max_burst=MAX_BURST, burst_window=BURST_WINDOW)
            if sent:
                _save_json(STATE_FILE, {"posted_at": now.isoformat(timespec="seconds"),
                                        "started_at": event.get("started_at"), "teams": teams})
            return sent
    except Exception as e:  # noqa: BLE001
        log.warning("stream_notify: announce failed: %s", e)
        return False


@router.post("/api/twitch/eventsub")
async def eventsub(request: Request):
    body = await request.body()
    h = request.headers
    msg_id = h.get("Twitch-Eventsub-Message-Id", "")
    ts = h.get("Twitch-Eventsub-Message-Timestamp", "")
    if not verify(os.environ.get("TWITCH_EVENTSUB_SECRET", ""), msg_id, ts, body,
                  h.get("Twitch-Eventsub-Message-Signature", "")):
        return Response(status_code=403)
    sent_at = _parse_ts(ts)
    if not sent_at or abs((datetime.now(timezone.utc) - sent_at).total_seconds()) > MAX_MESSAGE_AGE:
        return Response(status_code=403)
    try:
        data = json.loads(body)
    except ValueError:
        return Response(status_code=400)

    kind = h.get("Twitch-Eventsub-Message-Type", "")
    if kind == "webhook_callback_verification":
        return PlainTextResponse(str(data.get("challenge", "")))
    if kind == "revocation":
        sub = data.get("subscription") or {}
        log.warning("stream_notify: Twitch revoked %s (%s); re-run subscribe_twitch_eventsub.py",
                    sub.get("type"), sub.get("status"))
        return Response(status_code=204)
    if kind == "notification" and _first_sighting(msg_id):
        if (data.get("subscription") or {}).get("type") == "stream.online":
            await run_in_threadpool(announce, data.get("event") or {})
    return Response(status_code=204)

"""Post to #leaguetalk the moment twitch.tv/nothingbutnet goes live —
`POST /api/twitch/eventsub`.

`stream_live.py` only finds out the channel is live when someone loads the
site, and Helix's `/streams` itself runs a minute or two behind. Twitch
EventSub pushes `stream.online` to this endpoint within seconds of the stream
starting instead, and the post goes out on the shared Discord queue.

The post is a link to nbn.today (the homepage carries the player while live).
A day with a game flagged as a stream (`stream` on the schedule game, set by a
streamer or a bought stream) reads "NBN is live", then one line per stream
game, away at home, each team's role tag followed by its record (from the
standings the stats build writes, so going into the game); a day of box
scores reads "Box Scores live" and pings no one. Under each matchup goes one
line naming a random player from each roster ("Come watch Garland visit
Sabonis"), from `BLURBS`, never the same line twice in a row. "live" is
the link, and the angle brackets keep Discord from adding the site's generic
preview card under it. Which day is being streamed is the earliest
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
import random
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
from .discord_notify import TEAM_ROLE_IDS
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

# One under each matchup. {away} and {home} are a random player from each roster.
BLURBS = [
    "Come watch {away} visit {home}",
    "{home} hosts {away}",
    "{away} brings the road show to {home}",
    "Can {away} steal one on {home}'s floor?",
    "{home} defends home court against {away}",
    "{away} vs {home}. Pull up.",
    "{home} has {away} over tonight",
    "All eyes on {away} and {home}",
    "{away} walks into {home}'s building",
    "Who blinks first, {away} or {home}?",
    "{away} told reporters he's not scared of {home}. Sources say he is.",
    "{home} has been studying {away}'s film all week. All four minutes of it.",
    "{away} is reportedly bringing his own snacks to {home}'s arena",
    "Breaking: {home} has blocked {away} on Instagram",
    "{away} says this one's personal. {home} has no idea who he is.",
    "{home} vs {away}: the matchup nobody asked for, everybody's watching",
    "{away} just asked {home} for a jersey swap. Before the game.",
    "Vegas has {away} vs {home} as a pick'em. Vegas has never heard of either.",
    "{home} left {away} two tickets at will call. Both in the nosebleeds.",
    "{away} has been talking trash about {home} in the group chat all week",
]

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


def streamed_games() -> list[tuple[str, str]]:
    """(away, home) for every stream game on the day being streamed, in
    tip-off order; empty for a day of box scores. Imported at call time, like
    `game_day_notify` does."""
    from .game_day_notify import LOOKBACK_DAYS, _boxed_games, _played
    from .schedule import _load, _time_key
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
    games = sorted((g for g in unplayed if g["date"] == day and g.get("stream")),
                   key=lambda g: _time_key(g.get("time_et", "")))
    return [(g["away_team"], g["home_team"]) for g in games]


def team_records() -> dict[str, str]:
    """{team: "W-L"} for the current season, off standings-history.csv. A team
    with no row yet (no games played) is left out and shows 0-0."""
    from .home import STANDINGS_CSV
    from .storage import read_csv
    season = _current_league_year()
    _, rows = read_csv(STANDINGS_CSV)
    return {(r.get("TEAM") or "").upper(): f"{r.get('W') or 0}-{r.get('L') or 0}"
            for r in rows if r.get("SEASON") == season}


def random_player(team: str) -> Optional[str]:
    """Anyone under contract, by display name: the end of the bench is as
    likely as a starter, which is half the fun. Draft rights, dead cap and
    unsigned UFA/RFA holds aren't players on the team."""
    from .discord import _roster_augmented
    from .names import player_name
    names = [player_name(p["name_raw"]) for p in _roster_augmented(team)
             if p["type"] in ("player", "two-way") and p.get("hold_type") not in ("UFA", "RFA")]
    return random.choice(names) if names else None


def pick_blurbs(games: list, last: Optional[str]) -> tuple[list[Optional[str]], Optional[str]]:
    """(lines, first template): one line per game, or None for a game we
    couldn't name players for. Each post draws its templates without repeats,
    and its first is never `last`, the previous post's first."""
    pool = [b for b in BLURBS if b != last] or BLURBS
    picks = random.sample(pool, min(len(games), len(pool)))
    out: list[Optional[str]] = []
    for i, (away, home) in enumerate(games):
        try:
            a, h = random_player(away), random_player(home)
        except Exception as e:  # noqa: BLE001 — the matchup posts without its line
            log.warning("stream_notify: couldn't pick players for %s @ %s: %s", away, home, e)
            a = h = None
        out.append(picks[i % len(picks)].format(away=a, home=h) if a and h else None)
    return out, picks[0] if picks else None


def _tag(team: str) -> str:
    """The team's role mention, or its abbreviation in bold if it has none."""
    rid = TEAM_ROLE_IDS.get(team)
    return f"<@&{rid}>" if rid else f"**{team}**"


def build_payload(games: Optional[list], records: Optional[dict] = None,
                  blurbs: Optional[list] = None) -> dict:
    """`games` empty is a box-score day; None is a day we couldn't work out.
    `records` None leaves the records off rather than showing 0-0 for all;
    `blurbs` lines up with `games`, a None entry (or none at all) skipped.
    `allowed_mentions` names exactly the roles tagged, so nothing else pings."""
    if games == []:
        return {"content": f"🔴 Box Scores [live](<{SITE_URL}>)", "allowed_mentions": {"parse": []}}

    def side(team: str) -> str:
        return _tag(team) + (f" ({records.get(team, '0-0')})" if records is not None else "")

    lines = [f"🔴 NBN is [live](<{SITE_URL}>)"]
    for i, (away, home) in enumerate(games or []):
        lines.append(f"{side(away)} at {side(home)}")
        if blurbs and i < len(blurbs) and blurbs[i]:
            lines.append(blurbs[i])
    roles = list(dict.fromkeys(TEAM_ROLE_IDS[t] for g in games or [] for t in g if t in TEAM_ROLE_IDS))
    mentions = {"parse": [], "roles": roles} if roles else {"parse": []}
    return {"content": "\n".join(lines), "allowed_mentions": mentions}


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
                games = streamed_games()
            except Exception as e:  # noqa: BLE001 — a missing schedule still gets the link out
                log.warning("stream_notify: couldn't work out the stream game: %s", e)
                games = None
            records, blurbs, template = None, None, None
            if games:
                blurbs, template = pick_blurbs(games, state.get("blurb"))
                try:
                    records = team_records()
                except Exception as e:  # noqa: BLE001 — the matchup still posts, without records
                    log.warning("stream_notify: couldn't read the standings: %s", e)
            sent = transport.send(DISCORD_LEAGUETALK_CHANNEL, lambda: build_payload(games, records, blurbs),
                                  max_burst=MAX_BURST, burst_window=BURST_WINDOW)
            if sent:
                _save_json(STATE_FILE, {"posted_at": now.isoformat(timespec="seconds"),
                                        "started_at": event.get("started_at"),
                                        "games": [list(g) for g in games] if games is not None else None,
                                        "blurb": template})
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

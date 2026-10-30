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
standings the stats build writes, so going into the game), and under each a
line from `BLURBS` naming a random player from each roster ("Come watch
Garland visit Sabonis"). Only those roles ping.

A day of box scores reads "Box Scores live", then one game picked at random
from the night, its tags shown but pinging no one, and a line from
`BOX_BLURBS` naming a current front-office member of each team. Neither kind
reuses a line within a post or opens with its own kind's last opener. "live" is
the link, and the angle brackets keep Discord from adding the site's generic
preview card under it. The night being streamed is the date the stream
started, in league time, with anything before `NIGHT_ROLLOVER_HOURS` (6 AM)
counted as the night before, so a box-score stream that runs past midnight
stays with its games. Every game that date counts, played or not: a day is
marked done late or never, and box scores land over the next days, so neither
says anything about tonight. Only a date with no games on the schedule (a
catch-up stream) falls back to the earliest date in the last week with a game
not yet played — `game_day_notify`'s rule.

Three gates, so a restart or a redelivery never posts twice:

1. **Signature and age.** Every request is HMAC-verified against
   `TWITCH_EVENTSUB_SECRET` and refused when its timestamp is over 10 minutes
   old (Twitch's own replay rule). Until the secret is set, everything 403s.
2. **Message id.** Twitch redelivers a notification it isn't sure arrived; a
   message id already seen is acknowledged and dropped.
3. **Restart.** `stream.offline` is subscribed too, and stamped in
   `stream-announce.json`; a stream back within `RESTART_GRACE_MINUTES` of
   going offline, as the same kind of post as the last, is the same stream
   coming back and is silent. A switch of kind posts however soon it comes
   (the box scores go live minutes after the game stream ends), and anything
   past the grace is a new stream. Should the offline notice never arrive,
   the fallback is the old rule: nothing within `RESTART_GAP_HOURS` of the
   last post. Stamped on disk, so an API restart in between changes nothing. A stream that began more than
   `MAX_STREAM_AGE` ago isn't announced at all.

Requires (in .env):
  TWITCH_EVENTSUB_SECRET       (10-100 chars; the same value given to
                                subscribe_twitch_eventsub.py)
  DISCORD_LEAGUETALK_CHANNEL   (unset, nothing posts)
  DISCORD_LIVE_EMOJI           (optional; overrides the emoji that opens the
                                post, in the form a bot has to send it:
                                <:name:id>. Unset, the server's :minionannouncement:)

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

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from . import discord_transport as transport
from .constants import DATA_DIR
from .discord_notify import TEAM_ROLE_IDS
from .league_time import LEAGUE_TZ, league_today
from .storage import _current_league_year, _load_json, _save_json

router = APIRouter()
log = logging.getLogger(__name__)

DISCORD_LEAGUETALK_CHANNEL = os.environ.get("DISCORD_LEAGUETALK_CHANNEL", "").strip()
# A bot can't send a custom emoji as `:name:`; it has to be `<:name:id>`.
LIVE_EMOJI = os.environ.get("DISCORD_LIVE_EMOJI", "").strip() or "<:minionannouncement:1564839468823744572>"
SITE_URL = "https://nbn.today"
STATE_FILE = DATA_DIR / "stream-announce.json"

MAX_MESSAGE_AGE   = 600   # seconds; Twitch says to drop anything older
MAX_STREAM_AGE    = 900   # seconds; a stream this old when we hear of it isn't news
RESTART_GRACE_MINUTES = 30  # back within this of going offline, as the same kind: the same stream
NIGHT_ROLLOVER_HOURS = 6   # a stream starting before 6 AM league time is the night before
RESTART_GAP_HOURS = 12    # fallback when no offline notice came after the last post
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

# Box-score days. {away} and {home} are a front-office member of each team:
# real league members, so no pronouns.
BOX_BLURBS = [
    "{home} is watching from the luxury box. {away} is watching on a cracked phone.",
    "{away} has the trade proposal drafted for whichever way this goes",
    "{home} swears the sim is rigged. Box scores pending.",
    "{away} is refreshing the box scores like a stock ticker",
    "{home} and {away} both say they don't even care about this one",
    "{away} has the excuse ready. {home} has the screenshot ready.",
    "{home} told the coach to play the starters 48 minutes. Again.",
    "{away} vs {home}: two front offices, zero accountability",
    "Somewhere, {home} is already typing 'rigged' in #leaguetalk",
    "{away}'s trading block is about to get a lot longer",
    "{home} is one bad loss from blowing it all up. Again.",
    "{away} and {home} have both already simmed this in their heads. Both won.",
]

# The streamer retitles the box-score stream; that's what says which one this is.
BOX_TITLE_RE = re.compile(r"box\s*-?\s*scores?", re.I)
CHANNELS_URL = "https://api.twitch.tv/helix/channels"

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


def night_of(started: Optional[datetime]) -> str:
    """The league date a stream belongs to: the day it started, in league
    time, less `NIGHT_ROLLOVER_HOURS`, so 12:30 AM is still last night."""
    at = (started or datetime.now(timezone.utc)).astimezone(LEAGUE_TZ)
    return (at - timedelta(hours=NIGHT_ROLLOVER_HOURS)).date().isoformat()


def night_games(night: Optional[str] = None) -> list[dict]:
    """The games being streamed, in tip-off order: every game scheduled on
    `night`, played or not. A night with none (a catch-up stream) falls back to
    the earliest date in the last week with a game not yet played. Imported at
    call time, like `game_day_notify` does."""
    from .game_day_notify import LOOKBACK_DAYS, _boxed_games, _played
    from .schedule import _load, _time_key
    from .streaming_days import load_streaming_days
    season = _current_league_year()
    schedule = _load(season)["games"]
    tonight = [g for g in schedule if night and g["date"] == night]
    if tonight:
        return sorted(tonight, key=lambda g: _time_key(g.get("time_et", "")))
    today = league_today()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    games = [g for g in schedule if since <= g["date"] <= today.isoformat()]
    done = {d for d, rec in load_streaming_days().items() if rec.get("done")}
    boxed = _boxed_games(season)
    unplayed = [g for g in games if not _played(g, done, boxed)]
    if not unplayed:
        return []
    day = min(g["date"] for g in unplayed)
    return sorted((g for g in unplayed if g["date"] == day),
                  key=lambda g: _time_key(g.get("time_et", "")))


def fetch_title(broadcaster_id: str) -> Optional[str]:
    """The channel's current title, from Helix `/channels`, which is current
    the moment it's set (unlike `/streams`). None on any failure."""
    from .stream_live import _app_token
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not (client_id and secret and broadcaster_id):
        return None
    try:
        with httpx.Client(timeout=5) as client:
            token = _app_token(client, client_id, secret)
            r = client.get(CHANNELS_URL, params={"broadcaster_id": broadcaster_id},
                           headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"})
            r.raise_for_status()
            data = r.json().get("data") or []
        return data[0].get("title") if data else None
    except Exception as e:  # noqa: BLE001 — the post goes out on the schedule's word instead
        log.warning("stream_notify: couldn't read the stream title: %s", e)
        return None


def plan_post(state: Optional[dict] = None, title: Optional[str] = None,
              night: Optional[str] = None) -> tuple[str, list[tuple[str, str]], Optional[str]]:
    """(kind, games, day). ("stream", every stream game) when the night has
    one, else ("box", one of its games at random, or none).

    The title decides the kind outright: one saying "box scores" is the
    box-score post, and any other is the stream post, with the night's stream
    games when it has any and no matchup when it doesn't. The schedule only
    says which games. With no title (Twitch couldn't be asked), any later stream on a
    night that already had its stream post (`stream_day`) is the box scores:
    the stream games have no box score yet, so they'd still read as unplayed
    and the night would announce them twice. A box-score post's game comes
    from the night's other games when there are any."""
    games = night_games(night)
    day = games[0]["date"] if games else None
    stream = [(g["away_team"], g["home_team"]) for g in games if g.get("stream")]
    if title is not None:
        box = bool(BOX_TITLE_RE.search(title))
    else:
        box = bool(day) and (state or {}).get("stream_day") == day
    if (title is not None and not box) or (stream and not box):
        return "stream", stream, day
    pool = [g for g in games if not g.get("stream")] or games
    g = random.choice(pool) if pool else None
    return "box", [(g["away_team"], g["home_team"])] if g else [], day


def is_restart(state: dict, now: datetime, kind: Optional[str] = None) -> bool:
    """The same stream coming back, rather than a new one. A different kind of
    post than the last never is: game stream to box scores is a switch."""
    posted = _parse_ts(state.get("posted_at"))
    if not posted:
        return False
    if kind is not None and state.get("kind") is not None and kind != state.get("kind"):
        return False
    offline = _parse_ts(state.get("offline_at"))
    if offline and offline >= posted:
        return now - offline < timedelta(minutes=RESTART_GRACE_MINUTES)
    return now - posted < timedelta(hours=RESTART_GAP_HOURS)


def note_offline(now: Optional[datetime] = None) -> None:
    """Stamp `stream.offline`, which is what tells a restart from a new stream."""
    try:
        now = now or datetime.now(timezone.utc)
        with _lock:
            state = _load_json(STATE_FILE, {})
            state["offline_at"] = now.isoformat(timespec="seconds")
            _save_json(STATE_FILE, state)
    except Exception as e:  # noqa: BLE001
        log.warning("stream_notify: couldn't record the stream going offline: %s", e)


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


def _md_escape(text: str) -> str:
    """A member name is typed by a person; keep `_` and `*` from formatting."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", text)


def random_front_office(team: str) -> Optional[str]:
    """Anyone in a current front-office position on the team (an open tenure
    whose position isn't `none`, as `proposals._member_current_team` reads it)."""
    from .auth import load_members
    names = [name for name, m in load_members().items()
             if any(not t.get("end") and t.get("position") not in (None, "", "none")
                    and (t.get("team") or "").upper() == team for t in m.get("tenures") or [])]
    return _md_escape(random.choice(names)) if names else None


def pick_blurbs(games: list, last: Optional[str], kind: str = "stream"
                ) -> tuple[list[Optional[str]], Optional[str]]:
    """(lines, first template): one line per game, or None for a game we
    couldn't name both sides for. Each post draws its templates without
    repeats, and its first is never `last`, the previous post's first. A
    stream names players; a box-score day names front offices."""
    templates, pick = (BLURBS, random_player) if kind == "stream" else (BOX_BLURBS, random_front_office)
    pool = [b for b in templates if b != last] or templates
    picks = random.sample(pool, min(len(games), len(pool)))
    out: list[Optional[str]] = []
    for i, (away, home) in enumerate(games):
        try:
            a, h = pick(away), pick(home)
        except Exception as e:  # noqa: BLE001 — the matchup posts without its line
            log.warning("stream_notify: couldn't pick names for %s @ %s: %s", away, home, e)
            a = h = None
        out.append(picks[i % len(picks)].format(away=a, home=h) if a and h else None)
    return out, picks[0] if picks else None


def _tag(team: str) -> str:
    """The team's role mention, or its abbreviation in bold if it has none."""
    rid = TEAM_ROLE_IDS.get(team)
    return f"<@&{rid}>" if rid else f"**{team}**"


def build_payload(kind: Optional[str], games: list, records: Optional[dict] = None,
                  blurbs: Optional[list] = None) -> dict:
    """`kind` is "stream", "box", or None for a day we couldn't work out.
    `records` None leaves the records off rather than showing 0-0 for all;
    `blurbs` lines up with `games`, a None entry (or none at all) skipped.
    `allowed_mentions` names exactly the roles that should ping: a stream's
    teams, and no one on a box-score day, whose tags still render."""
    def side(team: str) -> str:
        return _tag(team) + (f" ({records.get(team, '0-0')})" if records is not None else "")

    head = "Box Scores" if kind == "box" else "NBN is"
    lines = [f"{LIVE_EMOJI} {head} [live](<{SITE_URL}>)"]
    for i, (away, home) in enumerate(games or []):
        lines.append(f"{side(away)} at {side(home)}")
        if blurbs and i < len(blurbs) and blurbs[i]:
            lines.append(blurbs[i])
    roles = [] if kind != "stream" else \
        list(dict.fromkeys(TEAM_ROLE_IDS[t] for g in games for t in g if t in TEAM_ROLE_IDS))
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
            title = fetch_title(event.get("broadcaster_user_id") or os.environ.get("TWITCH_BROADCASTER_ID", ""))
            try:
                kind, games, day = plan_post(state, title, night_of(started))
            except Exception as e:  # noqa: BLE001 — a missing schedule still gets the link out
                log.warning("stream_notify: couldn't work out the night's games: %s", e)
                kind, games, day = None, [], None
            if is_restart(state, now, kind):
                log.info("stream_notify: %s post at %s, offline at %s; treating this as a restart",
                         state.get("kind"), state.get("posted_at"), state.get("offline_at"))
                return False
            last_key = "box_blurb" if kind == "box" else "blurb"
            records, blurbs, template = None, None, state.get(last_key)
            if games:
                blurbs, template = pick_blurbs(games, state.get(last_key), kind)
                try:
                    records = team_records()
                except Exception as e:  # noqa: BLE001 — the matchup still posts, without records
                    log.warning("stream_notify: couldn't read the standings: %s", e)
            sent = transport.send(DISCORD_LEAGUETALK_CHANNEL, lambda: build_payload(kind, games, records, blurbs),
                                  max_burst=MAX_BURST, burst_window=BURST_WINDOW)
            if sent:
                state.update(posted_at=now.isoformat(timespec="seconds"),
                             started_at=event.get("started_at"), kind=kind, day=day,
                             games=[list(g) for g in games])
                if kind:
                    state[last_key] = template
                if kind == "stream":
                    state["stream_day"] = day
                _save_json(STATE_FILE, state)
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
        sub_type = (data.get("subscription") or {}).get("type")
        if sub_type == "stream.online":
            await run_in_threadpool(announce, data.get("event") or {})
        elif sub_type == "stream.offline":
            await run_in_threadpool(note_offline)
    return Response(status_code=204)

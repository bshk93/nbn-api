"""Attach the channel's YouTube videos to the games they show, on their own.

Every NBN video is titled one of two ways, and the title is the whole match:

    NBN 10/21: MIN @ MIA     one streamed game → that game's `youtube_url`
    NBN 1/29: Box Scores     the night's unstreamed games → that date's
                             `youtube_url` in streaming-days.json

When the league runs behind, one stream catches up on several days, and its
title has no fixed shape ("NBN 10/24 and 10/25", "NBN: Box Scores 10/24-10/25",
"NBN 10/24-25"). So any title starting "NBN" that names dates, and isn't one
game, is a day video for each of those dates (a range counts every day in it).

There is no fuzzy matching on team names or publish times. A title that names
a game or date the schedule doesn't have is `unmatched` and waits for Stats, as
is a multi-date title that also names a matchup. A title naming dates with no
games and no "box scores" (a draft night) is `ignored`, like any other title.

The source is the channel's public Atom feed, which lists the newest 15 uploads
and live streams with no API key or quota. A thread polls it every
POLL_INTERVAL, like routers/roster_log_relay.py polls Discord.

The feed goes down for hours at a time (404s all day on 2026-10-29, while the
channel itself was fine), so when it fails the poll reads the channel's
Live tab instead (`parse_streams_page`). That page says only "Streamed 2 hours
ago", so its upload time is the latest the video could have gone up: a title's
date is only ever checked against it as a ceiling, which that keeps loose
rather than wrong.

Two rules keep it from fighting people:
- **A manual link is never overwritten.** A link set through
  PUT /api/schedule/{id}/youtube or /api/streaming-days/{date}/youtube wins.
- **A video is linked once.** Once `linked`, it isn't looked at again, so
  clearing a wrong auto link sticks — the next poll doesn't put it back.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends

from . import inbox
from . import schedule as sched
from . import streaming_days as sd
from .auth import require_any_role
from .constants import VALID_TEAMS, YOUTUBE_VODS_FILE, _streaming_days_lock
from .league_time import LEAGUE_TZ
from .storage import _load_json, _save_json, log_write

logger = logging.getLogger("nbn-api")
logging.getLogger("httpx").setLevel(logging.WARNING)

router = APIRouter()

CHANNEL_HANDLE = "nothingbutnetNBN"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
STREAMS_URL = "https://www.youtube.com/channel/{cid}/streams"
POLL_INTERVAL = 15 * 60     # one small request; YouTube posts a VOD once a night
STATS_LINK = "/committees/stats/"

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

TITLE_RE = re.compile(r"^\s*NBN\s+(\d{1,2})/(\d{1,2})\s*:\s*(.+?)\s*$", re.I)
GAME_RE  = re.compile(r"^([A-Z]{2,4})\s*@\s*([A-Z]{2,4})$", re.I)
BOX_RE   = re.compile(r"^box\s*scores?$", re.I)
# The looser shapes of a catch-up stream's title.
NBN_RE   = re.compile(r"^\s*NBN\b", re.I)
DATES_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:\s*(?:-|–|—|to|thru|through)\s*(?:(\d{1,2})/)?(\d{1,2}))?(?![\d/])", re.I)
ANY_GAME_RE = re.compile(r"\b[A-Z]{2,4}\s*@\s*[A-Z]{2,4}\b", re.I)
ANY_BOX_RE  = re.compile(r"box\s*scores?", re.I)
INITIAL_DATA_RE = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", re.S)
AGO_RE = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
AGO_UNIT = {"second": timedelta(seconds=1), "minute": timedelta(minutes=1), "hour": timedelta(hours=1),
            "day": timedelta(days=1), "week": timedelta(weeks=1), "month": timedelta(days=30),
            "year": timedelta(days=365)}
MAX_RANGE_DAYS = 7

_state_lock = threading.Lock()
_thread_started = False
_thread_lock = threading.Lock()


# ── titles ────────────────────────────────────────────────────────────────────

def parse_title(title: str) -> Optional[dict]:
    """What a title names, or None for a title that isn't an NBN game or
    box-score video:

      {"kind": "game", month, day, away, home}   one game
      {"kind": "box",  days}                      the dates' day video
      {"kind": "day",  days}                      the same, with no "box scores"
      {"kind": "unclear", days}                   several dates and a matchup

    `days` is a list of (month, day) or ((month, day), (month, day)) ranges."""
    title = title or ""
    m = TITLE_RE.match(title)
    if m:
        month, day, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        g = GAME_RE.match(rest)
        if g and 1 <= month <= 12 and 1 <= day <= 31:
            return {"kind": "game", "month": month, "day": day,
                    "away": g.group(1).upper(), "home": g.group(2).upper()}
    if not NBN_RE.match(title):
        return None
    days = []
    for d in DATES_RE.finditer(title):
        m1, d1 = int(d.group(1)), int(d.group(2))
        if not (1 <= m1 <= 12 and 1 <= d1 <= 31):
            continue
        if d.group(4):
            m2, d2 = int(d.group(3) or m1), int(d.group(4))
            if not (1 <= m2 <= 12 and 1 <= d2 <= 31):
                continue
            days.append(((m1, d1), (m2, d2)))
        else:
            days.append((m1, d1))
    if not days:
        return None
    if ANY_GAME_RE.search(title):
        return {"kind": "unclear", "days": days}
    return {"kind": "box" if ANY_BOX_RE.search(title) else "day", "days": days}


def resolve_date(month: int, day: int, published: datetime) -> Optional[str]:
    """The title's M/D as a full date. Titles carry no year, so take the latest
    one that isn't after the upload — a 12/31 game posted on Jan 1 is last
    year's. A day of slack covers a video scheduled ahead of its game."""
    ceiling = published.astimezone(LEAGUE_TZ).date() + timedelta(days=1)
    for year in (ceiling.year, ceiling.year - 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d <= ceiling:
            return d.isoformat()
    return None


# ── feed ──────────────────────────────────────────────────────────────────────

def parse_feed(xml_text: str) -> list[dict]:
    """[{id, title, published}] from the channel's Atom feed, newest first."""
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        title = e.findtext("a:title", default="", namespaces=NS)
        pub = e.findtext("a:published", default="", namespaces=NS)
        if vid and pub:
            out.append({"id": vid, "title": title, "published": pub})
    return out


def _lockups(node, out: list) -> list:
    if isinstance(node, dict):
        if "lockupViewModel" in node:
            out.append(node["lockupViewModel"])
        for v in node.values():
            _lockups(v, out)
    elif isinstance(node, list):
        for v in node:
            _lockups(v, out)
    return out


def parse_streams_page(html: str, now: Optional[datetime] = None) -> list[dict]:
    """[{id, title, published}] from the channel's Live tab, newest first: the
    same shape as `parse_feed`. `published` is `now` less the page's "N units
    ago", the latest the video can be from; "Streaming now" or no age at all
    is `now`. Raises ValueError when the page has no video data."""
    now = now or datetime.now(timezone.utc)
    m = INITIAL_DATA_RE.search(html or "")
    if not m:
        raise ValueError("no ytInitialData on the page")
    out, seen = [], set()
    for lv in _lockups(json.loads(m.group(1)), []):
        vid = lv.get("contentId")
        md = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        title = (md.get("title") or {}).get("content")
        if not vid or not title or vid in seen:
            continue
        seen.add(vid)
        age = timedelta(0)
        for a in AGO_RE.finditer(json.dumps(md.get("metadata") or {})):
            age = int(a.group(1)) * AGO_UNIT[a.group(2).lower()]
            break
        out.append({"id": vid, "title": title, "published": (now - age).isoformat()})
    return out


def _get(url: str) -> Optional[str]:
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "nbn-api (nbn.today)", "Accept-Language": "en-US"})
    except Exception as exc:
        logger.warning("youtube vods: GET %s errored: %s", url, exc)
        return None
    if r.status_code >= 300:
        logger.warning("youtube vods: GET %s failed: %s", url, r.status_code)
        return None
    return r.text


def _channel_id(state: dict) -> Optional[str]:
    """YOUTUBE_CHANNEL_ID if set, else read once off the @handle page and kept."""
    cid = os.environ.get("YOUTUBE_CHANNEL_ID") or state.get("channel_id")
    if cid:
        return cid
    page = _get(f"https://www.youtube.com/@{CHANNEL_HANDLE}")
    m = re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', page or "")
    if not m:
        logger.warning("youtube vods: couldn't find the channel id for @%s", CHANNEL_HANDLE)
        return None
    state["channel_id"] = m.group(1)
    return state["channel_id"]


# ── state ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    st = _load_json(YOUTUBE_VODS_FILE, {})
    st.setdefault("videos", {})
    return st


def _save_state(st: dict) -> None:
    _save_json(YOUTUBE_VODS_FILE, st)


# ── linking ───────────────────────────────────────────────────────────────────

def _link_game(on: str, away: str, home: str, url: str) -> tuple[str, str]:
    """(status, note) for putting `url` on the `away @ home` game dated `on`."""
    with sched._schedule_lock:
        for season in sched._known_seasons():
            data = sched._load(season)
            for g in data["games"]:
                if g["date"] == on and g["away_team"] == away and g["home_team"] == home:
                    if g.get("youtube_url") == url:
                        return "linked", f"{season} {g['id']}"
                    if g.get("youtube_url"):
                        return "skipped", "game already has a video"
                    sched.set_game_youtube(g, url, "auto", None)
                    sched._save(season, data)
                    return "linked", f"{season} {g['id']}"
    return "unmatched", f"no {away} @ {home} game on {on}"


def _link_day(on: str, url: str) -> tuple[str, str]:
    with _streaming_days_lock:
        data = sd.load_streaming_days()
        rec = data.setdefault(on, {})
        if rec.get("youtube_url") == url:
            return "linked", on
        if rec.get("youtube_url"):
            return "skipped", "date already has a video"
        rec["youtube_url"] = url
        rec["youtube_source"] = "auto"
        rec["youtube_set_at"] = datetime.now(timezone.utc).isoformat()
        rec["youtube_set_by"] = None
        sd.save_streaming_days(data)
    return "linked", on


def _expand(days: list, published: datetime) -> Optional[list[str]]:
    """The full dates a title's `days` name, oldest first, or None when one
    can't be placed (no such date, or a range backwards or over a week)."""
    out = set()
    for d in days:
        if isinstance(d[0], tuple):
            a, b = resolve_date(*d[0], published), resolve_date(*d[1], published)
            if not a or not b:
                return None
            a, b = date.fromisoformat(a), date.fromisoformat(b)
            if a > b:   # 12/30-1/2: the start is the year before
                try:
                    a = a.replace(year=a.year - 1)
                except ValueError:
                    return None
            if (b - a).days >= MAX_RANGE_DAYS:
                return None
            out.update((a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1))
        else:
            on = resolve_date(*d, published)
            if not on:
                return None
            out.add(on)
    return sorted(out)


def _game_dates() -> set[str]:
    return {g["date"] for season in sched._known_seasons() for g in sched._load(season)["games"]}


def match_video(video: dict) -> dict:
    """What to do with one feed entry: {status, kind?, date?, dates?, note?}."""
    parsed = parse_title(video["title"])
    if not parsed:
        return {"status": "ignored"}
    published = datetime.fromisoformat(video["published"])
    url = f"https://www.youtube.com/watch?v={video['id']}"
    if parsed["kind"] == "game":
        on = resolve_date(parsed["month"], parsed["day"], published)
        if not on:
            return {"status": "unmatched", "kind": "game", "note": "date doesn't exist"}
        if parsed["away"] not in VALID_TEAMS or parsed["home"] not in VALID_TEAMS:
            status, note = "unmatched", f"unknown team in {parsed['away']} @ {parsed['home']}"
        else:
            status, note = _link_game(on, parsed["away"], parsed["home"], url)
        return {"status": status, "kind": "game", "date": on, "note": note}

    kind = parsed["kind"]
    dates = _expand(parsed["days"], published)
    if not dates:
        return {"status": "unmatched", "kind": kind, "note": "dates don't make sense"}
    if kind == "unclear":
        return {"status": "unmatched", "kind": kind, "date": dates[0], "dates": dates,
                "note": "names several dates and a game"}
    played = _game_dates()
    on_schedule = [d for d in dates if d in played]
    if not on_schedule:
        if kind == "day":
            return {"status": "ignored"}
        return {"status": "unmatched", "kind": kind, "date": dates[0], "dates": dates,
                "note": f"no games on {', '.join(dates)}"}
    results = [(d, *_link_day(d, url)) for d in on_schedule]
    statuses = {r[1] for r in results}
    status = "linked" if "linked" in statuses else "skipped"
    note = "; ".join(f"{d}: {n}" if s != "linked" else d for d, s, n in results)
    missing = [d for d in dates if d not in played]
    if missing:
        note += f"; no games on {', '.join(missing)}"
    return {"status": status, "kind": kind, "date": on_schedule[0], "dates": on_schedule, "note": note}


def process(videos: list[dict], state: dict) -> list[dict]:
    """Match every feed entry not already settled. Returns the ones that changed.
    `linked`, `skipped` and `ignored` are final; `unmatched` is retried each
    poll, since the game it names may simply not be scheduled yet."""
    changed = []
    for v in videos:
        prev = state["videos"].get(v["id"])
        if prev and prev["status"] != "unmatched":
            continue
        result = {**match_video(v), "title": v["title"], "published": v["published"]}
        if prev and prev["status"] == result["status"]:
            continue
        result["seen_at"] = datetime.now(timezone.utc).isoformat()
        state["videos"][v["id"]] = result
        changed.append({"id": v["id"], **result})
    return changed


def poll_once() -> dict:
    with _state_lock:
        state = _load_state()
        cid = _channel_id(state)
        if not cid:
            return {"ok": False, "error": "no channel id"}
        videos, source = None, "feed"
        xml_text = _get(FEED_URL.format(cid=cid))
        if xml_text is not None:
            try:
                videos = parse_feed(xml_text)
            except ET.ParseError as exc:
                logger.warning("youtube vods: feed didn't parse: %s", exc)
        if videos is None:
            source = "streams page"
            page = _get(STREAMS_URL.format(cid=cid))
            try:
                videos = parse_streams_page(page) if page is not None else None
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("youtube vods: streams page didn't parse: %s", exc)
        if videos is None:
            _save_state(state)
            return {"ok": False, "error": "feed and streams page unavailable"}
        changed = process(videos, state)
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
    for c in changed:
        if c["status"] == "linked":
            logger.info("youtube vods: linked %r (%s)", c["title"], c["note"])
        elif c["status"] == "unmatched":
            inbox.notify_roles(["stats"], f"Couldn't place the YouTube video \"{c['title']}\" "
                                          f"({c['note']}). Link it by hand.", STATS_LINK)
    return {"ok": True, "source": source, "feed": len(videos), "changed": changed}


def _loop() -> None:
    while True:
        try:
            poll_once()
        except Exception:
            logger.exception("youtube vods: poll failed")
        time.sleep(POLL_INTERVAL)


def start_youtube_vods() -> None:
    """Called from the app lifespan. YOUTUBE_VODS_DISABLED=1 turns it off."""
    global _thread_started
    if os.environ.get("YOUTUBE_VODS_DISABLED"):
        logger.info("youtube vods: disabled")
        return
    with _thread_lock:
        if _thread_started:
            return
        threading.Thread(target=_loop, daemon=True, name="youtube-vods").start()
        _thread_started = True
    logger.info("youtube vods: polling @%s every %ds", CHANNEL_HANDLE, POLL_INTERVAL)


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/api/youtube-vods")
def youtube_vods_status(_: dict = Depends(require_any_role("stats", "streamer"))):
    """Every video the poller has seen and what became of it, newest first."""
    st = _load_state()
    videos = sorted(({"id": k, **v} for k, v in st["videos"].items()),
                    key=lambda v: v.get("published", ""), reverse=True)
    return {"channel_id": st.get("channel_id"), "last_poll": st.get("last_poll"),
            "poll_interval": POLL_INTERVAL, "videos": videos}


@router.post("/api/youtube-vods/poll")
def youtube_vods_poll_now(info: dict = Depends(require_any_role("stats", "streamer"))):
    """Check the feed now rather than waiting for the next poll."""
    log_write(info, "POST youtube-vods/poll")
    return poll_once()

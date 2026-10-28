"""NBN Twitter — made-up posts reacting to each game day, for flavor.

A cast of fictional accounts (a stats account, an insider, beat writers, fan
accounts, the players themselves) reacting to what really happened that day.
They are written at the end of the daily box score run
(nbn-today/.claude/commands/parse-boxscores.md § 8), from the committed box
scores and the lore, and shown on the homepage card.

The text is fiction; the numbers in it are not. Nothing here checks that,
because it can't: the skill checks every number against the box score before
posting. What this module does guard is the shape: a tweet fits in 280
characters, a handle is a handle, and `teams` names real teams so the card can
link a tweet to its game.

Stored as {"seq": int, "items": [...]} like quadrants.json, so a tweet's
number stays its own after older ones are deleted.

    GET    /api/tweets             public, newest first (?limit=, ?before=, ?team=)
    POST   /api/tweets             stats or rosters — one game day's batch
    DELETE /api/tweets/{n}         stats or rosters

Other routers post through `post_one` for real events that happen off the box
score: arena naming rights (routers/arenas.py) post one when a bid lands. Those
carry a `ref` naming the event, so `delete_by_ref` can take the tweet down if
the event is undone.

Every stored tweet is also posted to `DISCORD_TWEETS_CHANNEL`, one message
each, as an embed that reads like the card (`_embed`). Inert without the
channel. Deleting a tweet here does not take its Discord post down: the shared
transport doesn't keep message ids.

Pinned by tests/test_tweets.py.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from . import discord_transport as transport
from .auth import require_any_role
from .constants import TWEETS_FILE
from .storage import _load_json, _save_json, log_write

router = APIRouter()
logger = logging.getLogger(__name__)

DISCORD_TWEETS_CHANNEL = os.environ.get("DISCORD_TWEETS_CHANNEL", "").strip()
SITE = "https://nbn.today"
# A game day is at most one batch of MAX_BATCH, plus the odd arena tweet.
MAX_BURST = 30
BURST_WINDOW = 900   # seconds

_lock = threading.Lock()

TEAMS = {
    "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BATCH = 20
MAX_LIMIT = 100


class TweetIn(BaseModel):
    handle: str
    name: str = Field(..., min_length=1, max_length=50)
    text: str = Field(..., min_length=1, max_length=280)
    # The game it is about, as [away, home]; empty for a tweet about the day.
    teams: list[str] = Field(default_factory=list, max_length=2)
    # Set when the account is the player himself, so the card shows his face.
    slug: Optional[str] = Field(None, max_length=80)
    # The fan account's team, for its colour. Optional for everyone else.
    team: Optional[str] = None
    likes: int = Field(0, ge=0, le=10_000_000)
    retweets: int = Field(0, ge=0, le=10_000_000)

    @field_validator("handle")
    @classmethod
    def _handle(cls, v):
        v = v.lstrip("@")
        if not HANDLE_RE.match(v):
            raise ValueError("handle is 1-15 letters, digits or underscores")
        return v

    @field_validator("teams")
    @classmethod
    def _teams(cls, v):
        v = [t.upper() for t in v]
        if any(t not in TEAMS for t in v):
            raise ValueError(f"unknown team in {v}")
        if len(v) == 1:
            raise ValueError("teams is [away, home], or empty")
        return v

    @field_validator("team")
    @classmethod
    def _team(cls, v):
        if v is None:
            return v
        v = v.upper()
        if v not in TEAMS:
            raise ValueError(f"unknown team {v!r}")
        return v


class TweetBatch(BaseModel):
    date: str  # the game day the batch is about
    tweets: list[TweetIn] = Field(..., min_length=1, max_length=MAX_BATCH)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("date is YYYY-MM-DD")
        return v


def _load_store() -> dict:
    return _load_json(TWEETS_FILE, {"seq": 0, "items": []})


def _save_store(store: dict):
    _save_json(TWEETS_FILE, store)


def _embed(item: dict) -> dict:
    """One tweet as a Discord embed: the account as the author line, the text
    as the body, likes and retweets in the footer, the team's colour."""
    from .discord import TEAM_COLORS, NBN_BLUE   # lazy: discord.py is a heavy import
    teams = item.get("teams") or []
    colour_team = item.get("team") or (teams[1] if teams else None)
    author = {"name": f"{item['name']} (@{item['handle']})"}
    if item.get("slug"):
        author["icon_url"] = f"{SITE}/api/players/{item['slug']}/headshot"
    embed = {
        "author": author,
        "description": item["text"],
        "color": TEAM_COLORS.get(colour_team, NBN_BLUE),
        "footer": {"text": f"\u2764 {item['likes']:,}   \U0001F501 {item['retweets']:,}   \u00b7 NBN Twitter"},
        "timestamp": item["created_at"],
    }
    if len(teams) == 2:
        embed["footer"]["text"] += f" \u00b7 {teams[0]} @ {teams[1]}"
    return embed


def _notify(items: list[dict]) -> None:
    """Post each tweet to Discord. Never raises; the tweets are already saved."""
    try:
        if not transport.configured(DISCORD_TWEETS_CHANNEL):
            return
        for item in items:
            transport.send(DISCORD_TWEETS_CHANNEL,
                           lambda item=item: {"embeds": [_embed(item)],
                                              "allowed_mentions": {"parse": []}},
                           max_burst=MAX_BURST, burst_window=BURST_WINDOW)
    except Exception as exc:
        logger.warning("Tweet notify failed: %s", exc)


def post_one(tweet: TweetIn, date: str, author: str, ref: str) -> dict:
    """Store one tweet written by the API itself, tagged with the event it is about."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        store = _load_store()
        store["seq"] += 1
        item = {"id": store["seq"], "date": date, "created_at": now,
                "author": author, "ref": ref, **tweet.model_dump()}
        store["items"].append(item)
        _save_store(store)
    _notify([item])
    return item


def delete_by_ref(ref: str) -> int:
    """Remove every tweet tagged `ref`; returns how many went."""
    with _lock:
        store = _load_store()
        keep = [t for t in store["items"] if t.get("ref") != ref]
        gone = len(store["items"]) - len(keep)
        if gone:
            store["items"] = keep
            _save_store(store)
    return gone


@router.get("/api/tweets")
def list_tweets(limit: int = Query(20, ge=1, le=MAX_LIMIT),
                before: Optional[int] = None,
                team: Optional[str] = None):
    items = _load_store()["items"]
    if before is not None:
        items = [t for t in items if t["id"] < before]
    if team:
        team = team.upper()
        items = [t for t in items if team in t["teams"] or t.get("team") == team]
    items = sorted(items, key=lambda t: t["id"], reverse=True)
    return items[:limit]


@router.post("/api/tweets")
def post_tweets(body: TweetBatch, info: dict = Depends(require_any_role("stats", "rosters"))):
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        store = _load_store()
        out = []
        for t in body.tweets:
            store["seq"] += 1
            out.append({"id": store["seq"], "date": body.date, "created_at": now,
                        "author": info["name"], **t.model_dump()})
        store["items"].extend(out)
        _save_store(store)
    _notify(out)
    log_write(info, f"POST tweets — {len(out)} for {body.date}")
    return out


@router.delete("/api/tweets/{tweet_id}")
def delete_tweet(tweet_id: int, info: dict = Depends(require_any_role("stats", "rosters"))):
    with _lock:
        store = _load_store()
        idx = next((i for i, t in enumerate(store["items"]) if t["id"] == tweet_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Tweet not found")
        store["items"].pop(idx)
        _save_store(store)
    log_write(info, f"DELETE tweets/{tweet_id}")
    return {"ok": True}

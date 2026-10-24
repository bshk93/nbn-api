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

Pinned by tests/test_tweets.py.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .auth import require_any_role
from .constants import TWEETS_FILE
from .storage import _load_json, _save_json, log_write

router = APIRouter()

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

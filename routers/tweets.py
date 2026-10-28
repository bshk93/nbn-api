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
    DELETE /api/tweets/{n}         stats or rosters, or the member who posted it

Members post too, one tweet at a time, as themselves or under an identity they
make up (a scoop account). An identity can be saved as a profile and picked
again later:

    POST   /api/tweets/member          member — {text, identity, profile_id?, handle?, name?, team?, save?}
    GET    /api/tweets/profiles        member — my own handle and my saved profiles
    POST   /api/tweets/profiles        member — save one {handle, name, team?}
    DELETE /api/tweets/profiles/{n}    the member who saved it, or admin
    GET    /api/tweets/{n}/poster      admin — who really posted a member tweet

Who really posted is stored on every member tweet (`poster`) and never leaves
the API except through that admin route; the public list drops it. A handle
belongs to whoever used it first (`_handle_owner`): the official cast, a
member's own handle, a saved profile, or a past tweet. Nobody else can post
under it, so a scoop account can't be impersonated and nobody can post as
@NBNStatMuse.

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

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from . import discord_transport as transport
from .auth import _resolve_token, get_token_info, has_role, load_members, require_admin, require_any_role
from .constants import TWEET_PROFILES_FILE, TWEETS_FILE
from .league_time import league_today_str
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
        "footer": {"text": (f"\u2764 {item['likes']:,}   \U0001F501 {item['retweets']:,}   \u00b7 NBN Twitter"
                            if item.get("likes") or item.get("retweets") else "NBN Twitter")},
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


# Fields that never leave the API on the public list: who really posted, and
# which member account an official batch came in on.
_PRIVATE = ("poster", "author", "profile_id")


def _public(item: dict, me: Optional[str]) -> dict:
    out = {k: v for k, v in item.items() if k not in _PRIVATE}
    if me and item.get("poster") == me:
        out["mine"] = True
    return out


@router.get("/api/tweets")
def list_tweets(limit: int = Query(20, ge=1, le=MAX_LIMIT),
                before: Optional[int] = None,
                team: Optional[str] = None,
                authorization: Optional[str] = Header(None)):
    items = _load_store()["items"]
    if before is not None:
        items = [t for t in items if t["id"] < before]
    if team:
        team = team.upper()
        items = [t for t in items if team in t["teams"] or t.get("team") == team]
    items = sorted(items, key=lambda t: t["id"], reverse=True)
    # Signed in, your own member tweets come back marked `mine`, so the card
    # can offer to delete them. Never an error: the list is public.
    me = None
    if authorization and authorization.startswith("Bearer "):
        me = (_resolve_token(authorization) or {}).get("name")
    return [_public(t, me) for t in items[:limit]]


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
def delete_tweet(tweet_id: int, info: dict = Depends(get_token_info)):
    with _lock:
        store = _load_store()
        idx = next((i for i, t in enumerate(store["items"]) if t["id"] == tweet_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Tweet not found")
        mine = store["items"][idx].get("poster") == info["name"]
        if not mine and not any(has_role(info, r) for r in ("stats", "rosters", "admin")):
            raise HTTPException(status_code=403, detail="Only the poster, stats or rosters can delete a tweet")
        store["items"].pop(idx)
        _save_store(store)
    log_write(info, f"DELETE tweets/{tweet_id}")
    return {"ok": True}


# ── Member tweets ─────────────────────────────────────────────────────────────

MEMBER_DAILY_LIMIT = 10     # tweets per member per rolling 24 hours
MAX_PROFILES = 10           # saved identities per member


def _load_profiles() -> dict:
    return _load_json(TWEET_PROFILES_FILE, {"seq": 0, "items": []})


def _save_profiles(store: dict):
    _save_json(TWEET_PROFILES_FILE, store)


def _self_handle(member: str) -> str:
    """A member's own handle: their name with anything Twitter wouldn't allow
    taken out, cut to 15."""
    return (re.sub(r"[^A-Za-z0-9_]", "", member) or "member")[:15]


def _handle_owner(handle: str, tweets: list[dict], profiles: list[dict]) -> Optional[str]:
    """Who a handle belongs to, case-insensitively: a member's name, or
    "official" for the cast the box score run and the API post as. None when
    it is free."""
    h = handle.lower()
    for name in load_members():
        if _self_handle(name).lower() == h:
            return name
    for p in profiles:
        if p["handle"].lower() == h:
            return p["owner"]
    for t in sorted(tweets, key=lambda t: t["id"]):
        if t["handle"].lower() == h:
            return t.get("poster") or "official"
    return None


def _member_team(name: str) -> Optional[str]:
    from .proposals import _member_current_team   # lazy, like the colours
    team = _member_current_team(name)
    return team.upper() if team and team.upper() in TEAMS else None


class Identity(BaseModel):
    handle: str
    name: str = Field(..., min_length=1, max_length=50)
    team: Optional[str] = None

    _handle = field_validator("handle")(TweetIn._handle.__func__)
    _team = field_validator("team")(TweetIn._team.__func__)


class MemberTweet(BaseModel):
    text: str = Field(..., min_length=1, max_length=280)
    # "self" posts as you; "profile" as one of your saved identities; "anon"
    # as a made-up one given here, saved as a profile too when `save` is set.
    identity: str = Field("self", pattern="^(self|profile|anon)$")
    profile_id: Optional[int] = None
    handle: Optional[str] = None
    name: Optional[str] = Field(None, max_length=50)
    team: Optional[str] = None
    save: bool = False


def _claim(handle: str, member: str, tweets: list[dict], profiles: list[dict]):
    owner = _handle_owner(handle, tweets, profiles)
    if owner and owner != member:
        raise HTTPException(status_code=409, detail=f"@{handle} is already taken")


@router.get("/api/tweets/profiles")
def my_profiles(info: dict = Depends(get_token_info)):
    me = info["name"]
    return {
        "self": {"handle": _self_handle(me), "name": me, "team": _member_team(me)},
        "profiles": [{k: p[k] for k in ("id", "handle", "name", "team")}
                     for p in _load_profiles()["items"] if p["owner"] == me],
    }


def _add_profile(ident: Identity, me: str, tweets: list[dict], profiles: dict) -> dict:
    """Save one identity into `profiles` (the caller saves the file)."""
    if sum(p["owner"] == me for p in profiles["items"]) >= MAX_PROFILES:
        raise HTTPException(status_code=400, detail=f"You can save up to {MAX_PROFILES} identities")
    _claim(ident.handle, me, tweets, profiles["items"])
    if any(p["owner"] == me and p["handle"].lower() == ident.handle.lower() for p in profiles["items"]):
        raise HTTPException(status_code=409, detail=f"You already saved @{ident.handle}")
    profiles["seq"] += 1
    p = {"id": profiles["seq"], "owner": me, "created_at": datetime.now(timezone.utc).isoformat(),
         **ident.model_dump()}
    profiles["items"].append(p)
    return p


@router.post("/api/tweets/profiles")
def save_profile(body: Identity, info: dict = Depends(get_token_info)):
    with _lock:
        profiles = _load_profiles()
        p = _add_profile(body, info["name"], _load_store()["items"], profiles)
        _save_profiles(profiles)
    log_write(info, f"POST tweets/profiles — @{p['handle']}")
    return {k: p[k] for k in ("id", "handle", "name", "team")}


@router.delete("/api/tweets/profiles/{profile_id}")
def delete_profile(profile_id: int, info: dict = Depends(get_token_info)):
    """Forget a saved identity. Its handle stays taken by any tweets already
    posted under it, so nobody else can pick it up and pass as you."""
    with _lock:
        profiles = _load_profiles()
        idx = next((i for i, p in enumerate(profiles["items"]) if p["id"] == profile_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Profile not found")
        if profiles["items"][idx]["owner"] != info["name"] and not has_role(info, "admin"):
            raise HTTPException(status_code=403, detail="Not your profile")
        profiles["items"].pop(idx)
        _save_profiles(profiles)
    log_write(info, f"DELETE tweets/profiles/{profile_id}")
    return {"ok": True}


@router.post("/api/tweets/member")
def post_member_tweet(body: MemberTweet, info: dict = Depends(get_token_info)):
    me = info["name"]
    now = datetime.now(timezone.utc)
    with _lock:
        store = _load_store()
        profiles = _load_profiles()
        day_ago = (now.timestamp() - 86400)
        recent = [t for t in store["items"] if t.get("poster") == me
                  and datetime.fromisoformat(t["created_at"]).timestamp() > day_ago]
        if len(recent) >= MEMBER_DAILY_LIMIT and not has_role(info, "admin"):
            raise HTTPException(status_code=429, detail=f"{MEMBER_DAILY_LIMIT} tweets a day — try again later")

        profile_id = None
        if body.identity == "self":
            ident = Identity(handle=_self_handle(me), name=me, team=_member_team(me))
        elif body.identity == "profile":
            p = next((p for p in profiles["items"] if p["id"] == body.profile_id and p["owner"] == me), None)
            if p is None:
                raise HTTPException(status_code=404, detail="Profile not found")
            ident, profile_id = Identity(handle=p["handle"], name=p["name"], team=p.get("team")), p["id"]
        else:
            if not body.handle or not body.name:
                raise HTTPException(status_code=422, detail="A made-up identity needs a handle and a name")
            try:
                ident = Identity(handle=body.handle, name=body.name, team=body.team)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc).splitlines()[-1])
            _claim(ident.handle, me, store["items"], profiles["items"])
            if body.save:
                profile_id = _add_profile(ident, me, store["items"], profiles)["id"]
                _save_profiles(profiles)

        store["seq"] += 1
        item = {"id": store["seq"], "date": league_today_str(), "created_at": now.isoformat(),
                "poster": me, "source": "member", "identity": body.identity,
                "profile_id": profile_id, "handle": ident.handle, "name": ident.name,
                "text": body.text, "teams": [], "slug": None, "team": ident.team,
                "likes": 0, "retweets": 0}
        store["items"].append(item)
        _save_store(store)
    _notify([item])
    log_write(info, f"POST tweets/member — #{item['id']} as @{item['handle']}")
    return _public(item, me)


@router.get("/api/tweets/{tweet_id}/poster")
def tweet_poster(tweet_id: int, _: dict = Depends(require_admin)):
    t = next((t for t in _load_store()["items"] if t["id"] == tweet_id), None)
    if t is None:
        raise HTTPException(status_code=404, detail="Tweet not found")
    return {"id": t["id"], "handle": t["handle"], "poster": t.get("poster") or t.get("author"),
            "identity": t.get("identity") or "official"}

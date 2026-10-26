"""Arena naming rights — members bid NB¥ to name any team's arena.

Flavor with a real price. The name shows on that team's box scores (the
attendance line) from the day it is bought, on `/finances` and on the team
page. It confers nothing else: no cap, no rule, no ledger revenue.

The rules, settled with the user 2026-10-26:

- **Any member can bid on any arena**, including one they already hold (that
  is how you rename it). Rivals naming your arena is the point.
- **A bid has to beat the standing one**: at least `RAISE_PCT` percent over it, rounded up
  to a whole NB¥; `OPENING_BID` for an arena nobody has bought.
- **The price only goes up.** No decay, no reset at the rollover.
- **An outbid holder's NB¥ is gone.** They paid for the time they held it, so
  every bid is a sink.
- **Live at once; an admin can pull a name.** Pulling refunds that bid in full
  and the arena goes back to the name before it, at that name's price — as if
  the pulled bid never happened.

Stored in `arena-names.json` in NBS_DATA_DIR: `{TEAM: [entry, ...]}`, oldest
first, each entry `{name, by, bid, at}` plus `pulled: {by, at, reason}` once
pulled. Nothing is deleted, so the ledger can show the name a game was played
under.

    GET    /api/arenas            public: every team's arena and the price to beat
    GET    /api/arenas/{team}     public: one team's, with its full history
    POST   /api/arenas/{team}     member token: {name, bid}
    DELETE /api/arenas/{team}     admin: pull the standing name, {reason}

Pinned by tests/test_arenas.py.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from stats_build import finances as fin

from . import inbox, wallet
from .auth import get_token_info, has_role, require_role
from .constants import DATA_DIR
from .storage import _load_json, _save_json, log_write

router = APIRouter()

ARENAS_FILE = DATA_DIR / "arena-names.json"
OPENING_BID = 100
RAISE_PCT = 10
NAME_MIN, NAME_MAX = 3, 40

TEAMS = {
    "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}
_LINK = "/finances/#arenas"
_URLISH = re.compile(r"://|\bwww\.|\.(com|net|org|gg|tv|io|ly)\b", re.I)

_lock = threading.Lock()


class BidIn(BaseModel):
    name: str = Field(..., max_length=200)
    bid: int = Field(..., ge=1, le=10_000_000)


class PullIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)


def clean_name(raw: str) -> str:
    """One line, spaces collapsed, `NAME_MIN`-`NAME_MAX` characters, no links."""
    name = " ".join(raw.split())
    if any(ord(c) < 32 for c in name):
        raise HTTPException(status_code=422, detail="The name can't contain control characters")
    if not NAME_MIN <= len(name) <= NAME_MAX:
        raise HTTPException(status_code=422,
                            detail=f"An arena name is {NAME_MIN}-{NAME_MAX} characters")
    if _URLISH.search(name):
        raise HTTPException(status_code=422, detail="No links in an arena name")
    return name


def standing(history: list[dict]) -> dict | None:
    """The name in force: the newest entry that wasn't pulled."""
    for e in reversed(history):
        if not e.get("pulled"):
            return e
    return None


def to_beat(history: list[dict]) -> int:
    cur = standing(history)
    if not cur:
        return OPENING_BID
    return cur["bid"] + max(1, -(-cur["bid"] * RAISE_PCT // 100))


def _rebuild() -> None:
    """Rebuild so the box scores carry the new name now, not after the next
    game is entered. Imported here: boxscores is a heavy module to load."""
    from .boxscores import _trigger_build
    _trigger_build()


def _load() -> dict:
    return _load_json(ARENAS_FILE, {})


def _team(team: str) -> str:
    t = team.upper()
    if t not in TEAMS:
        raise HTTPException(status_code=404, detail=f"No team {team!r}")
    return t


def _view(t: str, history: list[dict], defaults: dict, full: bool = False) -> dict:
    cur = standing(history)
    out = {
        "team": t,
        "name": cur["name"] if cur else defaults.get(t),
        "default": defaults.get(t),
        "bought": bool(cur),
        "by": cur["by"] if cur else None,
        "bid": cur["bid"] if cur else None,
        "since": cur["at"] if cur else None,
        "to_beat": to_beat(history),
    }
    if full:
        out["history"] = history
    return out


@router.get("/api/arenas")
def get_arenas():
    data, defaults = _load(), fin.home_arenas(DATA_DIR)
    return {"opening_bid": OPENING_BID, "raise_pct": RAISE_PCT,
            "teams": [_view(t, data.get(t, []), defaults) for t in sorted(TEAMS)]}


@router.get("/api/arenas/{team}")
def get_arena(team: str):
    t = _team(team)
    return _view(t, _load().get(t, []), fin.home_arenas(DATA_DIR), full=True)


@router.post("/api/arenas/{team}")
def bid(team: str, body: BidIn, info: dict = Depends(get_token_info)):
    member = info.get("name")
    if not member:
        raise HTTPException(status_code=401, detail="Sign in to bid")
    t = _team(team)
    name = clean_name(body.name)
    with _lock, wallet.lock:
        data = _load()
        history = data.setdefault(t, [])
        need = to_beat(history)
        if body.bid < need:
            raise HTTPException(status_code=409,
                                detail=f"The bid to beat is NB¥{need:,}")
        prev = standing(history)
        ref = f"arena:{t}:{len(history)}"
        new_balance = wallet.debit(member, body.bid, "arena_bid",
                                   f"Naming rights: {t} arena, \"{name}\"", ref=ref)
        history.append({"name": name, "by": member, "bid": body.bid,
                        "at": datetime.now(timezone.utc).isoformat()})
        try:
            _save_json(ARENAS_FILE, data)
        except Exception:
            wallet.credit(member, body.bid, "arena_refund",
                          f"Refund — naming rights bid failed to save: {t}", ref=ref)
            raise
    log_write(info, f"POST arenas/{t} — \"{name}\" for NB¥{body.bid:,}")
    _rebuild()
    if prev and prev["by"] != member:
        inbox.notify_member(prev["by"], f"{member} outbid you on {t}'s arena (NB¥{body.bid:,}). "
                                        f"It's now the {name}.", _LINK)
    if not has_role(info, t.lower()):
        inbox.notify_team(t, f"{member} bought the naming rights to your arena for "
                             f"NB¥{body.bid:,}. It's now the {name}.", _LINK)
    return {**_view(t, history, fin.home_arenas(DATA_DIR)), "new_balance": new_balance}


@router.delete("/api/arenas/{team}")
def pull(team: str, body: PullIn, info: dict = Depends(require_role("admin"))):
    t = _team(team)
    with _lock, wallet.lock:
        data = _load()
        history = data.get(t, [])
        cur = standing(history)
        if not cur:
            raise HTTPException(status_code=404, detail=f"{t}'s arena has no bought name")
        i = max(i for i, e in enumerate(history) if e is cur)
        wallet.credit(cur["by"], cur["bid"], "arena_refund",
                      f"Refund — {t} arena name \"{cur['name']}\" pulled", ref=f"arena:{t}:{i}")
        cur["pulled"] = {"by": info.get("name"), "at": datetime.now(timezone.utc).isoformat(),
                         "reason": body.reason.strip()}
        _save_json(ARENAS_FILE, data)
    _rebuild()
    log_write(info, f"DELETE arenas/{t} — pulled \"{cur['name']}\" ({cur['by']}): {body.reason}")
    inbox.notify_member(cur["by"], f"An admin pulled your name for {t}'s arena (\"{cur['name']}\"): "
                                   f"{body.reason.strip()}. NB¥{cur['bid']:,} refunded.", _LINK)
    return _view(t, history, fin.home_arenas(DATA_DIR))

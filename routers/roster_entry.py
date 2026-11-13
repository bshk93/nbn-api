"""Roster moves a streamer still has to make in 2K.

The league's 2K roster file is kept by hand. Every transaction that moves a
player on or off a roster (a trade, a signing, a release, a waiver claim) has
to be repeated there, and until now nothing said which ones had been. This
reads them off the transaction ledger and keeps a tick per transaction.

What counts is `needs_2k_entry`: a move of a player, not a contract change.
An extension, an option or a guarantee leaves every roster as it was, so it
never shows. A picks-only trade doesn't either.

- GET /api/roster-entry: public, like the ledger it reads. Every counting
  transaction since `SINCE`, oldest first (the order they have to be entered:
  a release before the signing it made room for), each with `done` null or
  {by, at}.
- PUT /api/roster-entry/{txn_id}: `streamer` or `updates`. Body {done}.

Stored in `roster-entry.json` as {txn_id: {by, at}}. The ledger is never
written.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import ROSTER_ENTRY_FILE, TRANSACTIONS_FILE, _roster_entry_lock
from .storage import _load_json, _save_json, log_write
from .auth import require_any_role

router = APIRouter()

# The queue started on 2026-11-13. Transactions from earlier in the month are
# included so the streamer can confirm the recent ones; anything older is
# taken as already in 2K.
SINCE = "2026-11-01"

# Types that always move a player on or off a roster, or change his roster
# slot (a two-way conversion). A signing of a player who was already on the
# team still counts: 2K holds the contract too.
_ALWAYS = {"sign", "sign_pick", "accept_qo", "release", "void_player", "convert_twoway"}


def needs_2k_entry(txn: dict) -> bool:
    if txn.get("voided"):
        return False
    ttype = txn.get("type")
    d = txn.get("details") or {}
    if ttype in _ALWAYS:
        return True
    if ttype == "trade":
        # A backfilled trade was in 2K long before the ledger had it.
        if d.get("historical"):
            return False
        return any(a.get("type") == "player"
                   for tr in d.get("transfers") or [] for a in tr.get("assets") or [])
    if ttype == "waiver_clear":
        # Unclaimed, he just stays a free agent: the release already moved him.
        return d.get("outcome") == "claimed"
    if ttype == "offer_sheet_decision":
        return d.get("outcome") != "matched"
    return False


def _queue(ledger: list[dict]) -> list[dict]:
    rows = [t for t in ledger if (t.get("created_at") or "") >= SINCE and needs_2k_entry(t)]
    # A release written together with a signing (to make room for it) has to
    # go into 2K first. Otherwise ledger order, since the sort is stable.
    return sorted(rows, key=lambda t: (t.get("created_at") or "",
                                       t.get("type") not in ("release", "void_player")))


def _load_ledger() -> list[dict]:
    return _load_json(TRANSACTIONS_FILE, [])


def _load_done() -> dict:
    return _load_json(ROSTER_ENTRY_FILE, {})


@router.get("/api/roster-entry")
def get_roster_entry():
    done = _load_done()
    items = [{
        "id": t.get("id"),
        "type": t.get("type"),
        "date": t.get("date"),
        "created_at": t.get("created_at"),
        "description": t.get("description"),
        "details": t.get("details") or {},
        "done": done.get(t.get("id")),
    } for t in _queue(_load_ledger())]
    return {"since": SINCE, "items": items}


class DoneBody(BaseModel):
    done: bool


@router.put("/api/roster-entry/{txn_id}")
def set_roster_entry(txn_id: str, body: DoneBody,
                     info: dict = Depends(require_any_role("streamer", "updates"))):
    """Tick (or untick) one transaction as entered into 2K. Refuses an id
    that isn't in the queue, so a tick can't sit where nothing shows it."""
    if not any(t.get("id") == txn_id for t in _queue(_load_ledger())):
        raise HTTPException(status_code=404, detail=f"No roster move {txn_id} to enter")
    with _roster_entry_lock:
        data = _load_done()
        if body.done:
            data[txn_id] = {"by": info.get("name"),
                            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        else:
            data.pop(txn_id, None)
        _save_json(ROSTER_ENTRY_FILE, data)
    log_write(info, f"roster move {txn_id} {'marked' if body.done else 'unmarked'} entered in 2K")
    return {"id": txn_id, "done": data.get(txn_id)}

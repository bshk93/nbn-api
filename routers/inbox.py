import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from .constants import INBOX_FILE
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, load_members

router = APIRouter()

_inbox_lock = threading.Lock()

# Bounds are deliberately generous — even at max, a member's inbox is a few
# hundred KB. The point of both is keeping the file from becoming an
# unbounded message log, not disk space.
RETENTION_DAYS = 90
MAX_PER_MEMBER = 500


def _load_store() -> dict:
    return _load_json(INBOX_FILE, {})


def _save_store(store: dict):
    _save_json(INBOX_FILE, store)


def _prune(items: list[dict]) -> list[dict]:
    """Drop *read* entries older than RETENTION_DAYS, then hard-cap at
    MAX_PER_MEMBER most recent regardless of read state. An unread entry never
    ages out on its own — an unactioned remand shouldn't silently vanish just
    because nobody looked at it in three months."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    items = [it for it in items if not it["read"] or it["ts"] > cutoff]
    return items[-MAX_PER_MEMBER:]


def notify_member(username: str, text: str, link: Optional[str] = None) -> None:
    """Append one notification to a member's inbox. Called in-process from
    wherever an event already exists (remand, void, a suggestion's status
    change, ...) — there is no HTTP path that fires this on its own, the same
    way discord_notify/fa_notify are called inline rather than polled.

    Pruning happens here, on write, rather than on a timer: an inbox only
    grows when something is appended to it, so checking the bound at that
    moment keeps the file bounded without a scheduled job."""
    now = datetime.now(timezone.utc).isoformat()
    entry = {"id": secrets.token_hex(8), "ts": now, "text": text, "link": link, "read": False}
    with _inbox_lock:
        store = _load_store()
        items = store.get(username, [])
        items.append(entry)
        store[username] = _prune(items)
        _save_store(store)


def notify_team(team: str, text: str, link: Optional[str] = None) -> None:
    """Same as notify_member, but for events that are a team's business
    generally rather than one member's — an office-entered transaction (an
    offer sheet, say) has no `submitted_by` the way a self-serve PDC offer
    does, so there's no single actor to address. Delivered to every current
    holder of the team's role (member roles use the lowercase team abbr, same
    as every other team-role check in this codebase)."""
    role = team.lower()
    for name, m in load_members().items():
        if role in (m.get("roles") or []):
            notify_member(name, text, link)


@router.get("/api/inbox")
def get_inbox(info: dict = Depends(get_token_info)):
    with _inbox_lock:
        items = _load_store().get(info["name"], [])
    items = sorted(items, key=lambda it: it["ts"], reverse=True)
    unread_count = sum(1 for it in items if not it["read"])
    return {"unread_count": unread_count, "items": items}


@router.post("/api/inbox/{item_id}/read")
def mark_read(item_id: str, info: dict = Depends(get_token_info)):
    with _inbox_lock:
        store = _load_store()
        items = store.get(info["name"], [])
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        items[idx]["read"] = True
        store[info["name"]] = items
        _save_store(store)
    return {"ok": True}


@router.post("/api/inbox/read-all")
def mark_all_read(info: dict = Depends(get_token_info)):
    with _inbox_lock:
        store = _load_store()
        items = store.get(info["name"], [])
        for it in items:
            it["read"] = True
        store[info["name"]] = items
        _save_store(store)
    log_write(info, "POST inbox/read-all")
    return {"ok": True}

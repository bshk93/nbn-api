import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

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


def notify_member(username: str, text: str, link: Optional[str] = None,
                  actions: Optional[list[dict]] = None, batch: Optional[str] = None) -> None:
    """Append one notification to a member's inbox. Called in-process from
    wherever an event already exists (remand, void, a suggestion's status
    change, ...) — there is no HTTP path that fires this on its own, the same
    way discord_notify/fa_notify are called inline rather than polled.

    `actions` puts buttons on the message — see `register_action`; actions
    sharing a `group` are alternatives, and one done closes the rest. `batch`
    ties together the copies of one message that notify_team/notify_roles
    hand to several members, so an action taken from one copy reads as done
    on all of them.

    Pruning happens here, on write, rather than on a timer: an inbox only
    grows when something is appended to it, so checking the bound at that
    moment keeps the file bounded without a scheduled job."""
    now = datetime.now(timezone.utc).isoformat()
    entry = {"id": secrets.token_hex(8), "ts": now, "text": text, "link": link, "read": False}
    if actions:
        entry["actions"] = [dict(a) for a in actions]
    if batch:
        entry["batch"] = batch
    with _inbox_lock:
        store = _load_store()
        items = store.get(username, [])
        items.append(entry)
        store[username] = _prune(items)
        _save_store(store)


def notify_team(team: str, text: str, link: Optional[str] = None,
                actions: Optional[list[dict]] = None) -> None:
    """Same as notify_member, but for events that are a team's business
    generally rather than one member's — an office-entered transaction (an
    offer sheet, say) has no `submitted_by` the way a self-serve PDC offer
    does, so there's no single actor to address. Delivered to every current
    holder of the team's role (member roles use the lowercase team abbr, same
    as every other team-role check in this codebase)."""
    role = team.lower()
    batch = secrets.token_hex(8) if actions else None
    for name, m in load_members().items():
        if role in (m.get("roles") or []):
            notify_member(name, text, link, actions=actions, batch=batch)


def notify_role(role: str, text: str, link: Optional[str] = None) -> None:
    """Same as notify_team, generalized to any role — a committee-wide event
    (e.g. an extension proposal reaching the queue, scoped to `poext` holders
    per nbn-today/docs/poext-extension-pipeline.md D13) that's every holder of
    that role's business, not one team's or one member's."""
    notify_roles([role], text, link)


def notify_roles(roles: list[str], text: str, link: Optional[str] = None) -> None:
    """notify_role for several roles at once, delivered once per member — a
    member holding two of them gets one notification, not two. Roles are
    matched literally, not through ROLE_IMPLIES, so name every role that
    should hear it."""
    wanted = set(roles)
    for name, m in load_members().items():
        if wanted & set(m.get("roles") or []):
            notify_member(name, text, link)


# ── Message actions ───────────────────────────────────────────────────────────
#
# A message's `actions` are data, not URLs: each is `{"kind", "label", ...}`
# and the kind names a handler registered here by the module that owns the
# thing being acted on. The page never chooses what gets called, so a button
# can only ever do what its kind's handler does, and the handler checks the
# clicking member's permission itself — being sent a message grants nothing.

_ACTION_HANDLERS: dict[str, Callable[[dict, dict], str]] = {}


def register_action(kind: str, handler: Callable[[dict, dict], str]) -> None:
    """`handler(info, action)` performs the action for the member in `info`
    and returns the label the done button should show. It raises
    HTTPException to refuse — the action then stays undone."""
    _ACTION_HANDLERS[kind] = handler


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


@router.post("/api/inbox/{item_id}/actions/{index}")
def run_action(item_id: str, index: int, info: dict = Depends(get_token_info)):
    """Run one of a message's action buttons. Once done it stays done, on every
    copy of the message (see `batch` in notify_member), and a second click is a
    no-op that returns the same result."""
    with _inbox_lock:
        items = _load_store().get(info["name"], [])
        item = next((it for it in items if it["id"] == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    actions = item.get("actions") or []
    if not 0 <= index < len(actions):
        raise HTTPException(status_code=404, detail="No such action")
    action = actions[index]
    if action.get("done_at"):
        return {"ok": True, "action": action}
    # Actions sharing a `group` are alternatives — "remove him" or "he's still
    # available" — so once one is done the others are closed.
    if action.get("group") and any(a.get("done_at") and a.get("group") == action["group"] for a in actions):
        raise HTTPException(status_code=409, detail="Already answered")
    handler = _ACTION_HANDLERS.get(action.get("kind"))
    if handler is None:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action.get('kind')}'")

    # Outside the inbox lock: the handler writes its own files under its own
    # rules, and nothing here depends on the inbox staying still meanwhile.
    done_label = handler(info, action)

    done = {"done_at": datetime.now(timezone.utc).isoformat(),
            "done_by": info["name"], "done_label": done_label}
    with _inbox_lock:
        store = _load_store()
        for member, its in store.items():
            for it in its:
                same = it["id"] == item_id or (item.get("batch") and it.get("batch") == item["batch"])
                if same and index < len(it.get("actions") or []):
                    it["actions"][index].update(done)
                    if member == info["name"]:
                        it["read"] = True
        _save_store(store)
    log_write(info, f"POST inbox action {action.get('kind')} — {done_label}")
    return {"ok": True, "action": {**action, **done}}

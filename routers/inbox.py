import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel

from .constants import INBOX_FILE, INBOX_PREFS_FILE
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, load_members

router = APIRouter()

_inbox_lock = threading.Lock()

# Bounds are deliberately generous — even at max, a member's inbox is a few
# hundred KB. The point of both is keeping the file from becoming an
# unbounded message log, not disk space.
RETENTION_DAYS = 90
MAX_PER_MEMBER = 500

# What a message is about, for the inbox page's filters, stripes and mutes.
# Ordered as the page shows them; the label is the chip's text.
KINDS: dict[str, str] = {
    "fa": "Free agency",
    "extensions": "Extensions",
    "trades": "Trades",
    "tradeblock": "Trade block",
    "transactions": "Transactions",
    "games": "Games & streams",
    "news": "News",
    "suggestions": "Suggestions",
    "finances": "Finances",
    "committees": "Committees",
    "account": "Account",
    "other": "Other",
}

# Link prefix → kind, first match wins (so the specific ones come first). A
# caller passes `kind=` only where its link would read as something else —
# every entry stored before `kind` existed is classified this same way on
# read, so old and new messages filter alike.
_LINK_KINDS: list[tuple[str, str]] = [
    ("/committees/pdc#poext", "extensions"),
    ("/committees/pdc", "fa"),
    ("/pdc", "fa"),
    ("/free-agency", "fa"),
    ("/extensions", "extensions"),
    ("/committees/trc", "trades"),
    ("/tradeblock", "tradeblock"),
    ("/transactions", "transactions"),
    ("/api/transactions", "transactions"),
    ("/committees/stream", "games"),
    ("/calendar", "games"),
    ("/schedule", "games"),
    ("/news", "news"),
    ("/suggestions", "suggestions"),
    ("/finances", "finances"),
    ("/committees", "committees"),
    ("/members", "account"),
]


def kind_for_link(link: Optional[str]) -> str:
    if not link:
        return "other"
    if link.startswith("/teams/"):
        # A team page is where team options and holds land (transactions.py);
        # its #coaching anchor is the game-day handoff (game_day_notify.py).
        return "games" if "#coaching" in link else "transactions"
    for prefix, kind in _LINK_KINDS:
        if link.startswith(prefix):
            return kind
    return "other"


def _kind_of(it: dict) -> str:
    k = it.get("kind")
    return k if k in KINDS else kind_for_link(it.get("link"))


def _load_prefs() -> dict:
    return _load_json(INBOX_PREFS_FILE, {})


def _save_prefs(prefs: dict):
    _save_json(INBOX_PREFS_FILE, prefs)


def muted_kinds(username: str) -> set[str]:
    return set((_load_prefs().get(username) or {}).get("muted") or [])


def _load_store() -> dict:
    return _load_json(INBOX_FILE, {})


def _save_store(store: dict):
    _save_json(INBOX_FILE, store)


def _prune(items: list[dict]) -> list[dict]:
    """Drop *read* entries older than RETENTION_DAYS, then hard-cap at
    MAX_PER_MEMBER most recent regardless of read state. An unread entry never
    ages out on its own — an unactioned remand shouldn't silently vanish just
    because nobody looked at it in three months. Archived counts as read: the
    member has dealt with it."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    items = [it for it in items
             if (not it["read"] and not it.get("archived")) or it["ts"] > cutoff]
    return items[-MAX_PER_MEMBER:]


def notify_member(username: str, text: str, link: Optional[str] = None,
                  actions: Optional[list[dict]] = None, batch: Optional[str] = None,
                  kind: Optional[str] = None) -> None:
    """Append one notification to a member's inbox. Called in-process from
    wherever an event already exists (remand, void, a suggestion's status
    change, ...) — there is no HTTP path that fires this on its own, the same
    way discord_notify/fa_notify are called inline rather than polled.

    `kind` files the message under one of KINDS; left out, it is read off the
    link (kind_for_link). A member who muted that kind doesn't get it — unless
    it carries actions, since a button nobody sees is a decision nobody makes.

    `actions` puts buttons on the message — see `register_action`; actions
    sharing a `group` are alternatives, and one done closes the rest. `batch`
    ties together the copies of one message that notify_team/notify_roles
    hand to several members, so an action taken from one copy reads as done
    on all of them.

    Pruning happens here, on write, rather than on a timer: an inbox only
    grows when something is appended to it, so checking the bound at that
    moment keeps the file bounded without a scheduled job."""
    kind = kind if kind in KINDS else kind_for_link(link)
    if not actions and kind in muted_kinds(username):
        return
    now = datetime.now(timezone.utc).isoformat()
    entry = {"id": secrets.token_hex(8), "ts": now, "text": text, "link": link, "read": False,
             "kind": kind}
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
                actions: Optional[list[dict]] = None, kind: Optional[str] = None) -> None:
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
            notify_member(name, text, link, actions=actions, batch=batch, kind=kind)


def notify_role(role: str, text: str, link: Optional[str] = None,
                kind: Optional[str] = None) -> None:
    """Same as notify_team, generalized to any role — a committee-wide event
    (e.g. an extension proposal reaching the queue, scoped to `poext` holders
    per nbn-today/docs/poext-extension-pipeline.md D13) that's every holder of
    that role's business, not one team's or one member's."""
    notify_roles([role], text, link, kind=kind)


def notify_roles(roles: list[str], text: str, link: Optional[str] = None,
                 kind: Optional[str] = None) -> None:
    """notify_role for several roles at once, delivered once per member — a
    member holding two of them gets one notification, not two. Roles are
    matched literally, not through ROLE_IMPLIES, so name every role that
    should hear it."""
    wanted = set(roles)
    for name, m in load_members().items():
        if wanted & set(m.get("roles") or []):
            notify_member(name, text, link, kind=kind)


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
    items = sorted(({**it, "kind": _kind_of(it)} for it in items), key=lambda it: it["ts"], reverse=True)
    unread_count = sum(1 for it in items if not it["read"] and not it.get("archived"))
    return {"unread_count": unread_count, "items": items,
            "kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
            "muted": sorted(muted_kinds(info["name"]))}


def _set_flag(info: dict, item_id: str, **flags) -> dict:
    with _inbox_lock:
        store = _load_store()
        items = store.get(info["name"], [])
        it = next((it for it in items if it["id"] == item_id), None)
        if it is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        it.update(flags)
        store[info["name"]] = items
        _save_store(store)
    return {"ok": True}


@router.post("/api/inbox/{item_id}/read")
def mark_read(item_id: str, info: dict = Depends(get_token_info)):
    return _set_flag(info, item_id, read=True)


@router.post("/api/inbox/{item_id}/unread")
def mark_unread(item_id: str, info: dict = Depends(get_token_info)):
    return _set_flag(info, item_id, read=False)


@router.post("/api/inbox/{item_id}/archive")
def archive(item_id: str, info: dict = Depends(get_token_info)):
    """Out of the way, not gone: an archived message is read, hidden from the
    default view, and ages out like any read one."""
    return _set_flag(info, item_id, archived=True, read=True)


@router.post("/api/inbox/{item_id}/unarchive")
def unarchive(item_id: str, info: dict = Depends(get_token_info)):
    return _set_flag(info, item_id, archived=False)


@router.post("/api/inbox/read-all")
def mark_all_read(info: dict = Depends(get_token_info), kind: Optional[str] = None):
    """Every message, or with `?kind=` only that kind's — so "Mark all read"
    on a filtered view clears what the member is looking at, not the rest."""
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind '{kind}'")
    with _inbox_lock:
        store = _load_store()
        items = store.get(info["name"], [])
        for it in items:
            if kind is None or _kind_of(it) == kind:
                it["read"] = True
        store[info["name"]] = items
        _save_store(store)
    log_write(info, "POST inbox/read-all" + (f"?kind={kind}" if kind else ""))
    return {"ok": True}


class BulkBody(BaseModel):
    ids: list[str]
    op: str


_BULK_OPS = {
    "read": {"read": True},
    "unread": {"read": False},
    "archive": {"archived": True, "read": True},
    "unarchive": {"archived": False},
}


@router.post("/api/inbox/bulk")
def bulk(body: BulkBody, info: dict = Depends(get_token_info)):
    """One op over several messages. Ids the member doesn't have are skipped
    rather than failing the lot — a message can age out between the page
    loading and the click."""
    flags = _BULK_OPS.get(body.op)
    if flags is None:
        raise HTTPException(status_code=400, detail=f"Unknown op '{body.op}'")
    wanted = set(body.ids)
    with _inbox_lock:
        store = _load_store()
        items = store.get(info["name"], [])
        n = 0
        for it in items:
            if it["id"] in wanted:
                it.update(flags)
                n += 1
        store[info["name"]] = items
        _save_store(store)
    log_write(info, f"POST inbox/bulk {body.op} ×{n}")
    return {"ok": True, "updated": n}


class PrefsBody(BaseModel):
    muted: list[str]


@router.get("/api/inbox/prefs")
def get_prefs(info: dict = Depends(get_token_info)):
    return {"muted": sorted(muted_kinds(info["name"])),
            "kinds": [{"id": k, "label": v} for k, v in KINDS.items()]}


@router.put("/api/inbox/prefs")
def put_prefs(body: PrefsBody, info: dict = Depends(get_token_info)):
    """Mute kinds of message. A muted message is never written, so unmuting
    doesn't bring back what was missed; messages with buttons always arrive."""
    bad = [k for k in body.muted if k not in KINDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown kind(s): {', '.join(bad)}")
    with _inbox_lock:
        prefs = _load_prefs()
        prefs[info["name"]] = {**(prefs.get(info["name"]) or {}), "muted": sorted(set(body.muted))}
        _save_prefs(prefs)
    log_write(info, f"PUT inbox/prefs muted={','.join(sorted(set(body.muted))) or '-'}")
    return {"ok": True, "muted": sorted(set(body.muted))}


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

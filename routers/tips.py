import secrets
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import TIPS_FILE, logger
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, load_members
from .bets import _load_balances, _save_balances, _init_bal, _append_ledger, _balances_lock

router = APIRouter()

_tips_lock = threading.Lock()
TIP_MESSAGE_THRESHOLD = 25.0


class TipIn(BaseModel):
    to: str
    amount: float
    message: str = ""
    context: str | None = None


class TipMultiIn(BaseModel):
    to: list[str]
    amount: float
    message: str = ""
    context: str | None = None


class TipError(ValueError):
    """Raised by perform_tip on a rejected tip (bad amount, self, unknown
    member, insufficient funds). Callers map this to their own error shape."""


def perform_tip(sender: str, to: str, amount: float, message: str = "",
                 context: str | None = None) -> dict:
    """Move NB¥ from `sender` to `to`, record the tip + ledger entries, and
    return balances. The single source of truth for tipping — used by both the
    HTTP endpoint and the Discord /tip command. Raises TipError on rejection."""
    if amount <= 0:
        raise TipError("amount must be positive")
    if sender == to:
        raise TipError("Cannot tip yourself")
    all_members = load_members()
    if sender not in all_members:
        raise TipError(f"Member '{sender}' not found")
    if to not in all_members:
        raise TipError(f"Member '{to}' not found")

    with _tips_lock, _balances_lock:
        balances = _load_balances()
        _init_bal(balances, sender)
        _init_bal(balances, to)
        if balances[sender] < amount:
            raise TipError(f"Insufficient balance — NB¥{balances[sender]:.2f} available")
        balances[sender] = round(balances[sender] - amount, 2)
        balances[to]     = round(balances[to]     + amount, 2)
        sender_bal    = balances[sender]
        recipient_bal = balances[to]
        _save_balances(balances)

        tips = _load_json(TIPS_FILE, [])
        msg = message.strip() if amount >= TIP_MESSAGE_THRESHOLD else ""
        tip = {
            "id":      secrets.token_hex(6),
            "from":    sender,
            "to":      to,
            "amount":  amount,
            "message": msg,
            "ts":      datetime.now(timezone.utc).isoformat(),
        }
        if context:
            tip["context"] = context
        tips.append(tip)
        _save_json(TIPS_FILE, tips)

    _append_ledger([
        {"ts": tip["ts"], "member": sender, "delta": -amount,
         "balance": sender_bal,    "reason": f"Tip to {to}"},
        {"ts": tip["ts"], "member": to,     "delta":  amount,
         "balance": recipient_bal, "reason": f"Tip from {sender}"},
    ])
    return {"sender_bal": sender_bal, "recipient_bal": recipient_bal, "tip": tip}


def perform_tip_multi(sender: str, to: list[str], amount: float, message: str = "",
                       context: str | None = None) -> dict:
    """Tip several recipients the same amount each in one all-or-nothing
    transfer — for a piece with more than one credited author (a power
    rankings edition's voters and blurb writers), where "any or all of them"
    needs the whole cost checked up front rather than draining the sender
    partway through a loop of single perform_tip() calls."""
    to = list(dict.fromkeys(to))  # dedupe, preserve order
    if not to:
        raise TipError("Pick at least one recipient")
    if amount <= 0:
        raise TipError("amount must be positive")
    if sender in to:
        raise TipError("Cannot tip yourself")
    all_members = load_members()
    if sender not in all_members:
        raise TipError(f"Member '{sender}' not found")
    unknown = [m for m in to if m not in all_members]
    if unknown:
        raise TipError(f"Member(s) not found: {', '.join(unknown)}")

    total = round(amount * len(to), 2)
    with _tips_lock, _balances_lock:
        balances = _load_balances()
        _init_bal(balances, sender)
        for m in to:
            _init_bal(balances, m)
        if balances[sender] < total:
            raise TipError(f"Insufficient balance — NB¥{balances[sender]:.2f} available, "
                            f"need NB¥{total:.2f} for {len(to)} recipients")
        balances[sender] = round(balances[sender] - total, 2)
        sender_bal = balances[sender]

        ts = datetime.now(timezone.utc).isoformat()
        msg = message.strip() if amount >= TIP_MESSAGE_THRESHOLD else ""
        tips = _load_json(TIPS_FILE, [])
        new_tips = []
        ledger = [{"ts": ts, "member": sender, "delta": -total, "balance": sender_bal,
                   "reason": f"Tip to {', '.join(to)}"}]
        for m in to:
            balances[m] = round(balances[m] + amount, 2)
            tip = {"id": secrets.token_hex(6), "from": sender, "to": m,
                   "amount": amount, "message": msg, "ts": ts}
            if context:
                tip["context"] = context
            new_tips.append(tip)
            ledger.append({"ts": ts, "member": m, "delta": amount,
                            "balance": balances[m], "reason": f"Tip from {sender}"})
        tips.extend(new_tips)
        _save_json(TIPS_FILE, tips)
        _save_balances(balances)

    _append_ledger(ledger)
    return {"sender_bal": sender_bal, "tips": new_tips}


@router.post("/api/tips")
def send_tip(body: TipIn, info: dict = Depends(get_token_info)):
    try:
        result = perform_tip(info["name"], body.to, body.amount, body.message, body.context)
    except TipError as e:
        code = 404 if "not found" in str(e) else 422
        raise HTTPException(status_code=code, detail=str(e))
    log_write(info, f"POST tips — NB¥{body.amount} to {body.to}")
    return {"ok": True, "new_balance": result["sender_bal"]}


@router.post("/api/tips/multi")
def send_tip_multi(body: TipMultiIn, info: dict = Depends(get_token_info)):
    try:
        result = perform_tip_multi(info["name"], body.to, body.amount, body.message, body.context)
    except TipError as e:
        code = 404 if "not found" in str(e) else 422
        raise HTTPException(status_code=code, detail=str(e))
    log_write(info, f"POST tips/multi — NB¥{body.amount} each to {', '.join(body.to)}")
    return {"ok": True, "new_balance": result["sender_bal"], "tips": result["tips"]}


@router.get("/api/tips/totals")
def get_tip_totals():
    """Total NB¥ received per member (used by the members list). Declared before
    the /{member} route so 'totals' isn't captured as a member name."""
    tips = _load_json(TIPS_FILE, [])
    totals: dict[str, float] = {}
    for t in tips:
        totals[t["to"]] = round(totals.get(t["to"], 0.0) + t["amount"], 2)
    return totals


@router.get("/api/tips/context/{context}")
def get_context_tips(context: str):
    """Tips sent against one piece of content (e.g. an article — context is
    `"article:{id}"`) rather than a member's whole history, for display on
    that content's own page. Declared before /{member} for the same reason
    /totals is — 'context' isn't a member name."""
    tips = _load_json(TIPS_FILE, [])
    result = []
    total = 0.0
    for t in tips:
        if t.get("context") != context:
            continue
        total = round(total + t["amount"], 2)
        entry = {"id": t["id"], "from": t["from"], "to": t["to"], "amount": t["amount"], "ts": t["ts"]}
        if t["amount"] >= TIP_MESSAGE_THRESHOLD and t.get("message"):
            entry["message"] = t["message"]
        result.append(entry)
    result.sort(key=lambda x: x["ts"], reverse=True)
    return {"total": round(total, 2), "tips": result}


@router.get("/api/tips/{member}")
def get_member_tips(member: str):
    all_members = load_members()
    if member not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{member}' not found")
    tips = _load_json(TIPS_FILE, [])
    result = []
    for t in tips:
        if t["to"] != member:
            continue
        entry = {"id": t["id"], "from": t["from"], "amount": t["amount"], "ts": t["ts"]}
        if t["amount"] >= TIP_MESSAGE_THRESHOLD and t.get("message"):
            entry["message"] = t["message"]
        result.append(entry)
    return sorted(result, key=lambda x: x["ts"], reverse=True)

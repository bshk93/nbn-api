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


@router.post("/api/tips")
def send_tip(body: TipIn, info: dict = Depends(get_token_info)):
    if body.amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    if info["name"] == body.to:
        raise HTTPException(status_code=422, detail="Cannot tip yourself")
    all_members = load_members()
    if body.to not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{body.to}' not found")

    with _tips_lock, _balances_lock:
        balances = _load_balances()
        _init_bal(balances, info["name"])
        _init_bal(balances, body.to)
        if balances[info["name"]] < body.amount:
            raise HTTPException(
                status_code=422,
                detail=f"Insufficient balance — NB¥{balances[info['name']]:.2f} available",
            )
        balances[info["name"]] = round(balances[info["name"]] - body.amount, 2)
        balances[body.to]      = round(balances[body.to]      + body.amount, 2)
        sender_bal    = balances[info["name"]]
        recipient_bal = balances[body.to]
        _save_balances(balances)

        tips = _load_json(TIPS_FILE, [])
        message = body.message.strip() if body.amount >= TIP_MESSAGE_THRESHOLD else ""
        tip = {
            "id":      secrets.token_hex(6),
            "from":    info["name"],
            "to":      body.to,
            "amount":  body.amount,
            "message": message,
            "ts":      datetime.now(timezone.utc).isoformat(),
        }
        tips.append(tip)
        _save_json(TIPS_FILE, tips)

    _append_ledger([
        {"ts": tip["ts"], "member": info["name"], "delta": -body.amount,
         "balance": sender_bal,    "reason": f"Tip to {body.to}"},
        {"ts": tip["ts"], "member": body.to,       "delta":  body.amount,
         "balance": recipient_bal, "reason": f"Tip from {info['name']}"},
    ])
    log_write(info, f"POST tips — NB¥{body.amount} to {body.to}")
    return {"ok": True, "new_balance": sender_bal}


@router.get("/api/tips/totals")
def get_tip_totals():
    """Total NB¥ received per member (used by the members list). Declared before
    the /{member} route so 'totals' isn't captured as a member name."""
    tips = _load_json(TIPS_FILE, [])
    totals: dict[str, float] = {}
    for t in tips:
        totals[t["to"]] = round(totals.get(t["to"], 0.0) + t["amount"], 2)
    return totals


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

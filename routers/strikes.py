import re
import secrets
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import DATA_DIR, logger
from .storage import _load_json, _save_json, log_write
from .auth import require_role

router = APIRouter()

STRIKES_FILE = DATA_DIR / "strikes.json"
_strikes_lock = threading.Lock()


def _load_strikes() -> dict:
    return _load_json(STRIKES_FILE, {})


def _save_strikes(data: dict):
    _save_json(STRIKES_FILE, data)


class StrikeIn(BaseModel):
    date: str
    reason: str


@router.get("/api/strikes")
def get_strike_counts():
    """Public — returns {member: count} for members with at least one strike."""
    data = _load_strikes()
    return {name: len(strikes) for name, strikes in data.items() if strikes}


@router.get("/api/strikes/{member}")
def get_member_strikes(member: str, info: dict = Depends(require_role("bod"))):
    data = _load_strikes()
    return data.get(member, [])


@router.post("/api/strikes/{member}", status_code=201)
def add_strike(member: str, body: StrikeIn, info: dict = Depends(require_role("bod"))):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    strike = {
        "id": secrets.token_hex(8),
        "date": body.date,
        "reason": body.reason.strip(),
        "issued_by": info.get("name", "unknown"),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    with _strikes_lock:
        data = _load_strikes()
        if member not in data:
            data[member] = []
        data[member].append(strike)
        _save_strikes(data)
    log_write(info, f"POST strikes/{member} — {body.date}: {body.reason!r}")
    return strike


@router.delete("/api/strikes/{member}/{strike_id}")
def delete_strike(member: str, strike_id: str, info: dict = Depends(require_role("bod"))):
    with _strikes_lock:
        data = _load_strikes()
        strikes = data.get(member, [])
        remaining = [s for s in strikes if s["id"] != strike_id]
        if len(remaining) == len(strikes):
            raise HTTPException(status_code=404, detail="Strike not found")
        data[member] = remaining
        _save_strikes(data)
    log_write(info, f"DELETE strikes/{member}/{strike_id}")
    return {"ok": True}

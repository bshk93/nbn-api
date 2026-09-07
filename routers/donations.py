import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import DATA_DIR, VALID_TEAMS, _donations_lock
from .storage import _load_json, _save_json, log_write
from .auth import require_role

router = APIRouter()

DONATIONS_FILE = DATA_DIR / "donations.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_donations() -> list:
    return _load_json(DONATIONS_FILE, [])


def _save_donations(data: list):
    _save_json(DONATIONS_FILE, data)


class DonationIn(BaseModel):
    team: str
    member: str
    amount: int
    date: str


def _validate(body: DonationIn):
    if body.team.upper() not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team {body.team!r}")
    if not body.member.strip():
        raise HTTPException(status_code=422, detail="member is required")
    if body.amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    if not DATE_RE.match(body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


@router.get("/api/donations")
def list_donations():
    """Public — the whole donation record."""
    return _load_donations()


@router.post("/api/donations", status_code=201)
def add_donation(body: DonationIn, info: dict = Depends(require_role("bod"))):
    _validate(body)
    donation = {
        "id": secrets.token_hex(8),
        "team": body.team.upper(),
        "member": body.member.strip(),
        "amount": body.amount,
        "date": body.date,
        "added_by": info.get("name", "unknown"),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    with _donations_lock:
        data = _load_donations()
        data.append(donation)
        _save_donations(data)
    log_write(info, f"POST donations — {donation['team']} {donation['member']} ${donation['amount']}")
    return donation


@router.put("/api/donations/{donation_id}")
def edit_donation(donation_id: str, body: DonationIn, info: dict = Depends(require_role("bod"))):
    _validate(body)
    with _donations_lock:
        data = _load_donations()
        for d in data:
            if d["id"] == donation_id:
                d.update({
                    "team": body.team.upper(),
                    "member": body.member.strip(),
                    "amount": body.amount,
                    "date": body.date,
                    "updated_by": info.get("name", "unknown"),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                _save_donations(data)
                log_write(info, f"PUT donations/{donation_id} — {d['team']} {d['member']} ${d['amount']}")
                return d
    raise HTTPException(status_code=404, detail="Donation not found")

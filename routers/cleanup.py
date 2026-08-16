"""Clean Up the Poo Poo — member-submitted bio-gap fills, admin-reviewed.

See nbn-today/docs/clean-up-the-poopoo-spec.md. Deliberately separate from the
curator/rosters direct-edit path (players.py's _maybe_award_bio_reward): this
is the open-to-everyone submission queue, gated on review rather than a role.
Approval writes the field directly (bypassing players.py's HTTP handler so its
NB¥10 auto-reward never fires here) and pays the submitter — not the
approving admin — through its own tiered reward table.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import CLEANUP_SUBMISSIONS_FILE, _cleanup_lock
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, require_admin
from .players import load_player_bios, save_player_bios
from .bets import _award_cleanup_reward

router = APIRouter()

# Draft info is handled as one compound field ("what year, and was this player
# drafted at all") rather than three separate gaps — see the spec's § 1 on why
# draft_year null is never legitimately "undrafted" on its own.
SIMPLE_FIELDS = ["college", "country", "height", "weight", "wingspan", "dob", "photo_url"]
ALL_FIELDS = SIMPLE_FIELDS + ["draft_info"]

# 25 NB¥ for a plain lookup, 50 for fields that take more work to source
# (a precise date, an image URL, or the compound draft question).
CLEANUP_FIELD_REWARDS = {
    "college": 25.0, "country": 25.0, "height": 25.0, "weight": 25.0, "wingspan": 25.0,
    "dob": 50.0, "photo_url": 50.0, "draft_info": 50.0,
}


def _load_store() -> dict:
    return _load_json(CLEANUP_SUBMISSIONS_FILE, {"seq": 0, "items": []})


def _save_store(store: dict):
    _save_json(CLEANUP_SUBMISSIONS_FILE, store)


def _find(store: dict, sub_id: int) -> int:
    for i, it in enumerate(store["items"]):
        if it["id"] == sub_id:
            return i
    raise HTTPException(status_code=404, detail="Submission not found")


def _field_is_empty(bio: dict, field: str) -> bool:
    if field == "draft_info":
        return bio.get("draft_year") is None and bio.get("draft_round") is None and bio.get("draft_pick") is None
    v = bio.get(field)
    if v is None:
        return True
    if isinstance(v, str):
        return v == ""
    return False


def _validate_value(field: str, value):
    """Returns the normalized value to store, or raises HTTPException(422)."""
    if field not in ALL_FIELDS:
        raise HTTPException(status_code=422, detail=f"Unknown field {field!r}")

    if field == "draft_info":
        if not isinstance(value, dict):
            raise HTTPException(status_code=422, detail="draft_info value must be an object")
        year, rnd, pick = value.get("draft_year"), value.get("draft_round"), value.get("draft_pick")
        if not isinstance(year, int) or not (1946 <= year <= 2035):
            raise HTTPException(status_code=422, detail="draft_year is required and must be a plausible year")
        if rnd is not None and rnd not in (1, 2):
            raise HTTPException(status_code=422, detail="draft_round must be 1, 2, or omitted for an undrafted player")
        if pick is not None and (not isinstance(pick, int) or pick < 1):
            raise HTTPException(status_code=422, detail="draft_pick must be a positive integer")
        if (rnd is None) != (pick is None):
            raise HTTPException(status_code=422, detail="draft_round and draft_pick must both be set, or both left blank for undrafted")
        return {"draft_year": year, "draft_round": rnd, "draft_pick": pick}

    if field == "weight":
        try:
            w = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="weight must be an integer (lbs)")
        if not (100 <= w <= 400):
            raise HTTPException(status_code=422, detail="weight must be a plausible number of lbs")
        return w

    if field == "dob":
        try:
            datetime.strptime(str(value), "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="dob must be YYYY-MM-DD")
        return str(value)

    if field == "photo_url":
        s = str(value).strip()
        if not s.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="photo_url must be a URL")
        return s

    s = str(value).strip()
    if not s:
        raise HTTPException(status_code=422, detail=f"{field} value is required")
    return s


def _apply_field(bios: dict, slug: str, field: str, value):
    bio = bios[slug]
    if field == "draft_info":
        bio["draft_year"] = value["draft_year"]
        bio["draft_round"] = value["draft_round"]
        bio["draft_pick"] = value["draft_pick"]
    else:
        bio[field] = value


class SubmissionCreate(BaseModel):
    slug: str
    field: str
    value: object
    source_note: str = ""


class RejectBody(BaseModel):
    reason: str


@router.get("/api/cleanup/gaps")
def get_gaps():
    """The open question bank — computed on read, never stored, so it can't
    drift from player-bios.json. Excludes fields with a pending submission
    (someone's already working on it) and the already-resolved-undrafted
    population (draft_year set, round/pick null)."""
    bios = load_player_bios()
    store = _load_store()
    pending_keys = {(it["slug"], it["field"]) for it in store["items"] if it["status"] == "pending"}
    gaps = []
    for slug, bio in bios.items():
        name = bio.get("name", slug)
        for field in ALL_FIELDS:
            if _field_is_empty(bio, field) and (slug, field) not in pending_keys:
                gaps.append({"slug": slug, "name": name, "field": field})
    return gaps


@router.get("/api/cleanup/submissions")
def list_submissions(status: Optional[str] = None, info: dict = Depends(require_admin)):
    items = _load_store()["items"]
    if status:
        items = [it for it in items if it["status"] == status]
    return sorted(items, key=lambda it: it["submitted_at"], reverse=True)


@router.get("/api/cleanup/submissions/mine")
def my_submissions(info: dict = Depends(get_token_info)):
    items = [it for it in _load_store()["items"] if it["submitted_by"] == info["name"]]
    return sorted(items, key=lambda it: it["submitted_at"], reverse=True)


@router.post("/api/cleanup/submissions")
def create_submission(body: SubmissionCreate, info: dict = Depends(get_token_info)):
    bios = load_player_bios()
    if body.slug not in bios:
        raise HTTPException(status_code=400, detail="Unknown player")
    if body.field not in ALL_FIELDS:
        raise HTTPException(status_code=422, detail=f"Unknown field {body.field!r}")
    if not _field_is_empty(bios[body.slug], body.field):
        raise HTTPException(status_code=409, detail="This field is no longer a gap — already filled")

    normalized = _validate_value(body.field, body.value)
    now = datetime.now(timezone.utc).isoformat()
    with _cleanup_lock:
        store = _load_store()
        store["seq"] += 1
        submission = {
            "id": store["seq"],
            "slug": body.slug,
            "field": body.field,
            "value": normalized,
            "source_note": body.source_note.strip(),
            "submitted_by": info["name"],
            "submitted_at": now,
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "reject_reason": None,
            "reward_nby": None,
        }
        store["items"].append(submission)
        _save_store(store)
    log_write(info, f"POST cleanup/submissions — {body.slug}.{body.field} = {normalized!r}")
    return submission


@router.post("/api/cleanup/submissions/{sub_id}/approve")
def approve_submission(sub_id: int, info: dict = Depends(require_admin)):
    with _cleanup_lock:
        store = _load_store()
        idx = _find(store, sub_id)
        sub = store["items"][idx]
        if sub["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Submission is already {sub['status']}")
        if sub["submitted_by"] == info["name"]:
            raise HTTPException(status_code=403, detail="Cannot approve your own submission")

        bios = load_player_bios()
        bio = bios.get(sub["slug"])
        if bio is None:
            raise HTTPException(status_code=404, detail="Player no longer exists")
        if not _field_is_empty(bio, sub["field"]):
            # Raced with a curator edit (or another approval) since submission —
            # nothing to apply, and no reward for a fact that's already on file.
            sub["status"] = "rejected"
            sub["reject_reason"] = "Field was filled through another path before this could be approved"
            sub["reviewed_by"] = info["name"]
            sub["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            _save_store(store)
            raise HTTPException(status_code=409, detail="Field was already filled elsewhere — submission auto-rejected")

        _apply_field(bios, sub["slug"], sub["field"], sub["value"])
        save_player_bios(bios)

        now = datetime.now(timezone.utc).isoformat()
        reward = CLEANUP_FIELD_REWARDS[sub["field"]]
        sub["status"] = "approved"
        sub["reviewed_by"] = info["name"]
        sub["reviewed_at"] = now
        sub["reward_nby"] = reward

        # Any other pending submission racing for the same gap is now moot —
        # not the submitter's fault, so superseded rather than rejected.
        for other in store["items"]:
            if other is sub:
                continue
            if other["status"] == "pending" and other["slug"] == sub["slug"] and other["field"] == sub["field"]:
                other["status"] = "superseded"
                other["reviewed_by"] = info["name"]
                other["reviewed_at"] = now

        _save_store(store)

    _award_cleanup_reward(
        sub["submitted_by"], reward,
        f"Clean Up the Poo Poo: {sub['slug']}.{sub['field']}",
    )
    log_write(info, f"POST cleanup/submissions/{sub_id}/approve — {sub['slug']}.{sub['field']}, NB¥{reward} to {sub['submitted_by']}")
    return sub


@router.post("/api/cleanup/submissions/{sub_id}/reject")
def reject_submission(sub_id: int, body: RejectBody, info: dict = Depends(require_admin)):
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A reason is required")
    with _cleanup_lock:
        store = _load_store()
        idx = _find(store, sub_id)
        sub = store["items"][idx]
        if sub["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Submission is already {sub['status']}")
        sub["status"] = "rejected"
        sub["reject_reason"] = reason
        sub["reviewed_by"] = info["name"]
        sub["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        _save_store(store)
    log_write(info, f"POST cleanup/submissions/{sub_id}/reject — {reason}")
    return sub

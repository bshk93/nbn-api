import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import SUGGESTIONS_FILE
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, has_role

router = APIRouter()

_suggestions_lock = threading.Lock()

VALID_STATUSES = {"open", "in_progress", "complete", "closed"}


# Stored as {"seq": int, "items": [...]} rather than a bare list — "seq" is a
# monotonic counter that only ever increments, so suggestion numbers stay
# permanent references (like GitLab issue numbers) even after older
# suggestions are deleted. Deriving the next number from max(existing) would
# let a number get reissued once everything ahead of it was deleted.
def _load_store() -> dict:
    return _load_json(SUGGESTIONS_FILE, {"seq": 0, "items": []})


def _save_store(store: dict):
    _save_json(SUGGESTIONS_FILE, store)


def load_suggestions() -> list[dict]:
    return _load_store()["items"]


def _is_privileged(info: dict) -> bool:
    return has_role(info, "bod") or has_role(info, "admin")


class SuggestionCreate(BaseModel):
    title: str
    description: str = ""


class SuggestionPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


@router.get("/api/suggestions")
def list_suggestions():
    return sorted(load_suggestions(), key=lambda s: s.get("created_at", ""), reverse=True)


@router.post("/api/suggestions")
def create_suggestion(body: SuggestionCreate, info: dict = Depends(get_token_info)):
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title is required")
    now = datetime.now(timezone.utc).isoformat()
    with _suggestions_lock:
        store = _load_store()
        store["seq"] += 1
        suggestion = {
            "id": secrets.token_hex(8),
            "number": store["seq"],
            "title": body.title.strip(),
            "description": body.description.strip(),
            "author": info["name"],
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }
        store["items"].append(suggestion)
        _save_store(store)
    log_write(info, f"POST suggestions — #{suggestion['number']} {body.title!r}")
    return suggestion


@router.patch("/api/suggestions/{suggestion_id}")
def patch_suggestion(suggestion_id: str, body: SuggestionPatch, info: dict = Depends(get_token_info)):
    with _suggestions_lock:
        store = _load_store()
        idx = next((i for i, s in enumerate(store["items"]) if s["id"] == suggestion_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        s = store["items"][idx]
        privileged = _is_privileged(info)

        if body.status is not None:
            if not privileged:
                raise HTTPException(status_code=403, detail="'bod' role required to change status")
            if body.status not in VALID_STATUSES:
                raise HTTPException(status_code=422, detail=f"status must be one of {sorted(VALID_STATUSES)}")
            s["status"] = body.status

        if body.title is not None or body.description is not None:
            if s.get("author") != info["name"] and not privileged:
                raise HTTPException(status_code=403, detail="Only the author or BOD can edit this suggestion")
            if s.get("status") != "open" and not privileged:
                raise HTTPException(status_code=422, detail="Can only edit a suggestion while it is open")
            if body.title is not None:
                if not body.title.strip():
                    raise HTTPException(status_code=422, detail="title is required")
                s["title"] = body.title.strip()
            if body.description is not None:
                s["description"] = body.description.strip()

        s["updated_at"] = datetime.now(timezone.utc).isoformat()
        store["items"][idx] = s
        _save_store(store)
    log_write(info, f"PATCH suggestions/{suggestion_id}")
    return s


@router.delete("/api/suggestions/{suggestion_id}")
def delete_suggestion(suggestion_id: str, info: dict = Depends(get_token_info)):
    privileged = _is_privileged(info)
    with _suggestions_lock:
        store = _load_store()
        idx = next((i for i, s in enumerate(store["items"]) if s["id"] == suggestion_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        s = store["items"][idx]
        if s.get("author") != info["name"] and not privileged:
            raise HTTPException(status_code=403, detail="Not authorized to delete this suggestion")
        if s.get("status") != "open" and not privileged:
            raise HTTPException(status_code=422, detail="Only BOD can delete a suggestion once it's been triaged")
        store["items"].pop(idx)
        _save_store(store)
    log_write(info, f"DELETE suggestions/{suggestion_id}")
    return {"ok": True}

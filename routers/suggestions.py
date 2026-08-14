import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import inbox
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


class CommentBody(BaseModel):
    body: str


# The thread on a suggestion holds two kinds of entry, in one list so the
# ordering between them is real rather than reconstructed at render time:
#   kind="comment" — written by a member, editable/deletable by its author
#   kind="status"  — appended automatically when the status changes
# Status entries are the record of what the board did, so they are never
# editable and never deletable; only comments are.
def _new_entry(kind: str, author: str, **fields) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"id": secrets.token_hex(8), "kind": kind, "author": author, "created_at": now, **fields}


def _find(store: dict, suggestion_id: str) -> int:
    idx = next((i for i, s in enumerate(store["items"]) if s["id"] == suggestion_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return idx


@router.get("/api/suggestions")
def list_suggestions():
    # Suggestions predating comments have no "comments" key; default it here so
    # every caller sees one shape and no client has to guard for its absence.
    items = [{**s, "comments": s.get("comments", [])} for s in load_suggestions()]
    return sorted(items, key=lambda s: s.get("created_at", ""), reverse=True)


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
            "comments": [],
        }
        store["items"].append(suggestion)
        _save_store(store)
    log_write(info, f"POST suggestions — #{suggestion['number']} {body.title!r}")
    return suggestion


@router.patch("/api/suggestions/{suggestion_id}")
def patch_suggestion(suggestion_id: str, body: SuggestionPatch, info: dict = Depends(get_token_info)):
    notify_status_change = None
    with _suggestions_lock:
        store = _load_store()
        idx = _find(store, suggestion_id)
        s = store["items"][idx]
        s.setdefault("comments", [])
        privileged = _is_privileged(info)

        if body.status is not None:
            if not privileged:
                raise HTTPException(status_code=403, detail="'bod' role required to change status")
            if body.status not in VALID_STATUSES:
                raise HTTPException(status_code=422, detail=f"status must be one of {sorted(VALID_STATUSES)}")
            if body.status != s["status"]:
                s["comments"].append(
                    _new_entry("status", info["name"], **{"from": s["status"], "to": body.status})
                )
                if s.get("author") and s["author"] != info["name"]:
                    notify_status_change = (s["author"], s["title"], body.status)
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
            s["edited_at"] = datetime.now(timezone.utc).isoformat()

        s["updated_at"] = datetime.now(timezone.utc).isoformat()
        store["items"][idx] = s
        _save_store(store)
    log_write(info, f"PATCH suggestions/{suggestion_id}")
    if notify_status_change:
        author, title, new_status = notify_status_change
        inbox.notify_member(author, f"Your suggestion \"{title}\" is now {new_status}", link="/suggestions")
    return s


@router.post("/api/suggestions/{suggestion_id}/comments")
def add_comment(suggestion_id: str, body: CommentBody, info: dict = Depends(get_token_info)):
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Comment body is required")
    # Deliberately allowed on every status: the point of comments is posting
    # updates as a suggestion is worked and after it lands, so a completed or
    # closed thread is exactly where the last word belongs.
    with _suggestions_lock:
        store = _load_store()
        idx = _find(store, suggestion_id)
        s = store["items"][idx]
        s.setdefault("comments", [])
        comment = _new_entry("comment", info["name"], body=text)
        s["comments"].append(comment)
        _save_store(store)
        author = s.get("author")
        title = s.get("title")
    log_write(info, f"POST suggestions/{suggestion_id}/comments")
    if author and author != info["name"]:
        inbox.notify_member(author, f"New comment on your suggestion \"{title}\"", link="/suggestions")
    return comment


def _mutate_comment(suggestion_id: str, comment_id: str, info: dict):
    """Locate an editable comment, or raise. Caller holds the lock."""
    store = _load_store()
    idx = _find(store, suggestion_id)
    s = store["items"][idx]
    comments = s.setdefault("comments", [])
    cidx = next((i for i, c in enumerate(comments) if c["id"] == comment_id), None)
    if cidx is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    c = comments[cidx]
    if c.get("kind") != "comment":
        raise HTTPException(status_code=422, detail="Status entries are part of the record and can't be changed")
    if c.get("author") != info["name"] and not _is_privileged(info):
        raise HTTPException(status_code=403, detail="Only the author or BOD can change this comment")
    return store, comments, cidx


@router.patch("/api/suggestions/{suggestion_id}/comments/{comment_id}")
def edit_comment(suggestion_id: str, comment_id: str, body: CommentBody, info: dict = Depends(get_token_info)):
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Comment body is required")
    with _suggestions_lock:
        store, comments, cidx = _mutate_comment(suggestion_id, comment_id, info)
        comments[cidx]["body"] = text
        comments[cidx]["edited_at"] = datetime.now(timezone.utc).isoformat()
        _save_store(store)
        comment = comments[cidx]
    log_write(info, f"PATCH suggestions/{suggestion_id}/comments/{comment_id}")
    return comment


@router.delete("/api/suggestions/{suggestion_id}/comments/{comment_id}")
def delete_comment(suggestion_id: str, comment_id: str, info: dict = Depends(get_token_info)):
    with _suggestions_lock:
        store, comments, cidx = _mutate_comment(suggestion_id, comment_id, info)
        comments.pop(cidx)
        _save_store(store)
    log_write(info, f"DELETE suggestions/{suggestion_id}/comments/{comment_id}")
    return {"ok": True}


@router.delete("/api/suggestions/{suggestion_id}")
def delete_suggestion(suggestion_id: str, info: dict = Depends(get_token_info)):
    privileged = _is_privileged(info)
    with _suggestions_lock:
        store = _load_store()
        idx = _find(store, suggestion_id)
        s = store["items"][idx]
        if s.get("author") != info["name"] and not privileged:
            raise HTTPException(status_code=403, detail="Not authorized to delete this suggestion")
        if s.get("status") != "open" and not privileged:
            raise HTTPException(status_code=422, detail="Only BOD can delete a suggestion once it's been triaged")
        store["items"].pop(idx)
        _save_store(store)
    log_write(info, f"DELETE suggestions/{suggestion_id}")
    return {"ok": True}

import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .constants import (
    PROPOSALS_FILE, CONSTITUTION_FILE, VALID_ROLES, VALID_TEAMS,
)
from .storage import _load_json, _save_json, log_write
from .auth import (
    get_token_info, has_role, require_role, _resolve_token, load_members,
)

router = APIRouter()

_proposals_lock = threading.Lock()
_constitution_lock = threading.Lock()

TEAM_ROLE_SET = {t.lower() for t in VALID_TEAMS}
VALID_TENURE_POSITIONS = {"owner", "gm", "coach"}


def _member_current_positions(name: str) -> set[str]:
    """Return the set of active (end=null) tenure positions for a member, excluding 'none'."""
    members = load_members()
    tenures = members.get(name, {}).get("tenures", [])
    return {t["position"] for t in tenures if not t.get("end") and t.get("position") and t["position"] != "none"}


def load_proposals() -> list[dict]:
    return _load_json(PROPOSALS_FILE, [])


def save_proposals(proposals: list[dict]):
    _save_json(PROPOSALS_FILE, proposals)


def load_constitution() -> dict:
    if not CONSTITUTION_FILE.exists():
        return {"version": 0, "date": None, "updated_by": None, "content": "", "history": []}
    return _load_json(CONSTITUTION_FILE, {"version": 0, "date": None, "updated_by": None, "content": "", "history": []})


def save_constitution(data: dict):
    _save_json(CONSTITUTION_FILE, data)


def _proposal_view(p: dict, viewer_name: Optional[str] = None) -> dict:
    """Return a safe copy of the proposal with votes masked appropriately."""
    votes = p.get("votes", {})
    status = p.get("status", "draft")
    out = {k: v for k, v in p.items() if k != "votes"}
    out["comment_count"] = len(p.get("comments", []))
    if status == "voting":
        out["vote_count"] = len(votes)
        out["my_vote"] = votes.get(viewer_name) if viewer_name else None
    elif status == "closed":
        tally = {"yes": 0, "no": 0, "abstain": 0}
        for v in votes.values():
            if v in tally:
                tally[v] += 1
        out["results"] = tally
        out["vote_count"] = len(votes)
        out["my_vote"] = votes.get(viewer_name) if viewer_name else None
    else:
        out["vote_count"] = 0
        out["my_vote"] = None
    return out


def _proposal_can_edit(p: dict, info: dict) -> bool:
    status = p.get("status")
    is_privileged = has_role(info, "bod") or has_role(info, "admin")
    if status == "draft":
        return p.get("author") == info["name"]
    if status == "submitted":
        return p.get("author") == info["name"] or is_privileged
    return False


# ── Pydantic models ───────────────────────────────────────────────────────────

class ProposalCreate(BaseModel):
    title: str
    body: str
    eligible_roles: list[str] = []
    eligible_positions: list[str] = []


class ProposalPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    eligible_roles: Optional[list[str]] = None
    eligible_positions: Optional[list[str]] = None


class CommentCreate(BaseModel):
    body: str


class VoteIn(BaseModel):
    vote: str


class ConstitutionUpdate(BaseModel):
    content: str
    summary: str


# ── Proposal routes ───────────────────────────────────────────────────────────

@router.get("/api/proposals")
def list_proposals(authorization: Optional[str] = Header(None)):
    info = _resolve_token(authorization)
    viewer = info["name"] if info else None
    proposals = load_proposals()
    result: dict = {"proposals": [], "drafts": []}
    for p in proposals:
        status = p.get("status", "draft")
        if status == "draft":
            if viewer and p.get("author") == viewer:
                result["drafts"].append(_proposal_view(p, viewer))
        else:
            result["proposals"].append(_proposal_view(p, viewer))
    result["proposals"].sort(key=lambda x: x.get("submitted_at") or x.get("created_at") or "", reverse=True)
    result["drafts"].sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    return result


@router.post("/api/proposals")
def create_proposal(body: ProposalCreate, info: dict = Depends(get_token_info)):
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title is required")
    invalid_roles = [r for r in body.eligible_roles if r not in VALID_ROLES]
    if invalid_roles:
        raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid_roles}")
    invalid_positions = [p for p in body.eligible_positions if p not in VALID_TENURE_POSITIONS]
    if invalid_positions:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_positions}")
    now = datetime.now(timezone.utc).isoformat()
    proposal = {
        "id": str(uuid.uuid4()),
        "title": body.title.strip(),
        "body": body.body,
        "author": info["name"],
        "status": "draft",
        "eligible_roles": body.eligible_roles,
        "eligible_positions": body.eligible_positions,
        "created_at": now,
        "updated_at": now,
        "submitted_at": None,
        "voting_opened_at": None,
        "voting_closed_at": None,
        "comments": [],
        "votes": {},
    }
    with _proposals_lock:
        proposals = load_proposals()
        proposals.append(proposal)
        save_proposals(proposals)
    log_write(info, f"POST proposals — {proposal['id']!r} {body.title!r}")
    return _proposal_view(proposal, info["name"])


@router.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: str, authorization: Optional[str] = Header(None)):
    info = _resolve_token(authorization)
    proposals = load_proposals()
    p = next((x for x in proposals if x["id"] == proposal_id), None)
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if p.get("status") == "draft":
        if not info or p.get("author") != info["name"]:
            raise HTTPException(status_code=403, detail="Not authorized to view this draft")
    return _proposal_view(p, info["name"] if info else None)


@router.patch("/api/proposals/{proposal_id}")
def patch_proposal(proposal_id: str, body: ProposalPatch, info: dict = Depends(get_token_info)):
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if not _proposal_can_edit(p, info):
            raise HTTPException(status_code=403, detail="Cannot edit this proposal")
        if body.title is not None:
            p["title"] = body.title.strip()
        if body.body is not None:
            p["body"] = body.body
        if body.eligible_roles is not None:
            invalid_roles = [r for r in body.eligible_roles if r not in VALID_ROLES]
            if invalid_roles:
                raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid_roles}")
            p["eligible_roles"] = body.eligible_roles
        if body.eligible_positions is not None:
            invalid_positions = [pos for pos in body.eligible_positions if pos not in VALID_TENURE_POSITIONS]
            if invalid_positions:
                raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_positions}")
            p["eligible_positions"] = body.eligible_positions
        p["updated_at"] = datetime.now(timezone.utc).isoformat()
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"PATCH proposals/{proposal_id}")
    return _proposal_view(p, info["name"])


@router.post("/api/proposals/{proposal_id}/submit")
def submit_proposal(proposal_id: str, info: dict = Depends(get_token_info)):
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if p.get("author") != info["name"] and not has_role(info, "admin"):
            raise HTTPException(status_code=403, detail="Only the author can submit this proposal")
        if p.get("status") != "draft":
            raise HTTPException(status_code=422, detail="Only drafts can be submitted")
        now = datetime.now(timezone.utc).isoformat()
        p["status"] = "submitted"
        p["submitted_at"] = now
        p["updated_at"] = now
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"POST proposals/{proposal_id}/submit")
    return _proposal_view(p, info["name"])


@router.post("/api/proposals/{proposal_id}/open-voting")
def open_proposal_voting(proposal_id: str, info: dict = Depends(require_role("bod"))):
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if p.get("status") != "submitted":
            raise HTTPException(status_code=422, detail="Only submitted proposals can be opened for voting")
        now = datetime.now(timezone.utc).isoformat()
        p["status"] = "voting"
        p["voting_opened_at"] = now
        p["updated_at"] = now
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"POST proposals/{proposal_id}/open-voting")
    return _proposal_view(p, info["name"])


@router.post("/api/proposals/{proposal_id}/close-voting")
def close_proposal_voting(proposal_id: str, info: dict = Depends(require_role("bod"))):
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if p.get("status") != "voting":
            raise HTTPException(status_code=422, detail="Proposal is not open for voting")
        now = datetime.now(timezone.utc).isoformat()
        p["status"] = "closed"
        p["voting_closed_at"] = now
        p["updated_at"] = now
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"POST proposals/{proposal_id}/close-voting")
    return _proposal_view(p, info["name"])


@router.post("/api/proposals/{proposal_id}/vote")
def cast_vote(proposal_id: str, body: VoteIn, info: dict = Depends(get_token_info)):
    if body.vote not in ("yes", "no", "abstain"):
        raise HTTPException(status_code=422, detail="vote must be 'yes', 'no', or 'abstain'")
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if p.get("status") != "voting":
            raise HTTPException(status_code=422, detail="Voting is not open for this proposal")
        eligible_roles = p.get("eligible_roles", [])
        eligible_positions = p.get("eligible_positions", [])
        voter_roles = set(info.get("roles", []))
        if not has_role(info, "admin"):
            if not eligible_roles and not eligible_positions:
                if not voter_roles & TEAM_ROLE_SET:
                    raise HTTPException(status_code=403, detail="You must have a team role to vote")
            else:
                role_match = bool(eligible_roles) and any(r in voter_roles for r in eligible_roles)
                pos_match = bool(eligible_positions) and bool(
                    _member_current_positions(info["name"]) & set(eligible_positions)
                )
                if not role_match and not pos_match:
                    raise HTTPException(status_code=403, detail="You are not eligible to vote on this proposal")
        votes = p.get("votes", {})
        already_voted = info["name"] in votes
        votes[info["name"]] = body.vote
        p["votes"] = votes
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"POST proposals/{proposal_id}/vote — {info['name']} {'changed' if already_voted else 'cast'} {body.vote}")
    return {"ok": True, "vote": body.vote, "changed": already_voted}


@router.post("/api/proposals/{proposal_id}/comments")
def add_proposal_comment(proposal_id: str, body: CommentCreate, info: dict = Depends(get_token_info)):
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Comment body is required")
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if p.get("status") == "draft":
            raise HTTPException(status_code=422, detail="Cannot comment on a draft")
        comment = {
            "id": secrets.token_hex(8),
            "author": info["name"],
            "body": body.body.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        p.setdefault("comments", []).append(comment)
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"POST proposals/{proposal_id}/comments — {info['name']}")
    return comment


@router.delete("/api/proposals/{proposal_id}/comments/{comment_id}")
def delete_proposal_comment(proposal_id: str, comment_id: str, info: dict = Depends(get_token_info)):
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        comments = p.get("comments", [])
        comment = next((c for c in comments if c["id"] == comment_id), None)
        if not comment:
            raise HTTPException(status_code=404, detail="Comment not found")
        if comment["author"] != info["name"] and not has_role(info, "bod") and not has_role(info, "admin"):
            raise HTTPException(status_code=403, detail="Not authorized to delete this comment")
        p["comments"] = [c for c in comments if c["id"] != comment_id]
        proposals[idx] = p
        save_proposals(proposals)
    log_write(info, f"DELETE proposals/{proposal_id}/comments/{comment_id}")
    return {"ok": True}


@router.delete("/api/proposals/{proposal_id}")
def delete_proposal(proposal_id: str, info: dict = Depends(get_token_info)):
    is_privileged = has_role(info, "bod") or has_role(info, "admin")
    with _proposals_lock:
        proposals = load_proposals()
        idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        p = proposals[idx]
        if p.get("author") != info["name"] and not is_privileged:
            raise HTTPException(status_code=403, detail="Not authorized to delete this proposal")
        if p.get("status") != "draft" and not is_privileged:
            raise HTTPException(status_code=422, detail="Only BOD can delete submitted proposals")
        proposals.pop(idx)
        save_proposals(proposals)
    log_write(info, f"DELETE proposals/{proposal_id}")
    return {"ok": True}


# ── Constitution routes ───────────────────────────────────────────────────────

@router.get("/api/constitution")
def get_constitution():
    return load_constitution()


@router.put("/api/constitution")
def put_constitution(body: ConstitutionUpdate, info: dict = Depends(require_role("bod"))):
    if not body.content.strip() or not body.summary.strip():
        raise HTTPException(status_code=422, detail="content and summary are required")
    with _constitution_lock:
        data = load_constitution()
        new_version = data.get("version", 0) + 1
        entry = {
            "version": new_version,
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "author": info["name"],
            "summary": body.summary.strip(),
        }
        data["version"] = new_version
        data["date"] = entry["date"]
        data["updated_by"] = info["name"]
        data["content"] = body.content
        data.setdefault("history", []).append(entry)
        save_constitution(data)
    log_write(info, f"PUT constitution v{new_version}: {body.summary[:60]}")
    return {"ok": True, "version": new_version}

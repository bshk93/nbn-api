"""Clean Up the Poo Poo — member-submitted bio-gap fills, admin-reviewed.

See nbn-today/docs/clean-up-the-poopoo-spec.md. Deliberately separate from the
curator/rosters direct-edit path (players.py's _maybe_award_bio_reward): this
is the open-to-everyone submission queue, gated on review rather than a role.
Approval writes the field directly (bypassing players.py's HTTP handler so its
NB¥10 auto-reward never fires here) and pays the submitter — not the
approving admin — through its own tiered reward table.

Two gap types share one submission model (`gap_type`, checked everywhere a
submission's identity or reward matters):
  - "bio_field": a missing player-bios.json fact. Approval writes the field.
  - "discord_fa": a flagged candidate from the § "Discord transaction backfill"
    pipeline (nbn-api/docs/discord-transaction-backfill.md) — a historical
    fa-news signing/option whose player name the parser couldn't confidently
    match. Approval calls transactions.py's existing historical-append
    functions directly (same code the admin's own submit script uses) and
    also records the candidate into discord-fa-signings-submitted.json, the
    same de-dup file that script reads — so a later run of it can never
    resubmit the same candidate as a duplicate transaction.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import CLEANUP_SUBMISSIONS_FILE, DISCORD_FA_RESOLVED_FILE, DISCORD_FA_SUBMITTED_FILE, _cleanup_lock
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, require_admin, load_members
from .players import load_player_bios, save_player_bios
from .bets import _award_cleanup_reward
from .transactions import SignDetails, OptionDetails, TransactionIn, _create_historical_sign, _create_historical_option

router = APIRouter()

# Draft info is handled as one compound field ("what year, and was this player
# drafted at all") rather than three separate gaps — see the spec's § 1 on why
# draft_year null is never legitimately "undrafted" on its own.
SIMPLE_FIELDS = ["college", "country", "height", "weight", "wingspan", "dob", "photo_url"]
ALL_FIELDS = SIMPLE_FIELDS + ["draft_info"]

# 25 NB¥ for a plain lookup, 50 for fields that take more work to source
# (a precise date, an image URL, or the compound draft question), 100 for a
# Discord-backfill candidate (requires reading the raw message and knowing —
# or researching — an often-obscure historical player).
CLEANUP_FIELD_REWARDS = {
    "college": 25.0, "country": 25.0, "height": 25.0, "weight": 25.0, "wingspan": 25.0,
    "dob": 50.0, "photo_url": 50.0, "draft_info": 50.0,
}
DISCORD_FA_REWARD = 100.0


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


def _gap_key(sub: dict):
    """Identity of the gap a submission is racing to fill, gap-type-aware.
    Used to (a) exclude a gap that already has a pending submission and
    (b) supersede the losing side of a competing pair on approval."""
    if sub["gap_type"] == "discord_fa":
        return ("discord_fa", sub["discord_id"], sub["candidate_index"])
    return ("bio_field", sub["slug"], sub["field"])


def _discord_fa_flagged_candidates() -> list[dict]:
    """One row per (discord_id, candidate_index) that actually needs a human
    to confirm or supply a player slug. A flagged message is flagged because
    of ONE problem candidate — it can carry other candidates (other players
    in the same batch announcement) that already matched cleanly (`note:
    "exact"`); those are not gaps and are excluded here."""
    data = _load_json(DISCORD_FA_RESOLVED_FILE, {})
    out = []
    for entry in data.get("flagged", []):
        for i, cand in enumerate(entry.get("candidates", [])):
            if cand.get("note") == "exact":
                continue
            out.append({
                "discord_id": entry["discord_id"], "candidate_index": i,
                "date": entry["date"], "description": entry["description"], "channel": entry["channel"],
                "candidate": cand,
            })
    return out


def _find_discord_fa_candidate(discord_id: str, candidate_index: int) -> Optional[dict]:
    data = _load_json(DISCORD_FA_RESOLVED_FILE, {})
    for entry in data.get("flagged", []):
        if entry["discord_id"] != discord_id:
            continue
        cands = entry.get("candidates", [])
        if candidate_index >= len(cands):
            return None
        return {
            "discord_id": discord_id, "candidate_index": candidate_index,
            "date": entry["date"], "description": entry["description"], "channel": entry["channel"],
            "candidate": cands[candidate_index],
        }
    return None


def _record_discord_fa_submitted(discord_id: str, cand: dict, slug: str):
    """Mirrors submit_discord_fa_signings.py's own candidate_key format so
    that script's de-dup check (reading the same file) can never resubmit
    this candidate as a duplicate transaction."""
    key = ":".join(str(x) for x in (
        discord_id, cand["kind"], slug,
        cand.get("team") or "", cand.get("decision") or "", cand.get("year") or "",
    ))
    state = _load_json(DISCORD_FA_SUBMITTED_FILE, {})
    state[key] = {"submitted_at": datetime.now(timezone.utc).isoformat(), "via": "cleanup"}
    _save_json(DISCORD_FA_SUBMITTED_FILE, state)


def _apply_discord_fa(cand_row: dict, slug: str, info: dict) -> dict:
    """Writes the historical transaction via the same functions
    submit_discord_fa_signings.py calls through the API — reused, not
    reimplemented. Returns the created transaction record."""
    cand = cand_row["candidate"]
    body = TransactionIn(type=cand["kind"], date=cand_row["date"],
                          description=cand_row["description"], details={}, historical=True)
    if cand["kind"] == "sign":
        details = SignDetails(player=slug, team=cand["team"], contract={})
        txn = _create_historical_sign(details, body, info)
    else:
        details = OptionDetails(player=slug, decision=cand["decision"],
                                 option_type=cand["option_type"], year=cand["year"])
        txn = _create_historical_option(details, body, info)
    _record_discord_fa_submitted(cand_row["discord_id"], cand, slug)
    return txn


def _compute_cleanup_stats(member: str, items: list) -> dict:
    approved_count = sum(1 for it in items if it["submitted_by"] == member and it["status"] == "approved")
    return {"approved_count": approved_count}


class SubmissionCreate(BaseModel):
    gap_type: str = "bio_field"     # "bio_field" | "discord_fa"
    slug: str = ""                  # bio_field: the player being edited
    field: str = ""                 # bio_field: which bio field
    discord_id: str = ""            # discord_fa: source message id
    candidate_index: int = 0        # discord_fa: which candidate within that message
    value: object = None            # bio_field: the field value; discord_fa: {"slug": "..."}
    source_note: str = ""


class RejectBody(BaseModel):
    reason: str


@router.get("/api/cleanup/gaps")
def get_gaps():
    """The open question bank — computed on read, never stored, so it can't
    drift from player-bios.json or the Discord-backfill flagged bucket.
    Excludes anything with a pending submission (someone's already working on
    it), bio fields the already-resolved-undrafted population (draft_year
    set, round/pick null), and — for discord_fa — candidates already approved
    (the source file itself never changes, so unlike a bio field, approval
    alone doesn't make the gap stop existing there)."""
    bios = load_player_bios()
    store = _load_store()
    taken_keys = {_gap_key(it) for it in store["items"] if it["status"] in ("pending", "approved")}
    gaps = []
    for slug, bio in bios.items():
        name = bio.get("name", slug)
        for field in ALL_FIELDS:
            key = ("bio_field", slug, field)
            if _field_is_empty(bio, field) and key not in taken_keys:
                gaps.append({"gap_type": "bio_field", "slug": slug, "name": name, "field": field})
    for row in _discord_fa_flagged_candidates():
        key = ("discord_fa", row["discord_id"], row["candidate_index"])
        if key in taken_keys:
            continue
        cand = row["candidate"]
        gaps.append({
            "gap_type": "discord_fa", "discord_id": row["discord_id"], "candidate_index": row["candidate_index"],
            "date": row["date"], "description": row["description"], "channel": row["channel"],
            "raw_player": cand.get("raw_player"), "guess_slug": cand.get("slug"), "kind": cand["kind"],
            "team": cand.get("team"), "decision": cand.get("decision"),
            "option_type": cand.get("option_type"), "year": cand.get("year"),
        })
    return gaps


@router.get("/api/cleanup/stats")
def get_all_cleanup_stats():
    """Approved-submission counts for every member, public — feeds the
    Archivist achievement tier (members/achievements.js) the same way
    /api/bets/stats feeds the betting ones."""
    all_members = load_members()
    items = _load_store()["items"]
    return {m: _compute_cleanup_stats(m, items) for m in all_members}


@router.get("/api/cleanup/stats/{member}")
def get_member_cleanup_stats(member: str):
    all_members = load_members()
    if member not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{member}' not found")
    items = _load_store()["items"]
    return _compute_cleanup_stats(member, items)


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
    context = None
    if body.gap_type == "discord_fa":
        row = _find_discord_fa_candidate(body.discord_id, body.candidate_index)
        if row is None:
            raise HTTPException(status_code=400, detail="Unknown Discord candidate")
        if not isinstance(body.value, dict) or not body.value.get("slug"):
            raise HTTPException(status_code=422, detail="value must be {\"slug\": \"...\"}")
        slug = str(body.value["slug"]).strip().lower()
        bios = load_player_bios()
        if slug not in bios:
            raise HTTPException(status_code=400, detail="Unknown player")
        normalized = {"slug": slug}
        log_label = f"{body.discord_id}#{body.candidate_index} -> {slug}"
        # Snapshotted so Mine/Review can render the raw message without a
        # second lookup — once pending, this candidate drops out of
        # /api/cleanup/gaps (see get_gaps), so there'd be nowhere left to
        # look it up from otherwise.
        cand = row["candidate"]
        context = {
            "date": row["date"], "description": row["description"], "channel": row["channel"],
            "raw_player": cand.get("raw_player"), "kind": cand["kind"], "team": cand.get("team"),
            "decision": cand.get("decision"), "option_type": cand.get("option_type"), "year": cand.get("year"),
        }
    else:
        bios = load_player_bios()
        if body.slug not in bios:
            raise HTTPException(status_code=400, detail="Unknown player")
        if body.field not in ALL_FIELDS:
            raise HTTPException(status_code=422, detail=f"Unknown field {body.field!r}")
        if not _field_is_empty(bios[body.slug], body.field):
            raise HTTPException(status_code=409, detail="This field is no longer a gap — already filled")
        normalized = _validate_value(body.field, body.value)
        log_label = f"{body.slug}.{body.field} = {normalized!r}"

    now = datetime.now(timezone.utc).isoformat()
    with _cleanup_lock:
        store = _load_store()
        store["seq"] += 1
        submission = {
            "id": store["seq"],
            "gap_type": body.gap_type,
            "slug": body.slug,
            "field": body.field,
            "discord_id": body.discord_id,
            "candidate_index": body.candidate_index,
            "context": context,
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
        key = _gap_key(submission)
        if any(_gap_key(it) == key and it["status"] == "approved" for it in store["items"]):
            raise HTTPException(status_code=409, detail="This gap is no longer open — already approved")
        store["items"].append(submission)
        _save_store(store)
    log_write(info, f"POST cleanup/submissions — {log_label}")
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

        if sub["gap_type"] == "discord_fa":
            row = _find_discord_fa_candidate(sub["discord_id"], sub["candidate_index"])
            if row is None:
                raise HTTPException(status_code=404, detail="Source candidate no longer exists")
            key = _gap_key(sub)
            if any(_gap_key(it) == key and it["status"] == "approved" and it["id"] != sub["id"] for it in store["items"]):
                sub["status"] = "rejected"
                sub["reject_reason"] = "Another submission for this candidate was already approved"
                sub["reviewed_by"] = info["name"]
                sub["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                _save_store(store)
                raise HTTPException(status_code=409, detail="Already resolved by another submission — auto-rejected")
            _apply_discord_fa(row, sub["value"]["slug"], info)
            reward = DISCORD_FA_REWARD
            reward_label = f"discord_fa {sub['discord_id']}#{sub['candidate_index']}"
        else:
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
            reward = CLEANUP_FIELD_REWARDS[sub["field"]]
            reward_label = f"{sub['slug']}.{sub['field']}"

        now = datetime.now(timezone.utc).isoformat()
        sub["status"] = "approved"
        sub["reviewed_by"] = info["name"]
        sub["reviewed_at"] = now
        sub["reward_nby"] = reward

        # Any other pending submission racing for the same gap is now moot —
        # not the submitter's fault, so superseded rather than rejected.
        key = _gap_key(sub)
        for other in store["items"]:
            if other is sub:
                continue
            if other["status"] == "pending" and _gap_key(other) == key:
                other["status"] = "superseded"
                other["reviewed_by"] = info["name"]
                other["reviewed_at"] = now

        _save_store(store)

    _award_cleanup_reward(sub["submitted_by"], reward, f"Clean Up the Poo Poo: {reward_label}")
    log_write(info, f"POST cleanup/submissions/{sub_id}/approve — {reward_label}, NB¥{reward} to {sub['submitted_by']}")
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

"""PDC extension pipeline (§ 6.2 / § 6.3) — see
nbn-today/docs/poext-extension-pipeline.md for the design record (D1-D15) this
follows, and its § 3 "object mapping" table for exactly what's reused
verbatim from routers/free_agency.py versus rebuilt.

Deliberately its own module and its own storage files rather than an
extension of free_agency.py, even though the shapes are close cousins — see
constants.py's comment on POEXT_PROPOSALS_FILE for why. What genuinely is
shared is imported, not re-derived: `_member_teams` (conflict resolution),
`_member_current_team`, and every piece of § 6.2 rules logic
(`_validate_extension`, `_extension_fact_sheet`, `_extension_frame`,
`_extension_eligibility_check`, `_bird_tenure`) come from elsewhere. Nothing
here re-implements a rule `POST /api/validate/extension` already enforces —
`_apply_extension` (also imported) is the one function that ever writes a
real extension to the ledger, at finalize... except finalize does *not* call
it (see `finalize_player` below): same manual hand-off free agency uses.

What's smaller here than free_agency.py, and why:
  * No mode/rounds/FFA clock (§ 2.6) — § 6.3's windows are calendar deadlines
    the validator already checks, not a race the pipeline has to referee.
  * No per-team offer list on a player — § 2.4, only the incumbent may
    extend, so there is one proposal in play per player, not N competing
    ones. `LIVE_STATUSES` still matters (draft/submitted/returned), but
    there's no "which of several" indexing anywhere.
  * No pending-cap-hold tracking (D7) — a live proposal holds no room.
  * Ballot is accept/reject majority (D6), not 1,000 balls — much less
    machinery, and the "revisit D6 later" flag lives in the spec, not here.
  * `finalize` has no agent-uncontested shortcut (§ 2.9a) — always head-only,
    since an extension never has FA's contested/uncontested axis to collapse.
"""
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import inbox, poext_notify
from .auth import get_token_info, has_role, load_members, require_role
from .constants import (POEXT_PROPOSALS_FILE, POEXT_STATE_FILE,
                        POEXT_VOTES_FILE, VALID_TEAMS)
from .free_agency import _member_teams
from .players import _build_team_map, load_player_bios
from .proposals import _member_current_team
from .storage import _current_league_year, _load_json, _save_json, log_write
from .transactions import (ContractIn, ExtensionDetails,
                           _bird_tenure, _extension_eligibility_check,
                           _extension_fact_sheet, _extension_frame,
                           _require_validatable, _validate_extension,
                           _validation_ctx)

router = APIRouter()

# One lock, same reasoning free_agency.py gives for its own: every write that
# matters spans proposals + state (submit stamps a rejection counter *and*
# appends history; finalize reads votes *and* writes state), and the write
# traffic is a committee of a few people.
_poext_lock = threading.RLock()

LIVE_STATUSES = {"draft", "submitted", "returned"}
VOIDABLE_STATUSES = {"submitted", "returned"}
MAX_REJECTIONS = 3   # § 6.3: "no further extension opportunities arise" after the third


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── storage ───────────────────────────────────────────────────────────────────

def _load_proposals() -> list[dict]:
    return _load_json(POEXT_PROPOSALS_FILE, [])


def _save_proposals(proposals: list[dict]):
    _save_json(POEXT_PROPOSALS_FILE, proposals)


def _load_state() -> dict:
    st = _load_json(POEXT_STATE_FILE, {})
    st.setdefault("seq", 0)
    st.setdefault("players", {})
    return st


def _save_state(state: dict):
    _save_json(POEXT_STATE_FILE, state)


def _load_votes() -> dict:
    return _load_json(POEXT_VOTES_FILE, {})


def _save_votes(votes: dict):
    _save_json(POEXT_VOTES_FILE, votes)


def _player_entry(state: dict, slug: str) -> dict:
    return state["players"].get(slug) or {}


# ── eligibility (Phase B) ───────────────────────────────────────────────────

@router.get("/api/poext/eligible")
def get_eligible():
    """Every rostered player, `eligible` + a server-composed `reason` — the
    same `_accepts_offers`-style contract `GET /api/fa/pool` uses, off the
    exact eligibility check `POST /api/validate/extension` runs (§ 6.2 rules
    1-2 only; service and terms are proposal-specific and can't be judged with
    no proposed contract to check them against). Public, like the FA pool."""
    bios = load_player_bios()
    team_map = _build_team_map()
    cur_season = _current_league_year()
    out = []
    for slug, team in sorted(team_map.items()):
        bio = bios.get(slug) or {}
        if bio.get("retired") or bio.get("type") in ("dead", "draft-rights"):
            continue
        frame = _extension_frame(slug, team, bio, cur_season)
        check = _extension_eligibility_check(slug, frame, cur_season)
        out.append({
            "player": slug, "player_name": bio.get("name") or slug, "team": team,
            "eligible": check.passed, "reason": check.message,
            "basis": frame["start_basis"],
        })
    return out


# ── visibility ────────────────────────────────────────────────────────────────

def _is_head(info: dict) -> bool:
    return has_role(info, "poext_head") or has_role(info, "admin")


def _is_agent(info: dict) -> bool:
    return has_role(info, "agent")


def _is_assigned(state: dict, slug: str, name: str) -> bool:
    return name in (_player_entry(state, slug).get("subcommittee") or [])


def _current_cycle(proposals: list[dict], slug: str) -> Optional[str]:
    """The id of this player's most recent proposal, live or not — the
    negotiation-cycle discriminator claim state and votes are scoped to.

    Mirrors FA's `round_id`, but there's no separate rounds concept to mint
    one from: a proposal's own id already identifies one negotiation
    uniquely, and § 2.4 (only the incumbent may propose) means there's never
    more than one live proposal to disambiguate between. Without this, a
    second proposal after a rejection would inherit the first proposal's
    claim and votes — an agent who can no longer act (the negotiation they
    were claimed for is over) still reading as the holder, and a fresh
    proposal reading as already-finalized before anyone has voted on it.
    """
    mine = [p for p in proposals if p["player"] == slug]
    return max(mine, key=lambda p: p["number"])["id"] if mine else None


def _agent_node(state: dict, slug: str, cycle: Optional[str]) -> dict:
    node = _player_entry(state, slug).get("agent") or {}
    return node if node.get("cycle") == cycle else {}


def _claim_holder(state: dict, slug: str, cycle: Optional[str]) -> Optional[str]:
    node = _agent_node(state, slug, cycle)
    return None if node.get("released_at") else node.get("claimed_by")


def _is_advanced(state: dict, slug: str, cycle: Optional[str]) -> bool:
    return bool(_agent_node(state, slug, cycle).get("advanced_at"))


def _proposal_conflict(name: str, proposal: dict) -> Optional[str]:
    """The member's own team, when it's the team proposing this extension —
    the poext analogue of free_agency._conflict_team, but against a single
    proposal.team rather than a list of live offers (§ 2.4: only the
    incumbent may propose, so there's exactly one team to check)."""
    team = (proposal.get("team") or "").upper()
    return team if team in _member_teams(name) else None


def _require_curator(info: dict, state: dict, slug: str, cycle: Optional[str]):
    """Who may submit-adjacent-act on this player's proposal — remand, void,
    restore (mirrors free_agency._require_curator, § 4.7 reversing D14)."""
    if _is_head(info):
        return
    name = info.get("name")
    if _claim_holder(state, slug, cycle) == name and not _is_advanced(state, slug, cycle):
        return
    if _is_agent(info):
        holder = _claim_holder(state, slug, cycle)
        if holder and holder != name:
            raise HTTPException(403, f"{holder} is the agent on this extension")
        if _is_advanced(state, slug, cycle):
            raise HTTPException(403, "This extension has been advanced to the sub-committee — "
                                     "ask the PO-EXT head to send it back")
        raise HTTPException(403, "Claim this player before acting on their proposal")
    raise HTTPException(403, "Only this player's agent or the PO-EXT head can do that")


def _require_reviewer(info: dict, state: dict, slug: str, cycle: Optional[str]):
    if _is_head(info) or (has_role(info, "poext") and _is_assigned(state, slug, info["name"])):
        return
    if _claim_holder(state, slug, cycle) == info.get("name"):
        return
    raise HTTPException(403, "You aren't on this player's sub-committee")


def _require_ballot_viewer(info: dict, state: dict, slug: str):
    """§ 4.5's rule, ported: an agent never sees a vote, including on a player
    they claimed and advanced — their judgment is spent on the proposal."""
    if _is_head(info) or (has_role(info, "poext") and _is_assigned(state, slug, info["name"])):
        return
    raise HTTPException(403, "You aren't on this player's sub-committee")


def _require_team_role(info: dict, team: str):
    if not (has_role(info, team.lower()) or has_role(info, "admin")):
        raise HTTPException(403, f"Requires the {team} role")


def _is_exhausted(state: dict, slug: str) -> bool:
    return _player_entry(state, slug).get("rejections", 0) >= MAX_REJECTIONS


def _vote_node(votes: dict, slug: str, cycle: Optional[str], create: bool = False) -> dict:
    """votes[slug][cycle] = {votes: {member: cast}, final: {...} | None} —
    mirrors free_agency._ballot_node(ballots, slug, round_id, create)
    exactly, with the live proposal's own id standing in for round_id."""
    per_player = votes.setdefault(slug, {}) if create else (votes.get(slug) or {})
    if create:
        return per_player.setdefault(cycle, {"votes": {}, "final": None})
    return per_player.get(cycle) or {"votes": {}, "final": None}


def _stage(state: dict, slug: str, cycle: Optional[str], has_live: bool, decided: bool) -> str:
    """`open` → `awaiting_agent` → `with_agent` → `with_committee` → `decided`.

    Unlike FA's `_agent_stage`, `awaiting_agent` isn't gated on a window
    closing — there is no window to wait out (§ 2.6). It just means "a live
    proposal exists and nobody has claimed it yet."
    """
    if decided:
        return "decided"
    if _is_advanced(state, slug, cycle):
        return "with_committee"
    if _claim_holder(state, slug, cycle):
        return "with_agent"
    if has_live:
        return "awaiting_agent"
    return "open"


@router.get("/api/poext/proposals")
def list_proposals(player: Optional[str] = None, team: Optional[str] = None,
                   status: Optional[str] = None, info: dict = Depends(get_token_info)):
    """Scoped per § 6.1's spirit — a member with no relevant standing gets an
    empty list, not a 403, mirroring free_agency.list_offers. A draft is
    visible only to the proposing team (+ admin) since a team hasn't chosen
    to show anyone yet; anything submitted or further along is visible to any
    committee holder (agent/poext/poext_head) as well as the proposing team,
    since that's the queue this endpoint exists to back."""
    proposals = _load_proposals()
    if player:
        proposals = [p for p in proposals if p["player"] == player]
    if team:
        proposals = [p for p in proposals if p["team"] == team.upper()]
    if status:
        proposals = [p for p in proposals if p["status"] == status]

    is_committee = _is_head(info) or has_role(info, "poext") or has_role(info, "agent")
    my_teams = _member_teams(info.get("name") or "")

    def visible(p):
        if p["team"] in my_teams:
            return True
        if p["status"] == "draft":
            return False
        return is_committee

    return [p for p in proposals if visible(p)]


# ── proposals ─────────────────────────────────────────────────────────────────

class ProposalContract(BaseModel):
    salaries: dict[str, str] = {}
    cap_holds: dict[str, str] = {}


class ProposalCreate(BaseModel):
    player: str
    team: str
    kind: str = "veteran"
    contract: ProposalContract
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None
    attested_contract_start: Optional[str] = None


class ProposalPatch(BaseModel):
    kind: Optional[str] = None
    contract: Optional[ProposalContract] = None
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None
    attested_contract_start: Optional[str] = None


def _find_proposal(proposals: list[dict], proposal_id: str) -> tuple[int, dict]:
    idx = next((i for i, p in enumerate(proposals) if p["id"] == proposal_id), None)
    if idx is None:
        raise HTTPException(404, "Proposal not found")
    return idx, proposals[idx]


def _is_live(p: dict) -> bool:
    return p.get("archived_at") is None and p["status"] in LIVE_STATUSES


def _live_proposal_for(proposals: list[dict], slug: str) -> Optional[dict]:
    return next((p for p in proposals if p["player"] == slug and _is_live(p)), None)


def _extension_details(p: dict) -> ExtensionDetails:
    return ExtensionDetails(
        player=p["player"], team=p["team"], kind=p["kind"],
        contract=ContractIn(type="player", **p["contract"]),
        bird_rights_type=p.get("bird_rights_type"),
        eaps_assumption=p.get("eaps_assumption"),
        attested_contract_start=p.get("attested_contract_start"),
    )


def _run_validation(p: dict, ctx: dict) -> dict:
    """The one legality call — same `_validate_extension` the submit path of
    `POST /api/transactions` runs, plus the same fact sheet `/transaction-sim`
    renders, so nothing in this module does cap math of its own."""
    details = _extension_details(p)
    _require_validatable(details.team, details.player, ctx)
    checks = [c.model_dump() for c in _validate_extension(details, ctx)]
    sheet = _extension_fact_sheet(details, ctx)
    return {
        "legal": not any(not c["passed"] and c["level"] == "error" for c in checks),
        "checks": checks, "fact_sheet": sheet,
        "validated_at": _now(), "season": ctx["cur_season"],
    }


@router.post("/api/poext/proposals")
def create_proposal(body: ProposalCreate, info: dict = Depends(get_token_info)):
    team = body.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(400, f"Unknown team '{team}'")
    _require_team_role(info, team)
    team_map = _build_team_map()
    if team_map.get(body.player) != team:
        raise HTTPException(422, f"'{body.player}' is not on {team}'s roster — "
                                 f"only the incumbent may propose an extension (§ 6.2)")

    with _poext_lock:
        state = _load_state()
        if _is_exhausted(state, body.player):
            raise HTTPException(422, f"§ 6.3: three proposals on {body.player} have already "
                                     f"been rejected — no further opportunities arise")
        proposals = _load_proposals()
        if _live_proposal_for(proposals, body.player):
            raise HTTPException(409, "There's already a live proposal on this player")
        state["seq"] += 1
        proposal = {
            "id": secrets.token_hex(4), "number": state["seq"],
            "player": body.player, "team": team,
            "status": "draft", "version": 1, "versions": [], "remands": [],
            "created_by": info["name"], "submitted_by": None,
            "created_at": _now(), "updated_at": _now(), "submitted_at": None,
            "archived_at": None, "void": None,
            "kind": body.kind,
            "contract": body.contract.model_dump(),
            "bird_rights_type": body.bird_rights_type,
            "eaps_assumption": body.eaps_assumption,
            "attested_contract_start": body.attested_contract_start,
            "validation": None,
            "history": [{"ts": _now(), "actor": info["name"], "from": None, "to": "draft"}],
        }
        proposals.append(proposal)
        _save_proposals(proposals)
        _save_state(state)
    log_write(info, f"POST poext/proposals — #{proposal['number']} {team} → {body.player}")
    return proposal


@router.patch("/api/poext/proposals/{proposal_id}")
def patch_proposal(proposal_id: str, body: ProposalPatch, info: dict = Depends(get_token_info)):
    """Editable while `draft` — and while `returned`, the committee's own
    doing. A team can never reach for the revision path itself (mirrors
    free_agency.patch_offer's § 3.14 reasoning)."""
    fields = body.model_dump(exclude_unset=True)
    with _poext_lock:
        proposals = _load_proposals()
        idx, p = _find_proposal(proposals, proposal_id)
        _require_team_role(info, p["team"])
        if not _is_live(p) or p["status"] not in ("draft", "returned"):
            raise HTTPException(409, "A submitted proposal is final — the committee may send it "
                                     "back, the team may not withdraw it")
        if "contract" in fields:
            p["contract"] = body.contract.model_dump()
        for key in ("kind", "bird_rights_type", "eaps_assumption", "attested_contract_start"):
            if key in fields:
                p[key] = fields[key]
        p["updated_at"] = _now()
        proposals[idx] = p
        _save_proposals(proposals)
    log_write(info, f"PATCH poext/proposals/{proposal_id}")
    return p


@router.delete("/api/poext/proposals/{proposal_id}")
def delete_proposal(proposal_id: str, info: dict = Depends(get_token_info)):
    with _poext_lock:
        proposals = _load_proposals()
        idx, p = _find_proposal(proposals, proposal_id)
        _require_team_role(info, p["team"])
        if p["status"] != "draft":
            raise HTTPException(409, "Only a draft can be deleted — a submitted proposal is final")
        proposals.pop(idx)
        _save_proposals(proposals)
    log_write(info, f"DELETE poext/proposals/{proposal_id}")
    return {"ok": True}


@router.post("/api/poext/proposals/{proposal_id}/submit")
def submit_proposal(proposal_id: str, info: dict = Depends(get_token_info)):
    """Submit, and the resubmit path for a remanded proposal. Once through,
    final at the team's initiative — no withdraw, no post-submit edit."""
    with _poext_lock:
        proposals = _load_proposals()
        idx, p = _find_proposal(proposals, proposal_id)
        _require_team_role(info, p["team"])
        if not _is_live(p) or p["status"] not in ("draft", "returned"):
            raise HTTPException(409, f"Proposal is {p['status']}, not submittable")
        state = _load_state()
        if _is_exhausted(state, p["player"]):
            raise HTTPException(422, f"§ 6.3: three proposals on {p['player']} have already "
                                     f"been rejected — no further opportunities arise")

        ctx = _validation_ctx()
        validation = _run_validation(p, ctx)
        if not validation["legal"]:
            failing = [c["check"] for c in validation["checks"]
                       if not c["passed"] and c["level"] == "error"]
            raise HTTPException(422, {"detail": "Proposal fails a rule check", "checks": failing,
                                      "validation": validation})

        resubmit = p["status"] == "returned"
        if resubmit:
            p["version"] += 1
        p["status"] = "submitted"
        p["submitted_by"] = info["name"]
        p["submitted_at"] = _now()
        p["updated_at"] = p["submitted_at"]
        p["validation"] = validation
        p["history"].append({"ts": _now(), "actor": info["name"],
                             "from": "returned" if resubmit else "draft",
                             "to": "submitted", "version": p["version"]})
        proposals[idx] = p
        _save_proposals(proposals)
    log_write(info, f"POST poext/proposals/{proposal_id}/submit — v{p['version']}")
    # Both the first submission and a resubmission after a remand post here —
    # one endpoint, one announcement path, same as fa_notify.notify_offer_submitted.
    poext_notify.notify_proposal_submitted(p)
    if not resubmit:
        # D13: scoped to poext holders, not the wider committee ecosystem —
        # mirrors the public/private Discord split at the individual level.
        inbox.notify_role("poext", f"{p['team']} submitted an extension proposal for "
                          f"{p['player']} — claim it to start negotiating.", link="/pdc")
    return p


@router.post("/api/poext/proposals/{proposal_id}/remand")
def remand_proposal(proposal_id: str, body: dict, info: dict = Depends(get_token_info)):
    note = (body.get("note") or "").strip()
    if not note:
        raise HTTPException(422, "A remand requires a note saying what should change")
    with _poext_lock:
        state = _load_state()
        proposals = _load_proposals()
        idx, p = _find_proposal(proposals, proposal_id)
        _require_curator(info, state, p["player"], p["id"])
        if not _is_live(p) or p["status"] not in ("submitted", "returned"):
            raise HTTPException(409, f"Proposal is {p['status']} — only a submitted proposal can be sent back")
        votes = _load_votes()
        if _vote_node(votes, p["player"], p["id"]).get("final"):
            raise HTTPException(409, "This proposal is finalized — reopen it before sending it back")
        if not any(v["version"] == p["version"] for v in p["versions"]):
            p["versions"].append({
                "version": p["version"], "kind": p["kind"], "contract": dict(p["contract"]),
                "bird_rights_type": p.get("bird_rights_type"),
                "eaps_assumption": p.get("eaps_assumption"),
                "validation": p.get("validation"),
                "submitted_at": p.get("submitted_at"), "submitted_by": p.get("submitted_by"),
            })
        entry = {"at": _now(), "by": info["name"], "note": note, "from_version": p["version"],
                 "conflict": _proposal_conflict(info["name"], p)}
        p["remands"].append(entry)
        if p["status"] == "submitted":
            p["status"] = "returned"
            p["history"].append({"ts": _now(), "actor": info["name"], "from": "submitted", "to": "returned"})
        p["updated_at"] = _now()
        proposals[idx] = p
        _save_proposals(proposals)
    log_write(info, f"POST poext/proposals/{proposal_id}/remand")
    poext_notify.notify_proposal_remanded(p, entry)
    recipient = p.get("submitted_by") or p.get("created_by")
    if recipient:
        inbox.notify_member(recipient, f"Your extension proposal for {p['player']} was sent back: {note}",
                            link="/extensions")
    return p


@router.post("/api/poext/proposals/{proposal_id}/void")
def void_proposal(proposal_id: str, body: dict, info: dict = Depends(get_token_info)):
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "A void requires a reason — the team can't answer it")
    with _poext_lock:
        state = _load_state()
        proposals = _load_proposals()
        idx, p = _find_proposal(proposals, proposal_id)
        _require_curator(info, state, p["player"], p["id"])
        if p.get("archived_at") is not None:
            raise HTTPException(409, "This player is finalized — reopen before voiding")
        if p["status"] not in VOIDABLE_STATUSES:
            raise HTTPException(409, f"Proposal is {p['status']} — only a submitted proposal can be voided")
        votes = _load_votes()
        if _vote_node(votes, p["player"], p["id"]).get("final"):
            raise HTTPException(409, "This proposal is finalized — reopen before voiding")
        p["void"] = {"at": _now(), "by": info["name"], "reason": reason, "from_status": p["status"]}
        p["history"].append({"ts": p["void"]["at"], "actor": info["name"],
                             "from": p["status"], "to": "voided", "reason": reason})
        p["status"] = "voided"
        p["updated_at"] = p["void"]["at"]
        proposals[idx] = p
        _save_proposals(proposals)
    log_write(info, f"POST poext/proposals/{proposal_id}/void")
    poext_notify.notify_proposal_voided(p)
    recipient = p.get("submitted_by") or p.get("created_by")
    if recipient:
        inbox.notify_member(recipient, f"Your extension proposal for {p['player']} was voided: {reason}",
                            link="/extensions")
    return p


@router.post("/api/poext/proposals/{proposal_id}/restore")
def restore_proposal(proposal_id: str, info: dict = Depends(get_token_info)):
    with _poext_lock:
        state = _load_state()
        proposals = _load_proposals()
        idx, p = _find_proposal(proposals, proposal_id)
        _require_curator(info, state, p["player"], _current_cycle(proposals, p["player"]))
        if p["status"] != "voided":
            raise HTTPException(409, f"Proposal is {p['status']}, not voided")
        if p.get("archived_at") is not None:
            raise HTTPException(409, "This player was finalized after the void — reopen first")
        back = (p.get("void") or {}).get("from_status") or "submitted"
        if any(o["team"] == p["team"] and o["player"] == p["player"] and o["id"] != p["id"] and _is_live(o)
               for o in proposals):
            raise HTTPException(409, f"{p['team']} has since submitted another proposal on this player")
        voided = p.get("void") or {}
        p["status"] = back
        p["void"] = None
        p["updated_at"] = _now()
        p["history"].append({"ts": p["updated_at"], "actor": info["name"], "from": "voided", "to": back})
        proposals[idx] = p
        _save_proposals(proposals)
    log_write(info, f"POST poext/proposals/{proposal_id}/restore")
    poext_notify.notify_proposal_restored(p)
    recipient = p.get("submitted_by") or p.get("created_by")
    if recipient:
        inbox.notify_member(recipient, f"Your extension proposal for {p['player']} was restored — it's live again",
                            link="/extensions")
    return p


# ── the agent stage ──────────────────────────────────────────────────────────

@router.post("/api/poext/players/{slug}/claim")
def claim_player(slug: str, info: dict = Depends(require_role("agent"))):
    """Take a player with a live submitted proposal (§ 6.2/D3). Exclusive.

    Unlike FA's claim, there's no permanent `blocked_teams` bar on success —
    that mechanism exists to stop an agent who has seen every rival's offer
    from later bidding with the information advantage. There are no rival
    bids here (§ 2.4: only the incumbent may propose), so the actual risk is
    narrower and different: an agent shouldn't be negotiating *for* the same
    team they're supposed to be representing the player *against*. Refused
    outright at claim time instead — a conflicted agent is simply not this
    player's agent, not a barred bidder to track after the fact.
    """
    with _poext_lock:
        state = _load_state()
        proposals = _load_proposals()
        live = _live_proposal_for(proposals, slug)
        if not live or live["status"] != "submitted":
            raise HTTPException(422, "No submitted proposal on this player to claim")
        if _is_exhausted(state, slug):
            raise HTTPException(422, "This player's extension opportunities are exhausted")
        cycle = live["id"]
        holder = _claim_holder(state, slug, cycle)
        if holder:
            raise HTTPException(422, f"You're the agent on this player." if holder == info["name"]
                                else f"{holder} is the agent on this player.")
        if _is_advanced(state, slug, cycle):
            raise HTTPException(422, "This player has already been advanced to the sub-committee")
        if not _is_head(info) and live["team"] in _member_teams(info["name"]):
            raise HTTPException(422, f"You hold a role with {live['team']}, the team proposing "
                                     f"this extension — an agent can't represent the player "
                                     f"against their own team")
        entry = state["players"].setdefault(slug, {})
        entry["agent"] = {
            "cycle": cycle,
            "claimed_by": info["name"], "claimed_at": _now(),
            "released_at": None, "released_by": None,
            "advanced_at": None, "advanced_by": None, "note": "", "returns": [],
        }
        _save_state(state)
    log_write(info, f"POST poext/players/{slug}/claim")
    return {"player": slug, "agent": entry["agent"]}


@router.post("/api/poext/players/{slug}/release")
def release_player(slug: str, info: dict = Depends(get_token_info)):
    with _poext_lock:
        state = _load_state()
        cycle = _current_cycle(_load_proposals(), slug)
        holder = _claim_holder(state, slug, cycle)
        if not holder:
            raise HTTPException(409, "Nobody holds this player")
        if holder != info["name"] and not _is_head(info):
            raise HTTPException(403, f"{holder} is the agent on this player")
        if _is_advanced(state, slug, cycle):
            raise HTTPException(409, "This player has already been advanced to the sub-committee")
        node = state["players"][slug]["agent"]
        node["released_at"] = _now()
        node["released_by"] = info["name"]
        _save_state(state)
    log_write(info, f"POST poext/players/{slug}/release")
    return {"player": slug, "agent": node}


@router.post("/api/poext/players/{slug}/advance")
def advance_player(slug: str, body: dict, info: dict = Depends(get_token_info)):
    note = (body.get("note") or "").strip()
    with _poext_lock:
        state = _load_state()
        proposals = _load_proposals()
        live = _live_proposal_for(proposals, slug)
        if not live or live["status"] not in ("submitted", "returned"):
            raise HTTPException(422, "No live proposal on this player to advance — "
                                     "void it or wait for a resubmission")
        cycle = live["id"]
        _require_curator(info, state, slug, cycle)
        if _is_advanced(state, slug, cycle):
            raise HTTPException(409, "This player has already been advanced")
        node = state["players"].setdefault(slug, {}).setdefault("agent", {
            "cycle": cycle, "claimed_by": None, "claimed_at": None, "released_at": None,
            "released_by": None, "returns": [],
        })
        node["cycle"] = cycle
        node["advanced_at"] = _now()
        node["advanced_by"] = info["name"]
        node["note"] = note
        _save_state(state)
    log_write(info, f"POST poext/players/{slug}/advance")
    return {"player": slug, "agent": node, "proposal": live["id"]}


@router.post("/api/poext/players/{slug}/return-to-agent")
def return_to_agent(slug: str, body: dict, info: dict = Depends(require_role("poext_head"))):
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(422, "A reason is required")
    with _poext_lock:
        state = _load_state()
        cycle = _current_cycle(_load_proposals(), slug)
        node = _agent_node(state, slug, cycle)
        if not node.get("advanced_at"):
            raise HTTPException(409, "This player hasn't been advanced")
        node.setdefault("returns", []).append(
            {"at": _now(), "by": info["name"], "reason": reason, "advanced_at": node["advanced_at"]})
        node["advanced_at"] = None
        node["advanced_by"] = None
        _save_state(state)
    log_write(info, f"POST poext/players/{slug}/return-to-agent")
    return {"player": slug, "agent": node}


@router.put("/api/poext/players/{slug}/assign")
def assign_subcommittee(slug: str, body: dict, info: dict = Depends(require_role("poext_head"))):
    members_body = body.get("subcommittee")
    if members_body is None:
        raise HTTPException(422, "subcommittee is required")
    members = load_members()
    unknown = [m for m in members_body if m not in members]
    if unknown:
        raise HTTPException(422, f"Unknown member(s): {unknown}")
    with _poext_lock:
        state = _load_state()
        entry = state["players"].setdefault(slug, {})
        entry["subcommittee"] = list(dict.fromkeys(members_body))
        _save_state(state)
    log_write(info, f"PUT poext/players/{slug}/assign — {entry['subcommittee']}")
    return {"player": slug, "subcommittee": entry["subcommittee"]}


# ── review + votes ────────────────────────────────────────────────────────────

@router.get("/api/poext/players/{slug}/review")
def review_player(slug: str, info: dict = Depends(get_token_info)):
    state = _load_state()
    proposals = _load_proposals()
    cycle = _current_cycle(proposals, slug)
    _require_reviewer(info, state, slug, cycle)
    live = _live_proposal_for(proposals, slug)
    votes_all = _load_votes()
    node = _vote_node(votes_all, slug, cycle)

    revalidation = None
    if live:
        ctx = _validation_ctx()
        try:
            revalidation = _run_validation(live, ctx)
        except HTTPException as e:
            revalidation = {"legal": False, "checks": [], "error": e.detail}

    voided = [p for p in proposals if p["player"] == slug and p["status"] == "voided"
             and p.get("archived_at") is None]
    assigned = _player_entry(state, slug).get("subcommittee") or []
    return {
        "player": slug,
        "proposal": live,
        "revalidation": revalidation,
        "outstanding_remands": [r for r in (live or {}).get("remands", []) if r["from_version"] >= (live or {}).get("version", 0)] if live else [],
        "voided_proposals": voided,
        "state": {
            **_player_entry(state, slug),
            "stage": _stage(state, slug, cycle, live is not None, bool(node.get("final"))),
            "claimed_by": _claim_holder(state, slug, cycle),
            "is_agent": _claim_holder(state, slug, cycle) == info.get("name"),
            "exhausted": _is_exhausted(state, slug),
            "rejections": _player_entry(state, slug).get("rejections", 0),
        },
        "votes": node["votes"] if _is_head(info) or (has_role(info, "poext") and _is_assigned(state, slug, info["name"])) else {},
        "assigned": assigned,
        "final": node["final"],
        "your_conflict": _proposal_conflict(info.get("name") or "", live) if live else None,
    }


class VoteIn(BaseModel):
    vote: str    # "accept" | "reject"
    note: str = ""


@router.put("/api/poext/players/{slug}/vote")
def cast_vote(slug: str, body: VoteIn, info: dict = Depends(get_token_info)):
    """Own vote only, and only if assigned — never admin-waved (D6, mirrors
    free_agency.cast_ballot: a vote is not an administrative action)."""
    if body.vote not in ("accept", "reject"):
        raise HTTPException(422, "vote must be 'accept' or 'reject'")
    state = _load_state()
    if not _is_assigned(state, slug, info["name"]):
        raise HTTPException(403, "You aren't on this player's sub-committee")
    proposals = _load_proposals()
    cycle = _current_cycle(proposals, slug)
    if not _is_advanced(state, slug, cycle):
        raise HTTPException(422, "This player is still with their agent — "
                                 "voting opens when the proposal is advanced")
    with _poext_lock:
        votes = _load_votes()
        node = _vote_node(votes, slug, cycle, create=True)
        if node.get("final"):
            raise HTTPException(409, "This proposal is finalized — voting is locked")
        live = _live_proposal_for(proposals, slug)
        node["votes"][info["name"]] = {
            "vote": body.vote, "note": body.note.strip(), "updated_at": _now(),
            "conflict": _proposal_conflict(info["name"], live) if live else None,
        }
        _save_votes(votes)
    log_write(info, f"PUT poext/players/{slug}/vote — {body.vote}")
    return node["votes"][info["name"]]


@router.post("/api/poext/players/{slug}/finalize")
def finalize_player(slug: str, info: dict = Depends(require_role("poext_head"))):
    """Locks the vote and records the outcome. Head-only, full stop — no
    agent-uncontested shortcut (§ 2.9a): an extension is always a single
    up-or-down merits call on one set of terms, since § 2.4 already removes
    the contested/uncontested axis FA's shortcut collapses.

    Does **not** write a transaction on accept, matching FA's finalize: the
    committee types the agreed extension into /transactions by hand, checked
    against the same POST /api/validate/extension either way.
    """
    with _poext_lock:
        state = _load_state()
        proposals = _load_proposals()
        cycle = _current_cycle(proposals, slug)
        if not _is_advanced(state, slug, cycle):
            raise HTTPException(422, "This player hasn't been advanced to the sub-committee")
        votes = _load_votes()
        node = _vote_node(votes, slug, cycle, create=True)
        if node.get("final"):
            raise HTTPException(409, "Already finalized")
        live = _live_proposal_for(proposals, slug)
        if not live:
            raise HTTPException(422, "No live proposal on this player to finalize")

        assigned = _player_entry(state, slug).get("subcommittee") or []
        accept = sum(1 for v in node["votes"].values() if v["vote"] == "accept")
        reject = sum(1 for v in node["votes"].values() if v["vote"] == "reject")
        if accept == reject:
            raise HTTPException(409, f"No majority ({accept} accept, {reject} reject) — "
                                     f"cast more votes or resolve it manually before finalizing")
        outcome = "agreed" if accept > reject else "rejected"

        idx = next(i for i, p in enumerate(proposals) if p["id"] == live["id"])
        live["status"] = outcome
        live["archived_at"] = _now()
        proposals[idx] = live
        for p in proposals:
            if p["player"] == slug and p["status"] == "voided" and p.get("archived_at") is None:
                p["archived_at"] = live["archived_at"]

        rejections = _player_entry(state, slug).get("rejections", 0)
        if outcome == "rejected":
            rejections += 1
        state["players"].setdefault(slug, {})["rejections"] = rejections
        exhausted = rejections >= MAX_REJECTIONS

        node["final"] = {
            "locked_at": _now(), "locked_by": info["name"],
            "outcome": outcome, "accept": accept, "reject": reject,
            "voters": sorted(node["votes"]),
            "abstained": [m for m in assigned if m not in node["votes"]],
            "rejections_total": rejections, "exhausted": exhausted,
        }
        _save_proposals(proposals)
        _save_votes(votes)
        _save_state(state)
    log_write(info, f"POST poext/players/{slug}/finalize — {outcome}")
    poext_notify.notify_player_finalized(slug, live, node["final"])
    recipient = live.get("submitted_by") or live.get("created_by")
    if recipient:
        tail = (" — no further extension opportunities arise (§ 6.3)" if outcome == "rejected" and exhausted else "")
        inbox.notify_member(recipient, f"Voting closed on {slug}'s extension: {outcome}{tail}",
                            link="/extensions")
    return node["final"]


@router.post("/api/poext/players/{slug}/unlock")
def unlock_player(slug: str, info: dict = Depends(require_role("poext_head"))):
    with _poext_lock:
        proposals = _load_proposals()
        cycle = _current_cycle(proposals, slug)
        votes = _load_votes()
        node = _vote_node(votes, slug, cycle, create=True)
        final = node.get("final")
        if not final:
            raise HTTPException(409, "Not finalized")
        if final["outcome"] == "rejected":
            state = _load_state()
            rejections = _player_entry(state, slug).get("rejections", 0)
            state["players"].setdefault(slug, {})["rejections"] = max(0, rejections - 1)
            _save_state(state)
        node["final"] = None
        node.setdefault("unlocks", []).append({"at": _now(), "by": info["name"], "undid": final})
        for p in proposals:
            if p["player"] == slug and p.get("archived_at") == final["locked_at"]:
                p["archived_at"] = None
                p["status"] = "submitted" if p["status"] in ("agreed", "rejected") else p["status"]
        _save_proposals(proposals)
        _save_votes(votes)
    log_write(info, f"POST poext/players/{slug}/unlock")
    return {"player": slug, "undid": final}

"""Trade Request Committee (TRC) — nbn-today/docs/trc-trade-pipeline.md.

A team proposes a trade (built and validated in transaction-sim's trade mode,
POST'd here as exactly the body `/api/validate/trade` already accepts), every
named team consents, three `trc` members ballot on fairness — legality is
already `_validate_trade`'s job, re-run live on every read rather than stored,
since a request must keep passing it for as long as it sits open, not just at
submission — and a `trc_head` finalizes it, which really applies the trade via
`transactions.apply_trade`, the same helper `POST /api/transactions`' own
trade branch uses. A finalized TRC trade is indistinguishable from one entered
directly by `rosters`: same ledger shape, same Discord post.
"""

import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import TRADE_REQUESTS_FILE
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, has_role, require_role, is_team_owner
from .transactions import (
    TradeValidateInput, TradeIn,
    _validation_ctx, _require_trade_validatable, _validate_trade,
    _trade_leg_ownership_problems, _build_team_map, load_picks,
    _load_conveyance_store_for_shadow_check, apply_trade,
)

router = APIRouter()

_trc_lock = threading.Lock()

# A fixed count, not a fraction of assigned members — TRC has no per-request
# sub-committee assignment (unlike PDC/POEXT's per-player ballot rosters);
# any `trc` holder may vote on any open request. Decided 2026-09-20: three
# approvals is a floor to clear the ballot stage, not a majority of the
# committee's actual size.
APPROVALS_NEEDED = 3

_TERMINAL_STATUSES = {"finalized", "rejected", "withdrawn"}


def _load_store() -> dict:
    return _load_json(TRADE_REQUESTS_FILE, {"seq": 0, "items": []})


def _save_store(store: dict):
    _save_json(TRADE_REQUESTS_FILE, store)


def _find(store: dict, request_id: str) -> int:
    idx = next((i for i, r in enumerate(store["items"]) if r["id"] == request_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Trade request not found")
    return idx


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parties(transfers) -> list[str]:
    teams: set[str] = set()
    for tr in transfers:
        teams.add(tr.from_team.upper())
        teams.add(tr.to_team.upper())
    return sorted(teams)


def _refuse_if_terminal(item: dict):
    if item["status"] in _TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Request is already {item['status']}")


def _live_check(item: dict) -> dict:
    """The legality + ownership snapshot, computed fresh on every call —
    never stored. A request has no expiry (decided 2026-09-20): instead it
    just has to keep passing this check for as long as it sits open. Legality
    (`_validate_trade`) and current-ownership (`_trade_leg_ownership_problems`,
    which `_validate_trade` itself never checks — see that function's
    docstring) are reported separately so the dashboard can distinguish "this
    trade is bad" from "one of these assets isn't here anymore"."""
    trade = TradeIn(**item["trade"])
    ctx = _validation_ctx()
    checks = _validate_trade(trade, ctx)
    legal = not any(not c.passed and c.level == "error" for c in checks)

    bios = ctx["bios"]
    team_map = _build_team_map()
    picks = load_picks()
    pick_index = {(int(p["YEAR"]), int(p["ROUND"]), p["ORIG"].upper()): p for p in picks}
    conveyance_store = _load_conveyance_store_for_shadow_check()
    ownership_problems = _trade_leg_ownership_problems(
        trade, bios, team_map, pick_index, conveyance_store)

    return {
        "legal": legal and not ownership_problems,
        "checks": [c.model_dump() for c in checks],
        "ownership_problems": ownership_problems,
    }


def _approve_count(item: dict) -> int:
    return sum(1 for b in item["ballots"].values() if b["decision"] == "approve")


def _public_view(item: dict) -> dict:
    return {**item, "validation": _live_check(item)}


class WithdrawBody(BaseModel):
    reason: str = ""


class BallotBody(BaseModel):
    decision: str
    note: str


class RejectBody(BaseModel):
    reason: str


@router.get("/api/trade-requests")
def list_trade_requests(team: Optional[str] = None, status: Optional[str] = None):
    items = _load_store()["items"]
    if team:
        t = team.upper()
        items = [i for i in items if t in i["parties"]]
    if status:
        items = [i for i in items if i["status"] == status]
    items = sorted(items, key=lambda i: i["created_at"], reverse=True)
    return [_public_view(i) for i in items]


@router.get("/api/trade-requests/{request_id}")
def get_trade_request(request_id: str):
    store = _load_store()
    idx = _find(store, request_id)
    return _public_view(store["items"][idx])


@router.post("/api/trade-requests")
def create_trade_request(body: TradeValidateInput, info: dict = Depends(get_token_info)):
    ctx = _validation_ctx()
    _require_trade_validatable(body, ctx)
    parties = _parties(body.transfers)
    if not parties:
        raise HTTPException(status_code=422, detail="A trade requires at least one transfer")
    if not any(has_role(info, p.lower()) for p in parties):
        raise HTTPException(
            status_code=403,
            detail="Must hold a role for at least one team in this trade to propose it")

    now = _now()
    with _trc_lock:
        store = _load_store()
        store["seq"] += 1
        item = {
            "id": secrets.token_hex(8),
            "number": store["seq"],
            "status": "awaiting_consent",
            "created_by": info["name"],
            "created_at": now,
            "updated_at": now,
            "trade": body.model_dump(),
            "parties": parties,
            # Every party, including the proposer's own team — one rule, no
            # auto-consent for whoever happened to click submit (decided
            # 2026-09-20: a GM who isn't the owner shouldn't be able to lock
            # in their own team's consent just by proposing).
            "consents": {p: {"consented": False, "by": None, "at": None} for p in parties},
            "ballots": {},
            "rejected": None,
            "withdrawn": None,
            "finalized": None,
            "history": [{"at": now, "by": info["name"], "action": "created"}],
        }
        store["items"].append(item)
        _save_store(store)
    log_write(info, f"POST trade-requests — #{item['number']} {'/'.join(parties)}")
    return _public_view(item)


@router.post("/api/trade-requests/{request_id}/consent")
def consent_trade_request(request_id: str, info: dict = Depends(get_token_info)):
    now = _now()
    with _trc_lock:
        store = _load_store()
        idx = _find(store, request_id)
        item = store["items"][idx]
        _refuse_if_terminal(item)
        owned = [p for p in item["parties"] if is_team_owner(info, p)]
        if not owned:
            raise HTTPException(status_code=403, detail="Must be the owner of a party team to consent")
        for p in owned:
            item["consents"][p] = {"consented": True, "by": info["name"], "at": now}
            item["history"].append({"at": now, "by": info["name"], "action": "consented", "team": p})
        if item["status"] == "awaiting_consent" and all(c["consented"] for c in item["consents"].values()):
            item["status"] = "balloting"
            item["history"].append({"at": now, "by": info["name"], "action": "all_consented"})
        item["updated_at"] = now
        _save_store(store)
    log_write(info, f"POST trade-requests/{request_id}/consent")
    return _public_view(item)


@router.post("/api/trade-requests/{request_id}/withdraw")
def withdraw_trade_request(request_id: str, body: WithdrawBody, info: dict = Depends(get_token_info)):
    now = _now()
    with _trc_lock:
        store = _load_store()
        idx = _find(store, request_id)
        item = store["items"][idx]
        _refuse_if_terminal(item)
        allowed = has_role(info, "trc_head") or has_role(info, "admin") or \
            any(is_team_owner(info, p) for p in item["parties"])
        if not allowed:
            raise HTTPException(status_code=403, detail="Must own a party team, or be trc_head, to withdraw")
        item["status"] = "withdrawn"
        item["withdrawn"] = {"at": now, "by": info["name"], "reason": body.reason}
        item["history"].append({"at": now, "by": info["name"], "action": "withdrawn", "reason": body.reason})
        item["updated_at"] = now
        _save_store(store)
    log_write(info, f"POST trade-requests/{request_id}/withdraw")
    return _public_view(item)


@router.put("/api/trade-requests/{request_id}/ballot")
def ballot_trade_request(request_id: str, body: BallotBody, info: dict = Depends(get_token_info)):
    # Deliberately `has_role`, not `require_role`/`require_any_role` — a
    # ballot is a judgment call a committee member makes, not an
    # administrative action, so `admin` is not waved through it (decided
    # 2026-09-20; admin can still reject/finalize below, same as every other
    # head-power in this codebase).
    if not has_role(info, "trc"):
        raise HTTPException(status_code=403, detail="'trc' role required")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")
    if not body.note.strip():
        raise HTTPException(status_code=422, detail="note is required — ballots judge fairness, not just legality")

    now = _now()
    with _trc_lock:
        store = _load_store()
        idx = _find(store, request_id)
        item = store["items"][idx]
        if item["status"] not in ("balloting", "ready_to_finalize"):
            raise HTTPException(status_code=422, detail=f"Request is {item['status']}, not open for ballots")
        # Re-voting overwrites rather than appending — same convention PDC/POEXT
        # use, freely revisable until finalized.
        item["ballots"][info["name"]] = {"decision": body.decision, "note": body.note.strip(), "at": now}
        item["history"].append({"at": now, "by": info["name"], "action": f"ballot:{body.decision}"})
        # A reject ballot never auto-rejects (decided 2026-09-20) — only
        # trc_head's own /reject does that. This only ever advances forward.
        if item["status"] == "balloting" and _approve_count(item) >= APPROVALS_NEEDED:
            item["status"] = "ready_to_finalize"
            item["history"].append({"at": now, "by": info["name"], "action": "ready_to_finalize"})
        item["updated_at"] = now
        _save_store(store)
    log_write(info, f"PUT trade-requests/{request_id}/ballot — {body.decision}")
    return _public_view(item)


@router.post("/api/trade-requests/{request_id}/reject")
def reject_trade_request(request_id: str, body: RejectBody, info: dict = Depends(require_role("trc_head"))):
    now = _now()
    with _trc_lock:
        store = _load_store()
        idx = _find(store, request_id)
        item = store["items"][idx]
        _refuse_if_terminal(item)
        item["status"] = "rejected"
        item["rejected"] = {"at": now, "by": info["name"], "reason": body.reason}
        item["history"].append({"at": now, "by": info["name"], "action": "rejected", "reason": body.reason})
        item["updated_at"] = now
        _save_store(store)
    log_write(info, f"POST trade-requests/{request_id}/reject")
    return _public_view(item)


@router.post("/api/trade-requests/{request_id}/finalize")
def finalize_trade_request(request_id: str, info: dict = Depends(require_role("trc_head"))):
    with _trc_lock:
        store = _load_store()
        idx = _find(store, request_id)
        item = store["items"][idx]
        _refuse_if_terminal(item)
        if item["status"] != "ready_to_finalize":
            raise HTTPException(
                status_code=422,
                detail=f"Request is {item['status']}, needs {APPROVALS_NEEDED} approvals first")

        live = _live_check(item)
        if not live["legal"]:
            raise HTTPException(status_code=422, detail={
                "message": "Trade is no longer legal to execute",
                "checks": live["checks"],
                "ownership_problems": live["ownership_problems"],
            })

        trade_in = TradeIn(**item["trade"])
        txn = apply_trade(
            trade_in, datetime.now(timezone.utc).strftime("%Y-%m-%d"), info,
            description=f"TRC request #{item['number']}")

        now = _now()
        item["status"] = "finalized"
        item["finalized"] = {"at": now, "by": info["name"], "txn_id": txn["id"]}
        item["history"].append({"at": now, "by": info["name"], "action": "finalized", "txn_id": txn["id"]})
        item["updated_at"] = now
        _save_store(store)
    log_write(info, f"POST trade-requests/{request_id}/finalize — txn {txn['id']}")
    return _public_view(item)

"""Correct a pick's structure (protection bands, a swap group, a binary chain)
from the rosters committee, with the same proof `restructure_picks.py` runs.

    POST /api/picks/structure/preview   {spec}            rosters role
    POST /api/picks/structure/apply     {spec, reason}    rosters role

Until 2026-10-29 the only way to fix a band was a CLI on the server, so a split
the league sheet and the trade log agreed on could sit wrong on the site for
weeks. `PUT /api/picks/...` can't do it: it writes the flat CSV, which holds
one owner and one protection number, and the registry overrides it for any
structured pick.

Preview writes nothing. Apply refuses anything preview would flag: a slot no
band covers, a draft order that leaves a team without a pick, a worked example
that doesn't hold, a new parity finding, or a registry that changed since the
preview was built from it. An applied change is logged to `edits.jsonl` like
every write that bypasses the ledger, and to `pick-rulings.jsonl` with its
reason and spec, since the edit log records what changed and not why.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_role
from .constants import DATA_DIR, PICKS_FILE
from .storage import log_write
from . import audit

router = APIRouter()

RULINGS_FILE = DATA_DIR / "pick-rulings.jsonl"


class StructureIn(BaseModel):
    spec: dict
    reason: str = ""


def _evaluate(spec: dict) -> dict:
    from picks_conveyance import registry, restructure
    kind = spec.get("kind", "chain")
    if kind not in ("chain", "protected", "swap_group"):
        raise HTTPException(status_code=422, detail=f"unknown kind {kind!r}")
    return restructure.evaluate(spec, registry.REGISTRY_FILE, PICKS_FILE)


def _public(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("old_text", "new_reg")}


@router.post("/api/picks/structure/preview")
def preview_structure(body: StructureIn, info: dict = Depends(require_role("rosters"))):
    return _public(_evaluate(body.spec))


@router.post("/api/picks/structure/apply")
def apply_structure(body: StructureIn, info: dict = Depends(require_role("rosters"))):
    reason = body.reason.strip()
    if len(reason) < 10:
        raise HTTPException(status_code=422, detail="Say why: name the trade or ruling this comes from.")
    from picks_conveyance import registry, restructure
    result = _evaluate(body.spec)
    if not result["ok"]:
        raise HTTPException(status_code=422, detail={"problems": result["problems"]})
    try:
        store = restructure.apply(result, registry.REGISTRY_FILE, PICKS_FILE, audit)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    with RULINGS_FILE.open("a") as fh:
        fh.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": info.get("name"), "reason": reason, "spec": body.spec,
        }, separators=(",", ":")) + "\n")
    members = ", ".join(f"{p['year']} R{p['round']} {p['orig']}" for p in result["after"])
    log_write(info, f"POST picks/structure/apply — {members}: {reason}")
    out = _public(result)
    out["store_regenerated"] = store is not None
    return out

"""`POST /api/picks/resolve` — who owns each pick of one draft, given its order.

The lottery page (/lottery) draws or projects a draft order and asks this route
who ends up holding each pick, so a protected pick stays home when it lands in
its band and a swap goes to the better side. It runs the conveyance model's own
draft-time resolver (`picks_conveyance.resolver.resolve_all`), the same engine
the draft uses, so the page never reimplements protections in JS.

Public and read-only: it takes no lock and writes nothing. A pick whose terms
depend on another year's draft (a ladder step after this one) comes back
without an owner, and the page falls back to the flat `owner` from /api/picks.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from picks_conveyance import store as conv_store
from picks_conveyance.resolver import ResolutionError, resolve_all
from routers.constants import VALID_TEAMS

router = APIRouter()


class ResolveBody(BaseModel):
    year: int
    round1: list[str]   # origs in slot order, pick 1 first
    round2: list[str] = []


@router.post("/api/picks/resolve")
def resolve_order(body: ResolveBody):
    for rnd, order in ((1, body.round1), (2, body.round2)):
        bad = [t for t in order if t not in VALID_TEAMS]
        if bad or len(set(order)) != len(order):
            raise HTTPException(status_code=422, detail=f"round {rnd}: unknown or repeated teams {bad}")
    try:
        store = conv_store.load_store()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="conveyance store not seeded")

    n = len(body.round1)
    pos = {(body.year, 1, t): i + 1 for i, t in enumerate(body.round1)}
    pos.update({(body.year, 2, t): n + i + 1 for i, t in enumerate(body.round2)})
    try:
        owners = resolve_all(store, pos)
    except ResolutionError as e:
        return {"owners": {}, "error": str(e)}
    return {"owners": {f"{r}-{o}": team for (y, r, o), team in owners.items()
                       if y == body.year and team}}

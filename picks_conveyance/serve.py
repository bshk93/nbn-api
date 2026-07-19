"""Shared helper for serving picks from the conveyance store in the flat shape.

Used by both the additive `/api/picks-preview` route and, when
`PICKS_READ_SOURCE=conveyance` is set, the real `/api/picks` /
`/api/picks/{team}` endpoints (routers/roster_picks.py) — same rows, same flat
contract, just sourced from the conveyance model instead of the raw CSV.
"""
from __future__ import annotations

from fastapi import HTTPException

from . import projection
from . import store as conv_store


def flat_rows() -> list[dict]:
    """All picks, flat-shaped, projected from the conveyance store.
    Raises HTTPException(503) if the store hasn't been seeded."""
    try:
        store = conv_store.load_store()
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="conveyance store not seeded "
                   "(run: python -m picks_conveyance.seed_store --curated)")
    return [projection.project_to_flat(p, store) for p in store.get("picks", [])]

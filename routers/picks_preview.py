"""Additive, non-breaking preview of the conveyance model.

`GET /api/picks-preview` serves the flat pick shape *projected from the new
conveyance store* (docs/picks-conveyance.md). It leaves the live `/api/picks`
completely untouched — this route exists so the two can be diffed against each
other before any cutover. For the ~469 settled picks the output is byte-identical
to `/api/picks` (proven by the parity test); the contingent rows differ, showing
the improved model (pipe-joined candidates, real protection thresholds).

Returns 503 if the store hasn't been seeded yet (nothing depends on it existing).
"""
from fastapi import APIRouter

from picks_conveyance.serve import flat_rows
from routers.roster_picks import enrich_swap_conveys

router = APIRouter()


@router.get("/api/picks-preview")
def get_picks_preview():
    rows = flat_rows()
    # match the live endpoint's swap_conveys enrichment (affects the not-yet-
    # modeled flat-structured rows that still carry a swap_owner)
    enrich_swap_conveys(rows)
    return rows

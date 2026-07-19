"""Draft-pick conveyance model (Phase 0).

Implements the model in docs/picks-conveyance.md. This package is intentionally
NOT wired into any live endpoint yet — it is built and validated behind the
existing flat picks API. The parity test (tests/test_projection_parity.py)
proves `project_to_flat` reproduces the current `/api/picks` response before any
consumer is switched over.

Public surface:
    - model:      node constructors + validation
    - resolver:   resolve(pick, positions) -> concrete owner at draft time
    - projection: project_to_flat(pick) -> the flat pick_to_response shape
"""
from . import model, resolver, projection  # noqa: F401

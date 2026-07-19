"""Resync the conveyance store from the flat picks CSV.

Called after every write to the flat picks ledger (`roster_picks.save_picks`) so
the conveyance store never drifts stale once real trades start happening again.
This is a full regeneration, not an incremental per-mutation patch: every call
just re-seeds + re-curates from the current CSV, reusing the exact code path
already proven by tests/test_projection_parity.py and tests/test_curated.py.
Cheap (~480 rows) and safe to run synchronously on every write.

Deliberately fails open: any error here is logged and swallowed, never raised —
a bug in this package must never be able to block or corrupt a real trade or
roster mutation. The flat CSV write always completes first and independently.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import registry, seed_store

logger = logging.getLogger(__name__)


def resync(csv_path: Path | None = None, out_path: Path | None = None) -> dict | None:
    """Rebuild the conveyance store from the current flat CSV and write it to
    disk. Returns the store dict on success, None on failure (logged)."""
    try:
        registry.seed_registry_from_curated()   # one-time; no-op once it exists
        store = seed_store.build_store(csv_path or seed_store.DEFAULT_IN)
        registry.apply_registry(store)
        target = out_path or seed_store.DEFAULT_OUT
        target.write_text(json.dumps(store))
        return store
    except Exception:
        logger.exception(
            "picks_conveyance resync failed (flat picks CSV write already "
            "completed and is unaffected)")
        return None

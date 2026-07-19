"""Load the conveyance JSON store from disk.

Pickkeys inside the store are JSON (so tuples became lists on the way out); this
loader is only concerned with returning the parsed dict. Consumers key picks via
resolver.key() / (year, round, orig) as needed.
"""
from __future__ import annotations

import json
from pathlib import Path

from .seed_store import DEFAULT_OUT


def load_store(path: Path | str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_OUT
    if not p.exists():
        raise FileNotFoundError(f"conveyance store not found: {p}")
    return json.loads(p.read_text())

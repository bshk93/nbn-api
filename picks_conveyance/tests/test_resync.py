"""Resync tests: correctness on a real CSV, and fail-open on a broken one.

    venv/bin/python -m picks_conveyance.tests.test_resync
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import resync, seed_store  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    tmp_out = Path("/tmp") / "picks_conveyance_resync_test.json"
    tmp_out.unlink(missing_ok=True)

    # 1. resync against the real live CSV writes a valid, fully-modeled store
    store = resync.resync(csv_path=seed_store.DEFAULT_IN, out_path=tmp_out)
    check("resync returns a store", store is not None)
    check("output file was written", tmp_out.exists())
    on_disk = json.loads(tmp_out.read_text())
    check("store has 480 picks", len(on_disk.get("picks", [])) == 480)
    # Any needs_structure row must be an already-drafted pick (has a player) —
    # curated.set_node deliberately skips overriding those (the flat OWNER is
    # already a settled historical fact once a pick has been used; see
    # curated.py's docstring). A needs_structure row with NO player would mean
    # a genuinely unmodeled future contingency slipped through.
    unresolved_needing_structure = [p for p in on_disk["picks"]
                                    if p.get("needs_structure") and not p.get("player")]
    check("needs_structure rows (if any) are all already-drafted",
          len(unresolved_needing_structure) == 0)

    # 2. fail-open: a nonexistent CSV must not raise, must return None
    result = resync.resync(csv_path=Path("/nonexistent/nope.csv"), out_path=tmp_out)
    check("missing CSV -> resync returns None (no raise)", result is None)

    tmp_out.unlink(missing_ok=True)
    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("RESYNC TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

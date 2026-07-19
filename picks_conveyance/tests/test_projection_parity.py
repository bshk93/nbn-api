"""Parity test: the Phase 0 projection must reproduce the live `/api/picks`
output exactly for every clean (settled) pick.

This is the safety property that lets the new store ship behind the existing
read API. Run:  python3 -m picks_conveyance.tests.test_projection_parity
(No pytest dependency — plain asserts so it runs anywhere the API runs.)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

# import the LIVE formatter we must match, plus our projection + seed
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from routers.roster_picks import pick_to_response, enrich_swap_conveys  # noqa: E402
from picks_conveyance import projection, seed_store                      # noqa: E402


def run(csv_path: Path) -> int:
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    # live output (with swap_conveys enrichment, as the real endpoint returns)
    live = [pick_to_response(r) for r in rows]
    enrich_swap_conveys(live)
    live_by_key = {(p["year"], p["round"], p["orig"]): p for p in live}

    seeded = [seed_store.seed_pick(r) for r in rows]

    checked = skipped = 0
    mismatches = []
    for sp in seeded:
        k = (sp["year"], sp["round"], sp["orig"])
        if sp.get("needs_structure"):
            skipped += 1
            continue
        got = projection.project_to_flat(sp)
        want = live_by_key[k]
        # swap_conveys is always None for settled picks; live agrees (no swap_owner)
        if got != want:
            mismatches.append((k, _diff(want, got)))
        checked += 1

    print(f"checked {checked} settled picks, skipped {skipped} needing structure")
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH(es):")
        for k, d in mismatches[:20]:
            print(f"  {k}: {d}")
        return 1
    print("PARITY OK — projection reproduces /api/picks for every settled pick")
    return 0


def _diff(want: dict, got: dict) -> dict:
    return {k: (want.get(k), got.get(k)) for k in set(want) | set(got)
            if want.get(k) != got.get(k)}


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else seed_store.DEFAULT_IN
    raise SystemExit(run(path))

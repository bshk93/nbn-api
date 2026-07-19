"""Project the full curated store (every node type) to the flat shape.

Asserts the projection never raises, always yields a well-formed flat row, and
produces the expected display for a sample of each node type.

    venv/bin/python -m picks_conveyance.tests.test_projection_full
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import seed_store, curated, projection  # noqa: E402

FLAT_KEYS = {"year", "round", "orig", "owner", "pick", "player", "protected",
             "conveys", "swap_owner", "swap_conveys", "notes", "frozen",
             "frozen_reason"}
FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def main():
    store = seed_store.build_store(seed_store.DEFAULT_IN)
    curated.apply_curated(store)
    by_key = {(p["year"], p["round"], p["orig"]): p for p in store["picks"]}

    # 1. project every pick — no exceptions, well-formed, non-empty owner
    bad = 0
    for p in store["picks"]:
        row = projection.project_to_flat(p, store)
        if set(row) != FLAT_KEYS or not row["owner"]:
            bad += 1
            if bad <= 5:
                print(f"  [FAIL] malformed row {p['year']} R{p['round']} {p['orig']}: {row}")
    print(f"projected all {len(store['picks'])} picks ({bad} malformed)")
    if bad:
        FAILS.append("malformed")

    def proj(k):
        return projection.project_to_flat(by_key[k], store)

    # 2. protected: keeper-is-origin collapses to (owner, protected)
    r = proj((2027, 1, "CHI"))
    check("2027 CHI -> owner DEN, protected 4", (r["owner"], r["protected"]), ("DEN", 4))
    # protected: keeper is a third team (NYK) -> candidates
    check("2027 IND 2nd -> candidates NYK|MIA", proj((2027, 2, "IND"))["owner"], "NYK|MIA")
    # range split, neither is origin -> candidates
    check("2027 GSW 2nd -> candidates UTA|LAC", proj((2027, 2, "GSW"))["owner"], "UTA|LAC")

    # 3. swap group -> pipe-joined priority teams
    check("2028 BOS 1st swap -> BOS|CHI", proj((2028, 1, "BOS"))["owner"], "BOS|CHI")
    check("2027 OKC 2nd swap -> BOS|NOP", proj((2027, 2, "OKC"))["owner"], "BOS|NOP")

    # 4. binary chain -> candidate teams from the chain
    nop = proj((2030, 1, "NOP"))["owner"].split("|")
    check("2030 NOP chain candidates incl MIL,POR,DET,NOP,HOU",
          set(["MIL", "POR", "DET", "NOP", "HOU"]).issubset(set(nop)), True)

    # 5. legacy -> nominal owner preserved from the CSV
    check("2027 DET legacy owner preserved", proj((2027, 1, "DET"))["owner"], "DET")

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("PROJECTION (full curated store) OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

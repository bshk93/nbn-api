"""Validate + resolve the curated nodes end to end.

Seeds a store from the live CSV, applies the curated conveyance, validates every
node, then resolves the whole store under a synthetic full-league draft order and
asserts every contingent (protected / swap / binary) pick lands on a real team
and every legacy pick is skipped. Proves the reconciliation is executable.

    venv/bin/python -m picks_conveyance.tests.test_curated
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import model, resolver, seed_store, curated, registry  # noqa: E402

TEAMS = sorted(["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET",
                "GSW", "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN",
                "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS",
                "TOR", "UTA", "WAS"])
FAILS = []


def synthetic_positions(years):
    """Deterministic full draft order: R1 = 1..30, R2 = 31..60, team order
    rotated per year so outcomes actually vary across years."""
    pos = {}
    for y in years:
        order = TEAMS[y % 30:] + TEAMS[:y % 30]
        for i, t in enumerate(order):
            pos[(y, 1, t)] = i + 1
            pos[(y, 2, t)] = i + 31
    return pos


def main():
    store = seed_store.build_store(seed_store.DEFAULT_IN)
    registry.seed_registry_from_curated()
    registry.apply_registry(store)

    # 1. validate every node
    nerr = 0
    for p in store["picks"]:
        try:
            model.validate(p["conveyance"])
        except model.ConveyanceError as e:
            nerr += 1
            print(f"  [FAIL] validate {p['year']} R{p['round']} {p['orig']}: {e}")
    for sid, n in store["binary_swaps"].items():
        try:
            model.validate(n)
        except model.ConveyanceError as e:
            nerr += 1
            print(f"  [FAIL] validate binary_swap {sid}: {e}")
    for gid, g in store["swap_groups"].items():
        for slot in g["priority"]:
            if model.is_node(slot):
                model.validate(slot)
    print(f"validated all nodes ({nerr} errors)")
    if nerr:
        FAILS.append("validation")

    # 2. resolve under a synthetic full draft
    years = sorted({p["year"] for p in store["picks"]})
    pos = synthetic_positions(years)
    owners = resolver.resolve_all(store, pos)

    # 3. every contingent curated pick must resolve; every legacy pick must not
    contingent = (list(curated.PROTECTED)
                  + [(m["year"], m["round"], m["orig"])
                     for g in curated.SWAP_GROUPS.values() for m in g["members"]]
                  + [k for ks in curated.CHAIN_MEMBERS.values() for k in ks])
    unresolved = [k for k in contingent if k not in owners]
    if unresolved:
        FAILS.append("unresolved")
        print(f"  [FAIL] {len(unresolved)} contingent picks unresolved: {unresolved[:8]}")
    else:
        print(f"resolved all {len(set(contingent))} contingent picks to a team")

    leaked = [k for k in curated.LEGACY if k in owners]
    if leaked:
        FAILS.append("legacy-leak")
        print(f"  [FAIL] legacy picks resolved (should be skipped): {leaked}")
    else:
        print(f"all {len(curated.LEGACY)} legacy picks correctly skipped by resolver")

    # 4. sample output for eyeballing
    print("\nsample resolved owners under synthetic draft:")
    for k in [(2030, 1, "NOP"), (2030, 1, "DET"), (2028, 1, "ORL"),
              (2028, 1, "WAS"), (2027, 1, "CHI"), (2027, 2, "GSW")]:
        print(f"  {k} -> {owners.get(k)}")

    # 5. deal-specific direction checks for the two picks resolved out of
    # LEGACY 2026-07-19 (real transactions found, not synthetic) -- the
    # generic loop above only proves these resolve at all, not that the
    # actual better/worse direction matches what the real trades granted.
    def check(name, got, want):
        ok = got == want
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
        if not ok:
            FAILS.append(name)

    print("\n2031 HOU/MIN swap (Trade 54 + Trade 73):")
    r_hou_better = resolver.resolve_all(store, {(2031, 1, "HOU"): 5, (2031, 1, "MIN"): 20})
    check("HOU's own pick better -> HOU keeps it",
          r_hou_better.get((2031, 1, "HOU")), "HOU")
    check("HOU's own pick better -> IND keeps the MIN pick",
          r_hou_better.get((2031, 1, "MIN")), "IND")
    r_min_better = resolver.resolve_all(store, {(2031, 1, "HOU"): 25, (2031, 1, "MIN"): 3})
    check("MIN pick better -> HOU swaps for it",
          r_min_better.get((2031, 1, "MIN")), "HOU")
    check("MIN pick better -> IND keeps HOU's own pick instead",
          r_min_better.get((2031, 1, "HOU")), "IND")

    print("\n2027 PHI/CHA/TOR/DAL chain (Trade 18 + Trade 27):")
    r_a = resolver.resolve_all(store, {(2027, 1, "PHI"): 2, (2027, 1, "TOR"): 15, (2027, 1, "DAL"): 8})
    check("PHI best -> CHA takes it", r_a.get((2027, 1, "PHI")), "CHA")
    check("TOR's leftover (15) worse than DAL (8) -> TOR swaps for DAL's",
          r_a.get((2027, 1, "DAL")), "TOR")
    check("DAL keeps TOR's own leftover pick", r_a.get((2027, 1, "TOR")), "DAL")
    r_b = resolver.resolve_all(store, {(2027, 1, "PHI"): 10, (2027, 1, "TOR"): 4, (2027, 1, "DAL"): 25})
    check("TOR best -> CHA takes it", r_b.get((2027, 1, "TOR")), "CHA")
    check("PHI's leftover (10) better than DAL (25) -> TOR keeps it, no swap",
          r_b.get((2027, 1, "PHI")), "TOR")
    check("DAL keeps its own pick (worse than PHI's leftover)",
          r_b.get((2027, 1, "DAL")), "DAL")

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("CURATED NODES OK — all validate, resolve, and legacy-skip correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

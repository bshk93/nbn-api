"""Ownership shadow-check tests, against real curated picks (not synthetic) so
the results mean something concrete before this ever touches a real trade.

    venv/bin/python -m picks_conveyance.tests.test_ownership
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import ownership, registry, seed_store  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    registry.seed_registry_from_curated()
    store = seed_store.build_store(seed_store.DEFAULT_IN)
    registry.apply_registry(store)
    by_key = {(p["year"], p["round"], p["orig"]): p for p in store["picks"]}

    def holds(k, team):
        return ownership.team_holds_claim(by_key[k], team, store)

    # settled
    p = by_key[(2029, 1, "ATL")]
    check("settled: nominal owner holds a claim", ownership.team_holds_claim(p, p["conveyance"]["team"], store))
    check("settled: unrelated team does not", not ownership.team_holds_claim(p, "ZZZ", store))

    # protected (2027 CHI: top-4 CHI, else DEN)
    check("protected: both band teams hold a claim (CHI)", holds((2027, 1, "CHI"), "CHI"))
    check("protected: both band teams hold a claim (DEN)", holds((2027, 1, "CHI"), "DEN"))
    check("protected: unrelated team does not", not holds((2027, 1, "CHI"), "BOS"))

    # swap (2028 BOS/CHI group)
    check("swap: both candidates hold a claim (BOS)", holds((2028, 1, "BOS"), "BOS"))
    check("swap: both candidates hold a claim (CHI)", holds((2028, 1, "BOS"), "CHI"))
    check("swap: unrelated team does not", not holds((2028, 1, "BOS"), "MIA"))

    # binary chain (2030 NOP/MIL/POR/DET/HOU)
    for team in ("NOP", "MIL", "POR", "DET", "HOU"):
        check(f"binary chain: {team} holds a claim", holds((2030, 1, "NOP"), team))
    check("binary chain: unrelated team does not", not holds((2030, 1, "NOP"), "BOS"))

    # legacy (2028 SAC/DAL/MIA/MEM/NYK/CHA cluster -- the 2027 DET cascade this
    # used to test was resolved out of legacy 2026-07-19, see curated.py)
    p = by_key[(2028, 1, "MIA")]
    check("legacy: preserved nominal owner holds a claim",
          ownership.team_holds_claim(p, p["conveyance"]["owner"], store))

    # ranked swap group (priority shorter than members): list_leaves must
    # surface a synthetic fallback leaf per team that could land at the
    # unnamed rank, not just the named priority slots -- otherwise a team
    # that could end up keeping its own pick wouldn't show up as holding
    # any claim at all.
    synth_store = {"swap_groups": {"rg_test": {
        "members": [{"year": 2028, "round": 1, "orig": "SAC"},
                   {"year": 2028, "round": 1, "orig": "DAL"},
                   {"year": 2028, "round": 1, "orig": "PHX"}],
        "priority": ["MIA", "SAC"],
    }}}
    synth_pick = {"year": 2028, "round": 1, "orig": "SAC",
                 "conveyance": {"type": "swap", "id": "x", "group": "rg_test"}}
    leaves = ownership.list_leaves(synth_pick, synth_store)
    check("ranked group: named priority leaves present (MIA, SAC)",
          {"MIA", "SAC"}.issubset({l["team"] for l in leaves}))
    check("ranked group: fallback leaves cover every member's own team too",
          {"SAC", "DAL", "PHX"}.issubset({l["team"] for l in leaves}))
    check("ranked group: DAL/PHX hold a claim via the fallback leaf",
          ownership.team_holds_claim(synth_pick, "DAL", synth_store)
          and ownership.team_holds_claim(synth_pick, "PHX", synth_store))

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("OWNERSHIP TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

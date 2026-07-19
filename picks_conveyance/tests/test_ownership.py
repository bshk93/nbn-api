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

    # legacy -- synthetic node, since as of 2026-07-19 all 6 real legacy
    # picks have been resolved to real structure (see curated.py); a real
    # example would only be transient, so this is deliberately not tied to
    # live data.
    legacy_pick = {"year": 2099, "round": 1, "orig": "ATL",
                  "conveyance": {"type": "legacy", "reason": "test", "owner": "BOS"}}
    check("legacy: preserved nominal owner holds a claim",
          ownership.team_holds_claim(legacy_pick, "BOS", store))
    check("legacy: unrelated team does not",
          not ownership.team_holds_claim(legacy_pick, "MIA", store))

    # ranked swap group (priority shorter than members): list_leaves must
    # surface a fallback leaf for the team that could end up keeping ITS OWN
    # pick at the unnamed rank -- but ONLY that specific member's own team,
    # never a *different* member's fallback identity. SAC's own row can only
    # ever end up owned by MIA, SAC, or (if it's the unranked one) SAC
    # itself -- never DAL or PHX, even though DAL/PHX are "possible" for
    # their OWN rows in the same group.
    synth_store = {"swap_groups": {"rg_test": {
        "members": [{"year": 2028, "round": 1, "orig": "SAC"},
                   {"year": 2028, "round": 1, "orig": "DAL"},
                   {"year": 2028, "round": 1, "orig": "PHX"}],
        "priority": ["MIA", "SAC"],
    }}}
    sac_pick = {"year": 2028, "round": 1, "orig": "SAC",
               "conveyance": {"type": "swap", "id": "x", "group": "rg_test"}}
    dal_pick = {"year": 2028, "round": 1, "orig": "DAL",
               "conveyance": {"type": "swap", "id": "x", "group": "rg_test"}}
    sac_leaves = ownership.list_leaves(sac_pick, synth_store)
    check("ranked group: SAC's row shows the named priorities (MIA, SAC)",
          {"MIA", "SAC"}.issubset({l["team"] for l in sac_leaves}))
    check("ranked group: SAC's row does NOT show DAL/PHX's fallback identity",
          {"DAL", "PHX"}.isdisjoint({l["team"] for l in sac_leaves}))
    check("ranked group: DAL does not hold a claim on SAC's own row",
          not ownership.team_holds_claim(sac_pick, "DAL", synth_store))
    dal_leaves = ownership.list_leaves(dal_pick, synth_store)
    check("ranked group: DAL's own row includes its own fallback",
          "DAL" in {l["team"] for l in dal_leaves})
    check("ranked group: DAL holds a claim on its own row's fallback",
          ownership.team_holds_claim(dal_pick, "DAL", synth_store))

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("OWNERSHIP TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

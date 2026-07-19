"""from_trade tests: protection direction, swap direction (verified against a
real note-documented row before shipping), and the no-counterpart fallback.

    venv/bin/python -m picks_conveyance.tests.test_from_trade
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import from_trade, model, registry, resolver  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    orig_registry = registry.REGISTRY_FILE
    registry.REGISTRY_FILE = Path("/tmp/picks_conveyance_from_trade_test.json")
    registry.REGISTRY_FILE.unlink(missing_ok=True)
    registry.save_registry(registry._empty())

    try:
        # 1. protection: keeper=from_team, conveys-to=to_team
        from_trade.register_protection((2099, 1, "SAC"), from_team="SAC",
                                       to_team="LAL", threshold=4)
        reg = registry.load_registry()
        node_spec = reg["protected"]["2099|1|SAC"]
        node = {"type": "protected", "id": "x", **node_spec}
        model.validate(node)
        store = {"picks": [{"year": 2099, "round": 1, "orig": "SAC", "conveyance": node}]}
        r1 = resolver.resolve_all(store, {(2099, 1, "SAC"): 3})
        r2 = resolver.resolve_all(store, {(2099, 1, "SAC"): 10})
        check("protected: within threshold -> from_team keeps",
              r1[(2099, 1, "SAC")] == "SAC")
        check("protected: beyond threshold -> conveys to to_team",
              r2[(2099, 1, "SAC")] == "LAL")

        # 2. swap: verified against the real 2031 HOU/IND/MIN row — the NAMED
        # team (swap_with) gets the better pick regardless of which one wins.
        picks_snapshot = [
            {"YEAR": "2099", "ROUND": "1", "ORIG": "MIN", "OWNER": "IND"},
            {"YEAR": "2099", "ROUND": "1", "ORIG": "HOU", "OWNER": "HOU"},
        ]
        found = from_trade.register_swap((2099, 1, "MIN"), to_team="IND",
                                         swap_with="HOU", picks_snapshot=picks_snapshot)
        check("swap: counterpart found", found is True)
        reg = registry.load_registry()
        gid = next(iter(reg["swap_groups"]))
        group = reg["swap_groups"][gid]
        check("swap group has both members",
              {(2099, 1, "MIN"), (2099, 1, "HOU")} ==
              {(m["year"], m["round"], m["orig"]) for m in group["members"]})
        check("priority: swap_with (HOU) first, to_team (IND) second",
              group["priority"] == ["HOU", "IND"])

        swap_store = {"swap_groups": {gid: group}}
        # branch A: MIN-pick (held by IND) is better
        rA = resolver.resolve_all(swap_store, {(2099, 1, "MIN"): 3, (2099, 1, "HOU"): 8})
        check("branch A (MIN-pick better): HOU still gets the better pick",
              rA[(2099, 1, "MIN")] == "HOU")
        check("branch A: IND gets the worse (HOU's own) pick",
              rA[(2099, 1, "HOU")] == "IND")
        # branch B: HOU's own pick is better
        rB = resolver.resolve_all(swap_store, {(2099, 1, "MIN"): 9, (2099, 1, "HOU"): 2})
        check("branch B (HOU's own better): HOU keeps its own (still the better one)",
              rB[(2099, 1, "HOU")] == "HOU")
        check("branch B: IND gets the worse (MIN) pick",
              rB[(2099, 1, "MIN")] == "IND")

        # 3. no counterpart found -> graceful False, no crash, no data written
        found2 = from_trade.register_swap((2099, 2, "ATL"), to_team="BOS",
                                          swap_with="NOP", picks_snapshot=[])
        check("no counterpart -> returns False (falls back to flat passthrough)",
              found2 is False)

        # 4. register_protection on a pick that ALREADY has real structure
        # (a prior, unrelated trade's claim) must subdivide the band the new
        # from_team actually holds, not overwrite the whole node — otherwise
        # the earlier party's claim silently vanishes. Mirrors the real 2029
        # DET 1st: DET holds bands 1-3 from an earlier trade, OKC holds 4-60;
        # this trade adds a new top-5 sub-protection on OKC's own share.
        registry.add_protected(
            (2099, 1, "DET"),
            on={"year": 2099, "round": 1, "orig": "DET"},
            bands=[{"min": 1, "max": 3, "to": "DET"}, {"min": 4, "max": 60, "to": "OKC"}],
        )
        from_trade.register_protection((2099, 1, "DET"), from_team="OKC",
                                       to_team="BKN", threshold=5)
        spec = registry.load_registry()["protected"]["2099|1|DET"]
        check("subdivide preserves the earlier party's untouched band",
              {"min": 1, "max": 3, "to": "DET"} in spec["bands"])
        check("subdivide splits OKC's band at the threshold (keep side)",
              {"min": 4, "max": 5, "to": "OKC"} in spec["bands"])
        check("subdivide splits OKC's band at the threshold (convey side)",
              {"min": 6, "max": 60, "to": "BKN"} in spec["bands"])
        check("subdivide didn't just wipe the structure down to 2 bands",
              len(spec["bands"]) == 3)

        # 5. register_ladder wires a fallback compensation trigger, keyed to
        # the pick's real orig (DET), not the ladder's from_team (OKC).
        from_trade.register_ladder((2099, 1, "DET"), from_team="OKC", to_team="BKN",
                                   protect_top=5,
                                   fallback_picks=[(2101, 1, "OKC")])
        ladders = registry.load_registry()["ladders"]
        ladder = next(L for L in ladders if L["steps"][0]["orig"] == "DET"
                     and L["from"] == "OKC")
        check("ladder step keyed to the real orig (DET), not from_team (OKC)",
              ladder["steps"][0]["orig"] == "DET")
        check("ladder fallback references the compensation pick",
              ladder["fallback"]["picks"] == [{"year": 2101, "round": 1, "orig": "OKC"}])

    finally:
        registry.REGISTRY_FILE = orig_registry

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("FROM_TRADE TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

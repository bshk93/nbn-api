"""Re-trade handling tests: swap/protected/binary structures update in place,
a plain settled pick is a no-op unless a ladder governs it (in which case
the ladder's `from` updates), legacy is blocked.

    venv/bin/python -m picks_conveyance.tests.test_retrade
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import registry  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    orig_registry = registry.REGISTRY_FILE
    registry.REGISTRY_FILE = Path("/tmp/picks_conveyance_retrade_test.json")
    registry.REGISTRY_FILE.unlink(missing_ok=True)
    registry.save_registry(registry._empty())

    try:
        # swap: CHI re-trades its stake to MIA, with a txn_id this time —
        # stamped at the group level, appended (not replacing prior history).
        registry.add_swap_group("sg_t1",
                                members=[{"year": 2099, "round": 1, "orig": "BOS"},
                                        {"year": 2099, "round": 1, "orig": "CHI"}],
                                priority=["BOS", "CHI"],
                                txn_entry={"id": "orig_trade", "date": "2026-01-01"})
        node = {"type": "swap", "id": "x", "group": "sg_t1"}
        changed = registry.handle_retrade((2099, 1, "BOS"), "CHI", "MIA", node,
                                          txn_id="retrade_1", txn_date="2026-07-19")
        check("swap retrade: reports changed", changed is True)
        check("swap retrade: priority updated to MIA",
              registry.load_registry()["swap_groups"]["sg_t1"]["priority"] == ["BOS", "MIA"])
        sg_txns = registry.load_registry()["swap_groups"]["sg_t1"]["txn_ids"]
        check("swap retrade: original txn_id preserved, not replaced",
              {"id": "orig_trade", "date": "2026-01-01"} in sg_txns)
        check("swap retrade: re-trade's txn_id appended",
              {"id": "retrade_1", "date": "2026-07-19"} in sg_txns)

        # protected: the conveying team re-trades its band, with a txn_id —
        # stamped on the specific band, sibling band untouched.
        registry.add_protected((2099, 1, "SAC"), on={"year": 2099, "round": 1, "orig": "SAC"},
                               bands=[{"min": 1, "max": 4, "to": "SAC"},
                                     {"min": 5, "max": 60, "to": "LAL"}],
                               txn_entry={"id": "orig_trade", "date": "2026-01-01"})
        node = {"type": "protected", "id": "x"}
        changed = registry.handle_retrade((2099, 1, "SAC"), "LAL", "DEN", node,
                                          txn_id="retrade_2", txn_date="2026-07-19")
        check("protected retrade: reports changed", changed is True)
        bands = registry.load_registry()["protected"]["2099|1|SAC"]["bands"]
        check("protected retrade: band updated to DEN",
              any(b["to"] == "DEN" for b in bands) and not any(b["to"] == "LAL" for b in bands))
        den_band = next(b for b in bands if b["to"] == "DEN")
        sac_band = next(b for b in bands if b["to"] == "SAC")
        check("protected retrade: re-traded band gets both txn_ids (append)",
              den_band["txn_ids"] == [{"id": "orig_trade", "date": "2026-01-01"},
                                      {"id": "retrade_2", "date": "2026-07-19"}])
        check("protected retrade: untouched sibling band's original txn_ids unaffected",
              sac_band.get("txn_ids", []) == [{"id": "orig_trade", "date": "2026-01-01"}])

        # binary: a chain-referenced team re-trades its stake, with a txn_id
        registry.save_registry(registry._empty())
        reg = registry.load_registry()
        reg["binary_chains"]["c1"] = [
            {"type": "binary_swap", "id": "c1_s1",
             "a": {"year": 2099, "round": 1, "orig": "NOP"},
             "b": {"year": 2099, "round": 1, "orig": "MIL"},
             "better_to": "NOP", "worse_to": "MIL",
             "txn_ids": [{"id": "orig_trade", "date": "2026-01-01"}]}]
        registry.save_registry(reg)
        node = {"type": "binary", "chain": "c1"}
        changed = registry.handle_retrade((2099, 1, "NOP"), "MIL", "POR", node,
                                          txn_id="retrade_3", txn_date="2026-07-19")
        check("binary retrade: reports changed", changed is True)
        chain_node = registry.load_registry()["binary_chains"]["c1"][0]
        check("binary retrade: worse_to updated to POR", chain_node["worse_to"] == "POR")
        check("binary retrade: re-trade's txn_id appended, original preserved",
              chain_node["txn_ids"] == [{"id": "orig_trade", "date": "2026-01-01"},
                                        {"id": "retrade_3", "date": "2026-07-19"}])

        # settled: no-op, nothing structural to update
        node = {"type": "settled", "team": "ATL"}
        changed = registry.handle_retrade((2099, 1, "ATL"), "ATL", "BOS", node)
        check("settled retrade: no-op (False)", changed is False)

        # settled + governing ladder: the pick's own node is plain settled
        # (the common shape register_ladder leaves it in), but a ladder
        # container elsewhere in the registry names this exact (year, round,
        # orig) as its governed step — re-trading the pick must update the
        # ladder's `from`, not silently leave it stale (the bug this fix
        # closes: resolver._resolve_ladders would otherwise credit the OLD
        # team as keeper at real draft time).
        registry.add_ladder({
            "id": "ladder_t1", "from": "MEM", "to": "IND",
            "steps": [{"year": 2099, "round": 1, "orig": "MEM", "protect_top": 10}],
            "fallback": {"type": "fixed_asset",
                        "picks": [{"year": 2100, "round": 2, "orig": "MEM"}]},
            "txn_ids": [{"id": "orig_trade", "date": "2026-01-01"}],
        })
        node = {"type": "settled", "team": "MEM"}
        changed = registry.handle_retrade((2099, 1, "MEM"), "MEM", "UTA", node,
                                          txn_id="retrade_4", txn_date="2026-07-19")
        check("ladder retrade: settled pick reports changed", changed is True)
        ladder_after = next(L for L in registry.load_registry()["ladders"]
                            if L["id"] == "ladder_t1")
        check("ladder retrade: from updated to UTA", ladder_after["from"] == "UTA")
        check("ladder retrade: to untouched", ladder_after["to"] == "IND")
        check("ladder retrade: txn_id appended, original preserved",
              ladder_after["txn_ids"] == [{"id": "orig_trade", "date": "2026-01-01"},
                                          {"id": "retrade_4", "date": "2026-07-19"}])

        # a later, unrelated re-trade of the SAME pick by a team that isn't
        # the ladder's current `from` must not touch the ladder.
        node = {"type": "settled", "team": "UTA"}
        changed = registry.handle_retrade((2099, 1, "MEM"), "GSW", "POR", node)
        check("ladder retrade: no-op when from_team doesn't match ladder's from",
              changed is False)
        ladder_after2 = next(L for L in registry.load_registry()["ladders"]
                             if L["id"] == "ladder_t1")
        check("ladder retrade: from still UTA after the non-matching call",
              ladder_after2["from"] == "UTA")

        # legacy: blocked
        node = {"type": "legacy", "reason": "test", "owner": "DET"}
        blocked = False
        try:
            registry.handle_retrade((2099, 1, "DET"), "DET", "GSW", node)
        except registry.RetradeBlocked:
            blocked = True
        check("legacy retrade: raises RetradeBlocked", blocked)

    finally:
        registry.REGISTRY_FILE = orig_registry

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("RETRADE TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

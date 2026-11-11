"""A trade's `swap` asset: one team gives another the right to swap picks.

The older `swap_with` field on a traded pick can't say this in one asset: it
names a pick that moves, and finds the other pick by who owns what mid-write,
so a plain two-team "A gives B the right to swap 1sts" came out as B holding
both picks, or as a flat transfer. The `swap` asset names both picks and moves
neither. These pin what it means and what it refuses.

Hermetic: synthetic years and team codes, a temp registry, injected picks.

    venv/bin/python -m tests.test_pick_swaps
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

from picks_conveyance import from_trade, registry, resolver  # noqa: E402
from routers import transactions as tx  # noqa: E402
from routers.transactions import TradeAsset, TradeIn, TradeTransfer  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def swap_trade(year=2099, rnd=1, giver="AAA", giver_orig="AAA", taker="BBB", taker_orig="BBB"):
    return TradeIn(transfers=[TradeTransfer(
        from_team=giver, to_team=taker,
        assets=[TradeAsset(type="swap", year=year, round=rnd,
                           orig=giver_orig, swap_orig=taker_orig)])])


def row(year, rnd, orig, owner, player="", frozen=""):
    return {"YEAR": str(year), "ROUND": str(rnd), "ORIG": orig, "OWNER": owner,
            "PLAYER": player, "FROZEN": frozen}


def settled(year, rnd, orig, team):
    return {"year": year, "round": rnd, "orig": orig,
            "conveyance": {"type": "settled", "team": team}}


def index(*rows):
    return {(int(r["YEAR"]), int(r["ROUND"]), r["ORIG"]): r for r in rows}


def test_register_direction():
    print("\n-- register_pick_swap: the receiving team gets the better pick --")
    from_trade.register_pick_swap(2099, 1, "AAA", "AAA", "BBB", "BBB",
                                  txn_id="t1", txn_date="2099-01-01")
    reg = registry.load_registry()
    gid = next(iter(reg["swap_groups"]))
    group = reg["swap_groups"][gid]
    check("priority is [to_team, from_team]", group["priority"] == ["BBB", "AAA"])
    check("txn stamped on the group", group.get("txn_ids") == [{"id": "t1", "date": "2099-01-01"}])
    store = {"swap_groups": {gid: group}}
    r = resolver.resolve_all(store, {(2099, 1, "AAA"): 3, (2099, 1, "BBB"): 20})
    check("AAA's pick better: BBB takes it", r[(2099, 1, "AAA")] == "BBB")
    check("AAA's pick better: AAA gets BBB's", r[(2099, 1, "BBB")] == "AAA")
    r = resolver.resolve_all(store, {(2099, 1, "AAA"): 25, (2099, 1, "BBB"): 2})
    check("BBB's own better: BBB keeps it", r[(2099, 1, "BBB")] == "BBB")
    check("BBB's own better: AAA keeps its own", r[(2099, 1, "AAA")] == "AAA")

    raised = False
    try:
        from_trade.register_pick_swap(2099, 1, "CCC", "CCC", "BBB", "BBB")
    except registry.SwapConflict:
        raised = True
    check("a second swap on a grouped pick raises SwapConflict", raised)


def test_problems():
    print("\n-- _pick_swap_problems: each side must hold its pick outright --")
    pi = index(row(2099, 1, "AAA", "AAA"), row(2099, 1, "BBB", "BBB"))
    store = {"picks": [settled(2099, 1, "AAA", "AAA"), settled(2099, 1, "BBB", "BBB")]}
    check("two outright picks: no problems",
          tx._pick_swap_problems(swap_trade(), pi, store) == [])

    # The giver offering a pick it holds from someone else is fine.
    pi2 = index(row(2099, 1, "CCC", "AAA"), row(2099, 1, "BBB", "BBB"))
    store2 = {"picks": [settled(2099, 1, "CCC", "AAA"), settled(2099, 1, "BBB", "BBB")]}
    check("an acquired pick held outright can be swapped",
          tx._pick_swap_problems(swap_trade(giver_orig="CCC"), pi2, store2) == [])

    store_wrong = {"picks": [settled(2099, 1, "AAA", "DDD"), settled(2099, 1, "BBB", "BBB")]}
    p = tx._pick_swap_problems(swap_trade(), pi, store_wrong)
    check("giver doesn't hold its pick: refused", len(p) == 1 and "AAA doesn't hold" in p[0])

    store_taker = {"picks": [settled(2099, 1, "AAA", "AAA"), settled(2099, 1, "BBB", "DDD")]}
    p = tx._pick_swap_problems(swap_trade(), pi, store_taker)
    check("receiver doesn't hold its pick: refused", len(p) == 1 and "BBB doesn't hold" in p[0])

    store_prot = {"picks": [{"year": 2099, "round": 1, "orig": "AAA",
                             "conveyance": {"type": "protected", "bands": []}},
                            settled(2099, 1, "BBB", "BBB")]}
    check("a protected pick is refused",
          len(tx._pick_swap_problems(swap_trade(), pi, store_prot)) == 1)

    pi_drafted = index(row(2099, 1, "AAA", "AAA", player="someone"), row(2099, 1, "BBB", "BBB"))
    check("a used pick is refused",
          len(tx._pick_swap_problems(swap_trade(), pi_drafted, store)) == 1)

    pi_frozen = index(row(2099, 1, "AAA", "AAA", frozen="TRUE"), row(2099, 1, "BBB", "BBB"))
    check("a frozen pick is refused",
          len(tx._pick_swap_problems(swap_trade(), pi_frozen, store)) == 1)

    check("the same pick twice is refused",
          len(tx._pick_swap_problems(swap_trade(taker_orig="AAA"), pi, store)) == 1)
    check("a missing pick is refused",
          len(tx._pick_swap_problems(swap_trade(taker_orig="ZZZ"), pi, store)) == 1)
    check("missing swap_orig is refused",
          len(tx._pick_swap_problems(swap_trade(taker_orig=None), pi, store)) == 1)

    # No store row: falls back to the flat owner, the same as the ownership gate.
    check("flat fallback: outright owner passes",
          tx._pick_swap_problems(swap_trade(), pi, {"picks": []}) == [])

    # Only the keys a ladder governs matter here, so it goes in raw.
    reg = registry.load_registry()
    reg["ladders"].append({"id": "lad_test", "from": "AAA", "to": "BBB",
                           "steps": [{"year": 2099, "round": 1, "protect_top": 5}],
                           "fallback": {"picks": []}})
    registry.save_registry(reg)
    check("a pick tied to a ladder is refused",
          any("ladder" in m for m in tx._pick_swap_problems(swap_trade(), pi, store)))


def test_stepien():
    print("\n-- Stepien: a swap leaves each team certain of one pick --")
    def pick(year, orig, owner):
        return {"year": year, "round": 1, "orig": orig, "owner": owner, "player": None}
    picks = [pick(2099, "AAA", "AAA"), pick(2100, "AAA", "AAA"), pick(2101, "AAA", "AAA"),
             pick(2099, "BBB", "BBB"), pick(2100, "BBB", "BBB"), pick(2101, "BBB", "BBB")]
    checks = tx._check_stepien_rule(swap_trade(year=2100), all_picks=picks)
    ids = {c.check: c.passed for c in checks}
    check("both teams checked", set(ids) == {"stepien_rule_aaa", "stepien_rule_bbb"})
    check("both still covered every year", all(ids.values()))

    # Giving away 2099 outright and swapping 2100 still leaves 2100 certain.
    trade = swap_trade(year=2100)
    trade.transfers[0].assets.append(TradeAsset(type="pick", year=2099, round=1, orig="AAA"))
    trade.transfers[0].assets.append(TradeAsset(type="pick", year=2101, round=1, orig="AAA"))
    r = {c.check: c.passed for c in tx._check_stepien_rule(trade, all_picks=picks)}
    check("AAA keeps one 2100 pick through the swap, so no two-year gap", r["stepien_rule_aaa"] is True)


def test_advance_limit():
    print("\n-- the 7-year limit covers a swap's year --")
    checks = tx._check_pick_advance_limit(swap_trade(year=2099), "26-27")
    check("a 2099 swap is beyond the limit",
          any(not c.passed and c.check.startswith("pick_advance_limit") for c in checks))


def test_apply_shape():
    print("\n-- _apply_trade refuses a swap whose pick also moves in the trade --")
    # Real team codes: _apply_trade checks teams before anything else.
    trade = swap_trade(giver="ATL", giver_orig="ATL", taker="BOS", taker_orig="BOS")
    trade.transfers[0].assets.append(TradeAsset(type="pick", year=2099, round=1, orig="ATL"))
    try:
        tx._apply_trade(trade, "2099-01-01", {"name": "test"})
        check("refused", False)
    except HTTPException as e:
        check("refused with 422 before touching data", e.status_code == 422 and "more than once" in e.detail)
    try:
        tx._apply_trade(swap_trade(giver="ATL", taker="BOS", taker_orig=None), "2099-01-01", {"name": "test"})
        check("missing swap_orig refused", False)
    except HTTPException as e:
        check("missing swap_orig refused", e.status_code == 422)


def main():
    orig = registry.REGISTRY_FILE
    registry.REGISTRY_FILE = Path("/tmp/test_pick_swaps_registry.json")
    registry.REGISTRY_FILE.unlink(missing_ok=True)
    registry.save_registry(registry._empty())
    try:
        test_register_direction()
        registry.save_registry(registry._empty())
        test_problems()
        test_stepien()
        test_advance_limit()
        test_apply_shape()
    finally:
        registry.REGISTRY_FILE = orig
    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("PICK SWAP TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

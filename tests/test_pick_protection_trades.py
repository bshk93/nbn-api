"""Trading a protection away, or lowering it.

A protection is bands: on a top-4 protected 1st, the keeper holds 1-4 and the
receiver 5-60. Lifting it is the keeper trading its band to the receiver.
Lowering it is the keeper sending that band with a smaller threshold, keeping
the top of it. These pin what the validator allows, and that the pick comes
out as one clean claim afterwards.

Hermetic: synthetic years, a temp registry.

    venv/bin/python -m tests.test_pick_protection_trades
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from picks_conveyance import registry  # noqa: E402
from routers import transactions as tx  # noqa: E402
from routers.transactions import TradeAsset, TradeIn, TradeTransfer  # noqa: E402

FAILS = []


def check(name, cond, detail=None):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


KEY = (2099, 1, "AAA")


def protect(bands):
    registry.save_registry(registry._empty())
    registry.add_protected(KEY, on={"year": 2099, "round": 1, "orig": "AAA"}, bands=bands)


def send(protection=None, leaf_id=None, giver="AAA", taker="BBB"):
    return TradeIn(transfers=[TradeTransfer(
        from_team=giver, to_team=taker,
        assets=[TradeAsset(type="pick", year=2099, round=1, orig="AAA",
                           protection=protection, leaf_id=leaf_id)])])


def test_problems():
    print("\n-- a new protection on a protected pick must fall inside the keeper's band --")
    protect([{"min": 1, "max": 10, "to": "AAA"}, {"min": 11, "max": 60, "to": "BBB"}])
    check("lowering top-10 to top-5: allowed", tx._pick_protection_problems(send(5)) == [])
    check("lowering to top-1: allowed", tx._pick_protection_problems(send(1)) == [])
    p = tx._pick_protection_problems(send(10))
    check("the same threshold is refused (nothing to convey)", len(p) == 1 and "1 to 9" in p[0], p)
    p = tx._pick_protection_problems(send(14))
    check("raising it is refused", len(p) == 1 and "not 14" in p[0], p)
    p = tx._pick_protection_problems(send(5, giver="CCC"))
    check("a team with no band can't set one", len(p) == 1 and "holds 0 bands" in p[0], p)
    check("lifting it (no threshold) needs no check", tx._pick_protection_problems(send()) == [])

    registry.save_registry(registry._empty())
    check("a pick with no protection takes any threshold",
          tx._pick_protection_problems(send(8)) == [])

    protect([{"min": 1, "max": 4, "to": "AAA"}, {"min": 5, "max": 14, "to": "BBB"},
             {"min": 15, "max": 60, "to": "AAA"}])
    p = tx._pick_protection_problems(send(2))
    check("a team holding two bands can't set one", len(p) == 1 and "holds 2 bands" in p[0], p)

    check("it's reported by /api/validate/trade as pick_protection",
          [c.check for c in tx._check_pick_protections(send(2))] == ["pick_protection"])


def test_lift():
    print("\n-- lifting: the keeper's band goes to the receiver --")
    protect([{"min": 1, "max": 4, "to": "AAA"}, {"min": 5, "max": 60, "to": "BBB"}])
    registry.handle_retrade(KEY, "AAA", "BBB", {"type": "protected", "id": "x"},
                            leaf_id="2099-1-AAA:protected:0")
    check("_normalize_pick_bands says the protection is gone", tx._normalize_pick_bands(KEY) is True)
    check("the pick has no bands left", registry.get_protected_spec(KEY) is None)


def test_lower():
    print("\n-- lowering: top-10 to top-5 --")
    protect([{"min": 1, "max": 10, "to": "AAA"}, {"min": 11, "max": 60, "to": "BBB"}])
    registry.subdivide_protected_band(KEY, "AAA", "BBB", 5)
    check("still protected", tx._normalize_pick_bands(KEY) is False)
    bands = registry.get_protected_spec(KEY)["bands"]
    check("AAA keeps 1-5, BBB holds 6-60 as one band",
          [(b["min"], b["max"], b["to"]) for b in bands] == [(1, 5, "AAA"), (6, 60, "BBB")], bands)
    check("BBB holds one band, so it can protect it again",
          tx._pick_protection_problems(send(20, giver="BBB", taker="CCC")) == [])


def test_stepien():
    print("\n-- Stepien sees a lifted protection --")
    def pick(year, orig, owner, leaves=None):
        return {"year": year, "round": 1, "orig": orig, "owner": owner, "player": None,
                "leaves": leaves or []}
    leaves = [{"team": "AAA", "description": "protected band 1-4", "leaf_id": "2100-1-AAA:protected:0"},
              {"team": "BBB", "description": "protected band 5-60", "leaf_id": "2100-1-AAA:protected:1"}]
    picks = [pick(2099, "AAA", "CCC"), pick(2100, "AAA", "BBB|AAA", leaves), pick(2101, "AAA", "CCC")]
    trade = TradeIn(transfers=[TradeTransfer(from_team="AAA", to_team="BBB", assets=[
        TradeAsset(type="pick", year=2100, round=1, orig="AAA", leaf_id="2100-1-AAA:protected:0")])])
    r = {c.check: c.passed for c in tx._check_stepien_rule(trade, all_picks=picks)}
    check("AAA giving up its only shot at a 2100 1st fails Stepien", r.get("stepien_rule_aaa") is False, r)


def test_claims():
    print("\n-- each traded band says what it does (TradeAsset.claim) --")
    protect([{"min": 1, "max": 4, "to": "AAA"}, {"min": 5, "max": 60, "to": "BBB"}])
    def claim(t):
        tx._describe_pick_claims(t)
        return t.transfers[0].assets[0].claim
    check("band to the team with the rest: protection removed", claim(send()) == "protection removed")
    check("with a smaller threshold: lowered", claim(send(2)) == "protection lowered to top 2")
    check("to a third team: just the slots", claim(send(taker="CCC")) == "picks 1-4")
    check("with a threshold, to a third team: the slots it conveys", claim(send(2, taker="CCC")) == "picks 3-4")
    t = send(); t.transfers[0].assets[0].claim = "made up"
    registry.save_registry(registry._empty())
    check("a client's claim is cleared on a plain pick", claim(t) is None)

    from routers import trc_notify
    check("#transactions post uses it",
          trc_notify._trade_asset({"type": "pick", "orig": "AAA", "year": 2099, "round": 1,
                                   "claim": "protection removed"}, {}) == "AAA 2099 1st (protection removed)")


def ladder(top=10):
    registry.save_registry(registry._empty())
    registry.add_ladder({"id": "lad", "from": "AAA", "to": "BBB",
                         "steps": [{"year": 2099, "round": 1, "orig": "AAA", "protect_top": top}],
                         "fallback": {"type": "fixed_asset",
                                      "picks": [{"year": 2099, "round": 2, "orig": "AAA"}]}})


def test_ladders():
    print("\n-- a ladder protection: AAA keeps it if top 10, else BBB; else BBB gets a 2nd --")
    settled = {"type": "settled", "team": "BBB"}
    ladder()
    registry.handle_retrade(KEY, "BBB", "CCC", settled, txn_id="t44", txn_date="2026-08-01")
    lad = registry.load_registry()["ladders"][0]
    check("the team owed the pick trading it moves the ladder's `to`", lad["to"] == "CCC", lad)
    check("and leaves the keeper alone", lad["from"] == "AAA")

    ladder()
    check("the keeper sending to the team owed it is a ladder keeper asset",
          tx._ladder_keeper(send().transfers[0].assets[0], "AAA", "BBB") is not None)
    check("to anyone else it isn't", tx._ladder_keeper(send(taker="CCC").transfers[0].assets[0], "AAA", "CCC") is None)
    t = send(); tx._describe_pick_claims(t)
    check("claim on the asset: protection removed", t.transfers[0].assets[0].claim == "protection removed")
    t = send(4); tx._describe_pick_claims(t)
    check("claim with a threshold: lowered", t.transfers[0].assets[0].claim == "protection lowered to top 4")
    check("lowering to 4: allowed", tx._pick_protection_problems(send(4)) == [])
    p = tx._pick_protection_problems(send(10))
    check("lowering to the same 10: refused", len(p) == 1 and "1 to 9" in p[0], p)

    tx._trade_ladder_protection(KEY, "AAA", "BBB", 4, "t1", "2099-01-01")
    check("lowered: protect_top is 4", registry.load_registry()["ladders"][0]["steps"][0]["protect_top"] == 4)
    tx._trade_ladder_protection(KEY, "AAA", "BBB", None, "t2", "2099-01-02")
    check("removed: the ladder is gone, fallback and all", registry.load_registry()["ladders"] == [])


def test_team_picks_claims():
    print("\n-- GET /api/picks/{team}?claims=all lists a band the team keeps --")
    from routers import roster_picks as rp
    real = rp._all_picks_flat
    rp._all_picks_flat = lambda: [{
        "year": 2099, "round": 1, "orig": "ATL", "owner": "BOS",
        "leaves": [{"team": "ATL", "description": "protected band 1-4"},
                   {"team": "BOS", "description": "protected band 5-60"}]}]
    try:
        check("the protecting team doesn't see it by default", rp.get_team_picks("ATL") == [])
        check("with claims=all it does", len(rp.get_team_picks("ATL", claims="all")) == 1)
        check("the owner sees it either way", len(rp.get_team_picks("BOS")) == 1)
        rp._all_picks_flat = lambda: [{"year": 2099, "round": 1, "orig": "ATL", "owner": "BOS",
                                       "leaves": [], "ladder": {"from": "ATL", "to": "BOS", "protect_top": 10}}]
        check("a ladder's keeper sees it with claims=all", len(rp.get_team_picks("ATL", claims="all")) == 1)
    finally:
        rp._all_picks_flat = real


def main():
    orig = registry.REGISTRY_FILE
    registry.REGISTRY_FILE = Path("/tmp/test_pick_protection_trades.json")
    registry.REGISTRY_FILE.unlink(missing_ok=True)
    try:
        test_problems()
        test_lift()
        test_lower()
        test_stepien()
        test_claims()
        test_ladders()
        test_team_picks_claims()
    finally:
        registry.REGISTRY_FILE = orig
    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("PICK PROTECTION TRADE TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

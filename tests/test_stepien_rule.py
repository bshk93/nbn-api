"""Regression tests for §7.2 Stepien Rule trade validation
(routers.transactions._check_stepien_rule).

Written 2026-07-23 after three real bugs shipped and were caught live
against production picks data instead of being caught here first:

  1. Two pick rows sharing a `group_id` (a swap group / binary chain) were
     treated as independent claims, so a team could "trade one leg, keep
     the other" when both rows are actually the SAME shared claim.
  2. A multi-band `protected` pick (no `group_id` — bands aren't shared
     the way swap groups are) had its WHOLE owner_map entry blindly
     overwritten on a plain retrade, silently deleting every OTHER
     band-holder's real, unrelated claim.
  3. A pick with `ladder_fallback_of` set (real compensation for a
     DIFFERENT pick's protection ladder never resolving) was invisible —
     its own owner/leaves look like a plain settled pick, so the real
     claimant's coverage was undercounted.

Every scenario below uses synthetic far-future years/team abbrevs (never
real NBN team codes) and injects `all_picks` directly — `_check_stepien_rule`
never touches the real picks store when called this way, so this suite is
fully hermetic.

    venv/bin/python -m tests.test_stepien_rule
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.transactions import (  # noqa: E402
    _check_stepien_rule, TradeIn, TradeTransfer, TradeAsset,
)

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def pick(year, orig, owner, group_id=None, ladder_fallback_of=None):
    return {"year": year, "round": 1, "orig": orig, "owner": owner,
            "player": None, "group_id": group_id,
            "ladder_fallback_of": ladder_fallback_of}


def result_for(checks, team):
    return next((c for c in checks if c.check == f"stepien_rule_{team.lower()}"), None)


def test_basic_gap_detection():
    print("\n-- basic gap detection: sole plain claim, trading it away opens a gap --")
    picks = [
        pick(2099, "ZZZ", "ZZZ"),
        pick(2100, "AAA", "AAA"),   # AAA's only claim anywhere in range
        pick(2101, "ZZZ", "ZZZ"),
    ]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="AAA")]),
    ])
    checks = _check_stepien_rule(details, all_picks=picks)
    r = result_for(checks, "AAA")
    check("AAA blocked after giving up its only claim", r is not None and r.passed is False)

    # Same trade, but AAA also holds real, unrelated claims in BOTH
    # neighboring years — losing 2100 alone (with 2099 and 2101 both still
    # covered) is a legal single-year gap, not a violation.
    picks2 = picks + [pick(2099, "AAA2", "AAA"), pick(2101, "AAA3", "AAA")]
    checks2 = _check_stepien_rule(details, all_picks=picks2)
    r2 = result_for(checks2, "AAA")
    check("AAA passes when both neighboring years are independently covered",
          r2 is not None and r2.passed is True)


def test_no_pick_assets_short_circuits():
    print("\n-- trade with no pick assets returns no Stepien checks --")
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ", assets=[]),
    ])
    checks = _check_stepien_rule(details, all_picks=[pick(2100, "AAA", "AAA")])
    check("empty result for a pick-free trade", checks == [])


def test_shared_swap_group_is_one_claim():
    print("\n-- shared swap-group rows are ONE claim, not two independent ones --")
    picks = [
        pick(2099, "ZZZ", "ZZZ"),
        pick(2100, "BBB", "CCC|BBB", group_id="swap:sg_test"),
        pick(2100, "YYY", "CCC|BBB", group_id="swap:sg_test"),
        pick(2101, "ZZZ", "ZZZ"),
    ]
    for leg_orig in ("BBB", "YYY"):
        details = TradeIn(transfers=[
            TradeTransfer(from_team="BBB", to_team="ZZZ",
                           assets=[TradeAsset(type="pick", year=2100, round=1, orig=leg_orig)]),
        ])
        checks = _check_stepien_rule(details, all_picks=picks)
        r = result_for(checks, "BBB")
        check(f"BBB blocked trading the {leg_orig}-orig leg (other leg doesn't save it)",
              r is not None and r.passed is False)


def test_independent_double_claim_not_treated_as_shared():
    print("\n-- two picks with NO shared group_id are genuinely independent --")
    picks = [
        pick(2099, "ZZZ", "ZZZ"),
        pick(2100, "AAA", "AAA"),          # AAA's own settled pick
        pick(2100, "MMM", "AAA"),          # a second, unrelated pick AAA also holds
        pick(2101, "ZZZ", "ZZZ"),
    ]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="AAA")]),
    ])
    checks = _check_stepien_rule(details, all_picks=picks)
    r = result_for(checks, "AAA")
    check("AAA still passes: the second, independent 2100 pick covers it",
          r is not None and r.passed is True)


def band(team, lo, hi):
    return {"team": team, "description": f"protected band {lo}-{hi}"}


def test_multiband_protected_preserves_other_bands():
    print("\n-- retrading one band of a protected pick leaves the others intact --")
    picks = [
        pick(2099, "ZZZ", "ZZZ"),
        dict(pick(2100, "DDD", "DDD|EEE"), leaves=[band("DDD", 1, 4), band("EEE", 5, 60)]),
        pick(2100, "GGG", "EEE"),           # EEE's own separate 2100 pick
        pick(2101, "ZZZ", "ZZZ"),
    ]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="DDD", to_team="EEE",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="DDD")]),
        TradeTransfer(from_team="EEE", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="GGG")]),
    ])
    checks = _check_stepien_rule(details, all_picks=picks)
    check("DDD blocked: gave up its only claim", result_for(checks, "DDD").passed is False)
    check("EEE passes: it now holds both bands, so the pick is certainly its own",
          result_for(checks, "EEE").passed is True)


def test_protected_claim_is_not_a_pick():
    print("\n-- a protected band only counts where the team actually gets the pick --")
    picks = [
        pick(2099, "AAA", "AAA"),
        dict(pick(2100, "AAA", "AAA|ZZZ"), leaves=[band("AAA", 1, 4), band("ZZZ", 5, 60)]),
        pick(2101, "AAA", "AAA"),
        pick(2102, "AAA", "AAA"),
    ]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2101, round=1, orig="AAA")]),
    ])
    r = result_for(_check_stepien_rule(details, all_picks=picks), "AAA")
    check("AAA blocked: its top-4-only 2100 claim doesn't cover 2100-2101",
          r.passed is False and "protections fall the wrong way" in r.message)


def test_trading_a_protected_pick_counts():
    print("\n-- trading away a pick with protection is checked, not skipped --")
    picks = [pick(2099, "ZZZ", "ZZZ"), pick(2100, "AAA", "AAA"), pick(2101, "ZZZ", "ZZZ")]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="AAA", protection=4)]),
    ])
    r = result_for(_check_stepien_rule(details, all_picks=picks), "AAA")
    check("AAA blocked: a top-4-protected pick is usually gone", r.passed is False)


def test_swap_with_hands_over_a_pick():
    print("\n-- giving a pick away inside a new swap still loses it --")
    picks = [pick(2099, "ZZZ", "ZZZ"), pick(2100, "AAA", "AAA"),
             pick(2100, "ZZZ", "ZZZ"), pick(2101, "ZZZ", "ZZZ")]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="AAA", swap_with="ZZZ")]),
    ])
    checks = _check_stepien_rule(details, all_picks=picks)
    check("AAA blocked: both swap picks now go to ZZZ", result_for(checks, "AAA").passed is False)


def test_ladder_fallback_scored_with_its_pick():
    print("\n-- a ladder pick and its fallback resolve together, not independently --")
    picks = [
        pick(2099, "AAA", "AAA"),
        pick(2100, "AAA", "AAA"),
        pick(2101, "AAA", "AAA"),
        pick(2102, "AAA", "AAA"),
    ]
    # AAA sends 2100 top-5 protected; if it stays protected, ZZZ gets 2101
    # instead. Either way AAA keeps one of the two, so no 2100-2101 gap.
    details = TradeIn(transfers=[
        TradeTransfer(from_team="AAA", to_team="ZZZ", assets=[TradeAsset(
            type="pick", year=2100, round=1, orig="AAA", protection=5,
            ladder_protect_top=5, ladder_fallback=[{"year": 2101, "round": 1, "orig": "AAA"}])]),
    ])
    r = result_for(_check_stepien_rule(details, all_picks=picks), "AAA")
    check("AAA passes: it always keeps 2100 or 2101", r.passed is True)

    # The same, already on the books, as GET /api/picks serves it.
    booked = [
        pick(2099, "AAA", "AAA"),
        dict(pick(2100, "AAA", "AAA|ZZZ"), leaves=[band("AAA", 1, 5), band("ZZZ", 6, 60)]),
        pick(2101, "AAA", "AAA", ladder_fallback_of={
            "to": "ZZZ", "protect_top": 5,
            "governing_pick": {"year": 2100, "round": 1, "orig": "AAA"}}),
        pick(2102, "AAA", "AAA"),
        pick(2103, "AAA", "AAA"),
    ]
    trade_2103 = TradeIn(transfers=[TradeTransfer(from_team="AAA", to_team="ZZZ", assets=[
        TradeAsset(type="pick", year=2103, round=1, orig="AAA")])])
    check("AAA passes on the booked version too",
          result_for(_check_stepien_rule(trade_2103, all_picks=booked), "AAA").passed is True)

    trade_2102 = TradeIn(transfers=[TradeTransfer(from_team="AAA", to_team="ZZZ", assets=[
        TradeAsset(type="pick", year=2102, round=1, orig="AAA")])])
    check("AAA blocked trading 2102: if 2100 stays protected it loses 2101 and 2102",
          result_for(_check_stepien_rule(trade_2102, all_picks=booked), "AAA").passed is False)


def main():
    test_basic_gap_detection()
    test_no_pick_assets_short_circuits()
    test_shared_swap_group_is_one_claim()
    test_independent_double_claim_not_treated_as_shared()
    test_multiband_protected_preserves_other_bands()
    test_protected_claim_is_not_a_pick()
    test_trading_a_protected_pick_counts()
    test_swap_with_hands_over_a_pick()
    test_ladder_fallback_scored_with_its_pick()

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

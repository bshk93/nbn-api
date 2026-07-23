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


def test_multiband_protected_preserves_other_bands():
    print("\n-- retrading one band of a multi-band protected pick leaves others intact --")
    picks = [
        pick(2099, "ZZZ", "ZZZ"),
        pick(2100, "DDD", "DDD|EEE|FFF"),   # 3 independent bands, NO group_id
        pick(2100, "GGG", "EEE"),           # EEE's own separate, unrelated 2100 pick
        pick(2101, "ZZZ", "ZZZ"),
    ]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="DDD", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="DDD")]),
        TradeTransfer(from_team="EEE", to_team="ZZZ",
                       assets=[TradeAsset(type="pick", year=2100, round=1, orig="GGG")]),
    ])
    checks = _check_stepien_rule(details, all_picks=picks)
    rd = result_for(checks, "DDD")
    re_ = result_for(checks, "EEE")
    check("DDD blocked: gave up its only claim",
          rd is not None and rd.passed is False)
    check("EEE still passes: its band on the DDD-orig pick was never touched, "
          "even though DDD retraded ITS OWN band of the same pick",
          re_ is not None and re_.passed is True)


def test_ladder_fallback_credited():
    print("\n-- ladder_fallback_of claims count toward the fallback team's coverage --")
    picks = [
        pick(2099, "JJJ", "JJJ"),
        pick(2099, "LLL", "JJJ"),           # JJJ's second, harmless 2099 claim
        pick(2100, "ZZZ", "ZZZTEAM"),       # unrelated, just fills out the year range
        pick(2101, "III", "III", ladder_fallback_of={"to": "JJJ"}),
    ]
    details = TradeIn(transfers=[
        TradeTransfer(from_team="JJJ", to_team="ZZZTEAM",
                       assets=[TradeAsset(type="pick", year=2099, round=1, orig="LLL")]),
    ])
    checks = _check_stepien_rule(details, all_picks=picks)
    r = result_for(checks, "JJJ")
    check("JJJ passes: the 2101 fallback claim covers what would otherwise be a "
          "2100+2101 gap (JJJ never appears in ZZZ's 2100 pick's owner at all)",
          r is not None and r.passed is True)

    # Same fixture, minus the fallback link — JJJ should now correctly fail,
    # proving the pass above is really the fallback credit at work, not a
    # test-fixture accident.
    picks_no_fallback = [dict(p, ladder_fallback_of=None) if p["orig"] == "III" else p
                          for p in picks]
    checks2 = _check_stepien_rule(details, all_picks=picks_no_fallback)
    r2 = result_for(checks2, "JJJ")
    check("JJJ fails without the fallback link (control case)",
          r2 is not None and r2.passed is False)


def main():
    test_basic_gap_detection()
    test_no_pick_assets_short_circuits()
    test_shared_swap_group_is_one_claim()
    test_independent_double_claim_not_treated_as_shared()
    test_multiband_protected_preserves_other_bands()
    test_ladder_fallback_credited()

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

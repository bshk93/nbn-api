"""Regression tests for §7.2's seven-year advance limit
(routers.transactions._check_pick_advance_limit).

The Stepien half of §7.2 was enforced 2026-07-23 (test_stepien_rule.py); the
companion "picks may only be traded up to 7 years ahead of the current league
year" rule had no check at all until now. The horizon is deliberately the
same number the picks ledger is kept populated through
(`roster_picks._pick_year_horizon`) — see that function's docstring for why
duplicating the "+7" arithmetic here would be a drift risk, not just
repetition.

    venv/bin/python -m tests.test_pick_advance_limit
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.transactions import (  # noqa: E402
    _check_pick_advance_limit, TradeIn, TradeTransfer, TradeAsset,
)
from routers.roster_picks import _pick_year_horizon  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


SEASON = "26-27"
HORIZON = _pick_year_horizon(SEASON)  # 2033


def pick_asset(year, round_=1, orig="ZZZ"):
    return TradeAsset(type="pick", year=year, round=round_, orig=orig)


def trade(*assets, from_team="AAA", to_team="BBB"):
    return TradeIn(transfers=[TradeTransfer(from_team=from_team, to_team=to_team, assets=list(assets))])


def result_for(checks, year, orig="zzz"):
    return next((c for c in checks if c.check == f"pick_advance_limit_{year}_{orig}"), None)


def test_horizon_math():
    print("\n-- the horizon is season's start year + 7, not hardcoded here --")
    check("26-27 -> 2033", HORIZON == 2033)
    check("20-21 -> 2027", _pick_year_horizon("20-21") == 2027)


def test_within_limit_passes():
    print("\n-- a pick at exactly the horizon is legal --")
    checks = _check_pick_advance_limit(trade(pick_asset(HORIZON)), SEASON)
    r = result_for(checks, HORIZON)
    check("a result exists", r is not None)
    check("passes", r.passed)


def test_beyond_limit_fails():
    print("\n-- a pick one year beyond the horizon is illegal --")
    checks = _check_pick_advance_limit(trade(pick_asset(HORIZON + 1)), SEASON)
    r = result_for(checks, HORIZON + 1)
    check("a result exists", r is not None)
    check("fails", not r.passed)
    check("is an error, not a warning", r.level == "error")
    check("names the horizon year", str(HORIZON) in r.message)


def test_second_round_checked_too():
    print("\n-- unlike Stepien, this isn't first-round only --")
    checks = _check_pick_advance_limit(trade(pick_asset(HORIZON + 3, round_=2)), SEASON)
    r = result_for(checks, HORIZON + 3)
    check("a 2nd-round pick beyond the horizon is caught", r is not None and not r.passed)


def test_mixed_trade():
    print("\n-- a trade with one legal and one illegal pick reports both --")
    checks = _check_pick_advance_limit(
        trade(pick_asset(HORIZON, orig="AAA"), pick_asset(HORIZON + 2, orig="BBB")), SEASON)
    legal = result_for(checks, HORIZON, orig="aaa")
    illegal = result_for(checks, HORIZON + 2, orig="bbb")
    check("the in-range pick passes", legal is not None and legal.passed)
    check("the out-of-range pick fails", illegal is not None and not illegal.passed)


def test_no_picks_in_trade():
    print("\n-- a player-only trade still returns a rubric line, not silence --")
    player_only = TradeIn(transfers=[TradeTransfer(
        from_team="AAA", to_team="BBB",
        assets=[TradeAsset(type="player", slug="doe-john")])])
    checks = _check_pick_advance_limit(player_only, SEASON)
    check("exactly one summary result", len(checks) == 1)
    check("and it passes", checks[0].passed)


def test_dedupes_the_same_pick_named_twice():
    print("\n-- the same (year, round, orig) named in two legs of the same trade dedupes --")
    checks = _check_pick_advance_limit(
        trade(pick_asset(HORIZON + 1, orig="CCC"), pick_asset(HORIZON + 1, orig="CCC")), SEASON)
    matches = [c for c in checks if c.check == f"pick_advance_limit_{HORIZON + 1}_ccc"]
    check("only one result for the repeated pick", len(matches) == 1)


def test_season_shifts_the_horizon():
    print("\n-- checked against the transaction's own season, not wall-clock today --")
    early_season = "20-21"
    early_horizon = _pick_year_horizon(early_season)
    checks = _check_pick_advance_limit(trade(pick_asset(early_horizon + 1)), early_season)
    r = result_for(checks, early_horizon + 1)
    check("a pick legal under 26-27's horizon is illegal under 20-21's",
          r is not None and not r.passed)


test_horizon_math()
test_within_limit_passes()
test_beyond_limit_fails()
test_second_round_checked_too()
test_mixed_trade()
test_no_picks_in_trade()
test_dedupes_the_same_pick_named_twice()
test_season_shifts_the_horizon()

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))
sys.exit(1 if FAILS else 0)

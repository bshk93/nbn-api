"""Regression tests for § 4.5 / § 6.2 rule 11's six-month extension trade
freeze (routers.transactions._check_extension_trade_restriction).

A trade-side check by design (extensions.md § 6) — it validates the *trade*,
not the extension, and reuses the "extension" entries
_player_acquisition_index carries specifically for this rather than a second
ledger scan (see that function's docstring, and test_bird_rights_tenure.py's
companion pin that an "extension" entry never becomes a "sign"/"trade"/
"release" one — this rule depending on the entry existing is exactly why it
had to be added there safely rather than left out).

    venv/bin/python -m tests.test_extension_trade_freeze
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as T  # noqa: E402
from routers.transactions import (  # noqa: E402
    _check_extension_trade_restriction, _add_months, TradeIn, TradeTransfer, TradeAsset,
)

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def player_asset(slug):
    return TradeAsset(type="player", slug=slug)


def trade(*assets, from_team="AAA", to_team="BBB"):
    return TradeIn(transfers=[TradeTransfer(from_team=from_team, to_team=to_team, assets=list(assets))])


def result_for(checks, slug):
    return next((c for c in checks if c.check == f"extension_trade_freeze_{slug}"), None)


def with_ledger(entries, run):
    orig = T._load_transactions
    T._load_transactions = lambda: entries
    try:
        T._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})
        return run()
    finally:
        T._load_transactions = orig
        T._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})


def ext_txn(player, announced, team="AAA"):
    return {"type": "extension", "date": announced,
           "details": {"player": player, "team": team, "announced_date": announced}}


def test_add_months():
    print("\n-- calendar-correct month addition --")
    from datetime import datetime
    check("Jan 15 + 6mo -> Jul 15", _add_months(datetime(2026, 1, 15), 6) == datetime(2026, 7, 15))
    check("Dec 1 + 6mo -> Jun 1 next year (year rollover)",
          _add_months(datetime(2026, 12, 1), 6) == datetime(2027, 6, 1))
    check("Aug 31 + 6mo -> Feb 28 (clamped, non-leap 2027)",
          _add_months(datetime(2026, 8, 31), 6) == datetime(2027, 2, 28))
    check("Aug 31 2027 + 6mo -> Feb 29 2028 (clamped, leap year)",
          _add_months(datetime(2027, 8, 31), 6) == datetime(2028, 2, 29))


def test_within_freeze_fails():
    print("\n-- inside the six-month window --")
    checks = with_ledger(
        [ext_txn("p", "2026-06-01")],
        lambda: _check_extension_trade_restriction(trade(player_asset("p")), {"txn_date": "2026-08-15"}))
    r = result_for(checks, "p")
    check("a trade 2.5 months after the extension is refused", r is not None and not r.passed)
    check("...and names the date it clears", "2026-12-01" in r.message)


def test_after_freeze_passes():
    print("\n-- past the six-month window --")
    checks = with_ledger(
        [ext_txn("p", "2026-01-01")],
        lambda: _check_extension_trade_restriction(trade(player_asset("p")), {"txn_date": "2026-08-15"}))
    r = result_for(checks, "p")
    check("a trade 7.5 months after the extension passes", r is not None and r.passed)


def test_exact_boundary_clears():
    print("\n-- exactly six months later clears --")
    checks = with_ledger(
        [ext_txn("p", "2026-02-01")],
        lambda: _check_extension_trade_restriction(trade(player_asset("p")), {"txn_date": "2026-08-01"}))
    r = result_for(checks, "p")
    check("trading on the exact six-month anniversary is legal", r is not None and r.passed)


def test_no_extension_no_check():
    print("\n-- a player with no extension on file is simply not checked --")
    checks = with_ledger([], lambda: _check_extension_trade_restriction(trade(player_asset("p")), {"txn_date": "2026-08-15"}))
    check("no extension_trade_freeze_p entry at all", result_for(checks, "p") is None)


def test_latest_extension_wins():
    print("\n-- only the most recent extension per player matters --")
    checks = with_ledger(
        [ext_txn("p", "2024-01-01"), ext_txn("p", "2026-06-01")],
        lambda: _check_extension_trade_restriction(trade(player_asset("p")), {"txn_date": "2026-08-15"}))
    r = result_for(checks, "p")
    check("the OLD 2024 extension (long since cleared) doesn't matter", r is not None and not r.passed)
    check("...the freeze runs from the 2026 one instead", "2026-12-01" in r.message)


def test_picks_never_checked():
    print("\n-- a pick asset is never mistaken for a player leg --")
    pick = TradeAsset(type="pick", year=2030, round=1, orig="AAA")
    checks = with_ledger(
        [ext_txn("p", "2026-06-01")],
        lambda: _check_extension_trade_restriction(trade(pick), {"txn_date": "2026-08-15"}))
    check("no player checks fired at all", checks == [])


def test_multi_player_trade():
    print("\n-- each player leg is checked independently --")
    checks = with_ledger(
        [ext_txn("frozen", "2026-06-01"), ext_txn("clear", "2024-01-01")],
        lambda: _check_extension_trade_restriction(
            trade(player_asset("frozen"), player_asset("clear"), player_asset("untouched")),
            {"txn_date": "2026-08-15"}))
    check("the recently-extended player blocks", not result_for(checks, "frozen").passed)
    check("the long-cleared player doesn't", result_for(checks, "clear").passed)
    check("the never-extended player isn't checked at all", result_for(checks, "untouched") is None)


test_add_months()
test_within_freeze_fails()
test_after_freeze_passes()
test_exact_boundary_clears()
test_no_extension_no_check()
test_latest_extension_wins()
test_picks_never_checked()
test_multi_player_trade()

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))
sys.exit(1 if FAILS else 0)

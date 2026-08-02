"""Regression tests for the two automation gaps closed 2026-07-28:

  1. § 4.1a Trade Exception *creation* — previously TPEs could only be entered
     by hand from the league spreadsheet. `_tpe_bankable` is the predicate
     `_apply_trade` now uses to bank one automatically.
  2. § 1.4 Row D / § 1.5.2 mid-season buyout signings above the NTMLE — the
     last unautomated hard-cap trigger. `_buyout_salary_above_ntmle` is the
     detector used by both the apply path (sets the hard cap) and the validate
     path (blocks the signing outright for an already-first-apron team).

Both helpers are pure: `_tpe_bankable` takes plain numbers, and
`_buyout_salary_above_ntmle` reads transactions only through
`_load_transactions`, which is monkeypatched here. Nothing in this suite
touches production data.

    venv/bin/python -m tests.test_tpe_and_hardcap
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []

CAP = 165_000_000
NTMLE = 15_044_000
SEASON = "26-27"
CAP_LEVELS = {SEASON: {"cap": CAP, "ntmle_amount": NTMLE}}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── § 4.1a: which teams bank a Trade Exception ──────────────────────────────

def test_tpe_bankable():
    print("\n§ 4.1a — Trade Exception creation")

    # Over the cap, sent out more than it took back: banks the difference.
    check("over-cap team banks outgoing minus incoming",
          T._tpe_bankable(20_000_000, 8_000_000, CAP + 30_000_000, CAP, False) == 12_000_000)

    # The core CBA rule this was built to honour: a below-cap team gets
    # nothing, because its cap room already absorbs the same difference.
    check("under-cap team banks nothing",
          T._tpe_bankable(20_000_000, 8_000_000, CAP - 1, CAP, False) is None)

    # Exactly at the cap is not *over* it.
    check("team exactly at the cap banks nothing",
          T._tpe_bankable(20_000_000, 8_000_000, CAP, CAP, False) is None)

    # One dollar over is over.
    check("team one dollar over the cap banks",
          T._tpe_bankable(20_000_000, 8_000_000, CAP + 1, CAP, False) == 12_000_000)

    # Took back at least as much as it sent: no difference to bank.
    check("net salary gain banks nothing",
          T._tpe_bankable(8_000_000, 20_000_000, CAP + 30_000_000, CAP, False) is None)
    check("even salary swap banks nothing",
          T._tpe_bankable(20_000_000, 20_000_000, CAP + 30_000_000, CAP, False) is None)

    # Can't absorb via an exception and bank a new one in the same trade.
    check("team that used an exception banks nothing",
          T._tpe_bankable(20_000_000, 8_000_000, CAP + 30_000_000, CAP, True) is None)

    # No cap configured for the season — refuse to guess.
    check("missing cap level banks nothing",
          T._tpe_bankable(20_000_000, 8_000_000, CAP + 30_000_000, None, False) is None)

    # Pure salary dump, nothing coming back.
    check("pure dump banks the full outgoing amount",
          T._tpe_bankable(9_500_000, 0, CAP + 5_000_000, CAP, False) == 9_500_000)


# ── § 1.4 Row D / § 1.5.2: buyout signings above the NTMLE ──────────────────

def _with_transactions(rows, fn):
    """Run `fn` with `_load_transactions` returning `rows`."""
    original = T._load_transactions
    T._load_transactions = lambda: rows
    try:
        return fn()
    finally:
        T._load_transactions = original


def _release(slug, date, salary, txn_type="release"):
    return {"type": txn_type, "date": date,
            "details": {"player": slug, "terminated_salary": salary}}


def test_buyout_detection():
    print("\n§ 1.4 Row D — buyout signing above the NTMLE")

    def detect(rows, sign_date="2027-01-15", prior=""):
        return _with_transactions(rows, lambda: T._buyout_salary_above_ntmle(
            "star-player", sign_date, SEASON, CAP_LEVELS, prior))

    # Released this season above the NTMLE, signed later: triggers.
    check("release above NTMLE earlier this season triggers",
          detect([_release("star-player", "2026-12-01", "$30,000,000")]) == 30_000_000)

    # Below the NTMLE: ordinary signing, no lock.
    check("release below NTMLE does not trigger",
          detect([_release("star-player", "2026-12-01", "$5,000,000")]) is None)

    # Exactly the NTMLE is not *exceeding* it.
    check("release exactly at NTMLE does not trigger",
          detect([_release("star-player", "2026-12-01", f"${NTMLE:,}")]) is None)

    # A different player's buyout is irrelevant.
    check("another player's release does not trigger",
          detect([_release("someone-else", "2026-12-01", "$30,000,000")]) is None)

    # A release dated after the signing can't be its cause.
    check("release after the signing date does not trigger",
          detect([_release("star-player", "2027-06-01", "$30,000,000")]) is None)

    # Prior league year — rule is scoped to the same season.
    check("release in a prior season does not trigger",
          detect([_release("star-player", "2025-12-01", "$30,000,000")]) is None)

    # void_player terminates a contract too.
    check("void_player above NTMLE triggers",
          detect([_release("star-player", "2026-12-01", "$30,000,000",
                           txn_type="void_player")]) == 30_000_000)

    # Unrelated transaction types are ignored.
    check("a trade of the same player does not trigger",
          detect([{"type": "trade", "date": "2026-12-01",
                   "details": {"player": "star-player"}}]) is None)

    # Legacy releases predate the `terminated_salary` field: fall back to the
    # salary captured off the bio before the new contract overwrote it.
    legacy = [{"type": "release", "date": "2026-12-01",
               "details": {"player": "star-player", "dead_cap": {}}}]
    check("legacy release with no terminated_salary uses the bio fallback",
          detect(legacy, prior="$30,000,000") == 30_000_000)
    check("legacy release with no fallback available does not trigger",
          detect(legacy) is None)

    # A non-guaranteed contract terminating with zero dead cap still counts —
    # this is exactly why dead cap can't stand in for the terminated salary.
    nongtd = [{"type": "release", "date": "2026-12-01",
               "details": {"player": "star-player", "dead_cap": {},
                           "terminated_salary": "$25,000,000"}}]
    check("terminated contract with no dead cap still triggers",
          detect(nongtd) == 25_000_000)

    # No cap levels for the season: refuse to guess.
    check("missing ntmle_amount does not trigger",
          _with_transactions([_release("star-player", "2026-12-01", "$30,000,000")],
                             lambda: T._buyout_salary_above_ntmle(
                                 "star-player", "2027-01-15", SEASON, {}, "")) is None)


def main():
    test_tpe_bankable()
    test_buyout_detection()
    print("\n" + "=" * 40)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

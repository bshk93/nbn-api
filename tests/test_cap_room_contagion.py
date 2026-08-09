"""Regression test for the § 4.3 contagion / cap-room absorption fix (2026-08-09).

The § 4.3 "contagion" rule hard-caps a below-First-Apron team at the First
Apron when a trade brings in more than outgoing + $250K. Until this fix, that
fired even when the incoming salary was absorbed purely via cap room (§ 4.2)
— a mechanic the real NBA doesn't have: a plain cap-space acquisition never
touches the apron there, only the named exceptions (NTMLE/TMLE/BAE/sign-and-
trade) and over-the-cap tiered matching do.

`_cap_room_absorbed` is the shared predicate `_check_salary_matching` and
both § 4.3 contagion sites (`_apply_trade`'s apply-time block and
`_validate_trade`'s pre-submit warning) now gate on. It's pure — plain
numbers in, bool out — so it's tested directly here rather than through the
full trade pipeline.

    venv/bin/python -m tests.test_cap_room_contagion
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []

CAP = 165_000_000


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def test_cap_room_absorbed():
    print("\n_cap_room_absorbed — § 4.2 predicate shared with § 4.3 contagion")

    # DEN-shaped case: well below the cap, $0 outgoing, incoming comfortably
    # inside the room. This is the clean cap-space trade the contagion rule
    # should now leave alone.
    check("comfortably below cap, $0 outgoing, room covers incoming",
          T._cap_room_absorbed(140_000_000, 0, 20_000_000, CAP) is True)

    # Room covers it exactly (post-trade == cap, not over it).
    check("lands exactly on the cap still counts as absorbed",
          T._cap_room_absorbed(145_000_000, 0, 20_000_000, CAP) is True)

    # One dollar short of the cap after the trade: not absorbed, falls
    # through to tiered matching / contagion as before.
    check("one dollar over the cap after the trade is not absorbed",
          T._cap_room_absorbed(145_000_001, 0, 20_000_000, CAP) is False)

    # Team already at/above the cap before the trade: never eligible for cap
    # room, regardless of outgoing salary sent.
    check("team already at the cap before the trade is not absorbed",
          T._cap_room_absorbed(CAP, 10_000_000, 15_000_000, CAP) is False)
    check("team already above the cap before the trade is not absorbed",
          T._cap_room_absorbed(CAP + 5_000_000, 10_000_000, 5_000_000, CAP) is False)

    # No cap level on file for the season: refuse to guess, same as every
    # other cap-levels-missing branch in this file.
    check("missing cap level is never absorbed",
          T._cap_room_absorbed(100_000_000, 0, 20_000_000, None) is False)

    # Outgoing salary is still netted in — a team sending real salary out
    # needs correspondingly less room.
    check("outgoing salary reduces the room needed",
          T._cap_room_absorbed(150_000_000, 10_000_000, 25_000_000, CAP) is True)
    check("without the outgoing credit the same incoming salary would fail",
          T._cap_room_absorbed(150_000_000, 0, 25_000_000, CAP) is False)


def test_check_salary_matching_uses_cap_room():
    print("\n_check_salary_matching — cap-room branch still short-circuits matching")

    cap_levels = {"26-27": {"cap": CAP, "apron1": 209_015_000}}

    # A trade that would fail standard tiered matching outright (huge
    # incoming, zero outgoing) still passes when the team has the room —
    # this is the Keldon-Johnson-shaped case that needed `force: true`
    # before the room was actually there.
    r = T._check_salary_matching(
        "DEN", outgoing=0, incoming=20_000_000,
        team_salary_before=140_000_000, cap_levels=cap_levels, season="26-27",
        team_salary_ex_holds_before=140_000_000,
    )
    check("cap room clears an otherwise-failing tiered match", r is not None and r.passed)

    # Same shape, but no room: falls through to tiered matching and fails,
    # exactly as before this fix (this path is untouched by it).
    r2 = T._check_salary_matching(
        "DEN", outgoing=0, incoming=20_000_000,
        team_salary_before=CAP + 1, cap_levels=cap_levels, season="26-27",
        team_salary_ex_holds_before=CAP + 1,
    )
    check("no room falls through to tiered matching and still fails",
          r2 is not None and not r2.passed)


def main():
    test_cap_room_absorbed()
    test_check_salary_matching_uses_cap_room()
    print("\n" + "=" * 40)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

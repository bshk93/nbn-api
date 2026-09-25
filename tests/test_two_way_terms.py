"""§ 2.2's two-way contract terms: $0 salary, 2 years at most, 3 years of
experience or fewer, and at most 2 consecutive years with one team.

Written 2026-09-25. `two_way_slots` had been the only § 2.2 check, and a
passing check returns nothing, so a two-way validated to an empty list — a
5-year two-way paying $3M to a veteran would have gone straight through, and
the rulebook still badged § 2.2 🔒. The `sign_pick` path didn't even run the
slot check.

Experience is reported, never blocked: the draft-year proxy read 10 of the 71
two-ways on the books over 3, for players whose real NBA service was within it.

    venv/bin/python -m tests.test_two_way_terms
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402

FAILS = []
SEASON = "26-27"


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def contract(salaries, holds=None, years_experience=None):
    return tx.ContractIn(type="two-way", salaries=salaries, cap_holds=holds or {},
                         years_experience=years_experience)


CLEAN = {"26-27": "$0", "27-28": "$0", "28-29": "$1"}
CLEAN_HOLDS = {"28-29": "RFA"}


def terms(c, bio=None, team="GSW"):
    return tx._check_two_way_terms(c, "some-player", team, bio or {"draft_year": 2026}, SEASON)


def failed(checks):
    return {r.check: r for r in checks if not r.passed}


def main():
    print("a clean two-way")
    out = terms(contract(CLEAN, CLEAN_HOLDS))
    check("fails nothing", not failed(out))
    check("the trailing $1 RFA hold isn't read as salary or as a third year",
          not failed(out))
    exp = [r for r in out if r.check == "two_way_experience"]
    check("but still reports the experience it read, so the list isn't empty",
          len(exp) == 1 and exp[0].passed and "0 years" in exp[0].message)
    check("a 1-year two-way is fine", not failed(terms(contract({"26-27": "$0"}))))

    print("\nsalary")
    f = failed(terms(contract({"26-27": "$3,000,000", "27-28": "$0"})))
    check("a paid year is refused", "two_way_salary" in f)
    check("...as a blocking error naming the year",
          "two_way_salary" in f and f["two_way_salary"].level == "error"
          and "26-27" in f["two_way_salary"].message)

    print("\nlength")
    f = failed(terms(contract({"26-27": "$0", "27-28": "$0", "28-29": "$0"})))
    check("three years is refused", "two_way_length" in f)
    check("...as a blocking error", "two_way_length" in f and f["two_way_length"].level == "error")
    f = failed(terms(contract({"26-27": "$0", "27-28": "$0", "28-29": "$0", "29-30": "$1"},
                              {"29-30": "RFA"})))
    check("a trailing hold after three years doesn't hide the third", "two_way_length" in f)
    check("...and an over-long deal with no history isn't also called consecutive",
          "two_way_consecutive" not in f)

    print("\nexperience — reported, never blocking")
    out = terms(contract(CLEAN, CLEAN_HOLDS), {"draft_year": 2018, "name": "REATH, DUOP"})
    exp = [r for r in out if r.check == "two_way_experience"][0]
    check("an old draft class is flagged for a human", "Confirm by hand" in exp.message and "8 years" in exp.message)
    check("...without blocking the signing", exp.passed and not failed(out))
    exp = [r for r in terms(contract(CLEAN, CLEAN_HOLDS), {"draft_year": None})
           if r.check == "two_way_experience"][0]
    check("no draft year asks for a manual check", exp.passed and "isn't on file" in exp.message)
    exp = [r for r in terms(contract(CLEAN, CLEAN_HOLDS, years_experience=2), {"draft_year": 2015})
           if r.check == "two_way_experience"][0]
    check("declared experience beats the proxy", "2 years" in exp.message and "declared" in exp.message)

    print("\nconsecutive years with one team")
    one_prior = {"contracts": [{"team": "GSW", "salaries": {"25-26": "$0", "26-27": "$1"},
                                "cap_holds": {"26-27": "RFA"}}], "draft_year": 2025}
    f = failed(terms(contract(CLEAN, CLEAN_HOLDS), one_prior))
    check("one prior year + a new two-year deal is flagged", "two_way_consecutive" in f)
    check("...as a forceable warning", "two_way_consecutive" in f
          and f["two_way_consecutive"].level == "warning")
    check("one prior year + a one-year deal is fine",
          not failed(terms(contract({"26-27": "$0"}), one_prior)))
    check("a two-way with another team doesn't count",
          not failed(terms(contract(CLEAN, CLEAN_HOLDS), one_prior, team="BOS")))
    standard = {"contracts": [{"team": "GSW", "salaries": {"25-26": "$2,000,000"}}]}
    check("a prior standard deal doesn't count",
          not failed(terms(contract(CLEAN, CLEAN_HOLDS), standard)))
    gap = {"contracts": [{"team": "GSW", "salaries": {"24-25": "$0"}}]}
    check("a two-way that ended before a gap year doesn't count",
          not failed(terms(contract(CLEAN, CLEAN_HOLDS), gap)))

    print("\nsign_pick runs the same § 2.2 checks")
    real = (tx._build_team_map, tx._count_two_way_roster)
    tx._build_team_map = lambda: {"pick-guy": "MIL"}
    tx._count_two_way_roster = lambda team, excluding=None: 3
    try:
        ctx = {"bios": {"pick-guy": {"type": "draft-rights", "draft_year": 2026, "draft_round": 2}},
               "cur_season": SEASON, "cap_levels": {}, "team_state": {}}
        d = tx.SignPickDetails(player="pick-guy", contract=contract(
            {"26-27": "$0", "27-28": "$0", "28-29": "$0"}))
        f = failed(tx._validate_sign_pick(d, ctx))
        check("a fourth two-way via a pick signing is refused", "two_way_slots" in f)
        check("a three-year pick two-way is refused", "two_way_length" in f)
    finally:
        tx._build_team_map, tx._count_two_way_roster = real

    print()
    print("=" * 40)
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()

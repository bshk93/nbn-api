"""Regression tests for the two § 3.1 / § 3.12 gates added 2026-08-07:

  _check_signing_eligibility — retired players can't be signed, and neither
  can players already under contract to another team.

  _check_minimum_salary — no contract year may pay below the Minimum Salary
  Scale. Reported live as "$1,000 passes validation", which it did: the only
  minimum-related check was _check_minimum_contract_cap_hit, which fires only
  for 1-year deals declared signing_method="minimum".

Proration is the subtlety. The league does prorate in-season minimum signings
(established practice, NOT in the rulebook — tracked in BACKLOG), so a Year 1
figure below the full-season minimum can be legitimate. That excuse applies to
Year 1 of an in-season signing only; every later year is a full season, so the
floor is hard there. Replaying the 28 real applied signings: 24 pass, 2 warn
(one genuine proration, one inferred-tier), 1 error (a corrupt $5,242 figure
that was later fixed in the bio).

    venv/bin/python -m tests.test_signing_eligibility
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []
SEASON = "26-27"
CAP_LEVELS = {
    "25-26": {"min_salary_scale": {"0": 1272870, "2": 2449421, "10+": 3876529}},
    "26-27": {"min_salary_scale": {"0": 1357763, "2": 2537526, "10+": 3876529}},
}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


class Contract:
    def __init__(self, salaries, ctype="standard", years_experience=None):
        self.salaries = salaries
        self.type = ctype
        self.years_experience = years_experience


def main():
    print("signing eligibility")
    bios = {
        "ret":   {"name": "RETIRED, RICK", "retired": True},
        "free":  {"name": "FREE, FRANK"},
        "owned": {"name": "OWNED, OSCAR"},
        "hold":  {"name": "HOLD, HANK", "cap_holds": {SEASON: "UFA"}},
    }
    orig = T._build_team_map
    T._build_team_map = lambda: {"owned": "OKC", "hold": "BOS"}
    try:
        r = T._check_signing_eligibility("ret", "BOS", SEASON, bios)
        check("a retired player cannot be signed", r is not None and r.level == "error")

        check("an unrostered free agent is signable",
              T._check_signing_eligibility("free", "BOS", SEASON, bios) is None)

        r = T._check_signing_eligibility("owned", "BOS", SEASON, bios)
        check("a player under contract elsewhere cannot be signed",
              r is not None and r.level == "error")
        check("...and the message names the holding team", "OKC" in r.message)

        check("a team may re-sign a player it already rosters",
              T._check_signing_eligibility("owned", "OKC", SEASON, bios) is None)

        # § 3.10: a UFA/RFA hold is a free agent whose rights the team holds,
        # so another team signing them outright is legal.
        check("a UFA cap hold elsewhere does NOT block a signing",
              T._check_signing_eligibility("hold", "LAL", SEASON, bios) is None)
    finally:
        T._build_team_map = orig

    print("\nminimum salary")
    bios2 = {"vet": {"draft_year": 2016}, "unknown": {}}

    def mins(salaries, player="unknown", ctype="standard", date="2026-08-07", exp=None):
        return T._check_minimum_salary(Contract(salaries, ctype, exp), player, bios2,
                                       SEASON, CAP_LEVELS, txn_date=date)

    r = mins({"26-27": "$1,000"})
    check("$1,000 in the offseason is an error", r is not None and not r.passed and r.level == "error")

    # Above the league floor is never an *error*. But for a player whose
    # experience can't be established, a figure this low is minimum territory
    # and no tier could be checked — that combination used to report a clean
    # pass, which is how a 12-year veteran got signed at the rookie minimum for
    # every year of a multi-year deal without a word from the validator.
    r = mins({"26-27": "$1,400,000"})
    check("at/above the league floor is not an error", r is not None and r.level != "error")
    check("...but unknown experience in minimum territory warns",
          r is not None and not r.passed and r.level == "warning"
          and "can't be established" in r.message)

    r = mins({"26-27": "$1,400,000"}, exp=0)
    check("...and declaring experience on the contract clears it",
          r is not None and r.passed)

    # A deal well clear of the scale isn't a minimum contract, so an unknown
    # tier is nothing to warn about — this must stay quiet for ordinary signings.
    r = mins({"26-27": "$25,000,000"})
    check("a non-minimum salary with unknown experience stays quiet",
          r is not None and r.passed)

    # Proration: only Year 1, only in-season.
    r = mins({"26-27": "$39,820"}, date="2026-04-11")
    check("below floor for an IN-SEASON Year 1 warns (may be prorated)",
          r is not None and not r.passed and r.level == "warning")
    r = mins({"26-27": "$39,820"}, date="2026-08-07")
    check("...but the same figure in the OFFSEASON is an error",
          r is not None and not r.passed and r.level == "error")

    r = mins({"26-27": "$3,000,000", "27-28": "$1,000"}, date="2026-04-11")
    check("a later year below the floor errors even for an in-season signing",
          r is not None and not r.passed and r.level == "error")
    check("...because a full contract year is never prorated",
          "never prorated" in r.message)

    # 27-28 has no scale of its own; minimums only rise, so the 26-27 figure
    # is a safe conservative floor for it.
    check("a season with no configured scale falls back to the latest one",
          T._min_salary_floor("27-28", CAP_LEVELS) == 1357763)

    r = mins({"26-27": "$1,400,000"}, player="vet")
    check("above the league floor but below the player's tier warns",
          r is not None and not r.passed and r.level == "warning")

    check("two-way contracts are exempt from the scale",
          mins({"26-27": "$500,000"}, ctype="two-way") is None)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

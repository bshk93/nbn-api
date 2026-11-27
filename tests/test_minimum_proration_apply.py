"""§ 3.12: an in-season minimum signing has Year 1 written at its prorated
figure for the day it goes through (`_prorate_minimum_year1`).

Added after Deandre Ayton's LAC minimum (2026-11-19), offered days earlier at
the full $2,449,421 and declared on the PDC dashboard with the mismatch
confirmed past as a warning. It should have been $2,027,107: 144 of 174 days.

    venv/bin/python -m tests.test_minimum_proration_apply
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _yos  # noqa: E402
_yos.pin()
from routers import transactions as T  # noqa: E402
from routers.txn.models import ContractIn, SignDetails  # noqa: E402

FAILS = []
CAP_LEVELS = {"26-27": {"min_salary_scale": {
    "0": 1357763, "1": 2185116, "2": 2449421, "3": 2537526, "4": 2625627, "5": 2845883,
    "6": 3066143, "7": 3286399, "8": 3506659, "9": 3524115, "10+": 3876529}}}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def sign(salaries, method="minimum", exp=8, ctype="player"):
    return SignDetails(player="p", team="LAC", signing_method=method,
                       contract=ContractIn(type=ctype, salaries=salaries, years_experience=exp))


def main():
    print("minimum proration on signing")
    orig_bounds = T.season_calendar.regular_season_bounds
    # 26-27 as it is on file: October 20 to April 11, 174 days.
    T.season_calendar.regular_season_bounds = (
        lambda s: ("2026-10-20", "2027-04-11") if s == "26-27" else None)
    try:
        bios = {"p": {}}

        d = sign({"26-27": "$2449421"})
        r = T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19")
        check("Ayton's full-minimum offer is prorated to 144 of 174 days",
              r == 2027107 and d.contract.salaries == {"26-27": "$2,027,107"})

        d = sign({"26-27": "$2,044,000"})
        r = T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19")
        check("a figure prorated on the earlier offer date is re-prorated to the signing date",
              r == 2027107)

        d = sign({"26-27": "$2,027,107"})
        check("an already-correct figure is left alone",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19") is None)

        # killerdawg7, 2026-11-27: a multi-year deal pays a full Year 1.
        # Sochan's IND 1+1 (4 YOE, signed 2026-11-26) was cut to $2,067,304.
        d = sign({"26-27": "$2625627", "27-28": "$2988177"}, exp=4)
        d.contract.cap_holds = {"27-28": "TEAM_OPT", "28-29": "UFA"}
        check("a multi-year minimum is not prorated (Sochan's 1+1 keeps $2,625,627)",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-26") is None
              and d.contract.salaries["26-27"] == "$2625627")

        d = sign({"26-27": "$5,000,000"})
        check("a figure above the full minimum isn't a minimum and is left for the checks",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19") is None)

        d = sign({"26-27": "$1,500,000"})
        check("a figure below the prorated floor is left to error",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19") is None)

        d = sign({"26-27": "$2,449,421"}, method="cap_space")
        check("a non-minimum signing method is never touched",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19") is None)

        d = sign({"26-27": "$2,449,421"})
        check("an offseason signing prorates nothing",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-08-19") is None)
        check("...nor does one on opening night",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-10-20") is None)

        d = sign({"27-28": "$2,449,421"})
        check("a deal starting next season prorates nothing",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19") is None)

        d = sign({"26-27": "$2,449,421"}, ctype="two-way")
        check("two-way contracts are exempt",
              T._prorate_minimum_year1(d, bios, CAP_LEVELS, "2026-11-19") is None)
    finally:
        T.season_calendar.regular_season_bounds = orig_bounds

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Regression test: `minimum_salary` must not contradict `minimum_contract_cap_hit`
on a genuine 1-year veteran-minimum deal.

2026-08-13: Nahshon Hyland's 1-yr minimum with GSW (5 years of NBA experience,
26-27) submitted at the correct § 3.12 veteran-minimum hardship figure —
$2,449,421, the 2-year-veteran tier — and got a contradictory verdict:
`minimum_contract_cap_hit` (built on `_one_year_min_cap_hit`) passed it as
exactly right, while `minimum_salary` (built on `_min_salary_for`, which knows
nothing about the hardship cap) warned it was below the player's raw
5-year-tier figure ($2,845,883). Both checks price the same submission and
must not disagree.

Fixed by threading `signing_method` into `_check_minimum_salary`: for a
genuine 1-year `minimum` deal (single salary year), it now checks against
`_one_year_min_cap_hit` instead of the raw tier floor, same as
`_check_minimum_contract_cap_hit` already did.

    venv/bin/python -m tests.test_one_year_min_cap_hit_consistency
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.transactions as tx  # noqa: E402

FAILS = []

app = FastAPI()
app.include_router(tx.router)
client = TestClient(app)

CAP_LEVELS = {
    "26-27": {"cap": 164961000, "min_salary_scale": {
        "0": 1357763, "1": 2185116, "2": 2449421, "3": 2537526, "4": 2625627,
        "5": 2845883, "6": 3066143, "7": 3286399, "8": 3506659, "9": 3524115,
        "10+": 3876529}},
}
SCALE = CAP_LEVELS["26-27"]["min_salary_scale"]

# 5 years of NBA experience, no bio draft_year needed — declared on the contract.
FIVE_YR_VET = {}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    print("the reported bug — a correctly-capped 1-yr veteran minimum")
    contract = tx.ContractIn(
        salaries={"26-27": f"${SCALE['2']:,}"}, cap_holds={"27-28": "UFA"},
        years_experience=5,
    )
    r = tx._check_minimum_salary(contract, "hyland-nahshon", {"hyland-nahshon": FIVE_YR_VET},
                                 "26-27", CAP_LEVELS, signing_method="minimum")
    check("minimum_salary passes the 2-yr-capped figure for a 1-yr minimum",
          r is not None and r.passed)

    cap_hit_r = tx._check_minimum_contract_cap_hit(
        tx.SignDetails(player="hyland-nahshon", team="GSW", contract=contract,
                       signing_method="minimum"),
        {"hyland-nahshon": FIVE_YR_VET}, "26-27", CAP_LEVELS,
    )
    check("...and agrees with minimum_contract_cap_hit",
          cap_hit_r is not None and cap_hit_r.passed)

    print("\nthe raw tier figure still isn't required")
    contract_over = tx.ContractIn(
        salaries={"26-27": f"${SCALE['5']:,}"}, cap_holds={"27-28": "UFA"},
        years_experience=5,
    )
    r_over = tx._check_minimum_salary(contract_over, "hyland-nahshon", {"hyland-nahshon": FIVE_YR_VET},
                                      "26-27", CAP_LEVELS, signing_method="minimum")
    check("paying the full uncapped tier also still passes (it's above the floor)",
          r_over is not None and r_over.passed)

    print("\nbelow the capped floor still errors")
    contract_low = tx.ContractIn(
        salaries={"26-27": f"${SCALE['1']:,}"}, cap_holds={"27-28": "UFA"},
        years_experience=5,
    )
    r_low = tx._check_minimum_salary(contract_low, "hyland-nahshon", {"hyland-nahshon": FIVE_YR_VET},
                                     "26-27", CAP_LEVELS, signing_method="minimum")
    check("below the 2-yr cap still fails minimum_salary",
          r_low is not None and not r_low.passed)

    print("\na multi-year minimum is untouched (no hardship cap applies)")
    contract_multi = tx.ContractIn(
        salaries={"26-27": f"${SCALE['2']:,}", "27-28": f"${SCALE['2']:,}"},
        years_experience=5,
    )
    r_multi = tx._check_minimum_salary(contract_multi, "hyland-nahshon", {"hyland-nahshon": FIVE_YR_VET},
                                       "26-27", CAP_LEVELS, signing_method="minimum")
    check("2-year minimum at tier 2 warns below the real 5-year tier (uncapped, as before)",
          r_multi is not None and not r_multi.passed)

    print("\ncallers that don't pass signing_method keep the old (uncapped) behaviour")
    r_default = tx._check_minimum_salary(contract, "hyland-nahshon", {"hyland-nahshon": FIVE_YR_VET},
                                         "26-27", CAP_LEVELS)
    check("no signing_method -> still checked against the raw tier, and warns",
          r_default is not None and not r_default.passed)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

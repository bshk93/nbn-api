"""`_validate_convert_twoway` never checked the § 3.12 minimum salary at all.

`_validate_sign`, `_validate_offer_sheet` and `_validate_extension` all run
`_check_minimum_salary` on the contract they're scoring; the new standard
contract a two-way conversion produces never got the same floor check —
confirmed by grepping every call site of `_check_minimum_salary` before this
was added. Added 2026-09-20 alongside `POST /api/self/convert_twoway`, which
needs a real minimum-salary check to exist before it can lean on it for its
own like-for-like § 2.2 gate (see that endpoint's docstring for why a floor
check alone — "not below the minimum" — still isn't "at the minimum").

    venv/bin/python -m tests.test_convert_twoway_minimum
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402
from routers.transactions import ContractIn, ConvertTwoWayDetails  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


SEASON = "26-27"
CAP_LEVELS = {SEASON: {
    "cap": 164961000, "apron1": 209015000, "apron2": 221686000,
    "hard_cap": 230000000, "min_salary_scale": {"0": 1357763, "10+": 3000000},
}}


def validate(salary, draft_year=2024, roster_count=14):
    tx._build_team_map = lambda: {"p": "PHX"}
    tx._count_standard_roster = lambda team: roster_count
    bios = {"p": {"name": "TEST, PLAYER", "type": "two-way", "salaries": {},
                 "draft_year": draft_year}}
    ctx = {"bios": bios, "cur_season": SEASON, "cap_levels": CAP_LEVELS,
           "team_state": {}, "txn_date": "2026-08-08", "trade_exceptions": {}}
    contract = ContractIn(type="player", salaries={SEASON: salary})
    details = ConvertTwoWayDetails(player="p", contract=contract, signing_method="minimum")
    return tx._validate_convert_twoway(details, ctx)


def find(checks, name):
    return next((c for c in checks if c.check == name), None)


print("_validate_convert_twoway now runs the § 3.12 floor check")

r = validate("$1,357,763", draft_year=2024)  # rookie-tier player at the rookie floor
check("a rookie priced at the rookie-tier minimum passes", find(r, "minimum_salary").passed)

r = validate("$500,000", draft_year=2024)
check("below the league floor is an error",
      find(r, "minimum_salary").level == "error" and not find(r, "minimum_salary").passed)

r = validate("$1,357,763", draft_year=2010)  # a real 10+-year veteran
check("a veteran priced at the rookie floor, below their own tier, warns",
      not find(r, "minimum_salary").passed and find(r, "minimum_salary").level == "warning")

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

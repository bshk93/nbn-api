"""§ 3.12: declared years of experience vs the draft-year count.

`_check_declared_experience` warns when a contract declares fewer years than
the player's draft year gives. Fewer years prices a minimum on a cheaper tier
(Blake Hinson's ORL minimum, 2026-10-08, declared 0 against a 2024 draft class).

    venv/bin/python -m tests.test_declared_experience
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routers.transactions as t  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


BIOS = {"hinson-blake": {"name": "HINSON, BLAKE", "draft_year": 2024},
        "no-draft": {"name": "DRAFT, NO"}}


def contract(exp, salaries=None, holds=None):
    return t.ContractIn(salaries=salaries or {"26-27": "$1357763", "27-28": "$2294372"},
                        years_experience=exp, cap_holds=holds or {})


r = t._check_declared_experience(contract(0), "hinson-blake", BIOS)
check("0 against a 2024 class in 26-27 warns", r and not r.passed and r.level == "warning", r)
check("the warning names the expected 2", r and "gives 2" in r.message, r and r.message)

r = t._check_declared_experience(contract(2), "hinson-blake", BIOS)
check("2 matches and passes", r and r.passed, r)

r = t._check_declared_experience(contract(3), "hinson-blake", BIOS)
check("more than the draft year gives passes", r and r.passed, r)

r = t._check_declared_experience(contract(None), "hinson-blake", BIOS)
check("nothing declared: no check", r is None, r)

r = t._check_declared_experience(contract(0), "no-draft", BIOS)
check("no draft year: no check", r is None, r)

r = t._check_declared_experience(
    contract(1, {"26-27": "$0", "27-28": "$1"}, {"27-28": "RFA"}), "hinson-blake", BIOS)
check("judged at the first real season, not a trailing hold", r and not r.passed and "in 26-27" in r.message, r)

check("mapped to a rulebook section", "declared_experience" in __import__("rulebook_coverage").CHECK_SECTIONS)

# ── The bio's own figure (`nba_experience`) and the order `_years_exp` reads in ──
YAB = {"name": "YABUSELE, GUERSCHON", "draft_year": 2016,
       "nba_experience": {"years": 4, "season": "26-27"}}
LAST = {"name": "LAST, ONE", "draft_year": 2016,
        "contracts": [{"years_experience": 1, "salaries": {"25-26": "$1"}, "cap_holds": {}}]}

check("draft year alone: season minus draft year",
      t._years_exp_basis({"draft_year": 2016}, "26-27") == (10, "2016 draft year"))
check("bio figure beats the draft year", t._years_exp_basis(YAB, "26-27") == (4, "set on his bio"))
check("bio figure climbs a season", t._years_exp(YAB, "28-29") == 6)
check("bio figure counts back too, never below 0",
      t._years_exp(YAB, "24-25") == 2 and t._years_exp(YAB, "18-19") == 0)
check("a contract's stated figure beats the bio",
      t._years_exp_basis(YAB, "26-27", contract(1)) == (1, "stated on the contract"))
check("last contract's figure beats the draft year",
      t._years_exp_basis(LAST, "26-27") == (2, "stated on his last contract"))
check("bio figure beats the last contract's",
      t._years_exp({**LAST, "nba_experience": {"years": 5, "season": "26-27"}}, "26-27") == 5)
check("nothing on file: None", t._years_exp_basis({}, "26-27") == (None, None))
check("bio figure alone, no draft year",
      t._years_exp({"nba_experience": {"years": 7, "season": "25-26"}}, "26-27") == 8)

BIOS2 = {"yab": YAB}
r = t._check_declared_experience(contract(4), "yab", BIOS2)
check("matching the bio figure passes, despite the 2016 draft year", r and r.passed, r)
r = t._check_declared_experience(contract(3), "yab", BIOS2)
check("below the bio figure warns and names the bio",
      r and not r.passed and "set on his bio gives 4" in r.message, r and r.message)

# § 3.1: the bio figure decides RFA eligibility, which the draft year can get wrong
check("QO experience reads the bio", t._qo_years_exp(
    {"draft_year": 2018, "nba_experience": {"years": 2, "season": "26-27"}}, "27-28") == 3)

# § 3.11: max salary follows the bio figure, not the draft year
CL = {"26-27": {"cap": 100_000_000}}
check("max salary tier from the bio (4 years -> 25%)", t._max_salary(YAB, "26-27", CL) == 25_000_000)
check("max salary tier from the draft year (10 years -> 35%)",
      t._max_salary({"draft_year": 2016}, "26-27", CL) == 35_000_000)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

"""§ 3.12: declared years of experience vs years of service on file.

`_check_declared_experience` warns when a contract declares fewer years than
the player's years of service (`years_of_service`) give. Fewer years prices a
minimum on a cheaper tier.

    venv/bin/python -m tests.test_declared_experience
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routers.transactions as t  # noqa: E402
import routers.years_of_service as yos  # noqa: E402

# Two NBN seasons on a roster before 26-27.
yos.service_seasons = lambda slug: {"24-25", "25-26"} if slug == "hinson-blake" else set()

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
check("0 against 2 seasons of service warns", r and not r.passed and r.level == "warning", r)
check("the warning names the expected 2", r and "give 2" in r.message, r and r.message)

r = t._check_declared_experience(contract(2), "hinson-blake", BIOS)
check("2 matches and passes", r and r.passed, r)

r = t._check_declared_experience(contract(3), "hinson-blake", BIOS)
check("more than the service count passes", r and r.passed, r)

r = t._check_declared_experience(contract(None), "hinson-blake", BIOS)
check("nothing declared: no check", r is None, r)

r = t._check_declared_experience(contract(0), "no-draft", BIOS)
check("no draft year: no check", r is None, r)

r = t._check_declared_experience(
    contract(1, {"26-27": "$0", "27-28": "$1"}, {"27-28": "RFA"}), "hinson-blake", BIOS)
check("judged at the first real season, not a trailing hold", r and not r.passed and "in 26-27" in r.message, r)

check("mapped to a rulebook section", "declared_experience" in __import__("rulebook_coverage").CHECK_SECTIONS)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

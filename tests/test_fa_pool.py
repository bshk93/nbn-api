"""Regression tests for the server-side FA pool derivation —
routers.free_agency._fa_pool (PDC free-agency spec § 7.1, Phase 1).

Pins behavioural parity with the page-JS rule it replaces
(free-agency/index.html): one FA class per player (earliest actionable
cap_holds year with a matching salaries entry), renounced/unsigned bucketing,
and the § 3.9 QO amount formula for RFAs.

    venv/bin/python -m tests.test_fa_pool
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.free_agency import _fa_pool  # noqa: E402

FAILS = []

CAP_LEVELS = {
    "26-27": {"cap": 164961000, "min_salary_scale": {"0": 1357763, "1": 2200000, "2": 2500000}},
}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    print("Basic UFA/RFA/option bucketing")
    bios = {
        "ufa-guy": {
            "type": "player", "draft_round": 1,
            "cap_holds": {"26-27": "UFA"}, "salaries": {"26-27": "$18,000,000"},
        },
        "rfa-2nd-round": {
            "type": "player", "draft_round": 2, "draft_year": 2024,
            "cap_holds": {"26-27": "RFA"}, "salaries": {"26-27": "$2,000,000"},
        },
        "rfa-1st-round": {
            "type": "player", "draft_round": 1, "draft_year": 2023,
            "cap_holds": {"26-27": "RFA"}, "salaries": {"26-27": "$6,000,000"},
        },
        "team-opt-guy": {
            "type": "player",
            "cap_holds": {"26-27": "TEAM_OPT"}, "salaries": {"26-27": "$4,000,000"},
        },
        "non-gtd-then-ufa": {
            # NON_GTD year should be skipped in favor of the next actionable year.
            "type": "player",
            "cap_holds": {"26-27": "NON_GTD", "27-28": "UFA"},
            "salaries": {"26-27": "$1,000,000", "27-28": "$3,000,000"},
        },
        "no-salary-for-hold": {
            # Hold exists but no matching salaries entry -> not actionable, no fallback -> excluded.
            "type": "player", "cap_holds": {"26-27": "UFA"}, "salaries": {},
        },
        "renounced-guy": {
            "type": "", "cap_holds": {}, "salaries": {"25-26": "$8,000,000"},
        },
        "unsigned-guy": {
            "type": "", "cap_holds": {}, "salaries": {},
        },
        "still-rostered-no-hold": {
            # On a roster, no cap hold at all -> not a free agent, excluded entirely.
            "type": "player", "cap_holds": {}, "salaries": {"26-27": "$5,000,000"},
        },
        "retired-guy": {
            "type": "", "cap_holds": {}, "salaries": {}, "retired": True,
        },
    }
    team_map = {"still-rostered-no-hold": "PHX"}

    pool = _fa_pool(bios, team_map, "26-27", CAP_LEVELS)

    check("ufa-guy: class_year 26-27, hold_type UFA, not rfa",
          pool["ufa-guy"]["class_year"] == "26-27"
          and pool["ufa-guy"]["hold_type"] == "UFA"
          and pool["ufa-guy"]["rfa"] is False
          and pool["ufa-guy"]["qo_amount"] is None)

    check("rfa-2nd-round: rfa true, qo = greater of min scale / 125% prior",
          pool["rfa-2nd-round"]["rfa"] is True
          and pool["rfa-2nd-round"]["qo_amount"] == 2500000)  # 125% of 2,000,000

    check("rfa-1st-round: rfa true but qo_amount None (rookie scale unsourced)",
          pool["rfa-1st-round"]["rfa"] is True
          and pool["rfa-1st-round"]["qo_amount"] is None)

    check("team-opt-guy: hold_type TEAM_OPT, not rfa",
          pool["team-opt-guy"]["hold_type"] == "TEAM_OPT"
          and pool["team-opt-guy"]["rfa"] is False)

    check("non-gtd-then-ufa: skips NON_GTD year, lands on 27-28 UFA",
          pool["non-gtd-then-ufa"]["class_year"] == "27-28"
          and pool["non-gtd-then-ufa"]["hold_type"] == "UFA"
          and pool["non-gtd-then-ufa"]["prior_salary"] == 3000000)

    check("no-salary-for-hold: excluded (hold with no priced salary, not renounced/unsigned)",
          "no-salary-for-hold" not in pool)

    check("renounced-guy: bucketed RENOUNCED with latest known salary",
          pool["renounced-guy"]["hold_type"] == "RENOUNCED"
          and pool["renounced-guy"]["prior_salary"] == 8000000
          and pool["renounced-guy"]["rfa"] is False)

    check("unsigned-guy: bucketed UNSIGNED with zero prior salary",
          pool["unsigned-guy"]["hold_type"] == "UNSIGNED"
          and pool["unsigned-guy"]["prior_salary"] == 0)

    check("still-rostered-no-hold: excluded (on a roster, no actionable hold)",
          "still-rostered-no-hold" not in pool)

    check("retired-guy: excluded",
          "retired-guy" not in pool)

    check("renounced/unsigned bucket into the earliest real FA class year seen",
          pool["renounced-guy"]["class_year"] == "26-27"
          and pool["unsigned-guy"]["class_year"] == "26-27")

    print("\nFallback bucket year when no actionable holds exist at all")
    only_renounced = {
        "solo-renounced": {"type": "", "cap_holds": {}, "salaries": {"25-26": "$1,000,000"}},
    }
    pool2 = _fa_pool(only_renounced, {}, "26-27", CAP_LEVELS)
    check("falls back to the passed-in season", pool2["solo-renounced"]["class_year"] == "26-27")

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILURES: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

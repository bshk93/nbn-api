"""§ 7.1 second-round minimum-scale contracts must not be scored by the § 3.9
raise ladder — and must be scored against the *flat* tier the rulebook
actually prescribes, not an escalating one.

Written 2026-08-14. `_validate_sign_pick` fell back to `_check_contract_raises`
for any pick with no rookie-scale.json entry (i.e. every second-rounder — that
file only carries the 30 first-round rows). The ladder's own minimum-scale
exemption (`_at_minimum`) leans on `_minimum_year_ceiling`, which — with no
`years_experience` declared on the contract, the normal case for a fresh pick
signing — falls back to a real-elapsed-years-since-draft proxy that starts at
**0** years of experience in the draft season. § 7.1 explicitly prices a
second-round Year 1 at the **1**-year-experience tier, one tier above what
that proxy assumes, so a contract priced exactly to the rulebook still read as
"not at minimum" and the ladder rejected the Year 1 -> Year 2 step outright.

Fixed by giving second-round picks the same treatment first-round picks
already have (`_check_rookie_scale_terms`): an exact-match check against a
fixed tier sequence, `_check_second_round_scale_terms`, run before the ladder
ever sees the contract.

Second bug, same day: the fix's first cut counted every season in
`contract.salaries` as a contract year, including a trailing § 3.10 RFA hold
season the office had priced with a real dollar figure (Otega Oweh's actual
submission). That inflated the year count to 4, which picked the *4-year*
tier table instead of the 3-year one. Fixed by excluding any season tagged
UFA/RFA in `cap_holds` before counting years — the same `isFaHold` convention
`contract.js` already applies everywhere else ("a trailing UFA/RFA line is the
hold the deal rolls into, not a contract year").

Third bug, same day: the tier sequence itself was wrong. The first cut read
"Year 1: 1 year of experience / Year 2: 2nd-year rookie minimum / Year 3:
3rd-year rookie minimum" as *escalating* tiers 1, 2, 3. It's flat at tier 1 —
"Nth-year rookie minimum" names which year of the deal it is, not a bumped
experience tier, mirroring how a general § 3.12 minimum contract already
works (a declared experience figure is fixed for the life of the deal; only
that season's own scale value moves the dollar amount). Confirmed against
Otega Oweh's real submission (pick 45, 2026): all three years priced at flat
tier 1 — $2,185,116 / $2,571,895 / $2,791,275 for 26-27/27-28/28-29 — which
only reads as a raise because the season's own scale is growing, not because
the tier climbed.

    venv/bin/python -m tests.test_second_round_scale
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402
from routers.transactions import ContractIn  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


CAP_LEVELS = json.loads(Path("/var/lib/nothing-but-stats/cap-levels.json").read_text())
BIO = {"draft_year": 2026, "draft_round": 2, "draft_pick": 45}


def main():
    print("3-year deal, flat tier 1 (Otega Oweh's real submission)")
    correct3 = ContractIn(
        type="player",
        salaries={"26-27": "$2,185,116", "27-28": "$2,571,895", "28-29": "$2,791,275"},
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT"},
    )
    r = tx._check_second_round_scale_terms(correct3, BIO, CAP_LEVELS)
    check("scored by the exact-match check, not None", r is not None)
    check("passes", r is not None and r.passed)
    scale = CAP_LEVELS["26-27"]["min_salary_scale"], CAP_LEVELS["27-28"]["min_salary_scale"], \
        CAP_LEVELS["28-29"]["min_salary_scale"]
    check("all three years are literally tier \"1\" of their own season's scale",
          correct3.salaries["26-27"] == f"${scale[0]['1']:,}"
          and correct3.salaries["27-28"] == f"${scale[1]['1']:,}"
          and correct3.salaries["28-29"] == f"${scale[2]['1']:,}")
    # For reference: the raw ladder still rejects this contract on its own
    # (the tier-1 figure grows season over season by more than 5%) —
    # `_validate_sign_pick` never lets it reach that check for a recognized
    # 3-/4-year shape, since `_check_second_round_scale_terms` runs first.
    ladder = tx._check_contract_raises(correct3, bird_pct=False, cur_season="26-27",
                                        bio=BIO, cap_levels=CAP_LEVELS)
    check("ladder alone would still reject this (confirms the fix routes around it, "
          "not that the ladder itself changed)", ladder is not None and not ladder.passed)

    print("\na genuinely mispriced Year 2 (escalated to tier 2 instead of staying flat)")
    mispriced = ContractIn(
        type="player",
        salaries={"26-27": "$2,185,116", "27-28": "$2,664,401", "28-29": "$2,791,275"},
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT"},
    )
    r2 = tx._check_second_round_scale_terms(mispriced, BIO, CAP_LEVELS)
    check("flagged", r2 is not None and not r2.passed)
    check("names the correct (flat tier-1) Year 2 figure",
          r2 is not None and "$2,571,895" in r2.message)

    print("\nsame 3-year deal PLUS the trailing § 3.10 RFA hold (a live 4th "
          "salary entry for the hold season, tagged RFA)")
    with_trailing_hold = ContractIn(
        type="player",
        salaries={**correct3.salaries, "29-30": "$5,303,423"},  # auto-priced hold; not scored here
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT", "29-30": "RFA"},
    )
    r3 = tx._check_second_round_scale_terms(with_trailing_hold, BIO, CAP_LEVELS)
    check("still recognized as the 3-year structure, not misread as a 4-year deal",
          r3 is not None and "3-year" in r3.message)
    check("passes — the RFA season isn't held to the salary-year table",
          r3 is not None and r3.passed)

    print("\n4-year deal — Year 1 -> Year 2 drops a tier (tier 2 -> tier 1), then holds flat")
    scale4 = tx._second_round_scale_contract(
        ContractIn(type="player", salaries={s: "$0" for s in ("26-27", "27-28", "28-29", "29-30")}),
        CAP_LEVELS,
    )
    check("Year 1 priced off the tier-2 figure",
          scale4["salaries"]["26-27"] == f"${CAP_LEVELS['26-27']['min_salary_scale']['2']:,}")
    check("Year 2 priced off the (lower-tier) tier-1 figure, per § 7.1's own text",
          scale4["salaries"]["27-28"] == f"${CAP_LEVELS['27-28']['min_salary_scale']['1']:,}")
    check("Years 3 and 4 hold flat at tier 1, not escalating further",
          scale4["salaries"]["28-29"] == f"${CAP_LEVELS['28-29']['min_salary_scale']['1']:,}"
          and scale4["salaries"]["29-30"] == f"${CAP_LEVELS['29-30']['min_salary_scale']['1']:,}")
    correct4 = ContractIn(type="player", salaries=scale4["salaries"], cap_holds=scale4["cap_holds"])
    r4 = tx._check_second_round_scale_terms(correct4, BIO, CAP_LEVELS)
    check("4-year deal passes the exact-match check", r4 is not None and r4.passed)

    print("\ntwo-way and off-scale shapes fall through untouched")
    two_way = ContractIn(type="two-way", salaries={"26-27": "$0"})
    check("_second_round_scale_contract returns None for a 1-year shape",
          tx._second_round_scale_contract(two_way, CAP_LEVELS) is None)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

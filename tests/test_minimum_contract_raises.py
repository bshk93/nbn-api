"""Regression tests for the § 3.12 minimum-scale exemption inside
routers.transactions._check_contract_raises.

Written 2026-08-10 after a team's multi-year minimum offer on /free-agency was
rejected as illegal. § 3.13 caps a year-over-year change at 5% of Year 1 (8%
with Full Bird), but § 3.12 says a multi-year minimum deal pays "the scale for
the player's years of experience that season" — and the scale's own steps at
the bottom are far bigger than 5%: the 26-27 rookie minimum ($1,357,763) rising
to the 1-year tier ($2,185,116) is a 61% raise. The ladder was being applied to
it, so the contract the rulebook prescribes couldn't be submitted.

The exemption must stay narrow: it applies only when *both* ends of the step
are at that season's minimum for that player (plus 5% of rounding grace), so an
ordinary contract with a big jump is still an error, and so is a deal that
starts at the minimum and then leaps above the scale.

    venv/bin/python -m tests.test_minimum_contract_raises
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.transactions import (  # noqa: E402
    ContractIn, _check_contract_raises, _min_salary_floor,
    _min_scale_for_season, _minimum_year_ceiling,
)

FAILS = []

# Real 25-26 and 26-27 minimum salary scales from cap-levels.json. 27-28 is
# deliberately absent — the office sets each season's figures when it gets
# there, so a deal signed today routinely runs past the last configured year.
CAP_LEVELS = {
    "25-26": {"cap": 154647000, "min_salary_scale": {
        "0": 1272870, "1": 2048494, "2": 2296274, "3": 2378870, "4": 2461463,
        "5": 2667947, "6": 2874436, "7": 3080921, "8": 3287409, "9": 3303774,
        "10+": 3634153}},
    "26-27": {"cap": 164961000, "min_salary_scale": {
        "0": 1357763, "1": 2185116, "2": 2449421, "3": 2537526, "4": 2625627,
        "5": 2845883, "6": 3066143, "7": 3286399, "8": 3506659, "9": 3524115,
        "10+": 3876529}},
}
SEASON = "26-27"
SCALE = CAP_LEVELS[SEASON]["min_salary_scale"]

ROOKIE = {"draft_year": 2026}   # 0 years of experience in 26-27
VET = {"draft_year": 2014}      # 10+ years
UNDRAFTED = {}                  # no experience proxy at all


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def run(salaries, bio=ROOKIE, bird=False, cap_levels=CAP_LEVELS, holds=None):
    """The CheckResult, or None when no raise check applies at all."""
    return _check_contract_raises(
        ContractIn(salaries={k: f"${v:,}" for k, v in salaries.items()},
                   cap_holds=holds or {}),
        bird_pct=bird, cur_season=SEASON, bio=bio, cap_levels=cap_levels,
    )


def main():
    print("the reported bug — a multi-year minimum on the real scale")
    r = run({"26-27": SCALE["0"], "27-28": SCALE["1"]}, holds={"28-29": "UFA"})
    check("2-year rookie-scale minimum is legal", r is not None and r.passed)
    check("...and says why the ladder didn't apply",
          r is not None and "3.12" in r.message)
    r = run({"26-27": SCALE["0"], "27-28": SCALE["1"], "28-29": SCALE["2"]})
    check("3-year minimum climbing two tiers is legal", r is not None and r.passed)
    # Every year identical is inside 5% on its own — the exemption is not
    # needed and no check should be emitted at all.
    check("a flat minimum needs no exemption",
          run({"26-27": SCALE["0"], "27-28": SCALE["0"]}) is None)

    print("\nthe exemption stays narrow")
    r = run({"26-27": 10_000_000, "27-28": 12_000_000}, bio=VET)
    check("an ordinary 20% raise is still an error", r is not None and not r.passed)
    r = run({"26-27": SCALE["0"], "27-28": 5_000_000})
    check("minimum Year 1 then a leap above the scale is still an error",
          r is not None and not r.passed)
    check("...and the message is the raise ladder, not the exemption",
          r is not None and "Raise/decrease limit violated" in r.message)
    # 10+ tier down to the rookie tier: a fall, not a raise. § 3.9 caps the
    # decrease the same way, and both ends are minimum figures.
    r = run({"26-27": SCALE["10+"], "27-28": SCALE["0"]}, bio=VET)
    check("a minimum-scale *decrease* is exempt too", r is not None and r.passed)

    print("\nrounded entry still reads as a minimum (5% grace)")
    r = run({"26-27": 1_400_000, "27-28": 2_200_000})
    check("hand-rounded rookie-scale figures are exempt", r is not None and r.passed)
    r = run({"26-27": 1_600_000, "27-28": 2_600_000})
    check("...but 18% over the scale is not a minimum deal",
          r is not None and not r.passed)

    print("\nthe ceiling is the player's own tier")
    check("rookie's 26-27 ceiling is the 0-year tier",
          _minimum_year_ceiling(ROOKIE, "26-27", CAP_LEVELS) == SCALE["0"])
    check("a 10+ vet's ceiling is the top row",
          _minimum_year_ceiling(VET, "26-27", CAP_LEVELS) == SCALE["10+"])
    # Nothing above the veteran minimum is a minimum contract for anyone, so an
    # unknown experience tier falls back to the top row rather than giving up.
    check("undrafted (no draft_year) falls back to the top row",
          _minimum_year_ceiling(UNDRAFTED, "26-27", CAP_LEVELS) == SCALE["10+"])
    # A rookie paid the *vet* minimum is above his own tier — over-scale, so the
    # ladder still binds. (§ 3.12's own below-tier warning is the other side.)
    r = run({"26-27": SCALE["0"], "27-28": SCALE["10+"]})
    check("a rookie jumping to the vet minimum is not exempt",
          r is not None and not r.passed)

    print("\nseasons with no scale configured yet")
    check("27-28 falls back to the 26-27 scale",
          _min_scale_for_season("27-28", CAP_LEVELS) is CAP_LEVELS["26-27"]["min_salary_scale"])
    check("...and 25-26 still reads its own",
          _min_scale_for_season("25-26", CAP_LEVELS) is CAP_LEVELS["25-26"]["min_salary_scale"])
    check("_min_salary_floor still returns the 0-year tier",
          _min_salary_floor("27-28", CAP_LEVELS) == SCALE["0"])
    check("no scale anywhere means 'can't tell', not 'not a minimum'",
          _minimum_year_ceiling(ROOKIE, "26-27", {}) is None)
    r = run({"26-27": SCALE["0"], "27-28": SCALE["1"]}, cap_levels={})
    check("...and with no scale the ladder applies as before",
          r is not None and not r.passed)

    print("\nexperience declared on the contract (§ 3.12)")
    # The bio proxy is unavailable for most veterans — draft_year is the NBN
    # draft and is null for everyone who predates it, and permanently null for
    # UDFAs. The contract states its own figure instead, anchored to its first
    # salary year so it stays correct read from any later league year.
    def ceil(exp, yr, first="26-27", n=3):
        seasons = ["26-27", "27-28", "28-29", "29-30"][:n]
        if first != "26-27":
            seasons = ["27-28", "28-29", "29-30"][:n]
        c = ContractIn(salaries={s: "$1" for s in seasons}, years_experience=exp)
        return _minimum_year_ceiling(UNDRAFTED, yr, CAP_LEVELS, c)

    check("declared experience beats an absent draft_year",
          ceil(4, "26-27") == SCALE["4"])
    check("...and the tier steps one row per contract year",
          ceil(4, "27-28") == SCALE["5"] and ceil(4, "28-29") == SCALE["6"])
    check("...anchored to the contract's own first year, not the league year",
          ceil(4, "28-29", first="27-28") == SCALE["5"])
    check("a 10+ veteran stays on the top row as the deal runs",
          ceil(12, "26-27") == SCALE["10+"] and ceil(12, "28-29") == SCALE["10+"])
    check("declaring nothing still falls back to the draft_year proxy",
          _minimum_year_ceiling(VET, "26-27", CAP_LEVELS,
                                ContractIn(salaries={"26-27": "$1"})) == SCALE["10+"])

    # The whole point: a multi-year minimum priced off the scale must survive
    # the § 3.13 ladder for a player the bio can say nothing about.
    r = _check_contract_raises(
        ContractIn(salaries={"26-27": f"${SCALE['4']:,}", "27-28": f"${SCALE['5']:,}",
                             "28-29": f"${SCALE['6']:,}"}, years_experience=4),
        bird_pct=False, cur_season=SEASON, bio=UNDRAFTED, cap_levels=CAP_LEVELS,
    )
    check("a climbing minimum for an undrafted vet clears the ladder",
          r is not None and r.passed)
    # ...and it must not become a loophole: a real raise off a minimum base is
    # still a raise, declared experience or not.
    r = _check_contract_raises(
        ContractIn(salaries={"26-27": f"${SCALE['4']:,}", "27-28": "$8,000,000"},
                   years_experience=4),
        bird_pct=False, cur_season=SEASON, bio=UNDRAFTED, cap_levels=CAP_LEVELS,
    )
    check("...but a jump clear of the scale is still an error",
          r is not None and not r.passed)

    print("\ncallers that pass no bio/cap_levels keep the old behaviour")
    r = _check_contract_raises(
        ContractIn(salaries={"26-27": "$1,357,763", "27-28": "$2,185,116"}),
        bird_pct=False, cur_season=SEASON,
    )
    check("unexempted call still flags the scale step", r is not None and not r.passed)

    print("\nunrelated rules still hold")
    check("a two-way contract is exempt from the ladder entirely",
          _check_contract_raises(
              ContractIn(type="two-way", salaries={"26-27": "$0", "27-28": "$0"}),
              bird_pct=False, cur_season=SEASON, bio=ROOKIE, cap_levels=CAP_LEVELS) is None)
    r = run({"26-27": 30_000_000, "27-28": 32_400_000}, bio=VET, bird=True)
    check("Full Bird still gets its 8% ladder", r is None)
    r = run({"26-27": 30_000_000, "27-28": 32_400_000}, bio=VET, bird=False)
    check("...and 5% without it", r is not None and not r.passed)
    # A trailing UFA/RFA line is the hold the deal rolls into, not a contract
    # year — it must not be measured as a raise off the last real year.
    check("a trailing UFA hold is not a raise step",
          run({"26-27": 30_000_000, "27-28": 71_000_000}, bio=VET,
              holds={"27-28": "UFA"}) is None)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

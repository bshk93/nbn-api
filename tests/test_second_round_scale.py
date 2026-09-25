"""§ 7.1 second-round minimum-scale contracts must not be scored by the § 3.9
raise ladder — and must be scored against a tier that starts where § 7.1 says
and climbs one row per contract year.

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
tier 1 for 26-27/27-28/28-29, which only reads as a raise because the season's
own scale is growing, not because the tier climbed.

Fourth bug, found 2026-09-19 — in this file, not in the code. The three
figures used to be written out here as literals. On **2026-09-10** the minimum
salary scale for 27-28 onward was corrected: every tier from that season on was
shifted one index, the old table having carried no real 0-years row at all (its
"0" was the 1-year figure, ~$2.29M where the rookie minimum is ~$1.43M). 25-26
and 26-27 were always right. The literals then described a table that no longer
existed, and this file failed for nine days while the code under it was
correct. Everything is derived from `cap-levels.json` now — see `tier()`.

Fifth, 2026-09-24: the third fix above was the wrong way round, and the fourth
explains why. Measured on the pre-2026-09-10 table, whose index ran one tier
high, Oweh's deal *looked* flat at tier 1. On the corrected table the same
three figures are tiers 1, 2 and 3: the deal climbs. So does the league
office's own published table (the "2026 Rookie Contracts" tab of the league
sheet: 2+1 = $2,185,116 / $2,571,895 / $2,791,275, 3+1 = $2,449,421 /
$2,664,401 / $2,888,193 / $3,272,766), and the commissioner's rulings in
#fa-news ("3+1 ... starts at the 2yr vet min", "2+1 ... starts at the 1yr vet
min"). The 3-year deal is tiers 1-2-3; the 4-year is 2-3-4-5.

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


def tier(season: str, t: str) -> str:
    """That season's own scale value at experience tier `t`, formatted the way
    a contract carries it.

    Derived, never hardcoded. This test originally pinned Otega Oweh's three
    figures literally, and on 2026-09-10 the minimum salary scale for 27-28
    onward was corrected — every tier from 27-28 on was shifted one index, the
    old table having had no real 0-years row at all (its "0" was the 1-year
    figure). The literals then described a table that no longer existed and the
    test failed for nine days while the code was right. A figure that comes out
    of `cap-levels.json` belongs in an assertion only by reference."""
    return f"${CAP_LEVELS[season]['min_salary_scale'][t]:,}"


def main():
    print("3-year deal, tiers 1-2-3 (Otega Oweh's real submission)")
    correct3 = ContractIn(
        type="player",
        salaries={"26-27": tier("26-27", "1"), "27-28": tier("27-28", "2"),
                  "28-29": tier("28-29", "3")},
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT"},
    )
    r = tx._check_second_round_scale_terms(correct3, BIO, CAP_LEVELS)
    check("scored by the exact-match check, not None", r is not None)
    check("passes", r is not None and r.passed)
    check("...which is the league sheet's 2+1 row (and Oweh's deal), to rounding",
          all(abs(int(correct3.salaries[s].strip("$").replace(",", "")) - want) <= 5
              for s, want in (("26-27", 2_185_116), ("27-28", 2_571_895),
                              ("28-29", 2_791_275))))
    # The point of the fix is the *routing*: `_validate_sign_pick` consults
    # `_check_second_round_scale_terms` first and only falls back to the § 3.9
    # ladder when the shape isn't a recognized 3-/4-year one, so a contract
    # priced exactly to § 7.1 is never judged by the ladder.
    #
    # That used to be shown by pointing the ladder at the real contract and
    # watching it reject — the old (pre-2026-09-10) scale grew tier 1 by 11.9%
    # from 26-27 to 27-28, well past the ladder's 5%. The corrected scale grows
    # it by exactly 5.0%, so the ladder now accepts the real contract and that
    # demonstration proves nothing. Worse, it proved nothing *quietly*: it kept
    # passing on data that had changed underneath it.
    #
    # Shown against a synthetic scale instead, so it tests the routing and not
    # whichever growth rate the committee last entered.
    steep = {s: {"min_salary_scale": {"0": 1_000_000, "1": 2_000_000, "2": 2_200_000,
                                      "3": 2_400_000}}
             for s in ("26-27", "27-28", "28-29")}
    steep["27-28"]["min_salary_scale"]["2"] = 3_000_000   # +50% on Year 1
    steep["28-29"]["min_salary_scale"]["3"] = 4_500_000   # +50% again
    steep3 = ContractIn(
        type="player",
        salaries={"26-27": "$2,000,000", "27-28": "$3,000,000", "28-29": "$4,500,000"},
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT"},
    )
    ladder = tx._check_contract_raises(steep3, bird_pct=False, cur_season="26-27",
                                        bio=BIO, cap_levels=steep)
    check("a scale that outruns § 3.9 is rejected by the ladder alone",
          ladder is not None and not ladder.passed)
    routed = tx._check_second_round_scale_terms(steep3, BIO, steep)
    check("but passes § 7.1's exact-match check, which is what runs first",
          routed is not None and routed.passed)

    print("\na genuinely mispriced Year 2 (left flat at tier 1 instead of climbing)")
    mispriced = ContractIn(
        type="player",
        salaries={"26-27": tier("26-27", "1"), "27-28": tier("27-28", "1"),
                  "28-29": tier("28-29", "3")},
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT"},
    )
    r2 = tx._check_second_round_scale_terms(mispriced, BIO, CAP_LEVELS)
    check("flagged", r2 is not None and not r2.passed)
    check("names the correct (tier-2) Year 2 figure",
          r2 is not None and tier("27-28", "2") in r2.message)

    print("\nsame 3-year deal PLUS the trailing § 3.10 RFA hold (a live 4th "
          "salary entry for the hold season, tagged RFA)")
    with_trailing_hold = ContractIn(
        type="player",
        # An auto-priced § 3.10 hold, not scored here — a deliberately off-scale
        # figure, so the check is that it is skipped rather than matched.
        salaries={**correct3.salaries, "29-30": "$5,303,423"},
        cap_holds={"27-28": "NON_GTD", "28-29": "TEAM_OPT", "29-30": "RFA"},
    )
    r3 = tx._check_second_round_scale_terms(with_trailing_hold, BIO, CAP_LEVELS)
    check("still recognized as the 3-year structure, not misread as a 4-year deal",
          r3 is not None and "3-year" in r3.message)
    check("passes — the RFA season isn't held to the salary-year table",
          r3 is not None and r3.passed)

    print("\n4-year deal — starts at tier 2 and climbs to 5")
    scale4 = tx._second_round_scale_contract(
        ContractIn(type="player", salaries={s: "$0" for s in ("26-27", "27-28", "28-29", "29-30")}),
        CAP_LEVELS,
    )
    check("Year 1 priced off the tier-2 figure",
          scale4["salaries"]["26-27"] == f"${CAP_LEVELS['26-27']['min_salary_scale']['2']:,}")
    check("Years 2-4 climb to tiers 3, 4 and 5",
          scale4["salaries"]["27-28"] == tier("27-28", "3")
          and scale4["salaries"]["28-29"] == tier("28-29", "4")
          and scale4["salaries"]["29-30"] == tier("29-30", "5"))
    check("...which is the league sheet's 3+1 row, to rounding",
          all(abs(int(scale4["salaries"][s].strip("$").replace(",", "")) - want) <= 5
              for s, want in (("26-27", 2_449_421), ("27-28", 2_664_401),
                              ("28-29", 2_888_193), ("29-30", 3_272_766))))
    correct4 = ContractIn(type="player", salaries=scale4["salaries"], cap_holds=scale4["cap_holds"])
    r4 = tx._check_second_round_scale_terms(correct4, BIO, CAP_LEVELS)
    check("4-year deal passes the exact-match check", r4 is not None and r4.passed)

    print("\ntwo-way and off-scale shapes fall through untouched")
    two_way = ContractIn(type="two-way", salaries={"26-27": "$0"})
    check("_second_round_scale_contract returns None for a 1-year shape",
          tx._second_round_scale_contract(two_way, CAP_LEVELS) is None)

    print("\nthe office form's prefill (_second_round_scale_options)")
    import json as _json, tempfile as _tf
    real = (tx.CAP_LEVELS_FILE, tx._current_league_year)
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump(CAP_LEVELS, fh)
    tx.CAP_LEVELS_FILE = Path(fh.name)
    tx._current_league_year = lambda: "26-27"
    scale3 = tx._second_round_scale_contract(
        ContractIn(type="player", salaries={s: "$0" for s in ("26-27", "27-28", "28-29")}),
        CAP_LEVELS,
    )
    try:
        opts = tx._second_round_scale_options({"draft_year": 2026, "draft_round": 2})
        check("offers both deals", opts is not None and set(opts) == {"2+1", "3+1"})
        check("...priced exactly as the validator scores them",
              opts["2+1"]["salaries"] == scale3["salaries"] and opts["3+1"]["salaries"] == scale4["salaries"])
        check("the 2+1 rolls into an RFA hold, the 3+1 into a UFA hold (§ 3.1)",
              opts["2+1"]["cap_holds"].get("29-30") == "RFA" and opts["3+1"]["cap_holds"].get("30-31") == "UFA")
        check("...with the hold season listed but not priced",
              opts["3+1"]["seasons"][-1] == "30-31" and "30-31" not in opts["3+1"]["salaries"])
        loaded = ContractIn(type="player", salaries=opts["3+1"]["salaries"], cap_holds=opts["3+1"]["cap_holds"])
        r = tx._check_second_round_scale_terms(loaded, BIO, CAP_LEVELS)
        check("a loaded 3+1 passes the scale check as-is", r is not None and r.passed)
        tx._current_league_year = lambda: "27-28"
        late = tx._second_round_scale_options({"draft_year": 2026, "draft_round": 2})
        check("a pick signed a year late starts in the current season",
              late is None or min(late["2+1"]["salaries"]) == "27-28")
        check("a first-rounder gets none",
              tx._second_round_scale_options({"draft_year": 2026, "draft_round": 1}) is None)
    finally:
        tx.CAP_LEVELS_FILE, tx._current_league_year = real

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

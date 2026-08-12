"""Regression tests for the § 3.10 auto-computed free-agent hold amount —
routers.transactions._compute_fa_hold_amount / _derive_bird_tier /
_autofill_fa_hold_amounts.

Written 2026-08-07 after discovering cap hold dollar amounts were never
computed anywhere in the codebase (§ 3.10 was pure manual review): a player's
`cap_holds` type (UFA/RFA) got set at signing time, but nothing ever priced
it into `salaries`, leaving holds like Mark Williams' 29-30 UFA with no
number behind them at all. This suite locks in the formula against real
production figures from that case.

    venv/bin/python -m tests.test_fa_hold_calc
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
from routers.transactions import (  # noqa: E402
    _compute_fa_hold_amount, _derive_bird_tier, _autofill_fa_hold_amounts,
    _preview_fa_hold,
)

FAILS = []

# Real 26-27/29-30-ish levels. 29-30 EAPS deliberately absent, matching the
# live cap-levels.json (only 25-26/26-27 configured as of this writing).
CAP_LEVELS = {
    "26-27": {"cap": 164961000, "min_salary_scale": {"0": 1357763}},
    "29-30": {"min_salary_scale": {"0": 1357763}},
}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    print("Mark Williams 29-30 UFA hold — Full Bird, no EAPS on file")
    amount, note = _compute_fa_hold_amount(
        "$16,548,400", "QVFA", "29-30", CAP_LEVELS, eaps_assumption="above",
    )
    check("150% of $16,548,400 = $24,822,600", amount == 24822600)
    check("flagged as a placeholder", note is not None and "placeholder" in note)

    amount_below, _ = _compute_fa_hold_amount(
        "$16,548,400", "QVFA", "29-30", CAP_LEVELS, eaps_assumption="below",
    )
    check("190% variant differs and is larger", amount_below == 31441960 and amount_below > amount)

    print("\nQVFA with no eaps_assumption and no real EAPS raises, doesn't guess")
    raised = False
    try:
        _compute_fa_hold_amount("$16,548,400", "QVFA", "29-30", CAP_LEVELS)
    except HTTPException as e:
        raised = e.status_code == 422
    check("raises 422 instead of silently picking a side", raised)

    print("\nQVFA with a real EAPS on file needs no assumption")
    levels_with_eaps = {"30-31": {"eaps": 15000000}}
    above, note = _compute_fa_hold_amount("$20,000,000", "QVFA", "30-31", levels_with_eaps)
    check("above EAPS -> 150%, no placeholder note", above == 30000000 and note is None)
    below, note = _compute_fa_hold_amount("$10,000,000", "QVFA", "30-31", levels_with_eaps)
    check("at/below EAPS -> 190%, no placeholder note", below == 19000000 and note is None)

    print("\nNon-QVFA / EQVFA flat percentages, no EAPS needed")
    amt, note = _compute_fa_hold_amount("$10,000,000", "Non-QVFA", "29-30", CAP_LEVELS)
    check("Non-Bird 120%", amt == 12000000 and note is None)
    amt, note = _compute_fa_hold_amount("$10,000,000", "EQVFA", "29-30", CAP_LEVELS)
    check("Early Bird 130%", amt == 13000000 and note is None)

    print("\nClamping to min/max")
    amt, _ = _compute_fa_hold_amount("$1,000,000", "Non-QVFA", "29-30", CAP_LEVELS, min_amt=5000000)
    check("floors at min_amt", amt == 5000000)
    amt, _ = _compute_fa_hold_amount("$50,000,000", "Non-QVFA", "29-30", CAP_LEVELS, max_amt=40000000)
    check("caps at max_amt", amt == 40000000)

    print("\n_derive_bird_tier from contract history")
    bio_full_bird = {"contracts": [
        {"team": "TOR", "salaries": {"26-27": "$1", "27-28": "$1", "28-29": "$1"}},
    ]}
    check("3 consecutive seasons with team -> QVFA",
          _derive_bird_tier(bio_full_bird, "TOR", "29-30") == "QVFA")

    bio_early_bird = {"contracts": [
        {"team": "TOR", "salaries": {"27-28": "$1", "28-29": "$1"}},
    ]}
    check("2 consecutive seasons with team -> EQVFA",
          _derive_bird_tier(bio_early_bird, "TOR", "29-30") == "EQVFA")

    bio_non_bird = {"contracts": [
        {"team": "TOR", "salaries": {"28-29": "$1"}},
    ]}
    check("1 season with team -> Non-QVFA",
          _derive_bird_tier(bio_non_bird, "TOR", "29-30") == "Non-QVFA")

    bio_diff_team = {"contracts": [
        {"team": "LAL", "salaries": {"26-27": "$1", "27-28": "$1", "28-29": "$1"}},
    ]}
    check("tenure with a different team doesn't count",
          _derive_bird_tier(bio_diff_team, "TOR", "29-30") == "Non-QVFA")

    print("\n_autofill_fa_hold_amounts end-to-end (Mark Williams shape)")
    bio = {
        "salaries": {"26-27": "$15,044,000", "27-28": "$15,796,200", "28-29": "$16,548,400"},
        "contracts": [{"team": "TOR", "salaries": {"26-27": "$15,044,000", "27-28": "$15,796,200",
                                                     "28-29": "$16,548,400"}}],
    }
    cap_holds = {"28-29": "PLAYER_OPT", "29-30": "UFA"}
    explicit_salaries = {"26-27": "$15,044,000", "27-28": "$15,796,200", "28-29": "$16,548,400"}
    notes = _autofill_fa_hold_amounts(
        bio, "TOR", cap_holds, explicit_salaries, CAP_LEVELS, eaps_assumption="above",
    )
    check("29-30 auto-filled to $24,822,600", bio["salaries"].get("29-30") == "$24,822,600")
    check("28-29 (PLAYER_OPT, already priced) left untouched", bio["salaries"]["28-29"] == "$16,548,400")
    check("29-30 noted as a placeholder", "29-30" in notes)

    print("\n_preview_fa_hold — the same figure, before anything is applied")

    class _C:
        def __init__(self, salaries, cap_holds, type_="player"):
            self.salaries, self.cap_holds, self.type = salaries, cap_holds, type_

    base_bio = {
        "salaries": {"25-26": "$14,000,000"},
        "contracts": [{"team": "TOR", "salaries": {"23-24": "$1", "24-25": "$1", "25-26": "$1"}}],
    }
    contract = _C({"26-27": "$15,044,000", "27-28": "$15,796,200", "28-29": "$16,548,400"},
                  {"28-29": "PLAYER_OPT", "29-30": "UFA"})
    h = _preview_fa_hold(base_bio, "TOR", contract, CAP_LEVELS,
                         bird_rights_type="QVFA", eaps_assumption="above")
    check("prices the trailing hold to the same $24,822,600", h["amount"] == 24822600)
    check("names the hold's season and type", (h["season"], h["type"]) == ("29-30", "UFA"))
    check("bases it on the deal's own final year", h["prior_salary"] == 16548400)
    # The whole point of the preview: it must not write anything, because the
    # simulator and the offer form call it on every keystroke.
    check("leaves the bio untouched", base_bio["salaries"] == {"25-26": "$14,000,000"})

    h2 = _preview_fa_hold(base_bio, "TOR", contract, CAP_LEVELS, bird_rights_type="QVFA")
    check("no EAPS assumption reports needs_eaps instead of raising",
          h2["needs_eaps"] is True and h2["amount"] is None)

    check("no trailing hold -> None",
          _preview_fa_hold(base_bio, "TOR", _C({"26-27": "$5,000,000"}, {}), CAP_LEVELS) is None)

    # A hold the submitter priced by hand is theirs to keep — same carve-out
    # _autofill_fa_hold_amounts makes for an explicitly-salaried hold season.
    check("an explicitly priced hold season isn't second-guessed",
          _preview_fa_hold(base_bio, "TOR",
                           _C({"26-27": "$5,000,000", "27-28": "$9,000,000"}, {"27-28": "UFA"}),
                           CAP_LEVELS) is None)

    no_history = _preview_fa_hold({}, "TOR", _C({}, {"27-28": "UFA"}), CAP_LEVELS)
    check("nothing to price off -> explains itself, doesn't raise",
          no_history["amount"] is None and no_history["note"])

    print("\n_preview_fa_hold must agree with what apply will actually require")
    # _apply_sign/_apply_sign_pick/_apply_convert_twoway all append the deal
    # being signed to bio["contracts"] *before* pricing its trailing hold, so
    # a fresh 3-year deal to a player with zero contract history reads as
    # QVFA at apply time (the fallback tenure scan counts the deal's own
    # years). The preview used to derive tier from the bio as it stood before
    # that append, so it missed this and reported Non-QVFA/no EAPS needed —
    # a green verdict for a signing that would 422 at submit. Regression for
    # the fix: preview must reach the same tier apply would.
    fresh_bio = {}
    fresh_contract = _C(
        {"26-27": "$5,000,000", "27-28": "$5,250,000", "28-29": "$5,500,000"},
        {"29-30": "UFA"},
    )
    fresh = _preview_fa_hold(fresh_bio, "ATL", fresh_contract, CAP_LEVELS)
    check("a 3-year deal to a no-history player derives QVFA, same as apply would",
          fresh["bird_tier"] == "QVFA")
    check("and previews needs_eaps instead of silently reporting Non-QVFA/legal",
          fresh["needs_eaps"] is True)
    check("still leaves the real bio untouched", fresh_bio == {})

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

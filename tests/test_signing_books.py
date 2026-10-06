"""`_signing_books` — the one signing projection shared by `_validate_sign`,
`_validate_offer_sheet`, `_validate_offer_sheet_decision` and
`_signing_fact_sheet` (it replaced four copies of the same lines, 2026-10-06).

Pins the § 2.1a Empty Roster Charge it adds: a team below 12 standard players
carries the rookie minimum per empty slot as guaranteed salary, priced on the
count *after* the signing. And pins that the simulator's fact sheet reads the
same figures the validator does, which is the point of having one helper.

Stubs every reader of the roster and the bios, so it never touches live data.

    venv/bin/python -m tests.test_signing_books
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []
SEASON = "26-27"
ROOKIE_MIN = 1_357_763
CAP = 164_961_000
CAP_LEVELS = {SEASON: {"cap": CAP, "apron1": 209_015_000, "apron2": 221_686_000,
                       "min_salary_scale": {"0": ROOKIE_MIN, "10+": 3_876_529}}}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def stub(standard_count, *, own_player=None, salary=150_000_000, hold=0, fa_hold=False):
    """A team with `standard_count` standard bodies (one of them `own_player`,
    if given) and the given books."""
    T._compute_team_salary = lambda team, bios, season: salary + (hold if fa_hold else 0)
    T._compute_team_salary_ex_holds = lambda team, bios, season: salary
    T._signee_existing_hold = (lambda team, player, bios, season:
                               (hold, fa_hold) if player == own_player else (0, False))
    T._count_standard_roster = (lambda team, excluding=None, bios=None:
                                standard_count - (1 if own_player and excluding == own_player else 0))


def books(player="new", two_way=False, sal="$2,000,000"):
    return T._signing_books("UTA", player, {SEASON: sal}, {}, SEASON, CAP_LEVELS, two_way=two_way)


def main():
    saved = {n: getattr(T, n) for n in ("_compute_team_salary", "_compute_team_salary_ex_holds",
                                        "_signee_existing_hold", "_count_standard_roster",
                                        "_preview_fa_hold")}
    try:
        print("§ 2.1a charge, priced after the signing")
        stub(10)
        b = books()
        check("10 + 1 signing leaves 11 standard", b.standard_after == 11)
        check("...and one empty slot below 12 is charged", b.erc_after == ROOKIE_MIN)
        check("the charge is in the projection",
              b.projected_ex_holds == 150_000_000 + 2_000_000 + ROOKIE_MIN)
        check("...and in both funding bases",
              b.funding_base == 150_000_000 + ROOKIE_MIN
              and b.funding_base_ex_holds == 150_000_000 + ROOKIE_MIN)

        stub(11)
        b = books()
        check("a signing that reaches 12 carries no charge",
              b.standard_after == 12 and b.erc_after == 0
              and b.projected_ex_holds == 152_000_000)

        stub(14)
        check("a full roster carries no charge", books().erc_after == 0)

        stub(10)
        b = books(two_way=True, sal="$0")
        check("a two-way fills no standard slot, so both slots stay charged",
              b.standard_after == 10 and b.erc_after == 2 * ROOKIE_MIN)

        # A team's own free agent already sits on the roster CSV. Re-signing
        # him reuses his slot; counting him twice would hide a real charge.
        stub(11, own_player="mine", hold=5_000_000, fa_hold=True)
        b = books(player="mine")
        check("re-signing your own free agent doesn't count him twice",
              b.standard_after == 11 and b.erc_after == ROOKIE_MIN)
        check("...and his UFA hold is replaced, not stacked",
              b.projected_ex_holds == 150_000_000 + 2_000_000 + ROOKIE_MIN
              and b.funding_base == 150_000_000 + ROOKIE_MIN)

        print("\nthe fact sheet reads the same figures")
        stub(10)
        T._preview_fa_hold = lambda *a, **k: None
        ctx = {"bios": {}, "cur_season": SEASON, "cap_levels": CAP_LEVELS, "team_state": {}}

        class Contract:
            type = "player"
            salaries = {SEASON: "$2,000,000"}
            cap_holds = {}

        sheet = T._signing_fact_sheet("UTA", "new", Contract(), ctx, signing_method="cap_space")
        b = books()
        check("cap room is measured from the validator's funding base",
              sheet["cap_room"] == CAP - b.funding_base)
        check("the projection matches", sheet["projected_salary_ex_holds"] == b.projected_ex_holds)
        check("the charge and the count after are shown",
              sheet["empty_roster_charge_after"] == ROOKIE_MIN
              and sheet["standard_count_after"] == 11)
    finally:
        for n, f in saved.items():
            setattr(T, n, f)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

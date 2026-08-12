"""A two-way signing must never trip the hard-cap checks in `_validate_sign`.

Written 2026-08-12. `_validate_sign`'s hard-cap block computed
`projected_ex_holds = current_ex_holds - existing_hold + new_sal` and ran
`_hard_cap_check`/`_universal_hard_cap_check` on it unconditionally. A two-way's
`new_sal` is $0 (§ 2.2 — two-ways sit outside Team Salary entirely), so
`projected_ex_holds` reduced to whatever the team's *existing* salary already
was. A team already over its stamped hard cap for unrelated reasons therefore
failed this check on a two-way signing that changed nothing, worded as if the
two-way had caused the breach ("{team} would be $X over their hard cap ...").

Every other type-conditional check in this file (`_roster_size_check`,
`_check_contract_raises`, `_check_minimum_salary`, `_check_max_salary`, and
`_validate_sign_pick`'s own hard-cap block) already early-returns on
`contract.type == "two-way"`. This was the one gap.

    venv/bin/python -m tests.test_two_way_hard_cap
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402
from routers.transactions import ContractIn, SignDetails  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


SEASON = "26-27"
CAP_LEVELS = {SEASON: {
    "cap": 164961000, "apron1": 209015000, "apron2": 221686000,
    "hard_cap": 230000000,
}}

# POR is already carrying a $250,000,000 standard-salary whale — over both
# aprons and the league Hard Cap — for reasons that have nothing to do with
# the two-way signing under test.
BIOS = {
    "whale": {"type": "player", "salaries": {SEASON: "$250,000,000"}},
    "two-way-guy": {"type": "two-way", "salaries": {}},
}


def install_fake_roster(tmp: Path):
    path = tmp / "por-roster.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["SLUG"])
        w.writerow(["whale"])
    tx.DATA_DIR = tmp
    tx.load_player_bios = lambda: BIOS


def run_sign(hard_cap_level):
    details = SignDetails(
        player="two-way-guy", team="POR",
        contract=ContractIn(type="two-way", salaries={SEASON: "$0"}),
    )
    ctx = {
        "bios": BIOS, "cur_season": SEASON,
        "team_state": {"POR": {SEASON: {"hard_cap": hard_cap_level}}},
        "cap_levels": CAP_LEVELS,
    }
    return tx._validate_sign(details, ctx)


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        install_fake_roster(Path(tmp))

        print("a $0 two-way sign to a team already over the First Apron")
        checks = run_sign("first_apron")
        names = [c.check for c in checks]
        check("no apron hard-cap check fires", "hard_cap_por" not in names)
        check("no league hard-cap check fires", "hard_cap_league_por" not in names)

        print("\nsame, at the Second Apron")
        checks = run_sign("second_apron")
        names = [c.check for c in checks]
        check("no apron hard-cap check fires", "hard_cap_por" not in names)
        check("no league hard-cap check fires", "hard_cap_league_por" not in names)

        print("\ncontrol — a standard signing to the same over-cap team still trips it")
        details = SignDetails(
            player="another-guy", team="POR",
            contract=ContractIn(type="player", salaries={SEASON: "$5,000,000"}),
        )
        ctx = {
            "bios": {**BIOS, "another-guy": {"type": "player", "salaries": {}}},
            "cur_season": SEASON,
            "team_state": {"POR": {SEASON: {"hard_cap": "first_apron"}}},
            "cap_levels": CAP_LEVELS,
        }
        checks = tx._validate_sign(details, ctx)
        names = [c.check for c in checks]
        check("apron hard-cap check still fires for a standard contract", "hard_cap_por" in names)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

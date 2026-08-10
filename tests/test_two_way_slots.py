"""§ 2.2's three two-way slots, and the roster-count exclusion that makes the
limit safe to enforce as a hard error.

Written 2026-08-10, during the first FFA free-agency class. Two-way signings had
been reaching `_apply_sign` having passed **zero** checks: `_validate_sign` skips
the standard roster check for a two-way contract (correctly — § 2.2 puts them
outside the 15), and every remaining check no-ops on the $0 salary a two-way
carries. The verdict on a two-way offer was not "legal", it was unexamined, and
MIL had a live offer out on a fourth two-way while already carrying three.

The exclusion is the load-bearing half. A team's own free agent **stays on the
roster CSV** — a cap hold is still a roster row — so `count + 1` double-counts a
team re-signing its own player. In § 2.1's 16–20 warning band that produced a
spurious warning (LAC's offer on its own Bitadze read 16 standard players when
the true post-signing count was 15). Against a 3-slot cap enforced as a hard
error, the same convention would *falsely block* a team re-signing its own
two-way, which is why the two shipped together.

    venv/bin/python -m tests.test_two_way_slots
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402
from routers.constants import TWO_WAY_MAX  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── a fake league on disk ─────────────────────────────────────────────────────

ROSTERS = {
    # Three two-ways (the § 2.2 ceiling) plus two standard players.
    "MIL": [("star-alpha", "player"), ("star-beta", "player"),
            ("tw-one", "two-way"), ("tw-two", "two-way"), ("tw-three", "two-way")],
    # Two two-ways — room for a third.
    "ORL": [("orl-star", "player"), ("orl-tw-one", "two-way"), ("orl-tw-two", "two-way")],
    # A dead cap hit and held draft rights are neither standard nor two-way.
    "SAS": [("sas-star", "player"), ("sas-ghost", "dead"),
            ("sas-stash", "draft-rights"), ("sas-tw", "two-way")],
}
BIOS = {slug: {"type": t} for rows in ROSTERS.values() for slug, t in rows}


def install_fake_league(tmp: Path):
    """Point the module's DATA_DIR at temp roster CSVs and stub the bios."""
    for team, rows in ROSTERS.items():
        path = tmp / f"{team.lower()}-roster.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["SLUG"])
            for slug, _ in rows:
                w.writerow([slug])
    tx.DATA_DIR = tmp
    tx.load_player_bios = lambda: BIOS


def main():
    print(f"the limit itself (TWO_WAY_MAX={TWO_WAY_MAX})")
    check("three two-ways is fine", tx._two_way_slot_check("MIL", 3, "signing") is None)
    check("two is fine", tx._two_way_slot_check("MIL", 2, "signing") is None)
    r = tx._two_way_slot_check("MIL", 4, "signing")
    check("a fourth is refused", r is not None)
    check("...as a blocking error", r is not None and r.level == "error")
    check("...citing § 2.2", r is not None and "§ 2.2" in r.message)
    check("...naming the team and the count", r is not None and "MIL" in r.message and "4" in r.message)
    check("...under a stable check name", r is not None and r.check == "two_way_slots")
    # No offseason band, deliberately: § 2.1 writes 16-20 into the rules and
    # § 2.2 grants no equivalent, so there is no level between pass and error.
    check("there is no warning band above the limit",
          all((tx._two_way_slot_check("MIL", n, "signing") or r).level == "error"
              for n in (4, 5, 9)))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        real_dir, real_bios = tx.DATA_DIR, tx.load_player_bios
        install_fake_league(tmp)
        try:
            print("\ncounting two-way slots")
            check("MIL is at the ceiling", tx._count_two_way_roster("MIL") == 3)
            check("ORL has one slot free", tx._count_two_way_roster("ORL") == 2)
            check("dead and draft-rights rows are not two-ways",
                  tx._count_two_way_roster("SAS") == 1)
            check("an unknown team counts zero rather than raising",
                  tx._count_two_way_roster("XXX") == 0)

            print("\ncounting standard slots")
            check("two-ways don't occupy standard slots",
                  tx._count_standard_roster("MIL") == 2)
            check("dead cap and draft rights don't either",
                  tx._count_standard_roster("SAS") == 1)

            print("\nthe exclusion — re-signing your own player reuses his slot")
            check("excluding a standard player drops the count",
                  tx._count_standard_roster("MIL", excluding="star-alpha") == 1)
            check("excluding a two-way drops the two-way count",
                  tx._count_two_way_roster("MIL", excluding="tw-two") == 2)
            check("excluding somebody not on the roster changes nothing",
                  tx._count_standard_roster("MIL", excluding="nobody-here") == 2)
            check("excluding None is the plain count",
                  tx._count_standard_roster("MIL", excluding=None) == 2)
            # The two cross-checks that motivated the whole change.
            check("a team at the ceiling may re-sign its OWN two-way",
                  tx._two_way_slot_check(
                      "MIL", tx._count_two_way_roster("MIL", excluding="tw-two") + 1,
                      "signing") is None)
            check("...but not add a fourth from outside",
                  tx._two_way_slot_check(
                      "MIL", tx._count_two_way_roster("MIL", excluding="outsider") + 1,
                      "signing") is not None)
            # A standard player is not in the two-way count, so excluding him
            # must not free a two-way slot for someone else.
            check("excluding a standard player frees no two-way slot",
                  tx._two_way_slot_check(
                      "MIL", tx._count_two_way_roster("MIL", excluding="star-alpha") + 1,
                      "signing") is not None)
        finally:
            tx.DATA_DIR, tx.load_player_bios = real_dir, real_bios

    print("\n§ 3.1-3.6 — a funding method must be declared")
    r = tx._check_signing_method_declared("MIA", None, "player")
    check("a standard contract with no method is refused", r is not None)
    check("...as a blocking error", r is not None and r.level == "error")
    check("...citing the funding articles", r is not None and "§ 3.1" in r.message)
    check("an empty string counts as undeclared",
          tx._check_signing_method_declared("MIA", "", "player") is not None)
    check("a declared method passes",
          tx._check_signing_method_declared("MIA", "minimum", "player") is None)
    check("cap space passes",
          tx._check_signing_method_declared("MIA", "cap_space", "player") is None)
    # Exempt for a reason, not by omission: $0, outside Team Salary (§ 2.2).
    check("a two-way needs no method",
          tx._check_signing_method_declared("ORL", None, "two-way") is None)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Parity tests: the flat picks ledger vs the store the site serves.

The case that matters is the one that happened: the flat ledger records a trade,
the conveyance store never hears about it, and the new owner has no claim on
anything `/api/picks/{team}` serves. The other cases pin what must NOT be
flagged, since the flat model is lossy and disagrees legitimately on every
contingent pick.

    venv/bin/python -m picks_conveyance.tests.test_parity
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import parity, seed_store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def row(year, rnd, orig, owner):
    return {"YEAR": str(year), "ROUND": str(rnd), "ORIG": orig, "OWNER": owner,
            "PICK": "", "PLAYER": "", "PROTECTED": "", "SWAP_OWNER": "",
            "NOTES": "", "FROZEN": "", "FROZEN_REASON": ""}


def store_of(rows):
    return {"picks": [seed_store.seed_pick(r) for r in rows],
            "swap_groups": {}, "binary_swaps": {}}


def main():
    rows = [row(2027, 1, "DAL", "DAL"), row(2027, 1, "SAC", "SAC")]
    store = store_of(rows)
    check("identical ledgers agree", parity.owner_mismatches(rows, store) == [])

    # The real failure: a trade reached the flat ledger and not the store.
    traded = [row(2027, 1, "DAL", "SAC"), row(2027, 1, "SAC", "SAC")]
    found = parity.owner_mismatches(traded, store)
    check("a trade the store missed is flagged", len(found) == 1, found)
    check("and names the pick and the missing team",
          found and "2027 R1 DAL" in found[0] and "SAC has no claim" in found[0], found)

    # A pipe-joined flat owner names both teams; each must hold a claim.
    found = parity.owner_mismatches([row(2027, 1, "DAL", "DAL|HOU"), rows[1]], store)
    check("every team in a pipe-joined flat owner is checked",
          len(found) == 1 and "HOU has no claim" in found[0], found)

    found = parity.owner_mismatches(rows[:1], store)
    check("a pick only in the store is flagged",
          len(found) == 1 and "not in draft-picks.csv" in found[0], found)
    found = parity.owner_mismatches(rows + [row(2028, 1, "DAL", "DAL")], store)
    check("a pick only in the flat ledger is flagged",
          len(found) == 1 and "not in the conveyance store" in found[0], found)

    # claimants(): the served shape names parties in several places.
    check("pipe-joined owner", parity.claimants({"orig": "LAL", "owner": "DET|HOU"}) == {"DET", "HOU"})
    check("undetermined owner falls back to orig, like /api/picks/{team}",
          parity.claimants({"orig": "LAL", "owner": "?"}) == {"LAL"})
    check("leaf teams count",
          "PHX" in parity.claimants({"orig": "OKC", "owner": "LAC",
                                     "leaves": [{"team": "PHX"}, {"team": "LAC"}]}))
    check("a ladder's from/to count",
          parity.claimants({"orig": "OKC", "owner": "OKC",
                            "ladder": {"from": "OKC", "to": "BOS"}}) == {"OKC", "BOS"})
    check("a ladder fallback's claimant counts",
          "NYK" in parity.claimants({"orig": "OKC", "owner": "OKC",
                                     "ladder_fallback_of": {"to": "NYK"}}))

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("test_parity: all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

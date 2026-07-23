"""Regression tests for routers.roster_picks._pick_matches_team — decides
which picks GET /api/picks/{team} returns for a given team.

Written 2026-07-23 alongside the ladder_fallback_of fix in
tests/test_stepien_rule.py: a team's real compensation claim for a
different pick's protection ladder never resolving was completely
invisible from this endpoint (not just from the Stepien check), since the
match only ever looked at `owner`/`orig`.

    venv/bin/python -m tests.test_picks_matching
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.roster_picks import _pick_matches_team  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    settled = {"orig": "AAA", "owner": "AAA"}
    check("settled: exact owner matches", _pick_matches_team(settled, "AAA"))
    check("settled: other team doesn't match", not _pick_matches_team(settled, "BBB"))

    pipe = {"orig": "AAA", "owner": "AAA|BBB"}
    check("pipe-owner: first candidate matches", _pick_matches_team(pipe, "AAA"))
    check("pipe-owner: second candidate matches", _pick_matches_team(pipe, "BBB"))
    check("pipe-owner: non-candidate doesn't match", not _pick_matches_team(pipe, "CCC"))

    undetermined = {"orig": "AAA", "owner": "?"}
    check("undetermined owner: falls back to orig", _pick_matches_team(undetermined, "AAA"))
    check("undetermined owner: non-orig doesn't match", not _pick_matches_team(undetermined, "BBB"))

    fallback = {"orig": "AAA", "owner": "AAA", "ladder_fallback_of": {"to": "CCC"}}
    check("ladder fallback: real owner still matches", _pick_matches_team(fallback, "AAA"))
    check("ladder fallback: fallback claimant ALSO matches even though "
          "owner/orig say nothing about them",
          _pick_matches_team(fallback, "CCC"))
    check("ladder fallback: unrelated team still doesn't match", not _pick_matches_team(fallback, "DDD"))

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

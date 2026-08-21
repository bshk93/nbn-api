"""Regression tests for season_clock.py — the one season-boundary definition
shared by box scores, the stats build, and cap/contract logic (unified
2026-08-21; see the module docstring for what it replaced).

Exercises the pure date math (no NBS_DATA_DIR I/O) plus the rollover-override
path against a scratch data directory, so a test run never touches the real
league-state.json.

    venv/bin/python -m tests.test_season_clock
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import season_clock as sc  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("default July 1 boundary, no overrides")
check("June 30 is the prior season", sc.season_for_date("2026-06-30", {}) == "25-26")
check("July 1 rolls over", sc.season_for_date("2026-07-01", {}) == "26-27")
check("mid-season resolves back", sc.season_for_date("2027-01-04", {}) == "26-27")

print("\nrollover overrides — a season's effective start can move")
early = {"26-27": "2026-06-15"}
check("a date after the override's earlier start rolls over early",
      sc.season_for_date("2026-06-20", early) == "26-27")
check("a date before the override's earlier start is still last season",
      sc.season_for_date("2026-06-10", early) == "25-26")
late = {"26-27": "2026-08-01"}
check("a date after the default July 1 but before a delayed override is still last season",
      sc.season_for_date("2026-07-15", late) == "25-26")
check("a date on/after the delayed override rolls over",
      sc.season_for_date("2026-08-01", late) == "26-27")

print("\ncurrent_season() reads rollovers from NBS_DATA_DIR, not a hardcoded live path")
with tempfile.TemporaryDirectory() as d:
    scratch = Path(d)
    (scratch / "league-state.json").write_text(json.dumps({"rollovers": {"26-27": "2020-01-01"}}))
    orig_data_dir, orig_state_file = sc.DATA_DIR, sc.LEAGUE_STATE_FILE
    sc.DATA_DIR = scratch
    sc.LEAGUE_STATE_FILE = scratch / "league-state.json"
    try:
        check("an absurdly early override is honored from the scratch dir",
              sc.current_season() == "26-27")
    finally:
        sc.DATA_DIR, sc.LEAGUE_STATE_FILE = orig_data_dir, orig_state_file

print("\nno league-state.json at all — defaults cleanly")
with tempfile.TemporaryDirectory() as d:
    scratch = Path(d)
    orig_data_dir, orig_state_file = sc.DATA_DIR, sc.LEAGUE_STATE_FILE
    sc.DATA_DIR = scratch
    sc.LEAGUE_STATE_FILE = scratch / "league-state.json"
    try:
        check("load_rollovers returns {} when the file is missing", sc.load_rollovers() == {})
    finally:
        sc.DATA_DIR, sc.LEAGUE_STATE_FILE = orig_data_dir, orig_state_file

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))
sys.exit(1 if FAILS else 0)

"""Regression tests for routers/game_day.py — which day's games are "now".

What must keep being true:

  * with no live stream, the game day is the ET date;
  * a live stream that began yesterday (running past midnight) keeps the game
    day on yesterday, however late it runs;
  * a stream started in the small hours is the previous night's when that
    night still has games without a box score, and today's otherwise;
  * a stream started in the evening is always its own day's.

    venv/bin/python -m tests.test_game_day
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.game_day as gd  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


UNFINISHED: set = set()
gd._night_unfinished = lambda date: date in UNFINISHED
TODAY = "2026-11-01"
OFF = {"live": False}

print("no stream")
check("the ET date", gd.game_day(OFF, TODAY) == TODAY)

print("last night's stream, still running")
late = {"live": True, "started_at": "2026-11-01T03:10:00Z"}   # 11:10pm ET 10/31
check("is last night's", gd.game_day(late, TODAY) == "2026-10-31")

print("a stream started after midnight")
resumed = {"live": True, "started_at": "2026-11-01T05:40:00Z"}  # 1:40am ET
UNFINISHED.add("2026-10-31")
check("is last night's while last night has games missing", gd.game_day(resumed, TODAY) == "2026-10-31")
UNFINISHED.clear()
check("is today's once last night is finished", gd.game_day(resumed, TODAY) == TODAY)

print("an evening stream")
UNFINISHED.add("2026-10-31")
evening = {"live": True, "started_at": "2026-11-01T23:00:00Z"}
check("is its own day's, whatever last night left", gd.game_day(evening, TODAY) == TODAY)
check("a stream with no start time is the ET date", gd.game_day({"live": True}, TODAY) == TODAY)

if FAILS:
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1)
print("\nall passed")

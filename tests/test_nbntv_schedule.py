"""/nbntv-schedule — upcoming games flagged as streams.

Only games with the `stream` flag on or after league today are listed, in
schedule order, capped at NBNTV_SCHEDULE_LIMIT with a "+N more" line. The
schedule and the clock are stubbed; nothing touches disk.

    venv/bin/python3 tests/test_nbntv_schedule.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord as d  # noqa: E402
import routers.schedule as sched  # noqa: E402
import routers.league_time as lt  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


def game(i, date, stream=True, **kw):
    g = {"id": f"g{i}", "date": date, "time_et": "7:00p",
         "away_team": "BOS", "home_team": "DET", **kw}
    if stream:
        g["stream"] = True
    return g


GAMES = []
sched._load = lambda season: {"season": season, "games": GAMES}
lt.league_today_str = lambda: "2026-10-20"
d._current_league_year = lambda: "26-27"


def desc():
    return d.dispatch({"name": "nbntv-schedule"}, {})["data"]["embeds"][0]["description"]


print("filtering")
GAMES[:] = [
    game(1, "2026-10-19"),                          # past
    game(2, "2026-10-20", streamer="Alice"),        # today
    game(3, "2026-10-21", stream=False),            # not a stream
    game(4, "2026-10-22", stream_buyer="Bob"),      # bought, unclaimed
]
out = desc()
check("past stream left out", "Mon Oct 19" not in out, out)
check("today's stream listed with its streamer", "Tue Oct 20" in out and "Alice" in out, out)
check("unflagged game left out", "Wed Oct 21" not in out, out)
check("bought stream names the buyer", "TBD (bought: Bob)" in out, out)
check("no overflow line", "more on" not in out, out)

print("empty")
GAMES[:] = [game(1, "2026-10-19")]
check("says nothing is scheduled", desc() == "No upcoming streams are scheduled.")

print("overflow")
GAMES[:] = [game(i, f"2026-11-{i:02d}") for i in range(1, d.NBNTV_SCHEDULE_LIMIT + 4)]
out = desc()
check("caps the list", out.count("BOS @ DET") == d.NBNTV_SCHEDULE_LIMIT, out)
check("counts the rest", "+3 more" in out, out)

print("help")
help_text = str(d.help_response())
check("listed in /help", "/nbntv-schedule" in help_text)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")

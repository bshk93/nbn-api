"""`sweep_team_options` — § 6.1: team options not decided in their window are
declined, across the board.

A rookie-scale option's window closes at the fall deadline (October 31, or
the Monday after a weekend); every other team option's closes June 30. The
sweep runs hourly (`sweep_team_options.py`) and does two things:

- **Reminders.** At 48 and 24 hours before a window closes (league time),
  each team with an undecided option in it gets one inbox message naming them.
  Each goes once per close date, and after downtime only the most urgent one
  goes, not both at once.
- **Automatic decline.** Once a window has closed, every option in it still
  undecided is declined as a UFA hold and announced like an owner's own
  decline. Options whose window is still open are left alone.

Every write is stubbed: bios, ledger, Discord, inbox, the notices file.

    venv/bin/python -m tests.test_team_option_sweep
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as T  # noqa: E402
import routers.season_calendar as cal  # noqa: E402
from routers.league_time import LEAGUE_TZ  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


BIOS = {
    "r25": {"name": "ROOKIE, TWENTYFIVE", "draft_year": 2025, "draft_round": 1,
            "cap_holds": {"27-28": "TEAM_OPT", "28-29": "TEAM_OPT"}},
    "r24": {"name": "ROOKIE, TWENTYFOUR", "draft_year": 2024, "draft_round": 1,
            "cap_holds": {"27-28": "TEAM_OPT"}},
    "r26": {"name": "ROOKIE, TWENTYSIX", "draft_year": 2026, "draft_round": 1,
            "cap_holds": {"28-29": "TEAM_OPT"}},
    "second": {"name": "SECOND, ROUNDER", "draft_year": 2025, "draft_round": 2,
               "cap_holds": {"27-28": "TEAM_OPT"}},
    "gone": {"name": "NOT, ROSTERED", "draft_year": 2025, "draft_round": 1,
             "cap_holds": {"27-28": "TEAM_OPT"}},
}
TEAMS = {"r25": "PHX", "r24": "PHX", "r26": "BOS", "second": "BOS"}

SENT, APPLIED, LEDGER, POSTS = [], [], [], []


def apply_option(details, info):
    """Stand-in for _apply_option: records the call and clears the tag, so a
    later run sees the option as decided, like the real one."""
    APPLIED.append((details.player, details.year, details.cap_hold_type))
    BIOS[details.player]["cap_holds"][details.year] = details.cap_hold_type
    return TEAMS[details.player]


T.load_player_bios = lambda: BIOS
T._build_team_map = lambda: TEAMS
T._current_league_year = lambda: "26-27"
T.TEAM_OPTION_NOTICES_FILE = Path(tempfile.mkdtemp()) / "notices.json"
T.inbox.notify_team = lambda team, text, link=None: SENT.append((team, text))
T._apply_option = apply_option
T._append_transaction = lambda txn: LEDGER.append(txn)
T.notify_transaction = lambda txn, relay_to_roster_log=False: POSTS.append(("embed", relay_to_roster_log))
T.announce_roster_move = lambda txn: POSTS.append(("fa-news", txn["details"]["player"]))
# The second-rounder's window opens after the 26-27 regular season; only its
# June 30 close matters here, and that needs no schedule.
cal.regular_season_bounds = lambda season: ("2026-10-20", "2027-04-11") if season == "26-27" else None


def at(stamp):
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=LEAGUE_TZ)


print("which options are undecided, and when their windows close")
und = {(o["slug"], o["year"]): o for o in T._undecided_team_options(BIOS)}
check("rookie options close at the fall deadline",
      und[("r25", "27-28")]["closes"] == "2026-11-02" and und[("r24", "27-28")]["closes"] == "2026-11-02")
check("...a 2025 pick's 28-29 option at next fall's", und[("r25", "28-29")]["closes"] == "2027-11-01")
check("a second-rounder's closes June 30", und[("second", "27-28")]["closes"] == "2027-06-30")
check("a player on no roster isn't listed", not any(s == "gone" for s, _ in und))

print("\nreminders before the rookie deadline (Monday Nov 2, 2026, ending midnight ET)")
r = T.sweep_team_options(at("2026-10-30 12:00"))
check("nothing three days out", SENT == [] and r["declined"] == [])
T.sweep_team_options(at("2026-11-01 00:30"))
check("the 48-hour notice goes once per team with options", [t for t, _ in SENT] == ["PHX"])
check("...naming both players, their option years and the day",
      "Twentyfive Rookie (27-28)" in SENT[0][1] and "Twentyfour Rookie (27-28)" in SENT[0][1]
      and "Monday, Nov 2" in SENT[0][1] and "48 hours" in SENT[0][1])
T.sweep_team_options(at("2026-11-01 01:30"))
check("...and not again an hour later", len(SENT) == 1)
T.sweep_team_options(at("2026-11-02 00:30"))
check("the 24-hour notice goes next", len(SENT) == 2 and "24 hours" in SENT[1][1])
T.sweep_team_options(at("2026-11-02 12:00"))
check("...once", len(SENT) == 2)
check("nothing has been declined yet", APPLIED == [])

SENT.clear()
T.TEAM_OPTION_NOTICES_FILE.unlink()
T.sweep_team_options(at("2026-11-02 06:00"))
check("after downtime, a run inside 24 hours sends only the 24-hour notice",
      len(SENT) == 1 and "24 hours" in SENT[0][1])

print("\nafter the rookie deadline")
SENT.clear()
T.sweep_team_options(at("2026-11-03 00:15"))
check("every undecided rookie option due then is declined",
      sorted((p, y) for p, y, _ in APPLIED) == [("r24", "27-28"), ("r25", "27-28")])
check("...as a UFA hold", all(h == "UFA" for _, _, h in APPLIED))
check("...one ledger entry each, marked automatic",
      len(LEDGER) == 2 and all(t["details"]["_source"] == "auto_deadline" and t["created_by"] == "system"
                               for t in LEDGER))
check("...announced and relayed to #roster-log",
      POSTS.count(("embed", True)) == 2 and sum(1 for p in POSTS if p[0] == "fa-news") == 2)
check("...and the team told he's a UFA after this season",
      len(SENT) == 2 and all(t == "PHX" and "after this season" in x for t, x in SENT))
check("a 2025 pick's 28-29 option is left for next fall", ("r25", "28-29") not in [(p, y) for p, y, _ in APPLIED])
check("a 2026 pick's option is left too", "r26" not in [p for p, _, _ in APPLIED])
check("a second-rounder's option is left until June 30", "second" not in [p for p, _, _ in APPLIED])
n = len(APPLIED)
T.sweep_team_options(at("2026-11-03 01:15"))
check("the next hour's run declines nothing twice", len(APPLIED) == n)

print("\nother team options: reminders before June 30, decline at July 1")
SENT.clear()
T.sweep_team_options(at("2027-06-29 01:00"))
check("the 48-hour notice for a June 30 close",
      len(SENT) == 1 and SENT[0][0] == "BOS" and "Rounder Second (27-28)" in SENT[0][1] and "Wednesday, Jun 30" in SENT[0][1])
T._current_league_year = lambda: "27-28"   # the league year turns over on July 1
SENT.clear()
T.sweep_team_options(at("2027-07-01 00:30"))
check("on July 1 the undecided option is declined", ("second", "27-28", "UFA") in APPLIED)
check("...and the team told he's a UFA now", any(t == "BOS" and "free agent now" in x for t, x in SENT))
check("...its ledger entry names the June 30 close",
      any("June 30" in t["description"] and t["details"]["player"] == "second" for t in LEDGER))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

"""`sweep_rookie_team_options` — § 6.1's rookie-scale team option deadline.

The sweep runs hourly (`sweep_team_options.py`) and does two things:

- **Reminders.** At 48 and 24 hours before the deadline day ends (league
  time), each team with an undecided rookie-scale option gets one inbox
  message naming them. Each goes once a season, and after downtime only the
  most urgent one goes, not both at once.
- **Automatic decline.** Once the deadline has passed, every rookie-scale
  option still undecided is declined as a UFA hold and announced like an
  owner's own decline. Other team options, and next fall's rookie options,
  are left alone.

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
T.load_player_bios = lambda: BIOS
T._build_team_map = lambda: TEAMS
T._current_league_year = lambda: "26-27"
T.TEAM_OPTION_NOTICES_FILE = Path(tempfile.mkdtemp()) / "notices.json"
T.inbox.notify_team = lambda team, text, link=None: SENT.append((team, text))
T._apply_option = lambda details, info: APPLIED.append((details.player, details.year, details.cap_hold_type)) or TEAMS[details.player]
T._append_transaction = lambda txn: LEDGER.append(txn)
T.notify_transaction = lambda txn, relay_to_roster_log=False: POSTS.append(("embed", relay_to_roster_log))
T.announce_roster_move = lambda txn: POSTS.append(("fa-news", txn["details"]["player"]))


def at(stamp):
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=LEAGUE_TZ)


print("which options the deadline decides")
due = T._due_rookie_options("26-27", BIOS)
check("next season's rookie-scale options on a roster",
      sorted(s for s, _, _ in due) == ["r24", "r25"])
check("...for 27-28 only, not a 2025 pick's 28-29 option", {y for _, _, y in due} == {"27-28"})

print("\nreminders (deadline Monday Nov 2, 2026, ending midnight ET)")
r = T.sweep_rookie_team_options(at("2026-10-30 12:00"))
check("nothing three days out", SENT == [] and r["declined"] == [])
T.sweep_rookie_team_options(at("2026-11-01 00:30"))
check("the 48-hour notice goes once per team with options", [t for t, _ in SENT] == ["PHX"])
check("...naming both players and the day",
      "Twentyfive" in SENT[0][1] and "Twentyfour" in SENT[0][1] and "Monday, Nov 2" in SENT[0][1]
      and "48 hours" in SENT[0][1])
T.sweep_rookie_team_options(at("2026-11-01 01:30"))
check("...and not again an hour later", len(SENT) == 1)
T.sweep_rookie_team_options(at("2026-11-02 00:30"))
check("the 24-hour notice goes next", len(SENT) == 2 and "24 hours" in SENT[1][1])
T.sweep_rookie_team_options(at("2026-11-02 12:00"))
check("...once", len(SENT) == 2)
check("nothing has been declined yet", APPLIED == [])

SENT.clear()
T.TEAM_OPTION_NOTICES_FILE.unlink()
T.sweep_rookie_team_options(at("2026-11-02 06:00"))
check("after downtime, a run inside 24 hours sends only the 24-hour notice",
      len(SENT) == 1 and "24 hours" in SENT[0][1])

print("\nafter the deadline")
SENT.clear()
r = T.sweep_rookie_team_options(at("2026-11-03 00:15"))
check("every undecided rookie option is declined", sorted(p for p, _, _ in APPLIED) == ["r24", "r25"])
check("...as a UFA hold, for 27-28", all(y == "27-28" and h == "UFA" for _, y, h in APPLIED))
check("...one ledger entry each, marked automatic",
      len(LEDGER) == 2 and all(t["details"]["_source"] == "auto_deadline" and t["created_by"] == "system"
                               for t in LEDGER))
check("...announced and relayed to #roster-log",
      POSTS.count(("embed", True)) == 2 and sum(1 for p in POSTS if p[0] == "fa-news") == 2)
check("...and the team told", len(SENT) == 2 and all(t == "PHX" for t, _ in SENT))
check("a second-rounder's option is never touched", "second" not in [p for p, _, _ in APPLIED])
check("a 2026 pick's option isn't due until next fall", "r26" not in [p for p, _, _ in APPLIED])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

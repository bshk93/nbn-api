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
  undecided is declined (a rookie-scale one always as a UFA hold, § 6.1) and announced like an owner's own
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

from tests import _yos  # noqa: E402
_yos.pin()
import routers.transactions as T  # noqa: E402
REAL_APPLY_OPTION = T._apply_option
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
    later run sees the option as decided, like the real one. The sweep names
    no UFA/RFA, so it resolves the tag from § 3.1 the way the real one does."""
    details.cap_hold_type = details.cap_hold_type or T._option_fa_tag(BIOS[details.player], details.year)[0] or "UFA"
    APPLIED.append((details.player, details.year, details.cap_hold_type))
    BIOS[details.player]["cap_holds"][details.year] = details.cap_hold_type
    return TEAMS[details.player]


T.load_player_bios = lambda: BIOS
T._build_team_map = lambda: TEAMS
T._current_league_year = lambda: "26-27"
T.TEAM_OPTION_NOTICES_FILE = Path(tempfile.mkdtemp()) / "notices.json"
T.inbox.notify_team = lambda team, text, link=None, **_: SENT.append((team, text))
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
check("...as a UFA hold, though both are under 4 years of experience (§ 6.1)",
      all(h == "UFA" for _, _, h in APPLIED))
check("...one ledger entry each, marked automatic",
      len(LEDGER) == 2 and all(t["details"]["_source"] == "auto_deadline" and t["created_by"] == "system"
                               for t in LEDGER))
check("...announced and relayed to #roster-log",
      POSTS.count(("embed", True)) == 2 and sum(1 for p in POSTS if p[0] == "fa-news") == 2)
check("...and the team told he's an unrestricted free agent after this season",
      len(SENT) == 2 and all(t == "PHX" and "unrestricted free agent after this season" in x for t, x in SENT))
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
check("on July 1 the undecided option is declined", ("second", "27-28", "RFA") in APPLIED)
check("...and the team told he's a free agent now", any(t == "BOS" and "free agent now" in x for t, x in SENT))
check("...its ledger entry names the June 30 close",
      any("June 30" in t["description"] and t["details"]["player"] == "second" for t in LEDGER))

print("\na Full Bird decline with no EAPS on file is priced off this season's average salary")
# The real _apply_option this time: 2026-11-02 declined nothing but crashed,
# because a QVFA hold can't pick 150% vs 190% without EAPS and the sweep
# passed no answer. Average salary here is $10M, so $3M is below it: 190%.
import json as _json  # noqa: E402
_cl = Path(tempfile.mkdtemp()) / "cap-levels.json"
_cl.write_text(_json.dumps({"26-27": {"eaps": 0}, "27-28": {"eaps": 0}}))
T.CAP_LEVELS_FILE = _cl
T._apply_option = REAL_APPLY_OPTION
T.save_player_bios = lambda bios: None
T.log_write = lambda *a, **k: None
T._derive_bird_tier = lambda bio, team, season, slug=None: "QVFA"
T._current_league_year = lambda: "26-27"
BIOS["fb"] = {"name": "FULL, BIRD", "draft_year": 2024, "draft_round": 1, "type": "player",
              "salaries": {"25-26": "$2,900,000", "26-27": "$3,000,000", "27-28": "$5,000,000"},
              "cap_holds": {"27-28": "TEAM_OPT"}}
BIOS["vet"] = {"name": "AVERAGE, VET", "type": "player", "salaries": {"26-27": "$17,000,000"}}
TEAMS.update({"fb": "SAS", "vet": "SAS"})
LEDGER.clear()
r = T.sweep_team_options(at("2026-11-03 02:00"))
check("the sweep declines him rather than crashing", ("SAS", "fb", "27-28") in r["declined"])
check("...at 190% of his last salary", BIOS["fb"]["salaries"].get("27-28") == "$5,700,000"
      and BIOS["fb"]["cap_holds"] == {"27-28": "UFA"})
check("...noted as a placeholder", "below EAPS" in (BIOS["fb"].get("cap_hold_notes") or {}).get("27-28", ""))
check("...and the ledger says what was assumed",
      any(t["details"]["player"] == "fb" and t["details"]["eaps_assumption"] == "below" for t in LEDGER))

print("\n§ 3.1 decides UFA vs RFA on a decline")
BIOS["old"] = {"name": "OLD, VET", "draft_year": 2018, "type": "player",
               "salaries": {"26-27": "$4,000,000", "27-28": "$4,500,000"}, "cap_holds": {"27-28": "TEAM_OPT"}}
BIOS["rs"] = {"name": "ROOKIE, SCALE", "draft_year": 2023, "draft_round": 1, "draft_pick": 10}
BIOS["nobody"] = {"name": "NO, DRAFT"}
BIOS["r2"] = {"name": "SECOND, YEAR", "draft_year": 2024, "draft_round": 2, "cap_holds": {"27-28": "TEAM_OPT"}}
BIOS["rso"] = {"name": "ROOKIE, OPTION", "draft_year": 2024, "draft_round": 1, "draft_pick": 13,
               "cap_holds": {"27-28": "TEAM_OPT"}}
check("under 4 years: RFA", T._option_fa_tag(BIOS["r2"], "27-28")[0] == "RFA")
check("a declined rookie-scale option: UFA, though under 4 years (§ 6.1)",
      T._option_fa_tag(BIOS["rso"], "27-28")[0] == "UFA")
check("...and so is its 3rd-year option",
      T._option_fa_tag({**BIOS["rso"], "draft_year": 2025}, "27-28")[0] == "UFA")
check("4 or more: UFA", T._option_fa_tag(BIOS["old"], "27-28")[0] == "UFA")
check("finishing a first-round rookie deal: RFA despite 4 years", T._option_fa_tag(BIOS["rs"], "27-28")[0] == "RFA")
check("no experience on file: undecided", T._option_fa_tag(BIOS["nobody"], "27-28")[0] is None)
TEAMS["old"] = "SAS"
d = T.OptionDetails(player="old", decision="decline", option_type="TEAM_OPT", year="27-28",
                    eaps_assumption="below")
T._apply_option(d, {"name": "t"})
check("a decline that names no tag gets § 3.1's", BIOS["old"]["cap_holds"] == {"27-28": "UFA"} and d.cap_hold_type == "UFA")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

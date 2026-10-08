"""Releasing a two-way (§ 2.2, § 5.1).

A two-way's contract years pay $0, and `_release_contract_years` used to count
only years with salary. So a two-way could not be released ("nothing to
release, renounce instead") or renounced ("under contract, release instead").
It now counts a two-way's $0 years, and `_apply_release` writes no dead cap
row for them.

Nothing here touches the live data dir: bios, rosters and the deadcap CSV are
stubbed onto a temp directory, and so is the audit log (`write_csv` audits
every roster write, even one to a temp file).

    venv/bin/python -m tests.test_two_way_release
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routers.audit as audit  # noqa: E402
import routers.transactions as t  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


SEASON = "26-27"
TWO_WAY = {"name": "HINSON, BLAKE", "type": "two-way",
           "salaries": {"26-27": "$0", "27-28": "$0", "28-29": "$1"}, "cap_holds": {"28-29": "UFA"}}
LAPSED = {"name": "JONES, ISAAC", "type": "two-way",
          "salaries": {"26-27": "$1"}, "cap_holds": {"26-27": "RFA"}}
STANDARD_HOLD = {"name": "ONLY, HOLD", "type": "player",
                 "salaries": {"26-27": "$0"}, "cap_holds": {}}

print("_release_contract_years")
check("a two-way's $0 years are releasable",
      t._release_contract_years(TWO_WAY, SEASON) == ["26-27", "27-28"],
      t._release_contract_years(TWO_WAY, SEASON))
check("a lapsed two-way (hold only) still renounces instead",
      t._release_contract_years(LAPSED, SEASON) == [])
check("a standard player's $0 year is still not releasable",
      t._release_contract_years(STANDARD_HOLD, SEASON) == [])

print("_apply_release on a two-way")
tmp = Path(tempfile.mkdtemp())
(tmp / "phx-roster.csv").write_text("SLUG\nhinson-blake\nother-guy\n")
bios = {"hinson-blake": dict(TWO_WAY)}
saved = {}
t.DATA_DIR = tmp
audit.EDITS_FILE = tmp / "edits.jsonl"
t.load_player_bios = lambda: bios
t.save_player_bios = lambda b: saved.update(b)
t._build_team_map = lambda: {"hinson-blake": "PHX"}
t._season_for_date = lambda d: SEASON
t._scrub_trading_block = lambda *a, **k: None
t.log_write = lambda *a, **k: None

team, dead_cap, _, _ = t._apply_release(t.ReleaseDetails(player="hinson-blake"), "2026-10-09", {"name": "test"})
check("released from PHX", team == "PHX")
check("no dead cap", dead_cap == {}, dead_cap)
check("no deadcap CSV written", not (tmp / "phx-deadcap.csv").exists())
check("off the roster", "hinson-blake" not in (tmp / "phx-roster.csv").read_text())
check("bio cleared to a free agent", saved["hinson-blake"]["type"] == "" and saved["hinson-blake"]["cap_holds"] == {})

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

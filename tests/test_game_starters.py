"""Regression tests for routers/game_starters.py — who started each game.

What must keep being true:

  * the five recorded are the *entered* depth chart, never the saved one 2K
    hasn't seen (a team with edits waiting still started its entered five);
  * a team never entered is unknown — no guess from its saved order;
  * a starter with no minutes, or missing from the box, is flagged and not
    counted as a start;
  * a snapshot entered after the game's date is marked so;
  * the last line for a game wins, so re-recording replaces rather than
    double-counts;
  * recording never raises into the box score commit.

    venv/bin/python -m tests.test_game_starters
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.game_starters as gs  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# Never the live file.
gs.STARTERS_FILE = Path(tempfile.mkdtemp()) / "game-starters.jsonl"

FIVE = {s: {"slug": f"{s.lower()}-guy", "minutes": 30, "res": False} for s in gs.SLOTS}
SAVED = {**FIVE, "C": {"slug": "new-center", "minutes": 30, "res": False}}
COACHING = {
    # Entered, then edited: 2K has the entered five.
    "BOS": {"minutes": SAVED, "pending": True, "entered_at": "2026-10-20T22:00:00+00:00",
            "entered_values": {}, "entered_minutes": FIVE},
    # Entered before the snapshot existed and not edited since: current = entered.
    "DET": {"minutes": FIVE, "pending": False, "entered_at": "2026-10-21T15:00:00+00:00"},
    # Saved, never entered.
    "NYK": {"minutes": FIVE, "pending": True},
}
rows = lambda zero=(): [{"slug": f"{s.lower()}-guy", "min": 0 if s in zero else 30} for s in gs.SLOTS]

print("which five")
check("entered five, not the saved edit",
      gs.team_starters(COACHING["BOS"])["starters"][-1] == "c-guy")
check("an unedited old record stands in as entered",
      gs.team_starters(COACHING["DET"])["source"] == "entered")
check("never entered is unknown", gs.team_starters(COACHING["NYK"]) ["starters"] is None)
check("no record at all is unknown", gs.team_starters(None)["starters"] is None)
gap = {"minutes": {**FIVE, "SF": None}, "pending": False, "entered_at": "2026-10-20T22:00:00+00:00"}
check("an empty starting slot is unknown, not four starters", gs.team_starters(gap)["starters"] is None)

print("\none game")
e = gs.build_entry(date="2026-10-20", season="26-27", game_type="REG", home_team="DET", away_team="BOS",
                   home_rows=rows(), away_rows=rows(zero=("PF",))[:4], coaching=COACHING)
bos, det = e["teams"]["BOS"], e["teams"]["DET"]
check("starter missing from the box is flagged",
      {"slug": "c-guy", "issue": "not in box score"} in bos["flags"])
check("starter with no minutes is flagged",
      {"slug": "pf-guy", "issue": "no minutes"} in bos["flags"])
check("entered the evening of the game is not 'after'", bos["entered_after_game"] is False)
check("entered the next day (ET) is 'after'", det["entered_after_game"] is True)
check("not reconstructed unless asked", "reconstructed" not in e)

print("\nthe file")
gs.append(e)
e2 = gs.build_entry(date="2026-10-20", season="26-27", game_type="REG", home_team="DET", away_team="BOS",
                    home_rows=rows(), away_rows=rows(), coaching=COACHING)
gs.append(e2)
gs.append(gs.build_entry(date="2026-10-22", season="26-27", game_type="REG", home_team="NYK", away_team="BOS",
                         home_rows=rows(), away_rows=rows(), coaching=COACHING, reconstructed=True))
loaded = gs.load("26-27")
check("the last line for a game wins", len(loaded) == 2 and loaded[0]["teams"]["BOS"]["flags"] == [])
check("another season reads empty", gs.load("25-26") == [])
counts = gs.starts_by_player(loaded)
check("starts counted across games", counts["pg-guy"]["gs"] == 3)  # DET once, BOS twice
check("an unknown team adds no starts", counts["c-guy"]["gs"] == 3)
api = gs.get_start_counts(season="26-27", game_type="REG")
check("team games known skips the unknown team", api["team_games_known"] == {"DET": 1, "BOS": 2})

print("\nnever raises")
gs.load_coaching_settings = lambda: (_ for _ in ()).throw(OSError("disk"))
try:
    gs.record_game(date="2026-10-23", season="26-27", game_type="REG", home_team="BOS",
                   away_team="NYK", home_rows=[], away_rows=[])
    check("a read failure is swallowed", True)
except Exception:
    check("a read failure is swallowed", False)

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

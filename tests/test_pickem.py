"""Regression tests for routers/pickem.py — the daily six-man lineup game.

What must keep being true:

  * a slot takes only a player whose 2K primary or effective secondary is
    that position; the sixth man takes anyone playing that day;
  * a player can't fill two slots, and every slot must be filled;
  * scoring is DraftKings classic, with a triple-double earning both bonuses;
  * a lineup can't be saved or deleted once the day's first tip has passed;
  * nobody sees another member's lineup before the lock.

    venv/bin/python -m tests.test_pickem
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.pickem as pk  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# Never the live data dir.
pk.DATA_DIR = Path(tempfile.mkdtemp())

GAMES = [{"date": "2026-11-01", "away_team": "BOS", "home_team": "NYK", "time_et": "7:30p"},
         {"date": "2026-11-01", "away_team": "LAL", "home_team": "GSW", "time_et": "3:00p"}]
ROSTERS = {"BOS": ["pg-one", "c-one"], "NYK": ["sg-one", "sf-one"], "LAL": ["pf-one", "wing-one", "rights-guy"],
           "GSW": ["two-way-c"]}
BIOS = {
    "pg-one": {"name": "ONE, PG", "type": "player"},
    "sg-one": {"name": "ONE, SG", "type": "player"},
    "sf-one": {"name": "ONE, SF", "type": "player"},
    "pf-one": {"name": "ONE, PF", "type": "player"},
    "c-one": {"name": "ONE, C", "type": "player"},
    "wing-one": {"name": "ONE, WING", "type": "player"},
    "two-way-c": {"name": "TWOWAY, C", "type": "two-way", "pos": ["C"]},
    "rights-guy": {"name": "GUY, RIGHTS", "type": "draft-rights"},
}
PRIMARY = {"pg-one": "PG", "sg-one": "SG", "sf-one": "SF", "pf-one": "PF", "c-one": "C", "wing-one": "SF"}
SECONDARY = {"wing-one": "SG"}

pk.load_schedule = lambda season: {"games": GAMES}
pk._roster_slugs = lambda team: ROSTERS.get(team, [])
pk.load_player_bios = lambda: BIOS
pk.load_ovr = lambda: {}
pk._primary_pos = lambda slug: PRIMARY.get(slug)
pk.effective_secondary = lambda slug, bio: SECONDARY.get(slug)
pk.box_rows = lambda season: ROWS
ROWS: list[dict] = []
NOW = [datetime(2026, 11, 1, 12, 0, tzinfo=pk.LEAGUE_TZ)]
pk.league_now = lambda: NOW[0]
pk.log_write = lambda *a, **k: None

GOOD = {"PG": "pg-one", "SG": "sg-one", "SF": "sf-one", "PF": "pf-one", "C": "c-one", "6TH": "wing-one"}

print("the pool")
_, games = pk.slate_games("2026-11-01")
pool = pk.player_pool(games)
check("draft rights are left out", "rights-guy" not in pool)
check("two-ways are in", "two-way-c" in pool)
check("a player 2K doesn't carry falls back to his bio positions", pool["two-way-c"]["positions"] == ["C"])
check("primary and secondary both count", pool["wing-one"]["positions"] == ["SF", "SG"])

print("the lock is the first tip, in ET")
lock = pk.lock_at("2026-11-01", games)
check("3:00p, not 7:30p", (lock.hour, lock.minute) == (15, 0))

print("lineup rules")
check("a legal lineup passes", pk.check_lineup(GOOD, pool) == [])
check("the sixth man can be anyone", pk.check_lineup({**GOOD, "6TH": "two-way-c"}, pool) == [])
check("a secondary position fits", pk.check_lineup({**GOOD, "SG": "wing-one", "6TH": "sg-one"}, pool) == [])
check("an off-position player is refused", pk.check_lineup({**GOOD, "PG": "c-one", "C": "two-way-c"}, pool) != [])
check("one player in two slots is refused", pk.check_lineup({**GOOD, "6TH": "pg-one"}, pool) != [])
check("an empty slot is refused", pk.check_lineup({k: v for k, v in GOOD.items() if k != "C"}, pool) != [])
check("someone not playing today is refused", pk.check_lineup({**GOOD, "6TH": "nobody"}, pool) != [])

print("scoring")
check("plain line", pk.fantasy_points({"P": 20, "R": 5, "A": 4, "S": 1, "B": 0, "3PM": 2, "TO": 2}) == 20 + 6.25 + 6 + 2 + 1 - 1)
check("double-double bonus", pk.fantasy_points({"P": 10, "R": 10}) == 10 + 12.5 + 1.5)
check("triple-double earns both bonuses", pk.fantasy_points({"P": 10, "R": 10, "A": 10}) == 10 + 12.5 + 15 + 1.5 + 3)
check("blank cells are zero", pk.fantasy_points({"P": "", "R": "NA"}) == 0)

print("saving, and the lock")
pk.put_entry("2026-11-01", pk.EntryIn(lineup={k.lower(): v for k, v in GOOD.items()}), {"name": "alice"})
pk.put_entry("2026-11-01", pk.EntryIn(lineup=GOOD), {"name": "bob"})
check("saved", set(pk._load("26-27")["2026-11-01"]) == {"alice", "bob"})
day = pk.get_day("2026-11-01", None)
check("before the lock, a stranger sees no lineups", day["entries"] == [] and day["entry_count"] == 2)
pk._resolve_token = lambda h: {"name": "alice"} if h else None
day = pk.get_day("2026-11-01", "Bearer x")
check("before the lock, you see only your own", [e["member"] for e in day["entries"]] == ["alice"])

NOW[0] = datetime(2026, 11, 1, 15, 0, tzinfo=pk.LEAGUE_TZ)
try:
    pk.put_entry("2026-11-01", pk.EntryIn(lineup=GOOD), {"name": "alice"})
    check("saving after the lock is refused", False)
except HTTPException as e:
    check("saving after the lock is refused", e.status_code == 409)
try:
    pk.delete_entry("2026-11-01", {"name": "alice"})
    check("deleting after the lock is refused", False)
except HTTPException as e:
    check("deleting after the lock is refused", e.status_code == 409)

print("after the lock, scored from the box scores")
ROWS[:] = [{"DATE": "2026-11-01", "TEAM": "BOS", "PLAYER": "ONE, PG", "P": 30, "R": 0, "A": 10},
           {"DATE": "2026-11-01", "TEAM": "LAL", "PLAYER": "ONE, PF", "P": 10}]
day = pk.get_day("2026-11-01", None)
check("everyone's lineup is shown", {e["member"] for e in day["entries"]} == {"alice", "bob"})
check("scored", day["entries"][0]["score"]["total"] == 30 + 15 + 1.5 + 10)
check("not final while games are missing", day["final"] is False)
st = pk.standings("26-27")
check("season totals", [t["total"] for t in st["season_totals"]] == [56.5, 56.5])
check("a tie shares the day", st["days"][0]["winners"] == ["alice", "bob"])
check("weeks start on Monday", st["weeks"][0]["week_of"] == "2026-10-26")

print()
if FAILS:
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1)
print("all pickem checks passed")

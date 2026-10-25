"""Regression tests for routers/pickem.py — the daily six-man lineup game.

What must keep being true:

  * a slot takes only a player whose 2K primary or effective secondary is
    that position; the sixth man takes anyone playing that day;
  * a player can't fill two slots, and every slot must be filled;
  * scoring is DraftKings classic, with a triple-double earning both bonuses;
  * a day locks when the stream goes live, a box score or the VOD comes in,
    or the day ends, never at the schedule's NBA tip time; the lock sticks;
  * a lineup can't be saved or deleted once the day is locked;
  * a streamer can reopen a day only a stream locked; that stream can't relock
    it, and a later stream (the real games) still can;
  * nobody sees another member's lineup before the lock;
  * each final day pays its winners 100 and the season pays 1,000, every tied
    winner the full amount, never twice, and never for a day still in progress;
  * each payout is announced on Discord once: the day with its winning
    lineup, and the season winner.

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
from types import SimpleNamespace  # noqa: E402
from contextlib import nullcontext  # noqa: E402

# Never the real NB¥ ledger.
LEDGER: list[dict] = []
POSTS: list[dict] = []
pk._post_discord = POSTS.append   # never the real channel
pk.wallet = SimpleNamespace(enabled=lambda kind: True, lock=nullcontext(),
                            read_ledger=lambda: list(LEDGER),
                            post=lambda lines: LEDGER.extend(lines))

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# Never the live data dir.
pk.DATA_DIR = Path(tempfile.mkdtemp())
pk.LOCKS_FILE = pk.DATA_DIR / "pickem-locks.json"

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
pk._primary_pos = lambda slug, attrs=None, bio=None: PRIMARY.get(slug)
pk.effective_secondary = lambda slug, bio, attrs=None: SECONDARY.get(slug)
pk.load_attributes = lambda: {}
pk.box_rows = lambda season: ROWS
ROWS: list[dict] = []
NOW = [datetime(2026, 11, 1, 12, 0, tzinfo=pk.LEAGUE_TZ)]
pk.league_now = lambda: NOW[0]
pk.log_write = lambda *a, **k: None
OFF = {"live": False}
STRAY = {"live": True, "started_at": "2026-11-01T10:00:00Z", "title": "Testing OBS"}
GAMES_ON = {"live": True, "started_at": "2026-11-01T23:00:00Z", "title": "NBN Night 3"}
LIVE = [OFF]
pk.stream_status = lambda: LIVE[0]
STREAM_DAY = {}
pk._streaming_day = lambda date: STREAM_DAY

GOOD = {"PG": "pg-one", "SG": "sg-one", "SF": "sf-one", "PF": "pf-one", "C": "c-one", "6TH": "wing-one"}

print("the pool")
_, games = pk.slate_games("2026-11-01")
pool = pk.player_pool(games)
check("draft rights are left out", "rights-guy" not in pool)
check("two-ways are in", "two-way-c" in pool)
check("a player 2K doesn't carry falls back to his bio positions", pool["two-way-c"]["positions"] == ["C"])
check("primary and secondary both count", pool["wing-one"]["positions"] == ["SF", "SG"])

print("when a day locks")
check("open before the stream, even past the NBA tip time",
      [NOW.__setitem__(0, datetime(2026, 11, 1, 20, 0, tzinfo=pk.LEAGUE_TZ)), pk.lock_state("2026-11-01", [])][1] is None)
check("a future day is open", pk.lock_state("2026-11-02", []) is None)
check("a past day is locked", pk.lock_state("2026-10-31", [])["why"] == "day over")
check("a box score for the day locks it", pk.lock_state("2026-11-01", [{"DATE": "2026-11-01"}])["why"] == "boxscore")
pk.LOCKS_FILE.unlink()
STREAM_DAY.update(youtube_url="https://youtu.be/x")
check("the day's VOD locks it", pk.lock_state("2026-11-01", [])["why"] == "vod")
pk.LOCKS_FILE.unlink()
STREAM_DAY.clear()
NOW[0] = datetime(2026, 11, 1, 12, 0, tzinfo=pk.LEAGUE_TZ)

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
check("before the lock, anyone sees who is in", day["entrants"] == ["alice", "bob"])
pk._resolve_token = lambda h: {"name": "alice"} if h else None
day = pk.get_day("2026-11-01", "Bearer x")
check("before the lock, you see only your own", [e["member"] for e in day["entries"]] == ["alice"])

print("the stream banner stamps the lock with nobody on Pick'em")
pk.note_stream_live(GAMES_ON)
check("stamped", pk.lock_state("2026-11-01", [])["why"] == "stream")
pk.LOCKS_FILE.unlink()

print("last night's stream running past midnight doesn't lock today")
LATE = {"live": True, "started_at": "2026-11-01T03:10:00Z", "title": "NBN 10/31"}   # 11:10pm ET on 10/31
NOW[0] = datetime(2026, 11, 1, 0, 1, tzinfo=pk.LEAGUE_TZ)
pk.note_stream_live(LATE)
LIVE[0] = LATE
check("not locked by the banner", pk.lock_state("2026-11-01", []) is None)
LIVE[0] = OFF

LIVE[0] = GAMES_ON
NOW[0] = datetime(2026, 11, 1, 13, 5, tzinfo=pk.LEAGUE_TZ)
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
LIVE[0] = OFF
check("the lock sticks after the stream ends", pk.lock_state("2026-11-01", [])["why"] == "stream")
check("and keeps when it happened", pk.lock_state("2026-11-01", [])["at"].startswith("2026-11-01T13:05"))

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

print("season averages, to pick by")
avg = pk.season_averages([{"DATE": "2026-10-30", "PLAYER": "ONE, PG", "M": 30, "P": 20},
                          {"DATE": "2026-10-31", "PLAYER": "ONE, PG", "M": 30, "P": 10},
                          {"DATE": "2026-10-31", "PLAYER": "ONE, C", "M": 0, "P": 0},
                          {"DATE": "2026-11-01", "PLAYER": "ONE, PG", "M": 30, "P": 90}], BIOS, "2026-11-01")
check("per game, from games before the day only", avg["pg-one"] == (15.0, 2))
check("a DNP isn't a game", "c-one" not in avg)

print("reopening a day a stray stream locked")
ROWS.clear()   # no box scores yet: those would lock the day for real
pk.LOCKS_FILE.unlink()
NOW[0] = datetime(2026, 11, 1, 6, 17, tzinfo=pk.LEAGUE_TZ)
LIVE[0] = STRAY
pk.note_stream_live(STRAY)
check("a stray stream locks the day", pk.lock_state("2026-11-01", [])["why"] == "stream")
check("and the lock names the stream", pk.lock_state("2026-11-01", [])["stream"]["title"] == "Testing OBS")
pk.reopen("2026-11-01", "streamy")
check("reopened", pk.lock_state("2026-11-01", []) is None)
pk.note_stream_live(STRAY)
check("the same stream, still live, can't relock it", pk.lock_state("2026-11-01", []) is None)
pk.put_entry("2026-11-01", pk.EntryIn(lineup=GOOD), {"name": "carol"})
check("lineups can be saved again", "carol" in pk._load("26-27")["2026-11-01"])
day = pk.get_day("2026-11-01", None)
check("the page says who reopened it", day["reopened"][0]["by"] == "streamy")
LIVE[0] = GAMES_ON
pk.note_stream_live(GAMES_ON)
check("the real games' stream still locks it", pk.lock_state("2026-11-01", [])["why"] == "stream")
pk.LOCKS_FILE.unlink()
LIVE[0] = OFF
pk.lock_state("2026-11-01", [{"DATE": "2026-11-01"}])
try:
    pk.reopen("2026-11-01", "streamy")
    check("a box score lock can't be reopened", False)
except HTTPException as e:
    check("a box score lock can't be reopened", e.status_code == 409)
pk.LOCKS_FILE.unlink()
try:
    pk.reopen("2026-11-01", "streamy")
    check("an open day can't be reopened", False)
except HTTPException as e:
    check("an open day can't be reopened", e.status_code == 409)

print("prizes")
pk.LOCKS_FILE.unlink(missing_ok=True)
pk._save("26-27", {})
NOW[0] = datetime(2026, 11, 2, 9, 0, tzinfo=pk.LEAGUE_TZ)   # the day after
LIVE[0] = OFF
pk._save("26-27", {"2026-11-01": {
    "alice": {"lineup": GOOD}, "bob": {"lineup": GOOD},
    "carol": {"lineup": {**GOOD, "PG": "wing-one", "6TH": "two-way-c"}}}})
ROWS[:] = [{"DATE": "2026-11-01", "TEAM": "BOS", "PLAYER": "ONE, PG", "P": 30}]
GAMES.append({"date": "2026-11-05", "away_team": "BOS", "home_team": "LAL", "time_et": ""})   # season not over
check("a day still missing box scores pays nothing", pk.settle("26-27") == [])
ROWS[:] = [{"DATE": "2026-11-01", "TEAM": t, "PLAYER": "ONE, PG" if t == "BOS" else "X", "P": 30}
           for t in ("BOS", "NYK", "LAL", "GSW")]
paid = pk.settle("26-27")
check("a final day pays each tied winner the full 100",
      sorted((p["member"], p["delta"]) for p in paid) == [("alice", 100), ("bob", 100)])
check("never twice", pk.settle("26-27") == [])
check("the day is announced once", len(POSTS) == 1 and "Pick'em" in POSTS[0]["title"])
check("naming both tied winners and the prize", "**alice** and **bob** tie" in POSTS[0]["description"]
      and "NB¥100 each" in POSTS[0]["description"])
check("with the winning lineup, names cased", "PG Pg One 30.0" in POSTS[0]["description"])
check("and the runner-up", "Next: carol" in POSTS[0]["description"])
check("the season isn't over while games are left", not any("season" in p["ref"] for p in LEDGER))
GAMES[:] = [g for g in GAMES if g["date"] <= "2026-11-01"]
paid = pk.settle("26-27")
check("the season pays its winners 1,000 once its last day is final",
      sorted((p["member"], p["delta"]) for p in paid) == [("alice", 1000), ("bob", 1000)])
check("the season pays once", pk.settle("26-27") == [])
check("the season winner is announced once", len(POSTS) == 2 and "season champion" in POSTS[1]["title"]
      and "NB¥1,000 each" in POSTS[1]["description"])
check("nothing more is posted", pk.settle("26-27") == [] and len(POSTS) == 2)
LEDGER.clear()
pk.wallet.enabled = lambda kind: False
check("nothing pays while Pick'em NB¥ is paused", pk.settle("26-27") == [] and LEDGER == [])

print()
if FAILS:
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1)
print("all pickem checks passed")

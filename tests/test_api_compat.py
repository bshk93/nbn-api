"""Backward compatibility for the shared-read change (home-team-bundle).

Every endpoint and helper that now reads player-bios.json / ovr-history.json
through the shared parse (`player_bios_shared`, `ovr_shared`) must answer
exactly what it answered when it loaded the file itself. Each check computes
the response twice, the default way and with the old loader forced back in,
and requires them equal. Also pins the defaults of the helpers that gained
optional parameters (`build_rows(teams=, bios=)`, `_roster_augmented(bios=,
ovr_history=)`, `_bios_by_name(bios=)`), the season-lines response, and what
`/api/me`'s last-seen record writes.

Reads DATA_DIR like the rest of the suite (live data in nbn-api-dev).

    venv/bin/python -m tests.test_api_compat
"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import auth, boxscores, cap_history, discord, players  # noqa: E402
from routers.constants import OVR_FILE, PLAYER_BIOS_FILE, VALID_TEAMS  # noqa: E402
from routers.storage import _load_json  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def same(a, b):
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


@contextmanager
def old_loaders():
    """Every shared accessor swapped back for a fresh `_load_json`, as before."""
    fresh_bios = lambda: _load_json(PLAYER_BIOS_FILE, {})
    fresh_ovr = lambda: _load_json(OVR_FILE, {})
    saved = [(m, n, getattr(m, n)) for m, n in [
        (players, "player_bios_shared"), (boxscores, "player_bios_shared"),
        (discord, "player_bios_shared"), (discord, "ovr_shared"), (cap_history, "player_bios_shared")]]
    for m, n, _ in saved:
        setattr(m, n, fresh_ovr if n == "ovr_shared" else fresh_bios)
    boxscores._NAME_INDEX[:] = [None, None]
    try:
        yield
    finally:
        for m, n, v in saved:
            setattr(m, n, v)
        boxscores._NAME_INDEX[:] = [None, None]


bios_now = _load_json(PLAYER_BIOS_FILE, {})
some_slugs = sorted(bios_now)[:5] + ["no-such-player"]
season = boxscores._current_league_year()
teams = sorted(VALID_TEAMS)

new, old = {}, {}
for store, ctx in ((new, nullcontext), (old, old_loaders)):
    with ctx():
        store["players"] = players.get_players()
        store["players?slugs"] = players.get_players(slugs=",".join(some_slugs))
        store["names"] = boxscores._bios_by_name()
        store["gamelog"] = {s: (boxscores.get_player_gamelog(s) if s in bios_now else None) for s in some_slugs[:5]}
        store["season-lines"] = {t: boxscores.get_team_season_lines(t, season=season) for t in teams}
        store["roster"] = {t: discord._roster_augmented(t) for t in teams}
        store["five"] = {t: discord._starting_five(t, discord._roster_augmented(t)) for t in teams}
        store["cap"] = cap_history.build_rows(season=season)

print("GET /api/players")
check("whole file, unchanged", same(new["players"], old["players"]) and same(new["players"], bios_now))
check("?slugs= filter, unchanged", same(new["players?slugs"], old["players?slugs"]))
print("box score name matching")
check("_bios_by_name()", same(new["names"], old["names"]))
check("gamelog", same(new["gamelog"], old["gamelog"]))
print("GET /api/teams/{team}/season-lines, every team")
check("unchanged", same(new["season-lines"], old["season-lines"]))
print("discord /roster and /lineups inputs, every team")
check("_roster_augmented", same(new["roster"], old["roster"]))
check("_starting_five", same(new["five"], old["five"]))
print("cap history rows (GET /api/cap-history/current, the daily snapshot)")
check("build_rows() unchanged", same(new["cap"], old["cap"]))
check("default is all 30 teams", sorted(r["team"] for r in new["cap"]) == teams)
by_team = {r["team"]: r for r in new["cap"]}
check("teams=[t] is exactly t's row", all(
    same(cap_history.build_rows(season=season, teams=[t]), [by_team[t]]) for t in teams[:6]))

print("a write to player-bios.json is seen by the next read")
tmp = Path(tempfile.mkdtemp()) / "player-bios.json"
tmp.write_text(json.dumps({"a-b": {"name": "B, A"}}))
saved_file = players.PLAYER_BIOS_FILE
players.PLAYER_BIOS_FILE = tmp
try:
    first = players.get_players()
    tmp.write_text(json.dumps({"a-b": {"name": "B, A"}, "c-d": {"name": "D, C"}}))
    check("new player appears", set(players.get_players()) == {"a-b", "c-d"} and set(first) == {"a-b"})
finally:
    players.PLAYER_BIOS_FILE = saved_file

print("/api/me's last-seen record writes what it always wrote")
seen = Path(tempfile.mkdtemp()) / "member-seen.json"
saved_seen = auth.MEMBER_SEEN_FILE
auth.MEMBER_SEEN_FILE = seen
try:
    auth._record_member_seen("a", "1.1.1.1", "UA")
    auth._record_member_seen("a", "1.1.1.1", "UA")
    auth._record_member_seen("b", None, None)          # first sighting, nothing to record
    auth._record_member_seen("a", "2.2.2.2", None)
    check("same file as before", json.loads(seen.read_text()) == {
        "a": {"ips": ["1.1.1.1", "2.2.2.2"], "user_agents": ["UA"]}, "b": {}})
finally:
    auth.MEMBER_SEEN_FILE = saved_seen

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

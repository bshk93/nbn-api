"""`GET /api/home/team/{team}` — the homepage team card in one request.

The signed-in homepage card used to make about a dozen requests (schedule,
standings, results, rotations, OVR, roster, bios, season lines, injuries, cap
history, and the opponent's lineup for the Matchup tab). Each is quick on the
server; over HTTP/1.1 the browser runs six at a time, and the card waited in
that queue behind the rest of the page. This answers for all of them at once.

Public: everything here is already public through the endpoints it bundles.
The private chips (coaching settings, waivers, FA offers) stay separate.

It reuses each source's own helper and adds no rule of its own:

| Part | From |
|---|---|
| `games`, `schedule_first/last` | `schedule._load` / `_public` (shared parse) |
| `results` | the box score game index (`boxscores._GAME_INDEX_CACHE`) |
| `standings` | `standings-history.csv`, as misc.py and discord.py read it |
| `cap` | `cap_history.build_rows(teams=[team])` |
| `injuries` | `injuries.load_injuries` + `_with_names` |
| `five` | `discord._roster_augmented` + `_starting_five` (the port of lineup.js) |
| `lines` | `boxscores.team_season_lines` |

Bios and OVR are read once, shared (`player_bios_shared` / `ovr_shared`), and
handed to every helper that takes them, so one request parses neither file.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import boxscores, cap_history, discord, injuries, schedule
from .constants import DERIVED_DIR, VALID_TEAMS
from .players import ovr_shared, player_bios_shared
from .league_time import league_today_str
from .storage import _current_league_year, _load_json_shared, read_csv

router = APIRouter()

STANDINGS_CSV = DERIVED_DIR / "standings" / "standings-history.csv"


def _five(team: str, bios: dict, ovr: dict) -> dict:
    """The starting five, as the site's `startersFor` returns it: the team's
    saved rotation, else the best legal five by OVR. Each slot is a player or
    None, PG to C."""
    players = discord._roster_augmented(team, bios=bios, ovr_history=ovr)
    five, from_rotation = discord._starting_five(team, players)
    return {
        "set": from_rotation,
        "players": [None if p is None else {
            "slug": p["slug"], "name": p["name_raw"], "pos": p["pos_list"],
            "ovr": p["ovr"], "type": p["type"],
        } for p in five],
    }


def _results(team: str, season: str) -> list[dict]:
    """This season's finished games for `team`, newest first — what
    `GET /api/boxscores/games?team=&season=` returns, off the same cache."""
    games = boxscores.get_boxscore_games(season=season, team=team)
    return sorted(games, key=lambda g: g["date"], reverse=True)


def _standings(team: str, season: str) -> list[dict]:
    """This season's rows for every team (conference rank, opponents'
    records) plus every row of `team`'s own (last season's record before this
    one has a row)."""
    if not STANDINGS_CSV.exists():
        return []
    _, rows = read_csv(STANDINGS_CSV)
    return [r for r in rows if r.get("SEASON") == season or r.get("TEAM") == team]


@router.get("/api/home/team/{team}")
def get_home_team(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team {team!r}")
    season = _current_league_year()
    today = league_today_str()
    bios = player_bios_shared()
    ovr = ovr_shared()

    sched = _load_json_shared(schedule._schedule_path(season), {"games": []})
    all_dates = sorted(g["date"] for g in sched.get("games", []))
    games = [schedule._public(g) for g in sched.get("games", [])
             if team in (g["home_team"], g["away_team"])]

    # The Matchup tab's opponent: whoever this team plays next.
    upcoming = sorted((g for g in games if g["date"] >= today), key=lambda g: g["date"])
    opponent = None
    if upcoming:
        nxt = upcoming[0]
        opp = nxt["away_team"] if nxt["home_team"] == team else nxt["home_team"]
        opponent = {"team": opp, "five": _five(opp, bios, ovr),
                    "lines": boxscores.team_season_lines(opp, season, bios)}

    cap_rows = cap_history.build_rows(season=season, teams=[team], bios=bios)
    inj = injuries.load_injuries()
    inj_rows = injuries._with_names(
        [p for p in inj["players"].values() if (p.get("team") or "").upper() == team], bios)

    return {
        "team": team,
        "season": season,
        "schedule_first": all_dates[0] if all_dates else None,
        "schedule_last": all_dates[-1] if all_dates else None,
        "games": games,
        "results": _results(team, season),
        "standings": _standings(team, season),
        "cap": cap_rows[0] if cap_rows else None,
        "injuries": {"last_report": inj["last_report"], "players": inj_rows},
        "five": _five(team, bios, ovr),
        "lines": boxscores.team_season_lines(team, season, bios),
        "opponent": opponent,
    }

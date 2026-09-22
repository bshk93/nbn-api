"""NBNFL — the sister American-football league. Deliberately much smaller than
the rest of this API: one file (`NBNFL_FILE`, see constants.py), no roster or
contract model, no build pipeline. A game is entered once, after it's been
played, with its final score and whatever player stat lines go with it;
standings and stat leaders are computed from that on every request rather than
written anywhere, since there's little enough data that recomputing is cheap
and a stored copy would just be one more place a correction could be missed.

Stat lines are deliberately schema-blind on the wire (`stats: {field: value}`
rather than fixed pydantic fields per category) — same call as
routers/coaching_settings.py's config blob, for the same reason: which fields
matter per category is a frontend-form decision, not a backend one, so a new
stat column is a `nbnfl/index.html`-only change.

Regular season only — no playoff bracket yet. When the postseason is added,
model it as its own endpoint/shape rather than overloading `week`; do not
guess a "week 19+" convention here first.
"""
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .constants import NBNFL_FILE, _nbnfl_lock
from .storage import _load_json, _save_json, log_write
from .auth import require_admin

router = APIRouter()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

NFL_TEAMS = {
    "BUF": {"name": "Bills",       "conference": "AFC", "division": "East"},
    "MIA": {"name": "Dolphins",    "conference": "AFC", "division": "East"},
    "NE":  {"name": "Patriots",    "conference": "AFC", "division": "East"},
    "NYJ": {"name": "Jets",        "conference": "AFC", "division": "East"},
    "BAL": {"name": "Ravens",      "conference": "AFC", "division": "North"},
    "CIN": {"name": "Bengals",     "conference": "AFC", "division": "North"},
    "CLE": {"name": "Browns",      "conference": "AFC", "division": "North"},
    "PIT": {"name": "Steelers",    "conference": "AFC", "division": "North"},
    "HOU": {"name": "Texans",      "conference": "AFC", "division": "South"},
    "IND": {"name": "Colts",       "conference": "AFC", "division": "South"},
    "JAX": {"name": "Jaguars",     "conference": "AFC", "division": "South"},
    "TEN": {"name": "Titans",      "conference": "AFC", "division": "South"},
    "DEN": {"name": "Broncos",     "conference": "AFC", "division": "West"},
    "KC":  {"name": "Chiefs",      "conference": "AFC", "division": "West"},
    "LV":  {"name": "Raiders",     "conference": "AFC", "division": "West"},
    "LAC": {"name": "Chargers",    "conference": "AFC", "division": "West"},
    "DAL": {"name": "Cowboys",     "conference": "NFC", "division": "East"},
    "NYG": {"name": "Giants",      "conference": "NFC", "division": "East"},
    "PHI": {"name": "Eagles",      "conference": "NFC", "division": "East"},
    "WAS": {"name": "Commanders",  "conference": "NFC", "division": "East"},
    "CHI": {"name": "Bears",       "conference": "NFC", "division": "North"},
    "DET": {"name": "Lions",       "conference": "NFC", "division": "North"},
    "GB":  {"name": "Packers",     "conference": "NFC", "division": "North"},
    "MIN": {"name": "Vikings",     "conference": "NFC", "division": "North"},
    "ATL": {"name": "Falcons",     "conference": "NFC", "division": "South"},
    "CAR": {"name": "Panthers",    "conference": "NFC", "division": "South"},
    "NO":  {"name": "Saints",      "conference": "NFC", "division": "South"},
    "TB":  {"name": "Buccaneers",  "conference": "NFC", "division": "South"},
    "ARI": {"name": "Cardinals",   "conference": "NFC", "division": "West"},
    "LAR": {"name": "Rams",        "conference": "NFC", "division": "West"},
    "SF":  {"name": "49ers",       "conference": "NFC", "division": "West"},
    "SEA": {"name": "Seahawks",    "conference": "NFC", "division": "West"},
}

# The stat a category's leaderboard ranks on, and what a stat line's `stats`
# dict is summed over per player. Other fields in a stat line (e.g. passing's
# comp/att/int) are stored and returned but don't drive a leaderboard.
PRIMARY_STAT = {
    "passing": "yds",
    "rushing": "yds",
    "receiving": "yds",
    "defense": "sack",
    "kicking": "fgm",
    "kick_returns": "yds",
    "punt_returns": "yds",
}


class StatLine(BaseModel):
    player: str
    team: str
    category: str
    stats: dict[str, float] = {}


class GameIn(BaseModel):
    season: int
    week: int
    date: str
    home: str
    away: str
    home_score: int
    away_score: int
    stats: list[StatLine] = []


class GamePatch(BaseModel):
    season: Optional[int] = None
    week: Optional[int] = None
    date: Optional[str] = None
    home: Optional[str] = None
    away: Optional[str] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    stats: Optional[list[StatLine]] = None


def _load() -> dict:
    return _load_json(NBNFL_FILE, {"games": []})


def _save(data: dict):
    _save_json(NBNFL_FILE, data)


def _new_id(games: list) -> str:
    existing = {g["id"] for g in games}
    gid = uuid.uuid4().hex[:8]
    while gid in existing:
        gid = uuid.uuid4().hex[:8]
    return gid


def _check_game(home: str, away: str, date: str, home_score: int, away_score: int, stats: list[StatLine]):
    if home not in NFL_TEAMS or away not in NFL_TEAMS:
        raise HTTPException(status_code=400, detail="home/away must be valid NFL team abbreviations")
    if home == away:
        raise HTTPException(status_code=400, detail="home and away must be different teams")
    if not DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if home_score < 0 or away_score < 0:
        raise HTTPException(status_code=400, detail="scores cannot be negative")
    for line in stats:
        if line.team not in (home, away):
            raise HTTPException(status_code=400, detail=f"stat line team {line.team!r} is not playing in this game")
        if line.category not in PRIMARY_STAT:
            raise HTTPException(status_code=400, detail=f"unknown stat category {line.category!r}")


@router.get("/api/nbnfl/teams")
def list_teams():
    return {"teams": [{"abbr": abbr, **info} for abbr, info in NFL_TEAMS.items()]}


@router.get("/api/nbnfl/games")
def list_games(season: Optional[int] = None, week: Optional[int] = None, team: Optional[str] = None):
    games = _load()["games"]
    if season is not None:
        games = [g for g in games if g["season"] == season]
    if week is not None:
        games = [g for g in games if g["week"] == week]
    if team is not None:
        team = team.upper()
        games = [g for g in games if g["home"] == team or g["away"] == team]
    games.sort(key=lambda g: (g["season"], g["week"], g["date"]))
    return games


@router.post("/api/nbnfl/games")
def add_game(game: GameIn, info: dict = Depends(require_admin)):
    _check_game(game.home, game.away, game.date, game.home_score, game.away_score, game.stats)
    row = game.model_dump()
    with _nbnfl_lock:
        data = _load()
        row["id"] = _new_id(data["games"])
        data["games"].append(row)
        _save(data)
    log_write(info, f"POST nbnfl/games — {game.away} at {game.home}, week {game.week} ({game.season})")
    return row


@router.put("/api/nbnfl/games/{game_id}")
def edit_game(game_id: str, patch: GamePatch, info: dict = Depends(require_admin)):
    with _nbnfl_lock:
        data = _load()
        idx = next((i for i, g in enumerate(data["games"]) if g["id"] == game_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="No such game")
        merged = {**data["games"][idx], **patch.model_dump(exclude_unset=True, exclude_none=True)}
        _check_game(merged["home"], merged["away"], merged["date"], merged["home_score"],
                    merged["away_score"], [StatLine(**s) for s in merged["stats"]])
        merged["id"] = game_id
        data["games"][idx] = merged
        _save(data)
    log_write(info, f"PUT nbnfl/games/{game_id}")
    return merged


@router.delete("/api/nbnfl/games/{game_id}")
def delete_game(game_id: str, info: dict = Depends(require_admin)):
    with _nbnfl_lock:
        data = _load()
        before = len(data["games"])
        data["games"] = [g for g in data["games"] if g["id"] != game_id]
        if len(data["games"]) == before:
            raise HTTPException(status_code=404, detail="No such game")
        _save(data)
    log_write(info, f"DELETE nbnfl/games/{game_id}")
    return {"deleted": game_id}


@router.get("/api/nbnfl/standings")
def standings():
    record = {
        abbr: {"abbr": abbr, "name": info["name"], "wins": 0, "losses": 0, "ties": 0, "pf": 0, "pa": 0}
        for abbr, info in NFL_TEAMS.items()
    }
    for g in _load()["games"]:
        home, away, hs, aws = g["home"], g["away"], g["home_score"], g["away_score"]
        if home not in record or away not in record:
            continue  # a game entered before a since-corrected team typo — skip rather than crash the board
        record[home]["pf"] += hs
        record[home]["pa"] += aws
        record[away]["pf"] += aws
        record[away]["pa"] += hs
        if hs > aws:
            record[home]["wins"] += 1
            record[away]["losses"] += 1
        elif aws > hs:
            record[away]["wins"] += 1
            record[home]["losses"] += 1
        else:
            record[home]["ties"] += 1
            record[away]["ties"] += 1

    for row in record.values():
        row["diff"] = row["pf"] - row["pa"]

    def sort_key(row):
        return (-row["wins"], -row["diff"])

    out = {}
    for abbr, info in NFL_TEAMS.items():
        conf = out.setdefault(info["conference"], {})
        conf.setdefault(info["division"], []).append(record[abbr])
    for conf in out.values():
        for division_rows in conf.values():
            division_rows.sort(key=sort_key)
    return out


@router.get("/api/nbnfl/leaders")
def leaders(limit: int = Query(5, ge=1, le=50)):
    totals: dict[tuple[str, str, str], float] = {}
    for g in _load()["games"]:
        for line in g.get("stats", []):
            category = line["category"]
            primary = PRIMARY_STAT.get(category)
            if primary is None:
                continue
            key = (category, line["player"], line["team"])
            totals[key] = totals.get(key, 0) + (line.get("stats", {}).get(primary) or 0)

    out = {cat: [] for cat in PRIMARY_STAT}
    for (category, player, team), value in totals.items():
        out[category].append({"player": player, "team": team, "value": value})
    for category in out:
        out[category].sort(key=lambda r: -r["value"])
        out[category] = out[category][:limit]
    return out

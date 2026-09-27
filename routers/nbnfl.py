"""NBNFL — a stat log for one team in the sister American-football league.
Deliberately much smaller than the rest of this API: one file (`NBNFL_FILE`,
see constants.py), no roster or contract model, no build pipeline.

It tracks `MY_TEAM` only (changed 2026-09-27; it launched as a league-wide
board with standings and leaders). Every game must involve that team, and
every stat line must be one of its players — the opponent is just a name and a
score. Records and season totals are computed by the page from the game list.

Stat lines are deliberately schema-blind on the wire (`stats: {field: value}`
rather than fixed pydantic fields per category) — same call as
routers/coaching_settings.py's config blob, for the same reason: which fields
matter per category is a frontend-form decision, not a backend one, so a new
stat column is a `nbnfl/index.html`-only change.

Regular season only. If playoff games are ever logged, model them as their
own field rather than overloading `week` with a "week 19+" convention.
"""
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import NBNFL_FILE, _nbnfl_lock
from .storage import _load_json, _save_json, log_write
from .auth import require_admin

router = APIRouter()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The one team whose games and stats are logged.
MY_TEAM = "CIN"

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

# Stat categories a line may use. Which fields each one carries is the page's
# call (see the module docstring), so this is only the set of names.
CATEGORIES = {"passing", "rushing", "receiving", "kicking", "kick_returns", "punt_returns"}


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
    if MY_TEAM not in (home, away):
        raise HTTPException(status_code=400, detail=f"only {MY_TEAM} games are logged")
    if not DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if home_score < 0 or away_score < 0:
        raise HTTPException(status_code=400, detail="scores cannot be negative")
    for line in stats:
        if line.team != MY_TEAM:
            raise HTTPException(status_code=400, detail=f"only {MY_TEAM} stat lines are logged, not {line.team!r}")
        if line.category not in CATEGORIES:
            raise HTTPException(status_code=400, detail=f"unknown stat category {line.category!r}")


@router.get("/api/nbnfl/teams")
def list_teams():
    return {"my_team": MY_TEAM, "teams": [{"abbr": abbr, **info} for abbr, info in NFL_TEAMS.items()]}


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

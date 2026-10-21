"""Who started each game — `game-starters.jsonl`, and `GET /api/game-starters`.

2K's box score screen lists players by points, not starters first, so the raw
box scores can't say who started. The coaching settings can: when a streamer
marks a team's settings entered (`POST /api/coaching-settings/{team}/enter`),
the record keeps the depth chart as entered (`entered_minutes`). Its PG–C slots
are the five 2K started. So when a box score is committed, `record_game` reads
each team's entered five and appends one line here.

What a line can and can't vouch for:

  * `source: "entered"` — the five as a streamer entered them. Trusted.
  * `source: null`, `starters: null` — nothing was ever entered for this team,
    so 2K used its own default lineup and nobody knows it. Never guessed from
    the team's saved-but-unentered order, which 2K never saw.
  * `flags` — a recorded starter who played no minutes in the box score (hurt,
    or 2K swapped him out), or who isn't in the box score at all. The line is
    kept as recorded; a reader decides what to do with a flagged starter.
  * `entered_after_game` — the snapshot was entered later than the game's
    date. Usually the same lineup (the box score is committed late, after
    tonight's settings went in), but not certainly.
  * `reconstructed: true` — written by `backfill_game_starters.py` for a game
    committed before this file existed, from the entered snapshot as it stood
    then. Opening week only.

Its own append-only file rather than a column on the raw box scores, which are
fixed-shape and guarded (`allstats_guard.py`). Re-recording a game appends a
new line; the last line for a game wins (`load`).

    venv/bin/python -m tests.test_game_starters
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from .constants import DATA_DIR, logger
from .coaching_settings import load_coaching_settings, _entered_snapshot
from .storage import _current_league_year

router = APIRouter()

STARTERS_FILE = DATA_DIR / "game-starters.jsonl"
SLOTS = ("PG", "SG", "SF", "PF", "C")
_ET = ZoneInfo("America/New_York")


def _slot_slug(v) -> Optional[str]:
    # A slot is {slug, minutes, res}; tolerate a bare slug too.
    if isinstance(v, dict):
        return v.get("slug") or None
    return v or None


def team_starters(rec: Optional[dict]) -> dict:
    """One team's entered five from its coaching record, PG→C, or unknown."""
    snap = _entered_snapshot(rec) if rec else None
    if snap is None:
        return {"starters": None, "source": None, "entered_at": None}
    _, minutes = snap
    five = [_slot_slug(minutes.get(s)) for s in SLOTS]
    if not all(five):
        # An entered depth chart with an empty starting slot: nothing to vouch for.
        return {"starters": None, "source": None, "entered_at": rec.get("entered_at")}
    return {"starters": five, "source": "entered", "entered_at": rec.get("entered_at")}


def _flags(starters: Optional[list], rows: list[dict]) -> list[dict]:
    if not starters:
        return []
    played = {r.get("slug"): r.get("min") or 0 for r in rows}
    out = []
    for slug in starters:
        if slug not in played:
            out.append({"slug": slug, "issue": "not in box score"})
        elif not played[slug]:
            out.append({"slug": slug, "issue": "no minutes"})
    return out


def _after_game(entered_at: Optional[str], game_date: str) -> bool:
    if not entered_at:
        return False
    try:
        when = datetime.fromisoformat(entered_at).astimezone(_ET).date().isoformat()
    except ValueError:
        return False
    return when > game_date


def build_entry(*, date: str, season: str, game_type: str, home_team: str, away_team: str,
                home_rows: list[dict], away_rows: list[dict], coaching: dict,
                reconstructed: bool = False) -> dict:
    """The line for one game. `*_rows` are `{slug, min}` per player."""
    teams = {}
    for team, rows in ((home_team, home_rows), (away_team, away_rows)):
        t = team_starters(coaching.get(team))
        t["flags"] = _flags(t["starters"], rows)
        t["entered_after_game"] = _after_game(t["entered_at"], date)
        teams[team] = t
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": date, "season": season, "game_type": game_type,
        "home_team": home_team, "away_team": away_team, "teams": teams,
    }
    if reconstructed:
        entry["reconstructed"] = True
    return entry


def append(entry: dict) -> None:
    with STARTERS_FILE.open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def record_game(**kw) -> None:
    """Called from the box score commit. Never raises: a missing starters
    line is a gap in a nice-to-have, a failed commit is not."""
    try:
        append(build_entry(coaching=load_coaching_settings(), **kw))
    except Exception as exc:
        logger.warning("game-starters: couldn't record %s %s@%s — %s",
                       kw.get("date"), kw.get("away_team"), kw.get("home_team"), exc)


def load(season: Optional[str] = None) -> list[dict]:
    """Every game's latest line, oldest first."""
    if not STARTERS_FILE.exists():
        return []
    latest: dict[tuple, dict] = {}
    for line in STARTERS_FILE.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if season and e.get("season") != season:
            continue
        key = (e.get("date"), e.get("game_type"), *sorted((e.get("home_team"), e.get("away_team"))))
        latest[key] = e
    return sorted(latest.values(), key=lambda e: e["date"])


def starts_by_player(entries: list[dict], game_type: str = "REG") -> dict:
    """slug → {gs, team_games}: games started, out of the games this file
    knows the starters for of each team the player started for. A flagged
    starter (no minutes, or not in the box) is not counted as a start."""
    out: dict[str, dict] = defaultdict(lambda: {"gs": 0})
    for e in entries:
        if e.get("game_type") != game_type:
            continue
        for t in (e.get("teams") or {}).values():
            flagged = {f["slug"] for f in t.get("flags") or []}
            for slug in t.get("starters") or []:
                if slug not in flagged:
                    out[slug]["gs"] += 1
    return dict(out)


@router.get("/api/game-starters")
def get_game_starters(season: Optional[str] = Query(None), team: Optional[str] = Query(None)):
    season = season or _current_league_year()
    entries = load(season)
    if team:
        t = team.upper()
        entries = [e for e in entries if t in (e["home_team"], e["away_team"])]
    return {"season": season, "games": entries}


@router.get("/api/game-starters/counts")
def get_start_counts(season: Optional[str] = Query(None),
                     game_type: str = Query("REG", pattern="^(REG|PLAYOFF)$")):
    """Games started per player, and how many games of each team have known
    starters — a reader needs the second to tell "came off the bench" from
    "nobody knows"."""
    season = season or _current_league_year()
    entries = load(season)
    known: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.get("game_type") != game_type:
            continue
        for team, t in (e.get("teams") or {}).items():
            if t.get("starters"):
                known[team] += 1
    return {"season": season, "game_type": game_type,
            "players": starts_by_player(entries, game_type), "team_games_known": dict(known)}

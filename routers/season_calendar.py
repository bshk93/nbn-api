"""The regular-season calendar: opening night, the last regular-season game,
the trade deadline and the draft days, for one league year.

Opening night and the last regular-season game are not stored anywhere of their
own. They are the first and last dates in that season's schedule file
(`schedule-{season}.json`, `routers/schedule.py`). The schedule is what the
league actually plays and is already edited through its own endpoints, so a
second copy of opening night could only drift from it.

The trade deadline and the draft days are not in the schedule, so they live in
`league-state.json` under `season_dates`, beside the rollover overrides, and are
set through `PUT /api/league-year/{season}/dates`. A draft day is filed under
the league year it falls in: the June 2027 draft is in 26-27, because the
league year turns over on July 1.

Until 2026-09-30 nothing read any of these, and three rules that turn on at
opening night were never checked: the in-season 15-man roster (§ 2.1), the
§ 6.3 extension windows, and the § 4.5 trade limit's exemptions.

Every function returns None (or an empty list) when the fact isn't known, and
never guesses. A caller asking "is it the regular season?" with no schedule on
file gets False — the offseason reading every validator used before this.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from . import constants
from .storage import _season_for_date

_BOUNDS_CACHE: dict[str, tuple] = {}


def _schedule_path(season: str):
    return constants.DATA_DIR / constants.SCHEDULE_FILE_FMT.format(season=season)


def regular_season_bounds(season: str) -> Optional[tuple[str, str]]:
    """(opening night, last regular-season game) for `season`, off its schedule
    file, or None when that season has no schedule. Cached against the file's
    mtime and size, since validators ask on every keystroke of the simulator."""
    path = _schedule_path(season)
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    hit = _BOUNDS_CACHE.get(season)
    if hit and hit[0] == key:
        return hit[1]
    try:
        games = json.loads(path.read_text()).get("games") or []
    except (json.JSONDecodeError, OSError):
        return None
    dates = sorted(g["date"] for g in games if g.get("date"))
    bounds = (dates[0], dates[-1]) if dates else None
    _BOUNDS_CACHE[season] = (key, bounds)
    return bounds


def opening_night(season: str) -> Optional[str]:
    b = regular_season_bounds(season)
    return b[0] if b else None


def is_regular_season(date: str) -> bool:
    """True from opening night through the last regular-season game, inclusive,
    of the league year `date` falls in. The playoffs and the draft are not the
    regular season: § 2.1 says "during the regular season", and a June draft
    trade must still be allowed to land a team at 16 for the summer."""
    b = regular_season_bounds(_season_for_date(date))
    return bool(b) and b[0] <= date <= b[1]


def day_before_opening_night(season: str) -> Optional[str]:
    """The last day of § 6.3's rookie-scale and non-expiring veteran extension
    windows ("any time up to the day before the regular season starts")."""
    start = opening_night(season)
    if not start:
        return None
    return (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _season_dates(season: str) -> dict:
    try:
        state = json.loads(constants.LEAGUE_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return (state.get("season_dates") or {}).get(season) or {}


def trade_deadline(season: str) -> Optional[str]:
    return _season_dates(season).get("trade_deadline") or None


def draft_days(season: str) -> list[str]:
    return list(_season_dates(season).get("draft_days") or [])


def season_calendar(season: str) -> dict:
    """Everything above for one season, for `GET /api/league-year`."""
    b = regular_season_bounds(season)
    return {
        "opening_night": b[0] if b else None,
        "last_regular_season_game": b[1] if b else None,
        "trade_deadline": trade_deadline(season),
        "draft_days": draft_days(season),
    }

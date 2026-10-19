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


# The league follows the NBA's trade deadline (settled 2026-09-30), so a known
# one is checked in here and needs nobody to enter it. A date set through
# PUT /api/league-year/{season}/dates wins over it.
DEFAULT_TRADE_DEADLINES = {"26-27": "2027-02-11"}


def trade_deadline(season: str) -> Optional[str]:
    return _season_dates(season).get("trade_deadline") or DEFAULT_TRADE_DEADLINES.get(season)


def draft_days(season: str) -> list[str]:
    return list(_season_dates(season).get("draft_days") or [])


def team_option_deadline(season: str) -> Optional[str]:
    """The rookie-scale team option deadline: October 31 of the season's
    first year, or the Monday after when that's a weekend (league office,
    2026-10-19). It decides next season's options for first-rounders in years
    2 and 3 of their rookie deals. A fixed rule rather than a stored date, so
    it needs no entering each year. See `team_option_window`."""
    try:
        year = 2000 + int(season.split("-")[0])
    except (ValueError, IndexError):
        return None
    d = datetime(year, 10, 31)
    if d.weekday() >= 5:                       # Saturday 5, Sunday 6
        d += timedelta(days=7 - d.weekday())   # → the Monday after
    return d.strftime("%Y-%m-%d")


def _prev_season(season: str) -> Optional[str]:
    try:
        y = int(season.split("-")[0])
    except (ValueError, IndexError):
        return None
    return f"{(y - 1) % 100:02d}-{y % 100:02d}"


def is_rookie_scale_option(bio: dict, year: str) -> bool:
    """A first-round rookie-scale deal carries team options on its 3rd and 4th
    seasons: a pick drafted in 2025 plays 25-26 and 26-27, then has options on
    27-28 and 28-29. Read off the draft record, since only a few bios carry
    their contract history. A first-rounder re-signed later onto a deal with an
    option in one of those two seasons would be misread as rookie-scale; none
    is on file today."""
    if (bio or {}).get("draft_round") != 1 or not bio.get("draft_year"):
        return False
    try:
        start = 2000 + int(year.split("-")[0])
    except (ValueError, IndexError):
        return False
    return start - int(bio["draft_year"]) in (2, 3)


def team_option_window(bio: dict, year: str) -> dict:
    """When a team option on `year` may be decided (§ 6.1, league office
    2026-10-19), inclusive dates, None meaning unbounded or unknown.

    * rookie-scale (`kind: "rookie"`): any time up to the team option
      deadline in the season before; unexercised by then, it's declined
      automatically (sweep_team_options.py).
    * every other team option (`kind: "other"`): from the day after the
      previous season's last regular-season game through June 30, the day
      before the option season's league year starts. `opens` is None when that
      season has no schedule on file, which the validator reports rather than
      guesses."""
    prev = _prev_season(year)
    if is_rookie_scale_option(bio, year):
        return {"kind": "rookie", "opens": None,
                "closes": team_option_deadline(prev) if prev else None}
    bounds = regular_season_bounds(prev) if prev else None
    opens = ((datetime.strptime(bounds[1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
             if bounds else None)
    try:
        closes = f"{2000 + int(year.split('-')[0])}-06-30"
    except (ValueError, IndexError):
        closes = None
    return {"kind": "other", "opens": opens, "closes": closes}


def season_calendar(season: str) -> dict:
    """Everything above for one season, for `GET /api/league-year`."""
    b = regular_season_bounds(season)
    return {
        "opening_night": b[0] if b else None,
        "last_regular_season_game": b[1] if b else None,
        "trade_deadline": trade_deadline(season),
        "draft_days": draft_days(season),
        "team_option_deadline": team_option_deadline(season),
    }

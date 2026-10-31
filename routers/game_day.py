"""The league's current game day — `GET /api/game-day`.

Our games are played on stream, and a night's stream often runs well past
midnight ET. So "tonight" is not the calendar date: at 2am on the 1st, the
games being played are the 31st's. Anything that means "tonight's games"
(Pick'em's lock and the day it opens on, the homepage slate, the stats
worklist) reads this instead of the ET date.

  * **While the stream is live**, the game day is the day that stream belongs
    to, however late it runs.
  * **Otherwise** it is the ET calendar date.

A stream belongs to the ET date it began on, with one exception: a stream
that began in the small hours (before `RESTART_HOUR` ET) belongs to the day
before when that day still has games without a box score and isn't marked
done. That is the night's stream dropping and coming back after midnight; a
new day's games never start at 1am.

Box score rows are dated by their schedule fixture, never by when they were
uploaded or committed, so a late upload already files under the right day.
This module is only about which day is "now".

    venv/bin/python -m tests.test_game_day
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter

from .league_time import LEAGUE_TZ, league_now

router = APIRouter()

# A stream starting before this hour ET can be the previous night's, resumed.
RESTART_HOUR = 6


def _night_unfinished(date: str) -> bool:
    """`date` had games, at least one has no box score, and the day isn't marked done."""
    from .boxscores import get_boxscore_games
    from .schedule import _load as load_schedule
    from .streaming_days import load_streaming_days
    import season_clock

    season = season_clock.season_for_date(date)
    games = [g for g in load_schedule(season).get("games", []) if g.get("date") == date]
    if not games:
        return False
    if (load_streaming_days().get(date) or {}).get("done"):
        return False
    have = {(g["date"], frozenset((g["home_team"], g["away_team"])))
            for g in get_boxscore_games(season=season, team=None) if g.get("date") == date}
    return any((date, frozenset((g.get("home_team"), g.get("away_team")))) not in have for g in games)


def stream_game_day(started_at: Optional[str]) -> Optional[str]:
    """The game day a stream that began at `started_at` (Twitch's ISO UTC) is for."""
    if not started_at:
        return None
    try:
        began = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone(LEAGUE_TZ)
    except ValueError:
        return None
    day = began.date()
    if began.hour < RESTART_HOUR:
        before = (day - timedelta(days=1)).isoformat()
        try:
            if _night_unfinished(before):
                return before
        except Exception:  # noqa: BLE001 — unreadable schedule/box scores: take the plain date
            pass
    return day.isoformat()


def game_day(status: Optional[dict] = None, today: Optional[str] = None) -> str:
    """Today's game day, 'YYYY-MM-DD'. `status` is the stream's
    (`stream_live()`) and `today` the ET date; each is looked up when not given."""
    today = today or league_now().date().isoformat()
    if status is None:
        from .stream_live import stream_live
        status = stream_live()
    if status.get("live"):
        day = stream_game_day(status.get("started_at"))
        if day and day < today:
            return day
    return today


@router.get("/api/game-day")
def get_game_day():
    from .stream_live import stream_live
    status = stream_live()
    today = league_now().date().isoformat()
    return {"date": game_day(status), "today": today, "live": bool(status.get("live"))}

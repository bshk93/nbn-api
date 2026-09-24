"""Inbox messages for the game-day handoffs between teams, streamers and Stats.

A game day passes through four hands, and each handoff used to rely on the
next person checking a dashboard. These put one message in the right inbox at
each step instead:

1. **A team saves coaching changes** → the streamer who claimed that team's
   next game. If nobody has claimed it yet, every streamer — but only when the
   game is today or tomorrow, so a change saved a week out doesn't ping the
   whole committee.
2. **A streamer marks those changes entered** → the team, as a receipt.
3. **A streamer marks a day done** → the Stats committee: the day's games are
   ready for screenshots.
4. **Every game on a day has both sides uploaded** → whoever parses, which is
   the `admin` role for now.

Two rules keep this quiet. A coaching save only notifies when the team goes
*from* not-pending *to* pending, so five saves in a row send one message. And
"ready to parse" goes once per date (stamped on the date's streaming-days
record), not once per upload.

Every function here swallows its own errors: a message that fails to send must
never be why a save, an upload, or a mark-done fails.
"""
from __future__ import annotations

from datetime import date as _date, timedelta
from typing import Iterable, Optional

from . import inbox
from .constants import logger
from .league_time import league_today
from .storage import _current_league_year


def _fmt_date(d: str) -> str:
    try:
        dt = _date.fromisoformat(d)
        return f"{dt.strftime('%a')} {dt.strftime('%b')} {dt.day}"
    except ValueError:
        return d


def _next_game(team: str) -> Optional[dict]:
    from .schedule import _load
    today = league_today().isoformat()
    games = _load(_current_league_year())["games"]
    for g in games:  # stored in date order
        if g["date"] >= today and team in (g["home_team"], g["away_team"]):
            return g
    return None


def coaching_saved(team: str, was_pending: bool, saved_by: Optional[str]) -> None:
    """Handoff 1. Call after a save, with whether the team was already pending."""
    if was_pending:
        return
    try:
        g = _next_game(team)
        if not g:
            return
        opp = g["away_team"] if g["home_team"] == team else g["home_team"]
        at = "vs" if g["home_team"] == team else "@"
        text = (f"{team} saved new coaching settings for {_fmt_date(g['date'])} "
                f"{at} {opp} — they need entering in 2K")
        link = "/committees/stream/"
        streamer = g.get("streamer")
        if streamer:
            if streamer != saved_by:
                inbox.notify_member(streamer, text, link)
            return
        soon = (league_today() + timedelta(days=1)).isoformat()
        if g["date"] <= soon:
            inbox.notify_role("streamer", text + " (no streamer has claimed that game)", link)
    except Exception as exc:
        logger.warning("coaching_saved notify failed for %s: %s", team, exc)


def coaching_entered(team: str, entered_by: Optional[str]) -> None:
    """Handoff 2."""
    try:
        inbox.notify_team(team, f"Your coaching settings were entered in 2K by {entered_by or 'a streamer'}",
                          link=f"/teams/{team}/")
    except Exception as exc:
        logger.warning("coaching_entered notify failed for %s: %s", team, exc)


def day_done(date: str, done_by: Optional[str]) -> None:
    """Handoff 3. Call only when the date goes from not-done to done."""
    try:
        from .schedule import _load
        n = sum(1 for g in _load(_current_league_year())["games"] if g["date"] == date)
        games = f"{n} game{'' if n == 1 else 's'}" if n else "The games"
        inbox.notify_role("stats", f"{_fmt_date(date)} is streamed — {games} need box score screenshots",
                          link="/committees/stats/")
    except Exception as exc:
        logger.warning("day_done notify failed for %s: %s", date, exc)


def maybe_day_ready(date: str, season: str, entered_keys: Iterable[frozenset],
                    ready_keys: Iterable[frozenset]) -> None:
    """Handoff 4. Call after an upload. `entered_keys` / `ready_keys` are the
    {team, team} pairs on `date` that already have a box score / have both
    sides uploaded. Notifies once per date, when every scheduled game on it is
    one or the other and at least one is waiting to be parsed."""
    try:
        from .schedule import _load
        from .streaming_days import load_streaming_days, save_streaming_days
        from .constants import _streaming_days_lock
        entered, ready = set(entered_keys), set(ready_keys)
        fixtures = {frozenset({g["home_team"], g["away_team"]})
                    for g in _load(season)["games"] if g["date"] == date}
        # A date the schedule doesn't carry (a playoff game) is judged on what
        # was uploaded for it.
        needed = fixtures or ready
        waiting = needed - entered
        if not waiting or not waiting <= ready:
            return
        with _streaming_days_lock:
            days = load_streaming_days()
            rec = days.setdefault(date, {})
            if rec.get("parse_ready_notified_at"):
                return
            from datetime import datetime, timezone
            rec["parse_ready_notified_at"] = datetime.now(timezone.utc).isoformat()
            save_streaming_days(days)
        n = len(waiting)
        inbox.notify_role("admin", f"{_fmt_date(date)} is ready to parse — {n} game{'' if n == 1 else 's'}, "
                                   f"both sides uploaded", link="/committees/stats/")
    except Exception as exc:
        logger.warning("day_ready notify failed for %s: %s", date, exc)

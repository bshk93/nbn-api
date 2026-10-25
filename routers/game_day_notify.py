"""Inbox messages for the game-day handoffs between teams, streamers and Stats.

A game day passes through four hands, and each handoff used to rely on the
next person checking a dashboard. These put one message in the right inbox at
each step instead:

1. **A team saves coaching changes** → the streamer who claimed that team's
   next game to be played (an unplayed game from the last week comes before
   today's, since the league can run behind). If nobody has claimed it yet, every streamer — but only when the
   game is today or tomorrow, so a change saved a week out doesn't ping the
   whole committee.
2. **A streamer marks those changes entered** → the team, as a receipt.
3. **A streamer marks a day done** → the Stats committee: the day's games are
   ready for screenshots. Only the games still without them are counted, and a
   day Stats has already finished sends nothing.
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


# How far back a team's unplayed game still counts as its next one. The league
# runs a day or two behind when it's busy; a game left unplayed longer than
# this is a schedule problem, not the game a coaching change is for.
LOOKBACK_DAYS = 7


def _played(g: dict, done: set[str], boxed: set[tuple]) -> bool:
    """Its day is marked streamed, or it has a box score or screenshots."""
    from .boxscores import _game_teams, _game_in_pending_screenshots
    return (g["date"] in done
            or (g["date"], _game_teams(g["home_team"], g["away_team"])) in boxed
            or _game_in_pending_screenshots(g["date"], g["home_team"], g["away_team"]))


def _boxed_games(season: str) -> set[tuple]:
    from .boxscores import _game_teams, allstats_path, read_csv
    path = allstats_path(season, "REG")
    if not path.exists():
        return set()
    _, rows = read_csv(path)
    return {(r.get("DATE", "").strip(), _game_teams(r.get("TEAM", "").strip(), r.get("OPP", "").strip().lstrip("@")))
            for r in rows}


def _next_game(team: str) -> Optional[dict]:
    """The team's next game to be played: the earliest of its games from the
    last week that hasn't been, so a change saved while the league is behind
    goes to the game it's really for, not the next date on the calendar."""
    from .schedule import _load
    from .streaming_days import load_streaming_days
    today = league_today()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    season = _current_league_year()
    games = [g for g in _load(season)["games"]  # stored in date order
             if g["date"] >= since and team in (g["home_team"], g["away_team"])]
    past = [g for g in games if g["date"] < today.isoformat()]
    if past:
        done = {d for d, rec in load_streaming_days().items() if rec.get("done")}
        boxed = _boxed_games(season)
        for g in past:
            if not _played(g, done, boxed):
                return g
    return next((g for g in games if g["date"] >= today.isoformat()), None)


def coaching_saved(team: str, was_pending: bool, saved_by: Optional[str]) -> None:
    """Handoff 1. Call after a save, with whether the team was already pending."""
    if was_pending:
        return
    _tell_next_streamer(team, saved_by, "saved new coaching settings", "they need entering in 2K", "coaching_saved")


def positions_changed(team: str, saved_by: Optional[str]) -> None:
    """Handoff 1 again, for a player's secondary position or jersey number.
    Call only when the team had no such change waiting already, for the same reason a
    coaching save notifies only on the way into pending."""
    _tell_next_streamer(team, saved_by, "changed a player's position or jersey number", "it needs entering in 2K", "positions_changed")


def note_left(team: str, saved_by: Optional[str], already_waiting: bool) -> None:
    """Handoff 1, for a note to the streamer. Quiet when the team already has
    changes waiting: the streamer will see the note on the same card."""
    if already_waiting:
        return
    _tell_next_streamer(team, saved_by, "left a note for the streamer", "read it before the game", "note_left")


def _tell_next_streamer(team: str, saved_by: Optional[str], what: str, tail: str, label: str) -> None:
    try:
        g = _next_game(team)
        if not g:
            return
        opp = g["away_team"] if g["home_team"] == team else g["home_team"]
        at = "vs" if g["home_team"] == team else "@"
        text = (f"{team} {what} for {_fmt_date(g['date'])} "
                f"{at} {opp} — {tail}")
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
        logger.warning("%s notify failed for %s: %s", label, team, exc)


def coaching_entered(team: str, entered_by: Optional[str]) -> None:
    """Handoff 2."""
    try:
        inbox.notify_team(team, f"Your coaching settings were entered in 2K by {entered_by or 'a streamer'}",
                          link=f"/teams/{team}/")
    except Exception as exc:
        logger.warning("coaching_entered notify failed for %s: %s", team, exc)


def positions_set(team: str, set_by: Optional[str]) -> None:
    """Handoff 2, for positions: the team's receipt."""
    try:
        inbox.notify_team(team, f"Your player positions and numbers were set in 2K by {set_by or 'a streamer'}",
                          link=f"/teams/{team}/#coaching")
    except Exception as exc:
        logger.warning("positions_set notify failed for %s: %s", team, exc)


def note_cleared(team: str, cleared_by: Optional[str]) -> None:
    """Handoff 2, for a note: the team's receipt."""
    try:
        inbox.notify_team(team, f"Your note to the streamer was marked clear by {cleared_by or 'a streamer'}",
                          link=f"/teams/{team}/#coaching")
    except Exception as exc:
        logger.warning("note_cleared notify failed for %s: %s", team, exc)


def streamer_assigned(name: str, date: str, games: list, by: Optional[str],
                      displaced: Iterable[str]) -> None:
    """An admin put `name` on `games` (all on `date`). Tells the new streamer,
    and anyone whose claim was replaced. Nobody is told about their own action."""
    try:
        n = len(games)
        what = (f"{games[0]['away_team']} @ {games[0]['home_team']}" if n == 1
                else f"{n} games")
        link = "/committees/stream/"
        if name != by:
            inbox.notify_member(name, f"{by or 'An admin'} assigned you to stream {what} on "
                                      f"{_fmt_date(date)}", link)
        for old in displaced:
            if old not in (name, by):
                inbox.notify_member(old, f"{by or 'An admin'} moved your {_fmt_date(date)} "
                                         f"stream to {name}", link)
    except Exception as exc:
        logger.warning("streamer_assigned notify failed for %s: %s", date, exc)


def day_done(date: str, done_by: Optional[str]) -> None:
    """Handoff 3. Call only when the date goes from not-done to done.

    Counts only the games still needing screenshots: a day is often marked done
    after Stats has already uploaded and entered it, and then there is nothing
    to ask for. A scheduled game with no box score and no screenshots item
    with both sides counts; a day with none of those sends nothing."""
    try:
        from .boxscores import _game_teams
        from .boxscore_shots import find_pending, is_ready
        from .schedule import _load
        season = _current_league_year()
        fixtures = [g for g in _load(season)["games"] if g["date"] == date]
        if fixtures:
            boxed = _boxed_games(season)

            def needs(g: dict) -> bool:
                if (date, _game_teams(g["home_team"], g["away_team"])) in boxed:
                    return False
                item = find_pending(date, g["home_team"], g["away_team"])
                return not (item and is_ready(item[1]))

            n = sum(1 for g in fixtures if needs(g))
            if not n:
                return
        else:
            n = 0  # a date the schedule doesn't carry (a playoff game)
        games = f"{n} game{'' if n == 1 else 's'}" if n else "The games"
        inbox.notify_role("stats", f"{_fmt_date(date)} is streamed — {games} need{'s' if n == 1 else ''} box score screenshots",
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

"""League time — one shared timezone for civil dates.

A transaction date, a deadline, the league-year rollover: these are labels on a
league business day, not instants. They need one shared zone or they're
ambiguous. The league is mostly US-based and real NBA transactions are reported
in ET, so Eastern is the league day. `routers/poeltl.py` already used
America/New_York for the daily puzzle; this makes that the explicit convention.

The host runs Etc/UTC, so bare `date.today()` / `datetime.utcnow()` yield the
*UTC* civil date — which is tomorrow's date for several hours every evening.
Use these helpers for anything that means "what day is it in the league".

Instants (created_at audit stamps, durations) stay UTC and are not this
module's business. Only civil dates belong here.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

# IANA zone, not a fixed -5 offset, so DST is handled.
LEAGUE_TZ = ZoneInfo("America/New_York")


def league_now() -> datetime:
    """Current time as an aware datetime in league time."""
    return datetime.now(LEAGUE_TZ)


def league_today() -> date:
    """Today's date in league time."""
    return league_now().date()


def league_today_str() -> str:
    """Today's date in league time, as 'YYYY-MM-DD'."""
    return league_today().isoformat()

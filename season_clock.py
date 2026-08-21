"""The one season-boundary clock, shared by the live API and the stats build.

Until 2026-08-21 there were three independent answers to "what season is
it?": the league year (BOD-settable, July-1 default, formerly in
routers/storage.py), the box-score/API stats clock (also July-1, but always
the *default* boundary -- never settable), and the build's own clock in
stats_build/buildargs.py (a hardcoded Sep-30 America/New_York cutoff). A box
score could be filed under a season the build then aggregated differently,
the first time a new season's games started before October.

Unified into one clock: one default (July 1 of the season's first calendar
year), one override mechanism (a season's effective start date stored in
`league-state.json`, set via `PUT /api/league-year/{season}`), used
everywhere a season boundary is needed -- box scores, the build, and
cap/contract logic alike. There is no longer a separate "stats clock";
`_current_league_year()` (routers/storage.py) and `resolve_season()`
(stats_build/buildargs.py) both resolve through this module and can no
longer disagree about which season a given date falls in.

`DATA_DIR` honors `NBS_DATA_DIR` (rather than hardcoding the live path) so a
build pointed at a scratch data directory reads that copy's rollovers, not
the live league's -- the same scratch-safety property the rest of
`stats_build` already depends on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("NBS_DATA_DIR", "/var/lib/nothing-but-stats"))
LEAGUE_STATE_FILE = DATA_DIR / "league-state.json"


def load_rollovers() -> dict:
    """season -> effective start date (YYYY-MM-DD) overriding that season's default July 1."""
    if not LEAGUE_STATE_FILE.exists():
        return {}
    return json.loads(LEAGUE_STATE_FILE.read_text()).get("rollovers", {})


def default_season_start(season: str) -> datetime:
    """Default rollover: July 1 of the season's first calendar year. '25-26' -> 2025-07-01."""
    yy = int(season.split("-")[0])
    return datetime(2000 + yy, 7, 1)


def season_shift(season: str, delta: int) -> str:
    a, b = (int(x) for x in season.split("-"))
    return f"{(a + delta) % 100:02d}-{(b + delta) % 100:02d}"


def season_start_date(season: str, rollovers: dict) -> datetime:
    ov = rollovers.get(season)
    return datetime.strptime(ov, "%Y-%m-%d") if ov else default_season_start(season)


def season_for_date(date_str: str, rollovers: dict | None = None) -> str:
    """Which season a YYYY-MM-DD date falls in, honoring rollover overrides.
    With no overrides this is the July-1 boundary."""
    if rollovers is None:
        rollovers = load_rollovers()
    d = datetime.strptime(date_str, "%Y-%m-%d")
    y = d.year % 100
    season = f"{y-1:02d}-{y:02d}" if d.month < 7 else f"{y:02d}-{(y+1) % 100:02d}"
    # Roll forward if a later season's (possibly overridden) start has already passed.
    while season_start_date(season_shift(season, 1), rollovers) <= d:
        season = season_shift(season, 1)
    # Roll back if this season's (possibly overridden) start is still in the future.
    while season_start_date(season, rollovers) > d:
        season = season_shift(season, -1)
    return season


def current_season() -> str:
    """Today's season (UTC), honoring rollover overrides."""
    return season_for_date(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

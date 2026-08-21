"""What a build run is told, resolved once and stated explicitly.

This existed twice before the cutover: `build.sh` inferred the season in bash
and `harness.py` re-derived it in Python so it could pin one. Both now call
`resolve_season` here, so the two can no longer disagree about which season a
build is for.

`resolve_season` used to have its *own* rule (a Sep-30 America/New_York
cutoff), independent of the one `routers/storage.py` used for box scores and
cap/contract logic (July 1 UTC) -- so the build and the rest of the API could
resolve a given date into two different seasons. As of 2026-08-21 both go
through `season_clock.py`, the one shared definition (July 1 default,
overridable per-season via league-state.json). See its docstring for the
full history.

Only `season` reaches the aggregation. `playoffs_from` and `through` are kept
because `job.R` still takes three positional arguments and R is retained,
dormant, for a season -- but note what reading `job.R` shows: it parses both,
defaults `through` to `Sys.Date()`, and then never uses either. Playoff rows
come from their own `allstats-playoffs-{YY}.csv` files, so there is no date to
split a season on. They are passed through verbatim rather than dropped, so
the dormant engine is invoked exactly as it always was.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import season_clock

# Used only for the `through` timestamp label below -- season resolution
# itself is UTC, via season_clock.
LEAGUE_TZ = ZoneInfo("America/New_York")

DATA_DIR = Path(os.environ.get("NBS_DATA_DIR", "/var/lib/nothing-but-stats"))
OUT_DIR = Path(os.environ.get("NBN_OUT_DIR", DATA_DIR / "derived"))

# Where the dormant R build and `seasons.conf` still live. Only the R path and
# `resolve_playoffs_from` read these; the Python pipeline needs neither, which
# is the cross-repo dependency the port set out to remove.
REPO_ROOT = Path(os.environ.get("NBN_SITE_REPO", "/home/skim/projects/nbn-today"))
BUILD_DIR = Path(os.environ.get("NBN_BUILD_DIR", REPO_ROOT / "build"))


def resolve_season(today: date | None = None) -> str:
    """The current season, honoring league-state.json rollover overrides.
    Defaults to a July 1 boundary -- see season_clock.py."""
    if today is None:
        return season_clock.current_season()
    return season_clock.season_for_date(today.isoformat())


def resolve_playoffs_from(season: str) -> str:
    """That season's playoff start date from `seasons.conf`, or "" if unlisted."""
    conf = BUILD_DIR / "seasons.conf"
    if not conf.exists():
        return ""
    for line in conf.read_text().splitlines():
        if line.startswith(f"{season}="):
            return line.split("=", 1)[1].strip()
    return ""


@dataclass(frozen=True)
class BuildArgs:
    """The three arguments a build takes, always stated rather than defaulted.

    `through` matters to the harness even though nothing reads it: `job.R`
    defaults it to `Sys.Date()`, so two runs being compared must agree on it
    or the diff is measuring the calendar.
    """

    season: str
    playoffs_from: str
    through: str

    @classmethod
    def resolve(cls, season: str | None = None, through: str | None = None) -> "BuildArgs":
        season = season or resolve_season()
        return cls(
            season=season,
            playoffs_from=resolve_playoffs_from(season),
            through=through or datetime.now(LEAGUE_TZ).date().isoformat(),
        )

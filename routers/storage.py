import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .constants import logger, LEAGUE_STATE_FILE


def _atomic_write(path: Path, text: str):
    """Write text by streaming to a temp file in the same dir, then os.replace().
    os.replace is atomic on POSIX, so concurrent readers never observe a partial
    file — they see either the old contents or the new, never a half-written mix."""
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{id(text)}")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    text = path.read_text()
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)
    return headers, rows


def write_csv(path: Path, headers: list[str], rows: list[dict]):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, out.getvalue())


def log_write(info: dict, action: str):
    name = info.get("name", "unknown")
    logger.info("[%s] %s", name, action)


def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save_json(path: Path, data):
    _atomic_write(path, json.dumps(data, indent=2))


def _parse_dollar(s) -> int:
    """Parse a salary string like '$37,000,000' to an integer. Returns 0 on empty/invalid."""
    if not s:
        return 0
    try:
        return round(float(re.sub(r"[$,\s]", "", str(s)) or 0))
    except (ValueError, TypeError):
        return 0


def _season_start(s: str) -> int:
    try:
        return int(s.split('-')[0])
    except Exception:
        return 0


def _current_season_str() -> str:
    """Date-based season for the *stats* clock (shared with the R build). This is the
    season a box score gets filed under and must stay tied to the calendar — it is
    intentionally NOT the league year. For cap/contract logic use _season_for_date /
    _current_league_year instead."""
    now = datetime.now(timezone.utc)
    y = now.year % 100
    if now.month < 7:
        return f"{y-1:02d}-{y:02d}"
    return f"{y:02d}-{(y+1) % 100:02d}"


# ── League year (the cap/contract clock) ────────────────────────────────────
# The league year is a function of a date, not a stored pointer. Each season's
# default start is July 1 of its first calendar year ("25-26" -> 2025-07-01);
# BOD can override a season's start date in league-state.json to roll the league
# year over on a date other than July 1. _season_for_date(d) answers "which league
# year is date d in?" — fed today's date for display, or a transaction's own date
# when writing contracts.

def _default_season_start(season: str) -> datetime:
    """Default rollover: July 1 of the season's first calendar year. '25-26' -> 2025-07-01."""
    yy = int(season.split('-')[0])
    return datetime(2000 + yy, 7, 1)


def _season_shift(season: str, delta: int) -> str:
    a, b = (int(x) for x in season.split('-'))
    return f"{(a + delta) % 100:02d}-{(b + delta) % 100:02d}"


def _league_rollovers() -> dict:
    """season -> effective start date (YYYY-MM-DD) overriding that season's default July 1."""
    return _load_json(LEAGUE_STATE_FILE, {}).get("rollovers", {})


def _season_start_date(season: str, rollovers: dict) -> datetime:
    ov = rollovers.get(season)
    return datetime.strptime(ov, "%Y-%m-%d") if ov else _default_season_start(season)


def _season_for_date(date_str: str, rollovers: dict | None = None) -> str:
    """Which league year a YYYY-MM-DD date falls in, honoring rollover overrides.
    With no overrides this equals the July-1 boundaries of _current_season_str."""
    if rollovers is None:
        rollovers = _league_rollovers()
    d = datetime.strptime(date_str, "%Y-%m-%d")
    y = d.year % 100
    season = f"{y-1:02d}-{y:02d}" if d.month < 7 else f"{y:02d}-{(y+1) % 100:02d}"
    # Roll forward if a later season's (possibly overridden) start has already passed.
    while _season_start_date(_season_shift(season, 1), rollovers) <= d:
        season = _season_shift(season, 1)
    # Roll back if this season's (possibly overridden) start is still in the future.
    while _season_start_date(season, rollovers) > d:
        season = _season_shift(season, -1)
    return season


def _current_league_year() -> str:
    return _season_for_date(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

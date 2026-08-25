import csv
import io
import json
import os
import re
from pathlib import Path

from . import audit
from .constants import logger
import season_clock
from season_clock import (
    season_for_date as _season_for_date,
    season_shift as _season_shift,
    season_start_date as _season_start_date,
    default_season_start as _default_season_start,
)


def _atomic_write(path: Path, text: str):
    """Write text by streaming to a temp file in the same dir, then os.replace().
    os.replace is atomic on POSIX, so concurrent readers never observe a partial
    file — they see either the old contents or the new, never a half-written mix."""
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{id(text)}")
    tmp.write_text(text)
    # New files are served statically by nginx (e.g. {abbr}-roster.csv), so they
    # must be world-readable regardless of the writing process's umask. Without
    # this, a writer running under umask 077 produces 0600 files and nginx 403s.
    #
    # An existing file keeps the mode it already has. os.replace() swaps in the
    # temp file wholesale, so a fixed 0644 here silently un-did any chmod an
    # operator applied — members.json and sessions.json hold credentials and are
    # deliberately 0600, and every write would have reset them to world-readable.
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    os.chmod(tmp, mode)
    # Forensics for the writes that never reach the transaction ledger. Read the
    # old text before the swap, record the diff after it, so a write that fails
    # leaves nothing behind. Both calls are no-ops off audit.py's allowlist, and
    # neither can raise. See routers/audit.py.
    before = audit.snapshot_before(path)
    os.replace(tmp, path)
    audit.record(path, before, text)


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


# ── The season clock (shared by box scores, the build, and cap/contract logic) ──
# One clock, one definition, in season_clock.py: default rollover is July 1 of
# a season's first calendar year, overridable per-season via league-state.json
# (BOD-settable through PUT /api/league-year/{season}). _season_for_date(d)
# answers "which season is date d in?" — fed today's date for display, or a
# transaction's own date when writing contracts or filing a box score.

def _league_rollovers() -> dict:
    """season -> effective start date (YYYY-MM-DD) overriding that season's default July 1."""
    return season_clock.load_rollovers()


def _current_league_year() -> str:
    return season_clock.current_season()

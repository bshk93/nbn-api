import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .constants import logger


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
    path.write_text(out.getvalue())


def log_write(info: dict, action: str):
    name = info.get("name", "unknown")
    logger.info("[%s] %s", name, action)


def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


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
    now = datetime.now(timezone.utc)
    y = now.year % 100
    if now.month < 7:
        return f"{y-1:02d}-{y:02d}"
    return f"{y:02d}-{(y+1) % 100:02d}"

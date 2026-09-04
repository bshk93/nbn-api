"""Append-only forensics for the writes that bypass the transaction ledger.

`POST /api/transactions` is audited to the dollar. The side doors that change
the same state are not: `PUT /api/players/{slug}` rewrites `salaries`,
`cap_holds`, `guaranteed` and `guarantee_dates`; `PUT /api/roster/{team}`,
`PUT /api/deadcap/{team}` and `PUT /api/picks/...` rewrite the files those
figures are read from. Each of them writes one `log_write()` line to journald,
which records *who touched what* and never *what it was before*. So "who moved
UTA's Guaranteed Salary, and when" has been unanswerable.

Every write in the API funnels through `storage._atomic_write`, so that is where
this hooks in: before the `os.replace` we keep the old text, and after it we
append one `{ts, actor, method, path, file, diff}` line here.

Three deliberate limits:

- **An allowlist, not a denylist.** `members.json`, `tokens.json` and
  `sessions.json` hold live credentials, and a value-level diff of any of them
  would copy bearer tokens into a plaintext log — the exact thing §1 of the
  backlog is trying to get *out* of the backup repo. `_NEVER` restates that as
  a hard guard so a later widening of the allowlist can't undo it by accident.
- **Bounded lines.** A diff is capped at `_MAX_CHANGES` entries and each value
  at `_MAX_VALUE_CHARS`, so one line stays appendable in a single write.
- **Never breaks a write.** Every entry point swallows its own exceptions. An
  audit log that can 500 a roster save is worse than no audit log.
"""

import csv
import io
import json
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .constants import DATA_DIR, logger

EDITS_FILE = DATA_DIR / "edits.jsonl"

# Files whose value-level history is worth keeping: the cap state the ledger
# doesn't cover, plus the two ratings/pick stores with their own PUT side door.
_AUDITED_NAMES = {
    "player-bios.json",
    "team-state.json",
    "trade-exceptions.json",
    "cap-levels.json",
    "rookie-scale.json",
    "transactions.json",
    "ovr-history.json",
    "draft-picks.csv",
    "coaching-settings.json",
    "streaming-days.json",
}
_AUDITED_SUFFIXES = ("-roster.csv", "-deadcap.csv", "-picks.csv")

# Never audited, whatever else says otherwise — a diff would embed credentials.
_NEVER = {"members.json", "tokens.json", "sessions.json", "google-oauth.json"}

# The build owns every file under this directory.
DERIVED_MARKER = "derived"

_MAX_CHANGES = 200
_MAX_VALUE_CHARS = 300
# Above this, diffing costs more than the answer is worth (the allstats CSVs are
# 27MB and have their own guard in allstats_guard.py).
_MAX_DIFF_BYTES = 8 * 1024 * 1024

_LOCK = threading.Lock()

# The per-request context. Set once by the middleware in main.py and *mutated*
# thereafter — never re-set. FastAPI runs a sync dependency and its endpoint as
# two separate threadpool tasks, each with its own copy of the context, so a
# `ContextVar.set()` inside `get_token_info` would not be visible to the
# endpoint that follows it. Both copies hold a reference to the same dict, so
# mutating that dict is visible everywhere downstream.
_REQ: ContextVar[Optional[dict]] = ContextVar("nbn_audit_request", default=None)


def begin_request(method: str, path: str):
    _REQ.set({"method": method, "path": path, "actor": None})


def set_actor(name: str):
    ctx = _REQ.get()
    if ctx is not None:
        ctx["actor"] = name


def current() -> dict:
    return _REQ.get() or {}


def should_audit(path: Path) -> bool:
    name = path.name
    if name in _NEVER:
        return False
    # The build owns everything under derived/ and rewrites all 86 files on
    # every box score; none of it is state anyone edits.
    if DERIVED_MARKER in path.parts:
        return False
    return name in _AUDITED_NAMES or name.endswith(_AUDITED_SUFFIXES)


def snapshot_before(path: Path) -> Optional[str]:
    """The file's current text, kept for the diff. None when not audited or new."""
    try:
        if not should_audit(path) or not path.exists():
            return None
        return path.read_text()
    except Exception:
        return None


def _trim(value):
    """Keep a changed value legible without letting one entry blow up the line."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(",", ":"))
    elif value is None or isinstance(value, (int, float, bool)):
        return value
    else:
        text = str(value)
    if len(text) > _MAX_VALUE_CHARS:
        return text[:_MAX_VALUE_CHARS] + "…"
    return text


class _Missing:
    def __repr__(self):
        return "<absent>"


_MISSING = _Missing()


def _diff_json(old, new, prefix: str, out: list):
    if len(out) >= _MAX_CHANGES:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            if len(out) >= _MAX_CHANGES:
                return
            child = f"{prefix}.{key}" if prefix else str(key)
            _diff_json(old.get(key, _MISSING), new.get(key, _MISSING), child, out)
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            if len(out) >= _MAX_CHANGES:
                return
            child = f"{prefix}[{i}]"
            _diff_json(old[i] if i < len(old) else _MISSING,
                       new[i] if i < len(new) else _MISSING, child, out)
    elif old != new:
        entry = {"key": prefix}
        if old is not _MISSING:
            entry["from"] = _trim(old)
        if new is not _MISSING:
            entry["to"] = _trim(new)
        out.append(entry)


def _rows_by_key(text: str) -> tuple[list[str], dict]:
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = {}
    for i, row in enumerate(reader):
        # Rosters key on SLUG, deadcap and picks on their own first column; an
        # empty or duplicated key falls back to the row index so nothing is lost.
        key = (row.get(headers[0], "") or "").strip() if headers else ""
        if not key or key in rows:
            key = f"#{i}"
        rows[key] = row
    return headers, rows


def _diff_csv(old_text: Optional[str], new_text: str) -> list:
    out: list = []
    _, old_rows = _rows_by_key(old_text) if old_text is not None else ([], {})
    _, new_rows = _rows_by_key(new_text)
    for key in sorted(set(old_rows) | set(new_rows)):
        if len(out) >= _MAX_CHANGES:
            break
        before, after = old_rows.get(key), new_rows.get(key)
        if before is None:
            out.append({"key": key, "to": _trim(after)})
        elif after is None:
            out.append({"key": key, "from": _trim(before)})
        elif before != after:
            for col in sorted(set(before) | set(after)):
                if before.get(col) != after.get(col):
                    out.append({"key": f"{key}.{col}",
                                "from": _trim(before.get(col)),
                                "to": _trim(after.get(col))})
    return out


def _build_diff(path: Path, old_text: Optional[str], new_text: str):
    if len(new_text) > _MAX_DIFF_BYTES or len(old_text or "") > _MAX_DIFF_BYTES:
        return None, "too_large"
    if path.suffix == ".json":
        old = json.loads(old_text) if old_text else _MISSING
        new = json.loads(new_text)
        changes: list = []
        _diff_json(old, new, "", changes)
    else:
        changes = _diff_csv(old_text, new_text)
    truncated = len(changes) >= _MAX_CHANGES
    return changes, ("truncated" if truncated else None)


def record(path: Path, old_text: Optional[str], new_text: str):
    """Append one edit line. Silent no-op for anything outside the allowlist."""
    try:
        if not should_audit(path):
            return
        if old_text == new_text:
            return
        changes, note = _build_diff(path, old_text, new_text)
        if changes is not None and not changes:
            return  # a rewrite that changed only formatting
        ctx = current()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": ctx.get("actor") or "system",
            "method": ctx.get("method"),
            "path": ctx.get("path"),
            "file": path.name,
            "created": old_text is None,
        }
        if note:
            entry["note"] = note
        if changes is not None:
            entry["diff"] = changes
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with _LOCK:
            with EDITS_FILE.open("a") as fh:
                fh.write(line)
    except Exception as exc:  # never let forensics break a write
        logger.warning("audit: could not record edit to %s: %s", path, exc)


def read_entries(limit: int = 200, file: Optional[str] = None,
                 actor: Optional[str] = None, key: Optional[str] = None) -> list:
    """Most recent entries first. Filters are plain substring/equality matches."""
    if not EDITS_FILE.exists():
        return []
    out: list = []
    with EDITS_FILE.open() as fh:
        lines = fh.readlines()
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if file and entry.get("file") != file:
            continue
        if actor and entry.get("actor") != actor:
            continue
        if key and not any(key in (c.get("key") or "") for c in entry.get("diff", [])):
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out

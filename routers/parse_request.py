"""Ask a Claude Code cloud session to run /parse-boxscores — the "uploads are
done" button on /committees/stats.

The parse runs as a Claude Code scheduled task (routine) with an API trigger.
This module only fires it: one authenticated POST to the task's `/fire` URL,
whose token stays in `.env` so it never reaches a browser.

    POST /api/boxscores/parse-request   stats / rosters   fire the task
    GET  /api/boxscores/parse-request   stats / rosters   the last request

Config, in `.env` (none set: POST is a 503 and nothing else changes):

    NBN_PARSE_ROUTINE_URL    the task's Fire URL, .../routines/trig_.../fire
    NBN_PARSE_ROUTINE_TOKEN  its API token, shown once when generated
    NBN_PARSE_ROUTINE_URL_2, NBN_PARSE_ROUTINE_TOKEN_2   optional backup task

The backup is the same task on a second member's Claude account. It is tried
only when the first fire fails (its account out of usage, a revoked token);
one press never starts two runs.

Three rules:

- **Nothing to parse, nothing fired.** A request with no ready game (both
  sides uploaded) and no pending injury report is a 409. A cloud session that
  opens on an empty queue is a wasted run.
- **One run at a time, roughly.** A second request within `COOLDOWN_MINUTES`
  of the last fire is a 409 naming that run's session, unless `force` is set.
  We can't see when a run finishes, so this is a window, not a lock; the
  commit path itself refuses a game committed twice.
- **The fire text is data.** It carries the date and counts for the session's
  context only; the task's saved prompt is what tells it to run
  /parse-boxscores. The platform wraps fire text as untrusted, and nothing a
  member types reaches it.

State is `parse-requests.json` in the data dir: the last 50 requests.
Pinned by `tests/test_parse_request.py` (no network).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import boxscore_shots as shots
from . import injuries
from .auth import require_any_role
from .constants import DATA_DIR, PENDING_BOXSCORES_DIR, logger

router = APIRouter()

STATE_FILE = DATA_DIR / "parse-requests.json"
COOLDOWN_MINUTES = 20
KEEP = 50
BETA_HEADER = "experimental-cc-routine-2026-04-01"
TIMEOUT = 20.0

_lock = threading.Lock()


class ParseRequest(BaseModel):
    force: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> list:
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[-KEEP:], indent=2))
    os.replace(tmp, STATE_FILE)


def _queue() -> dict:
    """What a run would find: ready games by date, and pending injury reports."""
    games: dict[str, int] = {}
    waiting = 0
    for _, meta in shots._items(PENDING_BOXSCORES_DIR):
        if shots.is_ready(meta):
            d = meta.get("date") or "?"
            games[d] = games.get(d, 0) + 1
        else:
            waiting += 1
    inj = sorted(m.get("date") or "?" for _, m in injuries._items(injuries.PENDING_DIR))
    return {"ready": games, "one_side_only": waiting, "injury_reports": inj}


def _fire_text(queue: dict, who: str) -> str:
    parts = [f"{d}: {n} game(s)" for d, n in sorted(queue["ready"].items())]
    lines = [
        f"Requested from /committees/stats by {who}.",
        "Ready box scores: " + (", ".join(parts) or "none") + ".",
        "Pending injury reports: " + (", ".join(queue["injury_reports"]) or "none") + ".",
    ]
    if queue["one_side_only"]:
        lines.append(f"{queue['one_side_only']} game(s) have only one side uploaded and are not ready.")
    return "\n".join(lines)


def _routines() -> list[tuple[str, str]]:
    """The configured (url, token) pairs, primary first."""
    out = []
    for sfx in ("", "_2"):
        url = os.environ.get(f"NBN_PARSE_ROUTINE_URL{sfx}")
        token = os.environ.get(f"NBN_PARSE_ROUTINE_TOKEN{sfx}")
        if url and token:
            out.append((url, token))
    return out


def _post_fire(url: str, token: str, text: str) -> dict:
    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={"text": text},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"fire returned {r.status_code}: {r.text[:300]}")
    return r.json()


def _recent(rows: list) -> Optional[dict]:
    for row in reversed(rows):
        if row.get("ok"):
            try:
                at = datetime.fromisoformat(row["at"])
            except Exception:
                continue
            if _now() - at < timedelta(minutes=COOLDOWN_MINUTES):
                return row
            return None
    return None


@router.get("/api/boxscores/parse-request")
def last_parse_request(info: dict = Depends(require_any_role("stats", "rosters"))):
    rows = _load()
    return {
        "configured": bool(_routines()),
        "routines": len(_routines()),
        "last": rows[-1] if rows else None,
        "queue": _queue(),
        "cooldown_minutes": COOLDOWN_MINUTES,
    }


@router.post("/api/boxscores/parse-request")
def request_parse(body: ParseRequest = ParseRequest(), info: dict = Depends(require_any_role("stats", "rosters"))):
    routines = _routines()
    if not routines:
        raise HTTPException(status_code=503, detail="The parse task isn't set up on the server (NBN_PARSE_ROUTINE_URL / _TOKEN).")
    who = info.get("name") or "?"
    with _lock:
        queue = _queue()
        if not queue["ready"] and not queue["injury_reports"]:
            raise HTTPException(status_code=409, detail="Nothing to parse: no game has both sides uploaded, and no injury report is pending.")
        rows = _load()
        recent = _recent(rows)
        if recent and not body.force:
            raise HTTPException(status_code=409, detail={
                "message": f"A parse was requested by {recent.get('by')} at {recent.get('at')}; it may still be running.",
                "session_url": recent.get("session_url"),
            })
        row = {"at": _now().isoformat(), "by": who, "queue": queue}
        errors = []
        for n, (url, token) in enumerate(routines, 1):
            try:
                out = _post_fire(url, token, _fire_text(queue, who))
            except Exception as e:
                errors.append(f"task {n}: {str(e)[:200]}")
                continue
            row.update(ok=True, routine=n, session_url=out.get("claude_code_session_url"),
                       session_id=out.get("claude_code_session_id"))
            break
        else:
            row.update(ok=False, error="; ".join(errors))
        if errors and row.get("ok"):
            row["fallback_errors"] = errors
        rows.append(row)
        _save(rows)
    logger.info("[%s] POST boxscores/parse-request — ok=%s %s", who, row["ok"], row.get("session_url") or row.get("error"))
    if not row["ok"]:
        raise HTTPException(status_code=502, detail=f"Couldn't start the parse: {row['error']}")
    return {"ok": True, "session_url": row["session_url"], "queue": queue}

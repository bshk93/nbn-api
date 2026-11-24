"""Every version of a team's coaching settings — `coaching-history.jsonl`, and
`GET /api/coaching-history/{team}`.

`coaching-settings.json` holds one record per team and every save replaces it,
so last week's rotation was gone the moment a team saved this week's. The edit
log (`routers/audit.py`) does see each save, but it trims long values, so a
version can't be rebuilt from it. This file keeps each one whole.

One line per event, appended, never rewritten:

  * `saved`        — the team saved its profile/rotation: `values`, `minutes`.
  * `entered`      — a streamer put the settings into 2K: the `values` and
                     `minutes` entered, and `version`, the `updated_at` of the
                     save that was entered. Its `at` is the record's
                     `entered_at`, the same value `game-starters.jsonl` stamps
                     on each game played on it, which is how a version is
                     matched to its games.
  * `note`         — the team wrote its note to the streamer (`text`; empty
                     when the note was deleted).
  * `note_cleared` — a streamer marked the note handled (`text`).

Every line has `at` (ISO, UTC), `team`, `kind` and `by`. Lines written by
`seed_coaching_history.py` from the records as they stood carry `seeded: true`.

Private the same way the settings are: a team's own role sees its own team,
streamers and admin see any team, anyone else gets a 401/403. Not in the
audit allowlist and never written through `_atomic_write`, so nothing here
reaches the public edit log.

Writing a line never fails a save: `record` swallows its own errors. A gap in
the history is better than a lost lineup.

    venv/bin/python -m tests.test_coaching_history
"""
from __future__ import annotations

import json
import threading
from collections import deque
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from .auth import _resolve_token, has_role
from .constants import COACHING_HISTORY_FILE, VALID_TEAMS, logger

router = APIRouter()

KINDS = ("saved", "entered", "note", "note_cleared")
_LOCK = threading.Lock()


def record(team: str, kind: str, at: Optional[str], by: Optional[str], **fields) -> None:
    """Append one event. Never raises."""
    try:
        line = {"at": at, "team": team.upper(), "kind": kind, "by": by, **fields}
        text = json.dumps(line, separators=(",", ":")) + "\n"
        with _LOCK, COACHING_HISTORY_FILE.open("a") as fh:
            fh.write(text)
    except Exception as exc:
        logger.warning("coaching-history: couldn't record %s %s — %s", team, kind, exc)


def read(team: str, limit: Optional[int] = None) -> list[dict]:
    """One team's events, newest first. Streams the file and keeps at most
    `limit` lines, so a long history never sits in memory whole."""
    team = team.upper()
    keep: deque = deque(maxlen=limit) if limit else deque()
    try:
        fh = COACHING_HISTORY_FILE.open()
    except FileNotFoundError:
        return []
    with fh:
        for raw in fh:
            # Cheap test before parsing: most lines are other teams'.
            if f'"team":"{team}"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("team") == team:
                keep.append(e)
    return sorted(keep, key=lambda e: e.get("at") or "", reverse=True)


def has_team(team: str) -> bool:
    return bool(read(team, limit=1))


def _games(team: str) -> list[dict]:
    """The team's played games and the five each was started with. Public
    data (GET /api/game-starters), trimmed to what the history page needs."""
    from .game_starters import load  # here, not at the top: game_starters imports coaching_settings
    out = []
    for g in load():
        t = (g.get("teams") or {}).get(team)
        if not t:
            continue
        home = g.get("home_team") == team
        out.append({
            "date": g.get("date"), "season": g.get("season"), "game_type": g.get("game_type"),
            "opp": g.get("away_team") if home else g.get("home_team"), "home": home,
            "source": t.get("source"), "entered_at": t.get("entered_at"), "starters": t.get("starters"),
        })
    return out


@router.get("/api/coaching-history/{team}")
def get_coaching_history(team: str, limit: int = Query(200, ge=1, le=1000),
                         authorization: Optional[str] = Header(None)):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    info = _resolve_token(authorization)
    if not info:
        raise HTTPException(status_code=401, detail="Sign in to see coaching history")
    if not (has_role(info, team.lower()) or has_role(info, "streamer") or has_role(info, "admin")):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")
    events = read(team, limit)
    try:
        games = _games(team)
    except Exception as exc:
        logger.warning("coaching-history: couldn't read games for %s — %s", team, exc)
        games = []
    return {"team": team, "events": events, "games": games}

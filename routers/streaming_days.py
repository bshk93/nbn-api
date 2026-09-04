"""Per-date streaming status: a day of games marked done, and its YouTube VOD.

Two independent fields per date, same "orthogonal, independently settable"
shape as a schedule game's `streamer`/`stream` (routers/schedule.py): a date
can be marked done with no video linked yet, or have a video linked before
anyone's marked it done. `done` is one shared flag per date, not per
streamer — the same "whoever acts is stamped, not who's allowed to" shape
routers/coaching_settings.py already uses for pending/entered.

Deliberately not cross-checked against /api/schedule — a streamer marking a
date done doesn't need the server to agree a game existed there.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import STREAMING_DAYS_FILE, _streaming_days_lock
from .storage import _load_json, _save_json, log_write
from .auth import require_role
from .schedule import DATE_RE

router = APIRouter()


def load_streaming_days() -> dict:
    return _load_json(STREAMING_DAYS_FILE, {})


def save_streaming_days(data: dict):
    _save_json(STREAMING_DAYS_FILE, data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_date(date: str):
    if not DATE_RE.match(date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


class YoutubeBody(BaseModel):
    url: Optional[str] = None


@router.get("/api/streaming-days")
def get_streaming_days():
    return load_streaming_days()


@router.post("/api/streaming-days/{date}/done")
def mark_day_done(date: str, info: dict = Depends(require_role("streamer"))):
    _check_date(date)
    with _streaming_days_lock:
        data = load_streaming_days()
        rec = data.setdefault(date, {})
        rec["done"] = True
        rec["done_at"] = _now()
        rec["done_by"] = info.get("name")
        save_streaming_days(data)
        out = dict(rec)
    log_write(info, f"POST streaming-days/{date}/done")
    return out


@router.delete("/api/streaming-days/{date}/done")
def unmark_day_done(date: str, info: dict = Depends(require_role("streamer"))):
    _check_date(date)
    with _streaming_days_lock:
        data = load_streaming_days()
        rec = data.get(date)
        if not rec or not rec.get("done"):
            raise HTTPException(status_code=404, detail="That date isn't marked done")
        rec.pop("done", None)
        rec.pop("done_at", None)
        rec.pop("done_by", None)
        if not rec:
            data.pop(date, None)
        save_streaming_days(data)
        out = dict(data.get(date, {}))
    log_write(info, f"DELETE streaming-days/{date}/done")
    return out


@router.put("/api/streaming-days/{date}/youtube")
def set_day_youtube(date: str, body: YoutubeBody, info: dict = Depends(require_role("streamer"))):
    _check_date(date)
    url = (body.url or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")
    with _streaming_days_lock:
        data = load_streaming_days()
        rec = data.setdefault(date, {})
        if url:
            rec["youtube_url"] = url
            rec["youtube_set_at"] = _now()
            rec["youtube_set_by"] = info.get("name")
        else:
            rec.pop("youtube_url", None)
            rec.pop("youtube_set_at", None)
            rec.pop("youtube_set_by", None)
        if not rec:
            data.pop(date, None)
        save_streaming_days(data)
        out = dict(data.get(date, {}))
    log_write(info, f"PUT streaming-days/{date}/youtube")
    return out

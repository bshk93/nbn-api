"""Team-submitted 2K coach profiles, entered into the game by a streamer.

Historically a per-team Google Sheet, copy-pasted into 2K by whoever was about
to sim/stream that team's games. This is the same workflow moved on-site: a
team's own role (`atl`, `bos`, ... — the same per-team roles trading-block
writes already gate on) saves a settings blob for their team, and the
`streamer` role sees which teams have unentered changes, applies them in the
game, and marks the team as entered.

Deliberately schema-blind on the server. The exact field/option list is a
2K-version-coupled thing that changes year to year (see nbn-today's
coaching-config.js, the one place that vocabulary lives) — validating field
names or option values here would mean a second copy of that schema that
drifts. `values`/`minutes` are stored as opaque dicts; the only thing this
router owns is *who* may write a team's blob, and the pending/entered
bookkeeping around it. Point-buy/minutes-total validation is enforced
client-side only, same as the § 4.4 PDC ballot widget it borrows its UI from.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import COACHING_SETTINGS_FILE, VALID_TEAMS, _coaching_lock
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, has_role, require_role

router = APIRouter()


def load_coaching_settings() -> dict:
    return _load_json(COACHING_SETTINGS_FILE, {})


def save_coaching_settings(data: dict):
    _save_json(COACHING_SETTINGS_FILE, data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CoachingSettingsBody(BaseModel):
    values: dict = {}
    minutes: dict = {}


class EnterBody(BaseModel):
    # The `updated_at` the streamer's dashboard last saw for this team. If the
    # team saved again in the meantime, this won't match and the enter is
    # rejected — otherwise the streamer's click would silently clear `pending`
    # on a version of the settings that was never actually entered into the
    # game. Optional so a caller without a stale-read concern can skip it.
    expected_updated_at: Optional[str] = None


@router.get("/api/coaching-settings")
def get_coaching_settings():
    return load_coaching_settings()


@router.put("/api/coaching-settings/{team}")
def put_coaching_settings(
    team: str,
    body: CoachingSettingsBody,
    info: dict = Depends(get_token_info),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if not has_role(info, team.lower()) and not has_role(info, "admin"):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")
    with _coaching_lock:
        data = load_coaching_settings()
        prev = data.get(team, {})
        data[team] = {
            "values": body.values,
            "minutes": body.minutes,
            "updated_at": _now(),
            "updated_by": info.get("name"),
            "pending": True,
            "entered_at": prev.get("entered_at"),
            "entered_by": prev.get("entered_by"),
        }
        save_coaching_settings(data)
        rec = data[team]
    log_write(info, f"PUT coaching-settings/{team}")
    return rec


@router.post("/api/coaching-settings/{team}/enter")
def enter_coaching_settings(
    team: str,
    body: EnterBody = EnterBody(),
    info: dict = Depends(require_role("streamer")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    with _coaching_lock:
        data = load_coaching_settings()
        rec = data.get(team)
        if not rec:
            raise HTTPException(status_code=404, detail="No settings saved for this team yet")
        if body.expected_updated_at and rec.get("updated_at") != body.expected_updated_at:
            raise HTTPException(status_code=409, detail="Settings changed since you loaded them — refresh")
        rec["pending"] = False
        rec["entered_at"] = _now()
        rec["entered_by"] = info.get("name")
        save_coaching_settings(data)
    log_write(info, f"POST coaching-settings/{team}/enter")
    return rec

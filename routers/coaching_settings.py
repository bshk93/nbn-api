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

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .constants import COACHING_SETTINGS_FILE, VALID_TEAMS, _coaching_lock
from .storage import _load_json, _save_json, log_write
from .auth import _resolve_token, get_token_info, has_role, require_role
from . import game_day_notify

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


# A starter has to play near his slot: his 2K primary position, one step
# either side of it (PG-SG-SF-PF-C), or the secondary his team gave him
# (§ 2.3 already keeps that within one step, so today it adds nothing, but it
# is part of the rule as the league states it). The one rule this
# schema-blind store enforces, because it is a league rule rather than a
# coaching preference.
STARTER_SLOTS = ("PG", "SG", "SF", "PF", "C")


def starter_positions(primary: Optional[str], secondary: Optional[str]) -> list[str]:
    """Where a player may start, in PG→C order. Empty when his primary is unknown."""
    from .players import POSITION_ORDER
    if primary not in POSITION_ORDER:
        return []
    i = POSITION_ORDER.index(primary)
    ok = {POSITION_ORDER[j] for j in (i - 1, i, i + 1) if 0 <= j < len(POSITION_ORDER)}
    if secondary in POSITION_ORDER:
        ok.add(secondary)
    return [p for p in POSITION_ORDER if p in ok]


def _illegal_starters(minutes: dict, prev_minutes: dict) -> list[str]:
    """What's wrong with the starting five, one line per bad slot. A slot left
    exactly as it was already saved passes, so a rotation saved before this
    rule existed doesn't block the team's next save of something else (the
    Coaching Profile resends the minutes untouched). A player with no 2K
    primary yet can't be checked, and isn't blocked."""
    from .players import _primary_pos, load_player_bios
    bios = None
    problems = []
    for slot in STARTER_SLOTS:
        row = minutes.get(slot)
        slug = row.get("slug") if isinstance(row, dict) else None
        if not slug:
            continue
        prev = prev_minutes.get(slot)
        if isinstance(prev, dict) and prev.get("slug") == slug:
            continue
        primary = _primary_pos(slug)
        if primary is None:
            continue
        if bios is None:
            bios = load_player_bios()
        bio = bios.get(slug) or {}
        legal = starter_positions(primary, bio.get("secondary_pos"))
        if slot not in legal:
            problems.append(f"{bio.get('name') or slug} can't start at {slot}: "
                            f"his 2K position is {primary}, so he can start at {', '.join(legal)}")
    return problems


class EnterBody(BaseModel):
    # The `updated_at` the streamer's dashboard last saw for this team. If the
    # team saved again in the meantime, this won't match and the enter is
    # rejected — otherwise the streamer's click would silently clear `pending`
    # on a version of the settings that was never actually entered into the
    # game. Optional so a caller without a stale-read concern can skip it.
    expected_updated_at: Optional[str] = None


@router.get("/api/coaching-settings")
def get_coaching_settings(authorization: Optional[str] = Header(None)):
    """A team's coach profile is private to that team.

    `streamer` (and admin) get every team, since entering them into 2K is
    their job. Anyone else gets only the teams they hold the role for, and a
    visitor with no token gets nothing. Not a 401: pages fetch this alongside
    public data and should just see no settings.
    """
    data = load_coaching_settings()
    info = _resolve_token(authorization)
    if not info:
        return {}
    if has_role(info, "streamer") or has_role(info, "admin"):
        return data
    return {team: rec for team, rec in data.items() if has_role(info, team.lower())}


@router.get("/api/rotations")
def get_rotations():
    """Every team's depth chart order, public — who holds each slot, nothing else.

    The rest of the record stays private (above). Team pages use this for the
    Rosters tab's starting five, so any visitor sees the lineup the team set.
    Minutes, the RES flag and `values` are deliberately left out: the order
    may be public (decided 2026-10-16), the minutes may not.
    """
    out = {}
    for team, rec in load_coaching_settings().items():
        minutes = rec.get("minutes") if isinstance(rec, dict) else None
        if not isinstance(minutes, dict):
            continue
        slots = {slot: row["slug"] for slot, row in minutes.items()
                 if isinstance(row, dict) and isinstance(row.get("slug"), str) and row["slug"]}
        if slots:
            out[team] = slots
    return out


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
        problems = _illegal_starters(body.minutes or {}, prev.get("minutes") or {})
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))
        was_pending = bool(prev.get("pending"))
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
    game_day_notify.coaching_saved(team, was_pending, info.get("name"))
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
        was_pending = bool(rec.get("pending"))
        rec["pending"] = False
        rec["entered_at"] = _now()
        rec["entered_by"] = info.get("name")
        save_coaching_settings(data)
    log_write(info, f"POST coaching-settings/{team}/enter")
    if was_pending:
        game_day_notify.coaching_entered(team, info.get("name"))
    return rec

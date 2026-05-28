import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, PLAYER_BIOS_FILE, OVR_FILE, VALID_TEAMS,
    _ovr_lock, CURATOR_FIELDS, BIO_REWARD_FIELDS, BIO_REWARDS_FILE, _bio_rewards_lock,
)
from .storage import _load_json, _save_json, read_csv, log_write
from .auth import get_token_info, has_role, require_any_role, require_admin
from .bets import _award_bio_reward, NBY_BIO_REWARD

router = APIRouter()

VALID_POSITIONS = {"PG", "SG", "SF", "PF", "C"}


def load_player_bios() -> dict:
    return _load_json(PLAYER_BIOS_FILE, {})


def save_player_bios(data: dict):
    _save_json(PLAYER_BIOS_FILE, data)


def load_ovr() -> dict:
    return _load_json(OVR_FILE, {})


def save_ovr(data: dict):
    _save_json(OVR_FILE, data)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _display_name(canonical: str) -> str:
    """Convert 'LAST, FIRST' bio name to title-case 'First Last', mirroring JS displayNameFromBio."""
    if not canonical:
        return ''
    if ',' in canonical:
        last, _, first = canonical.partition(',')
        return f"{first.strip()} {last.strip()}".title()
    return canonical.title()


def _build_team_map() -> dict[str, str]:
    result = {}
    for team in VALID_TEAMS:
        path = DATA_DIR / f"{team.lower()}-roster.csv"
        if not path.exists():
            continue
        _, rows = read_csv(path)
        for row in rows:
            slug = row.get("SLUG", "").strip()
            if slug:
                result[slug] = team
    return result


def _scrub_trading_block(removals: dict[str, set[str]], bios: dict) -> None:
    """Remove players from their source teams' trading block entries after a trade or release.

    removals: {team_abbr: {slug, ...}}
    """
    from .roster_picks import load_trading_block, save_trading_block, _normalize_team_block
    data = load_trading_block()
    changed = False
    for team, slugs in removals.items():
        display_names = {_display_name(bios[s].get('name', '')) for s in slugs if s in bios}
        display_names.discard('')
        if not display_names:
            continue
        block    = _normalize_team_block(data.get(team, []))
        original = block["players"]
        updated  = [e for e in original if e.get('player') not in display_names]
        if len(updated) != len(original):
            block["players"] = updated
            data[team] = block
            changed = True
    if changed:
        save_trading_block(data)


# ── Pydantic models ───────────────────────────────────────────────────────────

class PlayerBio(BaseModel):
    name: str
    pos: list[str] = []
    dob: str = ""
    college: str = ""
    country: str = ""
    draft_year: Optional[int] = None
    draft_round: Optional[int] = None
    draft_pick: Optional[int] = None
    photo_url: str = ""
    height: str = ""
    weight: Optional[int] = None
    wingspan: str = ""
    type: str = ""
    cap_holds: dict[str, str] = {}
    salaries: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}
    guarantee_schedule: dict[str, list[dict]] = {}
    jersey_number: Optional[str] = None
    retired: bool = False
    notes: str = ""


class PlayerCreate(PlayerBio):
    slug: str


class JerseyUpdate(BaseModel):
    jersey_number: Optional[str] = None


class OvrEntry(BaseModel):
    date: str
    ovr: int


class OvrBatchEntry(BaseModel):
    slug: str
    date: str
    ovr: int


# ── Bio reward helpers ────────────────────────────────────────────────────────

def _is_bio_empty(val) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return val == ""
    return False


def _maybe_award_bio_reward(member: str, slug: str, old_bio: dict, new_bio: dict):
    """Award NB¥100 per BIO_REWARD_FIELD that goes from empty to non-empty for the first time."""
    newly_filled = [
        f for f in BIO_REWARD_FIELDS
        if _is_bio_empty(old_bio.get(f)) and not _is_bio_empty(new_bio.get(f))
    ]
    if not newly_filled:
        return
    with _bio_rewards_lock:
        rewarded = _load_json(BIO_REWARDS_FILE, {})
        new_fields = [f for f in newly_filled if f"{slug}:{f}" not in rewarded]
        if not new_fields:
            return
        for f in new_fields:
            rewarded[f"{slug}:{f}"] = True
        _save_json(BIO_REWARDS_FILE, rewarded)
    _award_bio_reward(member, NBY_BIO_REWARD * len(new_fields))


# ── Player routes ─────────────────────────────────────────────────────────────

@router.get("/api/players")
def get_players():
    return load_player_bios()


@router.post("/api/players")
def create_player(body: PlayerCreate, info: dict = Depends(require_any_role("rosters"))):
    slug = body.slug.strip().lower()
    if not slug:
        raise HTTPException(status_code=422, detail="slug is required")
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    bios = load_player_bios()
    if slug in bios:
        raise HTTPException(status_code=409, detail="Player already exists")
    data = body.model_dump()
    data.pop("slug")
    bios[slug] = data
    save_player_bios(bios)
    log_write(info, f"POST players — created {slug!r} ({body.name})")
    return {"ok": True, "slug": slug}


@router.put("/api/players/{slug}")
def update_player(slug: str, body: PlayerBio, info: dict = Depends(require_any_role("rosters", "curator"))):
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    bios = load_player_bios()
    existed = slug in bios
    old_bio = dict(bios.get(slug, {}))
    if has_role(info, "curator") and not has_role(info, "rosters") and not has_role(info, "admin"):
        existing = bios.get(slug, {})
        update_data = body.model_dump()
        for field in CURATOR_FIELDS:
            existing[field] = update_data[field]
        bios[slug] = existing
        new_bio = existing
    else:
        bios[slug] = body.model_dump()
        new_bio = bios[slug]
    save_player_bios(bios)
    log_write(info, f"PUT players/{slug} ({body.name})")
    if existed:
        _maybe_award_bio_reward(info["name"], slug, old_bio, new_bio)
    return {"ok": True}


@router.put("/api/players/{slug}/jersey")
def update_jersey(slug: str, body: JerseyUpdate, info: dict = Depends(get_token_info)):
    bios = load_player_bios()
    if slug not in bios:
        raise HTTPException(status_code=404, detail="Player not found")

    is_admin   = has_role(info, "admin")
    is_rosters = has_role(info, "rosters")
    team_roles = {r.upper() for r in info.get("roles", []) if r.upper() in VALID_TEAMS}

    if not is_admin and not is_rosters:
        if not team_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        team_map = _build_team_map()
        player_team = team_map.get(slug)
        if not player_team or player_team not in team_roles:
            raise HTTPException(status_code=403, detail="Player is not on your roster")

    if body.jersey_number is not None and not re.fullmatch(r'\d{1,2}', body.jersey_number):
        raise HTTPException(status_code=422, detail="jersey_number must be 1–2 digits")
    bios[slug]["jersey_number"] = body.jersey_number
    save_player_bios(bios)
    log_write(info, f"PUT players/{slug}/jersey — {body.jersey_number}")
    return {"ok": True}


# ── OVR routes ────────────────────────────────────────────────────────────────

@router.get("/api/ovr")
def get_ovr():
    return load_ovr()


@router.get("/api/ovr/current")
def get_ovr_current():
    history = load_ovr()
    return {slug: entries[-1]["ovr"] for slug, entries in history.items() if entries}


@router.put("/api/ovr/{slug}")
def put_ovr(slug: str, body: OvrEntry, info: dict = Depends(require_any_role("rosters", "curator"))):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if not (50 <= body.ovr <= 99):
        raise HTTPException(status_code=422, detail="ovr must be 50–99")
    with _ovr_lock:
        history = load_ovr()
        entries = history.get(slug, [])
        entries.append({"date": body.date, "ovr": body.ovr})
        entries.sort(key=lambda e: e["date"])
        history[slug] = entries
        save_ovr(history)
    log_write(info, f"PUT ovr/{slug} — {body.date} {body.ovr}")
    return {"ok": True}


@router.put("/api/ovr/{slug}/history")
def put_ovr_history(slug: str, body: list[OvrEntry], info: dict = Depends(require_any_role("rosters", "curator"))):
    bad_dates = [e.date for e in body if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', e.date)]
    if bad_dates:
        raise HTTPException(status_code=422, detail=f"bad date format: {bad_dates}")
    bad_ovr = [e.ovr for e in body if not (50 <= e.ovr <= 99)]
    if bad_ovr:
        raise HTTPException(status_code=422, detail="ovr must be 50–99")
    entries = sorted([{"date": e.date, "ovr": e.ovr} for e in body], key=lambda e: e["date"])
    with _ovr_lock:
        history = load_ovr()
        history[slug] = entries
        save_ovr(history)
    log_write(info, f"PUT ovr/{slug}/history — {len(entries)} entries")
    return {"ok": True}


@router.post("/api/ovr/batch")
def post_ovr_batch(body: list[OvrBatchEntry], info: dict = Depends(require_admin)):
    bad_dates = [e.slug for e in body if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', e.date)]
    if bad_dates:
        raise HTTPException(status_code=422, detail=f"bad date format for: {bad_dates}")
    with _ovr_lock:
        history = load_ovr()
        for entry in body:
            entries = history.get(entry.slug, [])
            entries.append({"date": entry.date, "ovr": entry.ovr})
            entries.sort(key=lambda e: e["date"])
            history[entry.slug] = entries
        save_ovr(history)
    log_write(info, f"POST ovr/batch — {len(body)} entries")
    return {"ok": True, "count": len(body)}


# ── Team map ──────────────────────────────────────────────────────────────────

@router.get("/api/team-map")
def get_team_map():
    return _build_team_map()

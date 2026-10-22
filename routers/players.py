import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .constants import (
    DATA_DIR, PLAYER_BIOS_FILE, OVR_FILE, ATTRIBUTES_FILE, VALID_TEAMS,
    _ovr_lock, _txn_lock, CURATOR_FIELDS, BIO_REWARD_FIELDS, BIO_REWARDS_FILE, _bio_rewards_lock, logger,
)
from .storage import _load_json, _save_json, read_csv, log_write
from .auth import get_token_info, has_role, require_any_role, require_admin
from .bets import _award_bio_reward, NBY_BIO_REWARD

router = APIRouter()

VALID_POSITIONS = {"PG", "SG", "SF", "PF", "C"}

# Rulebook § 2.3. Positions in floor order: a secondary position is one step
# up or down from the player's 2K primary, so a PG can only add SG and a C
# only PF. The primary itself always stays the 2K one.
POSITION_ORDER = ["PG", "SG", "SF", "PF", "C"]


def _primary_pos(slug: str, attrs: Optional[dict] = None) -> Optional[str]:
    """The player's 2K-designated primary position, from the latest scrape.
    Pass `attrs` (load_attributes()) when asking about many players: the file
    is 2.7MB, and loading it per player made a 400-player slate take a minute."""
    entries = (attrs if attrs is not None else load_attributes()).get(slug) or []
    pos = entries[-1].get("2k_pos") if entries else None
    return pos[0] if isinstance(pos, list) and pos and pos[0] in POSITION_ORDER else None


def _twok_secondary(slug: str, attrs: Optional[dict] = None) -> Optional[str]:
    """The secondary position 2K gives the player, from the latest scrape, as
    2K has it. It may break § 2.3's one step; effective_secondary drops it then."""
    entries = (attrs if attrs is not None else load_attributes()).get(slug) or []
    pos = entries[-1].get("2k_pos") if entries else None
    return pos[1] if isinstance(pos, list) and len(pos) > 1 and pos[1] in POSITION_ORDER else None


# `secondary_pos` on a bio: null means the team hasn't chosen, so 2K's own
# secondary stands if it is one step from the primary (none if it isn't);
# "none" means the team removed it; a position is the team's pick (§ 2.3).
NO_SECONDARY = "none"


def effective_secondary(slug: str, bio: dict, attrs: Optional[dict] = None) -> Optional[str]:
    """The secondary position a player actually has: his team's pick, or 2K's
    when that is one step from the primary (§ 2.3)."""
    sec = bio.get("secondary_pos") or None
    if sec == NO_SECONDARY:
        return None
    if sec:
        return sec
    twok = _twok_secondary(slug, attrs)
    return twok if twok in secondary_pos_options(_primary_pos(slug, attrs)) else None


def secondary_pos_options(primary: Optional[str]) -> list[str]:
    if primary not in POSITION_ORDER:
        return []
    i = POSITION_ORDER.index(primary)
    return [POSITION_ORDER[j] for j in (i - 1, i + 1) if 0 <= j < len(POSITION_ORDER)]


def _check_secondary_pos(slug: str, secondary: Optional[str]):
    if not secondary or secondary == NO_SECONDARY:
        return
    primary = _primary_pos(slug)
    if primary is None:
        raise HTTPException(status_code=422, detail="This player has no 2K primary position yet, so no secondary position can be set (§ 2.3)")
    options = secondary_pos_options(primary)
    if secondary not in options:
        raise HTTPException(status_code=422, detail=f"A {primary}'s secondary position must be {' or '.join(options)} (§ 2.3): got {secondary}")


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


def _block_key(name: str) -> str:
    """How trading-block names compare. The block stores display names, and
    older entries carry a hand-cased spelling ("Jared McCain") where
    `_display_name` now gives "Jared Mccain" — so a match must ignore case, or
    the same player reads as absent and gets listed twice."""
    return (name or "").strip().casefold()


def _scrub_trading_block(removals: dict[str, set[str]], bios: dict) -> None:
    """Remove players from their source teams' trading block entries after a trade or release.

    removals: {team_abbr: {slug, ...}}
    """
    from .roster_picks import load_trading_block, save_trading_block, _normalize_team_block
    data = load_trading_block()
    changed = False
    for team, slugs in removals.items():
        display_names = {_block_key(_display_name(bios[s].get('name', ''))) for s in slugs if s in bios}
        display_names.discard('')
        if not display_names:
            continue
        block    = _normalize_team_block(data.get(team, []))
        original = block["players"]
        updated  = [e for e in original if _block_key(e.get('player')) not in display_names]
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
    draft_team: Optional[str] = None
    photo_url: str = ""
    height: str = ""
    weight: Optional[int] = None
    wingspan: str = ""
    type: str = ""
    cap_holds: dict[str, str] = {}
    bird_tiers: dict[str, str] = {}
    salaries: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}
    guarantee_schedule: dict[str, list[dict]] = {}
    contracts: list[dict] = []
    jersey_number: Optional[str] = None
    secondary_pos: Optional[str] = None
    retired: bool = False
    notes: str = ""


class PlayerCreate(PlayerBio):
    slug: str


class TeamSettingsUpdate(BaseModel):
    jersey_number: Optional[str] = None
    secondary_pos: Optional[str] = None


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
def get_players(slugs: Optional[str] = None):
    # `?slugs=a,b,c` returns only those bios. The whole file is ~85KB gzipped,
    # which a page naming a dozen players (the homepage feed) shouldn't pay.
    # Unknown slugs are simply absent from the result.
    bios = load_player_bios()
    if slugs is None:
        return bios
    wanted = {s.strip() for s in slugs.split(",") if s.strip()}
    return {s: bios[s] for s in wanted if s in bios}


@router.post("/api/players")
def create_player(body: PlayerCreate, info: dict = Depends(require_any_role("rosters"))):
    slug = body.slug.strip().lower()
    if not slug:
        raise HTTPException(status_code=422, detail="slug is required")
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    _check_secondary_pos(slug, body.secondary_pos)
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
    with _txn_lock:
        bios = load_player_bios()
        existed = slug in bios
        old_bio = dict(bios.get(slug, {}))

        # § 2.3 applies only when this write changes secondary_pos, so a bio edit
        # that carries an older, now-invalid value along doesn't fail on it.
        if 'secondary_pos' in body.model_fields_set and body.secondary_pos != old_bio.get('secondary_pos'):
            _check_secondary_pos(slug, body.secondary_pos)
        if has_role(info, "curator") and not has_role(info, "rosters") and not has_role(info, "admin"):
            existing = bios.get(slug, {})
            update_data = body.model_dump()
            for field in CURATOR_FIELDS:
                existing[field] = update_data[field]
            bios[slug] = existing
            new_bio = existing
        else:
            # Merge onto the existing record instead of replacing it wholesale.
            # Any key the PlayerBio model doesn't declare (e.g. draft_team, stamped
            # directly by the draft-pick transaction) or that this particular caller
            # simply didn't include in its JSON body must survive untouched — a full
            # replace has silently wiped fields like this before (jersey_number).
            bios[slug] = {**old_bio, **body.model_dump(exclude_unset=True)}
            new_bio = bios[slug]
        save_player_bios(bios)
    log_write(info, f"PUT players/{slug} ({body.name})")
    if existed:
        _maybe_award_bio_reward(info["name"], slug, old_bio, new_bio)
    return {"ok": True}


@router.put("/api/players/{slug}/team-settings")
def update_team_settings(slug: str, body: TeamSettingsUpdate, info: dict = Depends(get_token_info)):
    bios = load_player_bios()
    if slug not in bios:
        raise HTTPException(status_code=404, detail="Player not found")

    # Deliberately narrower than most roster writes: only the player's own
    # team role may set these, never admin/rosters/bod — these are the team's
    # own cosmetic identity choices (jersey number, chosen secondary position),
    # not league-administered roster data.
    team_roles = {r.upper() for r in info.get("roles", []) if r.upper() in VALID_TEAMS}
    if not team_roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    team_map = _build_team_map()
    player_team = team_map.get(slug)
    if not player_team or player_team not in team_roles:
        raise HTTPException(status_code=403, detail="Player is not on your roster")

    if body.jersey_number is not None and not re.fullmatch(r'\d{1,2}', body.jersey_number):
        raise HTTPException(status_code=422, detail="jersey_number must be 1–2 digits")
    _check_secondary_pos(slug, body.secondary_pos)

    from . import coaching_settings, game_day_notify
    # The team page saves every changed player at once, so these requests
    # arrive in parallel. Without the lock each one reloads the file, sets its
    # own player and writes it back over the others, and only the last save
    # survives.
    with _txn_lock:
        bios = load_player_bios()
        pos_changed = (bios[slug].get("secondary_pos") or None) != (body.secondary_pos or None)
        # Tell the streamer once, on the way from nothing waiting to something.
        # Coaching settings waiting count too: the streamer enters both from
        # one card, so a team saving both gets one message, not two.
        # A failed check only costs the message, never the save.
        try:
            already_waiting = pos_changed and (coaching_settings.has_position_changes(player_team)
                                               or coaching_settings.coaching_pending(player_team))
        except Exception:
            already_waiting = True

        bios[slug]["jersey_number"] = body.jersey_number
        bios[slug]["secondary_pos"] = body.secondary_pos
        save_player_bios(bios)
    log_write(info, f"PUT players/{slug}/team-settings — jersey={body.jersey_number} secondary_pos={body.secondary_pos}")
    try:
        if pos_changed and not already_waiting and coaching_settings.has_position_changes(player_team):
            game_day_notify.positions_changed(player_team, info.get("name"))
    except Exception as exc:
        logger.warning("positions_changed check failed for %s: %s", player_team, exc)
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


# ── Attribute routes ──────────────────────────────────────────────────────────
# player-attributes.json is written by build/scrape_2k_attributes.py (2kratings.com
# scrape), not through this API — these routes are read-only.
# Stored as {slug: [snapshot, ...]} (append-only, oldest first, mirrors
# ovr-history.json) — a new snapshot is only appended when the scrape detects
# a real change, so "current" is always the last entry in the list.

def load_attributes() -> dict:
    return _load_json(ATTRIBUTES_FILE, {})


@router.get("/api/attributes")
def get_attributes():
    return load_attributes()


@router.get("/api/attributes/current")
def get_attributes_current():
    history = load_attributes()
    return {slug: entries[-1] for slug, entries in history.items() if entries}


@router.get("/api/attributes/{slug}")
def get_player_attributes(slug: str):
    entries = load_attributes().get(slug)
    if not entries:
        raise HTTPException(status_code=404, detail="No attribute data for this player")
    return entries[-1]


@router.get("/api/attributes/{slug}/history")
def get_player_attributes_history(slug: str):
    entries = load_attributes().get(slug)
    if not entries:
        raise HTTPException(status_code=404, detail="No attribute data for this player")
    return entries


# ── Ratings changes ───────────────────────────────────────────────────────────
# Derived, not stored: player-attributes.json is already a full append-only
# snapshot history per player (a new entry only lands when something really
# changed -- see the module comment above), so "who changed and by how much"
# is just consecutive-pair diffing within each player's own list. No separate
# log to write or keep in sync, and it surfaces every change already on
# record rather than only ones from scrapes run after this endpoint existed.

def _diff_2k_snapshots(prev: dict, cur: dict) -> dict:
    change = {}
    if prev.get("2k_ovr") != cur.get("2k_ovr"):
        change["ovr"] = {"old": prev.get("2k_ovr"), "new": cur.get("2k_ovr")}
    if prev.get("2k_pos") != cur.get("2k_pos"):
        change["position"] = {"old": prev.get("2k_pos"), "new": cur.get("2k_pos")}

    prev_attrs, cur_attrs = prev.get("attributes", {}), cur.get("attributes", {})
    changed_attrs = {
        k: {"old": prev_attrs.get(k), "new": v}
        for k, v in cur_attrs.items()
        if prev_attrs.get(k) != v
    }
    if changed_attrs:
        change["attributes"] = changed_attrs

    prev_badges, cur_badges = prev.get("badges", []), cur.get("badges", [])
    if prev_badges != cur_badges:
        prev_names = {b["name"] for b in prev_badges}
        cur_names = {b["name"] for b in cur_badges}
        gained = sorted(cur_names - prev_names)
        lost = sorted(prev_names - cur_names)
        prev_tiers = {b["name"]: b["tier"] for b in prev_badges}
        cur_tiers = {b["name"]: b["tier"] for b in cur_badges}
        retiered = {
            n: {"old": prev_tiers[n], "new": cur_tiers[n]}
            for n in (prev_names & cur_names) if prev_tiers[n] != cur_tiers[n]
        }
        if gained:
            change["badges_gained"] = gained
        if lost:
            change["badges_lost"] = lost
        if retiered:
            change["badges_retiered"] = retiered

    return change


@router.get("/api/ratings-changes")
def get_ratings_changes(
    slug: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = Query(2000, ge=1, le=20000),
):
    history = load_attributes()
    bios = load_player_bios()
    entries = []
    for player_slug, snapshots in history.items():
        if slug and player_slug != slug:
            continue
        for prev, cur in zip(snapshots, snapshots[1:]):
            change = _diff_2k_snapshots(prev, cur)
            if not change:
                continue
            date = cur.get("date", "")
            if since and date < since:
                continue
            if until and date > until:
                continue
            change["date"] = date
            change["slug"] = player_slug
            change["name"] = bios.get(player_slug, {}).get("name") or cur.get("2k_name") or player_slug
            # Stamped by the scrape at the moment it ran (see scrape_2k_attributes.py's
            # module docstring) -- never reconstructed here, so it's null for any
            # snapshot taken before "team" started being recorded.
            change["team"] = cur.get("team")
            # True when this change was a not-yet-rated stub (2K hadn't assigned
            # the player a real rating -- see scrape_2k_attributes.py's
            # STUB_OVR_THRESHOLD/DIVERGENCE_THRESHOLD) and so was never synced
            # into the live OVR badge/roster tables. Absent (falsy) for any
            # snapshot taken before this field existed, same as "team" above.
            change["ovr_stub"] = bool(cur.get("ovr_stub"))
            entries.append(change)
    entries.sort(key=lambda e: e["date"], reverse=True)
    return {"count": len(entries), "entries": entries[:limit]}


# ── Team map ──────────────────────────────────────────────────────────────────

@router.get("/api/team-map")
def get_team_map():
    return _build_team_map()

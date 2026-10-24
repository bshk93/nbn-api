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

from .constants import COACHING_SETTINGS_FILE, COACHING_NOTES_FILE, POSITION_CHECKS_FILE, DATA_DIR, VALID_TEAMS, _coaching_lock
from .storage import _load_json, _save_json, log_write, read_csv
from .league_time import league_today_str
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


# A starter has to play near his slot: his primary position, one step
# either side of it (PG-SG-SF-PF-C), or the secondary his team gave him. A
# secondary outside § 2.3's one step counts only once a streamer has OK'd it
# (`counted_secondary`). The one rule this schema-blind store enforces,
# because it is a league rule rather than a coaching preference.
STARTER_SLOTS = ("PG", "SG", "SF", "PF", "C")


def counted_secondary(slug: str, bio: dict, attrs: Optional[dict], entered: dict) -> Optional[str]:
    """The secondary that counts for starting: a legal one, or one outside
    § 2.3 that a streamer has OK'd by setting it in 2K (`entered`)."""
    from .players import effective_secondary, secondary_is_legal
    sec = effective_secondary(slug, bio, attrs)
    if secondary_is_legal(slug, bio, sec, attrs):
        return sec
    return sec if slug in entered and entered[slug] == sec else None


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


def _illegal_starters(minutes: dict, prev_minutes: dict, team: str = "") -> list[str]:
    """What's wrong with the starting five, one line per bad slot. A slot left
    exactly as it was already saved passes, so a rotation saved before this
    rule existed doesn't block the team's next save of something else (the
    Coaching Profile resends the minutes untouched). A player with no
    primary yet can't be checked, and isn't blocked."""
    from .players import _primary_pos, effective_secondary, load_player_bios
    bios = None
    entered = None
    problems = []
    for slot in STARTER_SLOTS:
        row = minutes.get(slot)
        slug = row.get("slug") if isinstance(row, dict) else None
        if not slug:
            continue
        prev = prev_minutes.get(slot)
        if isinstance(prev, dict) and prev.get("slug") == slug:
            continue
        if bios is None:
            bios = load_player_bios()
            entered = (load_position_checks().get(team) or {}).get("entered") or {}
        bio = bios.get(slug) or {}
        primary = _primary_pos(slug, None, bio)
        if primary is None:
            continue
        sec = counted_secondary(slug, bio, None, entered)
        legal = starter_positions(primary, sec)
        if slot not in legal:
            line = (f"{bio.get('name') or slug} can't start at {slot}: "
                    f"his primary is {primary}, so he can start at {', '.join(legal)}")
            if effective_secondary(slug, bio) == slot:
                line += f" ({slot} counts once a streamer has OK'd it)"
            problems.append(line)
    return problems


class EnterBody(BaseModel):
    # The `updated_at` the streamer's dashboard last saw for this team. If the
    # team saved again in the meantime, this won't match and the enter is
    # rejected — otherwise the streamer's click would silently clear `pending`
    # on a version of the settings that was never actually entered into the
    # game. Optional so a caller without a stale-read concern can skip it.
    expected_updated_at: Optional[str] = None


def _entered_snapshot(rec: dict):
    """(values, minutes) as a streamer last entered them into 2K, or None.

    Kept so the streamer dashboard can show what changed since. A record
    entered before the snapshot existed has none, but if it isn't pending its
    current settings are exactly what was entered, so they stand in.
    """
    if "entered_values" in rec:
        return rec.get("entered_values") or {}, rec.get("entered_minutes") or {}
    if rec.get("entered_at") and not rec.get("pending"):
        return rec.get("values") or {}, rec.get("minutes") or {}
    return None


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
        problems = _illegal_starters(body.minutes or {}, prev.get("minutes") or {}, team)
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
        entered = _entered_snapshot(prev)
        if entered:
            data[team]["entered_values"], data[team]["entered_minutes"] = entered
        save_coaching_settings(data)
        rec = data[team]
    log_write(info, f"PUT coaching-settings/{team}")
    # One message per batch of changes: a team that already has positions
    # waiting has already pinged the streamer, who will see these with them.
    try:
        was_pending = was_pending or has_position_changes(team) or note_open(team)
    except Exception:
        was_pending = True
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
        rec["entered_values"] = rec.get("values") or {}
        rec["entered_minutes"] = rec.get("minutes") or {}
        save_coaching_settings(data)
    log_write(info, f"POST coaching-settings/{team}/enter")
    if was_pending:
        game_day_notify.coaching_entered(team, info.get("name"))
    return rec


# ── Position checks ──────────────────────────────────────────────────────────
#
# A team's positions (§ 2.3) live on the player bios, but they only matter
# once a streamer has set them in 2K, and 2K resets them when ratings are
# updated. Our primary is frozen (routers/players.py, "Positions"), so after
# a re-rating 2K can have a different primary too. This tracks, per team,
# what the streamer last confirmed:
#
# - `entered`: slug → the secondary position as it was when a streamer last
#   said it was set (null for "none"). A roster player whose bio differs is a
#   change waiting to be entered. A player missing from it has never been
#   confirmed, so his secondary, if he has one, is waiting too.
# - `entered_primary`: slug → the primary he set, the same way.
# - `entered_2k`: slug → 2K's position list at that confirm. When the scrape
#   shows a different one, 2K has re-rated him and the game reset both, so
#   what he entered no longer stands and 2K's own positions are what it has.
# - `pins`: slugs the team ticked 📌 for: check this player before every
#   game, not just once after a change. Opt-in, and kept per team, so a
#   player traded away leaves his pin behind. A pin is due again on any
#   league day it hasn't been checked. A pin on a player with no secondary
#   means "make sure 2K has none", which is just as real a request.
# - `checked`: slug → the league date a streamer last confirmed him.
#
# Kept out of coaching-settings.json because that record is a team's private,
# full-replace coach profile; this is public (secondary positions already
# are), keyed by player, and needs to exist for a team that has no profile.


class PinsBody(BaseModel):
    # slug → pinned or not. Merged: a player left out keeps what he had.
    pins: dict[str, bool]


class ConfirmBody(BaseModel):
    # slug → the secondary position the streamer was shown and set (null for
    # none). Only these are confirmed, as these values: a change the team
    # made after the dashboard loaded stays waiting, since it won't match.
    seen: dict[str, Optional[str]]
    # slug → the jersey number the streamer was shown and set (null for
    # none). Same rule as `seen`: only these values are confirmed.
    jerseys: dict[str, Optional[str]] = {}
    # slug → the primary position the streamer was shown and set.
    primaries: dict[str, Optional[str]] = {}


def load_position_checks() -> dict:
    return _load_json(POSITION_CHECKS_FILE, {})


def save_position_checks(data: dict):
    _save_json(POSITION_CHECKS_FILE, data)


def _roster_slugs(team: str) -> list[str]:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return []
    _, rows = read_csv(path)
    return [r.get("SLUG", "").strip() for r in rows if r.get("SLUG", "").strip()]


def position_items(team: str, rec: dict, bios: dict, roster: list[str], today: str,
                   attrs: Optional[dict] = None) -> list[dict]:
    """The players a streamer has to look at for this team, in roster order:
    a primary or secondary that differs from what 2K has, or a locked player not yet
    checked today. Locked players already checked today are listed too, with
    `due` false, so the streamer sees what the team asked for.

    What 2K has is what the streamer last entered, or, before he has entered
    anything for that player, 2K's own secondary. So a team that leaves 2K's
    secondary alone, or picks the same one, gives the streamer nothing to do.

    Jersey numbers ride along: `jerseys` is the number 2K has, recorded when
    a team first changes it (`note_jersey_change`). We have no scrape of 2K's
    numbers, so a player with no record there has nothing to enter."""
    from .players import (_primary_pos, _twok_pos, _twok_primary, _twok_secondary, effective_secondary,
                          load_attributes, secondary_is_legal)
    # Loaded once: the file is 2.7MB, and reading it per player made the
    # all-teams list take over a minute, so nginx timed it out (2026-10-22).
    attrs = load_attributes() if attrs is None else attrs
    entered = rec.get("entered") or {}
    entered_primary = rec.get("entered_primary") or {}
    entered_2k = rec.get("entered_2k") or {}
    pins = set(rec.get("pins") or [])
    checked = rec.get("checked") or {}
    jerseys = rec.get("jerseys") or {}
    items = []
    for slug in roster:
        bio = bios.get(slug) or {}
        sec = effective_secondary(slug, bio, attrs)
        reset = slug in entered_2k and entered_2k[slug] != _twok_pos(slug, attrs)
        in_2k = (entered.get(slug) or None) if slug in entered and not reset else _twok_secondary(slug, attrs)
        changed = in_2k != sec
        primary = _primary_pos(slug, attrs, bio)
        primary_was = (entered_primary.get(slug) if slug in entered_primary and not reset
                       else _twok_primary(slug, attrs))
        # Unknown on either side is nothing a streamer can act on.
        primary_changed = bool(primary and primary_was and primary_was != primary)
        locked = slug in pins
        jersey = bio.get("jersey_number") or None
        # A cleared number is nothing to enter: 2K always shows one.
        jersey_changed = jersey is not None and slug in jerseys and (jerseys[slug] or None) != jersey
        if not (changed or primary_changed or jersey_changed or locked):
            continue
        items.append({
            "slug": slug,
            "name": bio.get("name") or slug,
            "primary": primary,
            "secondary": sec,
            # False: outside § 2.3's one step. Setting it is the streamer's OK.
            "legal": secondary_is_legal(slug, bio, sec, attrs),
            "was": in_2k,
            "changed": changed,
            "primary_was": primary_was,
            "primary_changed": primary_changed,
            "jersey": jersey,
            "jersey_was": jerseys.get(slug) or None,
            "jersey_changed": jersey_changed,
            "locked": locked,
            "due": changed or primary_changed or jersey_changed or checked.get(slug) != today,
        })
    return items


def position_exceptions(rec: dict, bios: dict, roster: list[str], attrs: dict) -> dict:
    """slug → {secondary, approved} for each roster player whose secondary is
    outside the pool (§ 2.3). Approved once a streamer has set that exact
    position in 2K, which is how a streamer OKs an exception."""
    from .players import effective_secondary, secondary_is_legal
    entered = rec.get("entered") or {}
    out = {}
    for slug in roster:
        bio = bios.get(slug) or {}
        sec = effective_secondary(slug, bio, attrs)
        if secondary_is_legal(slug, bio, sec, attrs):
            continue
        out[slug] = {"secondary": sec, "approved": slug in entered and entered[slug] == sec}
    return out


def team_positions_view(team: str, data: Optional[dict] = None, bios: Optional[dict] = None,
                        attrs: Optional[dict] = None) -> dict:
    from .players import load_player_bios, load_attributes
    data = load_position_checks() if data is None else data
    bios = load_player_bios() if bios is None else bios
    attrs = load_attributes() if attrs is None else attrs
    rec = data.get(team) or {}
    roster = _roster_slugs(team)
    pins = set(rec.get("pins") or [])
    return {
        "items": position_items(team, rec, bios, roster, league_today_str(), attrs),
        "exceptions": position_exceptions(rec, bios, roster, attrs),
        "pinned": [s for s in roster if s in pins],
        "checked_at": rec.get("checked_at"),
        "checked_by": rec.get("checked_by"),
    }


def _waiting(item: dict) -> bool:
    return item["changed"] or item["primary_changed"] or item["jersey_changed"]


def has_position_changes(team: str) -> bool:
    return any(_waiting(i) for i in team_positions_view(team)["items"])


def note_jersey_change(team: str, slug: str, old: Optional[str]) -> None:
    """A team changed a player's jersey number. Record the number 2K still
    has, unless one is already waiting: then 2K still has the first one."""
    with _coaching_lock:
        data = load_position_checks()
        jerseys = data.setdefault(team, {}).setdefault("jerseys", {})
        if slug not in jerseys:
            jerseys[slug] = old or None
            save_position_checks(data)


def coaching_pending(team: str) -> bool:
    return bool((load_coaching_settings().get(team) or {}).get("pending"))


@router.get("/api/position-checks")
def get_position_checks():
    """Every team's position checklist. Public: secondary positions already
    are, and a lock says nothing a team needs to keep to itself."""
    from .players import load_player_bios, load_attributes
    data, bios, attrs = load_position_checks(), load_player_bios(), load_attributes()
    return {team: team_positions_view(team, data, bios, attrs) for team in sorted(VALID_TEAMS)}


@router.get("/api/position-checks/{team}")
def get_team_position_checks(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    return team_positions_view(team)


@router.put("/api/position-checks/{team}/pins")
def put_position_pins(team: str, body: PinsBody, info: dict = Depends(get_token_info)):
    """Pin or unpin players for a position check before every game."""
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if not has_role(info, team.lower()) and not has_role(info, "admin"):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")
    roster = set(_roster_slugs(team))
    off = [s for s in body.pins if s not in roster]
    if off:
        raise HTTPException(status_code=422, detail=f"Not on the {team} roster: {', '.join(off)}")
    with _coaching_lock:
        data = load_position_checks()
        rec = data.setdefault(team, {})
        pins = set(rec.get("pins") or [])
        for slug, on in body.pins.items():
            (pins.add if on else pins.discard)(slug)
        rec["pins"] = sorted(pins)
        save_position_checks(data)
    log_write(info, f"PUT position-checks/{team}/pins — "
                    + ", ".join(f"{s}={'on' if on else 'off'}" for s, on in sorted(body.pins.items())))
    return team_positions_view(team)


@router.post("/api/position-checks/{team}/confirm")
def confirm_positions(team: str, body: ConfirmBody, info: dict = Depends(require_role("streamer"))):
    """A streamer has set these players' positions in 2K."""
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    today = league_today_str()
    with _coaching_lock:
        data = load_position_checks()
        rec = data.setdefault(team, {})
        before = {i["slug"] for i in position_items(team, rec, _bios(), _roster_slugs(team), today) if _waiting(i)}
        from .players import _twok_pos, load_attributes
        attrs = load_attributes()
        entered = rec.setdefault("entered", {})
        entered_primary = rec.setdefault("entered_primary", {})
        entered_2k = rec.setdefault("entered_2k", {})
        checked = rec.setdefault("checked", {})
        for slug, sec in body.seen.items():
            entered[slug] = sec or None
            checked[slug] = today
        for slug, prim in body.primaries.items():
            entered_primary[slug] = prim or None
        for slug in set(body.seen) | set(body.primaries):
            entered_2k[slug] = _twok_pos(slug, attrs)
        jerseys = rec.setdefault("jerseys", {})
        for slug, num in body.jerseys.items():
            jerseys[slug] = num or None
        rec["checked_at"] = _now()
        rec["checked_by"] = info.get("name")
        save_position_checks(data)
    log_write(info, f"POST position-checks/{team}/confirm — {', '.join(sorted(body.seen))}"
                    + (f"; jerseys {', '.join(sorted(body.jerseys))}" if body.jerseys else ""))
    view = team_positions_view(team)
    still = {i["slug"] for i in view["items"] if _waiting(i)}
    if before - still:
        game_day_notify.positions_set(team, info.get("name"))
    return view


def _bios() -> dict:
    from .players import load_player_bios
    return load_player_bios()


# ── Notes to the streamer ────────────────────────────────────────────────────
#
# One free-text note per team ("make sure X plays in the 4th"), for whoever
# streams its games. A streamer marks it clear once handled; the team saving
# again reopens it. Kept out of coaching-settings.json so a note never makes
# a team look like it has a coach profile, and so the full-replace profile
# PUT can't wipe it. Private the same way as the profile: the team and the
# streamers.

NOTE_MAX = 1000


class NoteBody(BaseModel):
    text: str = ""


class ClearNoteBody(BaseModel):
    # Same stale-read guard as EnterBody: a note rewritten since the streamer
    # loaded it stays open.
    expected_updated_at: Optional[str] = None


def load_coaching_notes() -> dict:
    return _load_json(COACHING_NOTES_FILE, {})


def save_coaching_notes(data: dict):
    _save_json(COACHING_NOTES_FILE, data)


def note_open(team: str) -> bool:
    note = load_coaching_notes().get(team) or {}
    return bool(note.get("text")) and not note.get("cleared_at")


@router.get("/api/coaching-notes")
def get_coaching_notes(authorization: Optional[str] = Header(None)):
    """Scoped like GET /api/coaching-settings: streamers get every team, a
    team role its own, anyone else nothing."""
    data = load_coaching_notes()
    info = _resolve_token(authorization)
    if not info:
        return {}
    if has_role(info, "streamer") or has_role(info, "admin"):
        return data
    return {team: rec for team, rec in data.items() if has_role(info, team.lower())}


@router.put("/api/coaching-notes/{team}")
def put_coaching_note(team: str, body: NoteBody, info: dict = Depends(get_token_info)):
    """Write the team's note. Empty text deletes it."""
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if not has_role(info, team.lower()) and not has_role(info, "admin"):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")
    text = body.text.strip()
    if len(text) > NOTE_MAX:
        raise HTTPException(status_code=422, detail=f"Keep the note under {NOTE_MAX} characters")
    with _coaching_lock:
        data = load_coaching_notes()
        prev = data.get(team) or {}
        was_open = bool(prev.get("text")) and not prev.get("cleared_at")
        if text:
            data[team] = {"text": text, "updated_at": _now(), "updated_by": info.get("name"),
                          "cleared_at": None, "cleared_by": None}
        else:
            data.pop(team, None)
        save_coaching_notes(data)
        rec = data.get(team)
    log_write(info, f"PUT coaching-notes/{team}" + ("" if text else " — deleted"))
    if text and not was_open:
        try:
            busy = coaching_pending(team) or has_position_changes(team)
        except Exception:
            busy = False
        game_day_notify.note_left(team, info.get("name"), busy)
    return rec or {}


@router.post("/api/coaching-notes/{team}/clear")
def clear_coaching_note(team: str, body: ClearNoteBody = ClearNoteBody(),
                        info: dict = Depends(require_role("streamer"))):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    with _coaching_lock:
        data = load_coaching_notes()
        rec = data.get(team)
        if not rec or not rec.get("text"):
            raise HTTPException(status_code=404, detail="No note for this team")
        if body.expected_updated_at and rec.get("updated_at") != body.expected_updated_at:
            raise HTTPException(status_code=409, detail="The note changed since you loaded it — refresh")
        was_open = not rec.get("cleared_at")
        rec["cleared_at"] = _now()
        rec["cleared_by"] = info.get("name")
        save_coaching_notes(data)
    log_write(info, f"POST coaching-notes/{team}/clear")
    if was_open:
        game_day_notify.note_cleared(team, info.get("name"))
    return rec

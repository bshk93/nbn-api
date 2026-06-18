import threading
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_token_info, has_role, require_admin, require_any_role
from .constants import (
    DRAFT_LIVE_FILE, DRAFT_LIVE_PICKS_FILE, DRAFT_SNAPSHOT_FILE,
    PICKS_FILE, PICKS_HEADERS, PLAYER_BIOS_FILE, VALID_TEAMS, logger,
)
from .roster_picks import enrich_swap_conveys, pick_to_response
from .storage import _load_json, _save_json, log_write, read_csv, write_csv

router = APIRouter()

EASTERN = ZoneInfo("America/New_York")
_draft_lock = threading.Lock()


def _default_state() -> dict:
    return {
        "year": 2026,
        "round1_date": None,
        "youtube_embed_url": "",
        "queue": {},
        "revealed": [],
    }


def load_draft_live() -> dict:
    data = _load_json(DRAFT_LIVE_FILE, None)
    if data is None:
        return _default_state()
    state = _default_state()
    state.update(data)
    return state


def save_draft_live(state: dict):
    _save_json(DRAFT_LIVE_FILE, state)


def _load_picks() -> list[dict]:
    # The live show is divorced from the permanent draft-picks.csv: it reads/writes its
    # own DRAFT_LIVE_PICKS_FILE. Until the first write forks it, read through to the
    # permanent file as the seed source (so the board is correct from the start).
    # To re-seed for a new show/rehearsal, delete DRAFT_LIVE_PICKS_FILE.
    src = DRAFT_LIVE_PICKS_FILE if DRAFT_LIVE_PICKS_FILE.exists() else PICKS_FILE
    if not src.exists():
        return []
    _, rows = read_csv(src)
    return rows


def _save_picks(picks: list[dict]):
    write_csv(DRAFT_LIVE_PICKS_FILE, PICKS_HEADERS, picks)


def get_window(state: dict, round_num: int, pick_num: int):
    """Return (start, end) as tz-aware datetimes, or (None, None) if not configured."""
    r1 = state.get("round1_date")
    if not r1:
        return None, None
    try:
        base = date.fromisoformat(r1) + timedelta(days=round_num - 1)
        noon = datetime(base.year, base.month, base.day, 12, 0, 0, tzinfo=EASTERN)
        start = noon + timedelta(minutes=(pick_num - 1) * 10)
        return start, start + timedelta(minutes=10)
    except Exception:
        return None, None


def _pick_owners(p: dict) -> list[str]:
    """Return list of owner team abbrs; handles pipe-separated and '?' fallback."""
    owner = p.get("OWNER", "").strip()
    if not owner or owner == "?":
        return [p.get("ORIG", "").strip().upper()]
    return [o.strip().upper() for o in owner.split("|") if o.strip()]


def auto_submit_loop_sync():
    """Check for expired pick windows and auto-submit queued players. Called every 30s."""
    with _draft_lock:
        state = load_draft_live()
        if not state.get("round1_date"):
            return

        year = state.get("year", 2026)
        picks = _load_picks()
        year_picks = [
            p for p in picks
            if p.get("YEAR") and int(p["YEAR"]) == year and p.get("PICK")
        ]
        year_picks.sort(key=lambda p: (int(p["ROUND"]), int(p["PICK"])))

        now = datetime.now(tz=EASTERN)
        drafted = {p["PLAYER"] for p in year_picks if p.get("PLAYER")}
        changed = False

        for p in year_picks:
            if p.get("PLAYER"):
                continue
            _, end = get_window(state, int(p["ROUND"]), int(p["PICK"]))
            if end is None or now < end:
                break
            owners = _pick_owners(p)
            # Find first available player from any owner's queue
            chosen: Optional[str] = None
            chosen_owner: Optional[str] = None
            for o in owners:
                raw = state["queue"].get(o)
                q_list = raw if isinstance(raw, list) else ([raw] if raw else [])
                for slug in q_list:
                    if slug not in drafted:
                        chosen, chosen_owner = slug, o
                        break
                if chosen:
                    break
            if chosen:
                p["PLAYER"] = chosen
                drafted.add(chosen)
                # Remove submitted player from that owner's queue; keep the rest
                raw = state["queue"].get(chosen_owner, [])
                remaining = [s for s in (raw if isinstance(raw, list) else [raw]) if s != chosen]
                if remaining:
                    state["queue"][chosen_owner] = remaining
                else:
                    state["queue"].pop(chosen_owner, None)
                state.setdefault("events", []).append({
                    "type": "pick",
                    "ts": datetime.now(tz=EASTERN).isoformat(),
                    "year": year,
                    "round": int(p["ROUND"]),
                    "pick": int(p["PICK"]),
                    "orig": p.get("ORIG", "").upper(),
                    "owner": p.get("OWNER", ""),
                    "player": chosen,
                    "auto": True,
                })
                changed = True
                logger.info(
                    "[auto-submit] %d R%d P%d %s → %s",
                    year, int(p["ROUND"]), int(p["PICK"]), p.get("OWNER"), chosen,
                )

        if changed:
            _save_picks(picks)
            save_draft_live(state)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/draft/live")
def get_draft_live():
    return load_draft_live()


@router.get("/api/draft/picks")
def get_draft_picks():
    """Picks as seen by the live show — served from the isolated DRAFT_LIVE_PICKS_FILE.
    Same response shape as GET /api/picks so the live page can consume it directly."""
    picks = [pick_to_response(p) for p in _load_picks()]
    enrich_swap_conveys(picks)
    return picks


class DraftLivePatch(BaseModel):
    year: Optional[int] = None
    round1_date: Optional[str] = None
    youtube_embed_url: Optional[str] = None
    highlights: Optional[dict] = None  # { slug: youtube_embed_url }
    pool_order: Optional[list] = None  # ordered list of slugs for Best Available


@router.patch("/api/draft/live")
def patch_draft_live(body: DraftLivePatch, info: dict = Depends(require_any_role("bod"))):
    with _draft_lock:
        state = load_draft_live()
        if body.year is not None:
            state["year"] = body.year
        if body.round1_date is not None:
            state["round1_date"] = body.round1_date
        if body.youtube_embed_url is not None:
            state["youtube_embed_url"] = body.youtube_embed_url
        if body.highlights is not None:
            state["highlights"] = {**state.get("highlights", {}), **body.highlights}
        if body.pool_order is not None:
            state["pool_order"] = body.pool_order
        save_draft_live(state)
    log_write(info, f"PATCH draft/live — {body.model_dump(exclude_none=True)}")
    return state


class QueueBody(BaseModel):
    players: list[str] = []  # ordered preference list; empty = clear queue


@router.put("/api/draft/queue/{team}")
def set_queue(team: str, body: QueueBody, info: dict = Depends(get_token_info)):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if not has_role(info, team.lower()) and not has_role(info, "admin") and not has_role(info, "bod"):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")

    with _draft_lock:
        state = load_draft_live()
        year = state.get("year", 2026)

        if body.players:
            bios = _load_json(PLAYER_BIOS_FILE, {})
            picks = _load_picks()
            drafted = {p["PLAYER"] for p in picks if p.get("PLAYER")}
            seen: set[str] = set()
            for slug in body.players:
                if slug in seen:
                    raise HTTPException(status_code=422, detail=f"Duplicate player: {slug!r}")
                seen.add(slug)
                bio = bios.get(slug)
                if not bio or bio.get("draft_year") != year:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Player '{slug}' not in {year} draft class",
                    )
                if slug in drafted:
                    raise HTTPException(status_code=422, detail=f"Player '{slug}' already drafted")
            state["queue"][team] = body.players
        else:
            state["queue"].pop(team, None)

        save_draft_live(state)

    log_write(info, f"PUT draft/queue/{team} — {len(body.players)} players")
    return {"team": team, "players": body.players}


class DraftPickBody(BaseModel):
    year: int
    round: int
    orig: str
    player: str


@router.post("/api/draft/pick")
def submit_pick(body: DraftPickBody, info: dict = Depends(get_token_info)):
    orig = body.orig.upper()
    if orig not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    is_privileged = has_role(info, "admin") or has_role(info, "bod")

    with _draft_lock:
        state = load_draft_live()

        picks = _load_picks()
        match = next(
            (p for p in picks
             if p.get("YEAR") and int(p["YEAR"]) == body.year
             and p.get("ROUND") and int(p["ROUND"]) == body.round
             and p.get("ORIG", "").upper() == orig),
            None,
        )
        if not match:
            raise HTTPException(status_code=404, detail="Pick not found")
        if match.get("PLAYER"):
            raise HTTPException(status_code=422, detail="Pick already submitted")

        pick_num = int(match["PICK"]) if match.get("PICK") else None
        if pick_num is None:
            raise HTTPException(status_code=422, detail="Pick number not assigned")

        owners = _pick_owners(match)

        if not is_privileged:
            if not any(has_role(info, o.lower()) for o in owners):
                raise HTTPException(status_code=403, detail="Not your pick")
            start, _ = get_window(state, body.round, pick_num)
            now = datetime.now(tz=EASTERN)
            if start is None or now < start:
                raise HTTPException(status_code=422, detail="Pick window has not opened yet")

        bios = _load_json(PLAYER_BIOS_FILE, {})
        bio = bios.get(body.player)
        if not bio or bio.get("draft_year") != body.year:
            raise HTTPException(
                status_code=422,
                detail=f"Player '{body.player}' not in {body.year} draft class",
            )
        if any(p.get("PLAYER") == body.player for p in picks if p.get("PLAYER")):
            raise HTTPException(status_code=422, detail="Player already drafted")

        match["PLAYER"] = body.player
        _save_picks(picks)

        # Remove submitted player from each owner's queue; keep remaining entries
        for o in owners:
            raw = state["queue"].get(o)
            if raw is None:
                continue
            q_list = raw if isinstance(raw, list) else [raw]
            remaining = [s for s in q_list if s != body.player]
            if remaining:
                state["queue"][o] = remaining
            else:
                state["queue"].pop(o, None)

        # Append to event log
        event = {
            "type": "pick",
            "ts": datetime.now(tz=EASTERN).isoformat(),
            "year": body.year,
            "round": body.round,
            "pick": pick_num,
            "orig": orig,
            "owner": "|".join(owners),
            "player": body.player,
        }
        state.setdefault("events", []).append(event)
        save_draft_live(state)

    log_write(info, f"POST draft/pick — {body.year} R{body.round} {orig} → {body.player}")
    return {"ok": True, "year": body.year, "round": body.round, "orig": orig, "player": body.player}


class RevealBody(BaseModel):
    round: int
    pick: int


@router.post("/api/draft/reveal")
def reveal_pick(body: RevealBody, info: dict = Depends(require_any_role("bod"))):
    key = f"{body.round}-{body.pick}"
    with _draft_lock:
        state = load_draft_live()
        if key not in state["revealed"]:
            state["revealed"].append(key)
            save_draft_live(state)
    log_write(info, f"POST draft/reveal — {key}")
    return {"revealed": key}


# ── Reassign pick ──────────────────────────────────────────────────────────────

class ReassignPickBody(BaseModel):
    year: int
    round: int
    orig: str       # original pick origin team
    new_owner: str  # team receiving the pick


@router.post("/api/draft/pick/reassign")
def reassign_pick(body: ReassignPickBody, info: dict = Depends(require_any_role("bod"))):
    orig = body.orig.upper()
    new_owner = body.new_owner.upper()
    if orig not in VALID_TEAMS or new_owner not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")

    with _draft_lock:
        picks = _load_picks()
        match = next(
            (p for p in picks
             if p.get("YEAR") and int(p["YEAR"]) == body.year
             and p.get("ROUND") and int(p["ROUND"]) == body.round
             and p.get("ORIG", "").upper() == orig),
            None,
        )
        if not match:
            raise HTTPException(status_code=404, detail="Pick not found")
        if match.get("PLAYER"):
            raise HTTPException(status_code=422, detail="Pick already submitted — cannot reassign")

        old_owner = match.get("OWNER") or orig
        match["OWNER"] = new_owner
        _save_picks(picks)

        # Append to event log
        state = load_draft_live()
        event = {
            "type": "reassign",
            "ts": datetime.now(tz=EASTERN).isoformat(),
            "year": body.year,
            "round": body.round,
            "orig": orig,
            "from_owner": old_owner,
            "to_owner": new_owner,
            "pick": int(match["PICK"]) if match.get("PICK") else None,
        }
        state.setdefault("events", []).append(event)
        save_draft_live(state)

    log_write(info, f"POST draft/pick/reassign — {body.year} R{body.round} {orig}: {old_owner} → {new_owner}")
    return {"ok": True, **event}


# ── Snapshot / restore ─────────────────────────────────────────────────────────

@router.post("/api/draft/snapshot")
def save_snapshot(info: dict = Depends(require_any_role("bod"))):
    """Save current draft state + picks as a named snapshot for dry-run rehearsals."""
    with _draft_lock:
        snapshot = {
            "saved_at": datetime.now(tz=EASTERN).isoformat(),
            "state": load_draft_live(),
            "picks": _load_picks(),
        }
        _save_json(DRAFT_SNAPSHOT_FILE, snapshot)
    log_write(info, "POST draft/snapshot — saved")
    return {"ok": True, "saved_at": snapshot["saved_at"]}


@router.get("/api/draft/snapshot")
def get_snapshot(info: dict = Depends(require_any_role("bod"))):
    """Return snapshot metadata (saved_at) without the full payload."""
    snap = _load_json(DRAFT_SNAPSHOT_FILE, None)
    if snap is None:
        raise HTTPException(status_code=404, detail="No snapshot saved")
    return {"saved_at": snap["saved_at"]}


@router.post("/api/draft/snapshot/restore")
def restore_snapshot(info: dict = Depends(require_any_role("bod"))):
    """Restore draft state + picks from the last saved snapshot."""
    with _draft_lock:
        snap = _load_json(DRAFT_SNAPSHOT_FILE, None)
        if snap is None:
            raise HTTPException(status_code=404, detail="No snapshot to restore")
        save_draft_live(snap["state"])
        _save_picks(snap["picks"])
    log_write(info, f"POST draft/snapshot/restore — restored to {snap['saved_at']}")
    return {"ok": True, "restored_to": snap["saved_at"]}


@router.post("/api/draft/reset")
def reset_live_draft(info: dict = Depends(require_any_role("bod"))):
    """Reset the live draft to a clean broadcast slate after rehearsals.

    Deletes the isolated DRAFT_LIVE_PICKS_FILE so picks re-seed from the current
    permanent draft-picks.csv, and clears the rehearsal-dirtied state (events,
    revealed, queue) while keeping year / round1_date / highlights / pool_order.
    """
    with _draft_lock:
        DRAFT_LIVE_PICKS_FILE.unlink(missing_ok=True)
        state = load_draft_live()
        state["events"] = []
        state["revealed"] = []
        state["queue"] = {}
        save_draft_live(state)
    log_write(info, "POST draft/reset — live draft reset to clean slate")
    return {"ok": True}

import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_token_info, has_role, require_admin, require_any_role
from .constants import (
    DRAFT_LIVE_FILE, DRAFT_LIVE_PICKS_FILE, DRAFT_SNAPSHOT_FILE,
    PICKS_FILE, PICKS_HEADERS, PLAYER_BIOS_FILE, VALID_TEAMS, logger,
)
from .players import _display_name
from .roster_picks import enrich_swap_conveys, pick_to_response
from .storage import _load_json, _save_json, log_write, read_csv, write_csv

router = APIRouter()

EASTERN = ZoneInfo("America/New_York")
_draft_lock = threading.Lock()

# Discord webhook for live draft-pick announcements. Posted to on reveal only.
# Kept best-effort: a failure here must never disrupt the live show (see
# _post_draft_webhook / reveal_pick).
DRAFT_WEBHOOK = "https://discord.com/api/webhooks/1517890636810817569/7MW228lLGQkhh7Ykx4ijABkm9xIrqfDhrLCICyE8vOtMeW_4tLdLHT3-88vAYp8k5yCj"

# Delay (seconds) before firing the reveal webhook, so the Discord post trails the
# broadcast stream delay instead of beating it.
DRAFT_WEBHOOK_DELAY_SECONDS = 30

DRAFT_TEAM_NAMES = {
    "ATL": "Atlanta Hawks",    "BKN": "Brooklyn Nets",       "BOS": "Boston Celtics",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls",      "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",       "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",       "MIL": "Milwaukee Bucks",      "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",  "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",    "PHI": "Philadelphia 76ers",   "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors",  "UTA": "Utah Jazz",            "WAS": "Washington Wizards",
}


def _default_state() -> dict:
    return {
        "year": 2026,
        "round1_date": None,
        "youtube_embed_url": "",
        "queue": {},
        "revealed": [],
        "trades": [],
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


def _owners_swap_aware(picks: list[dict], round_num: int, pick_num: int) -> list[str]:
    """True current owner(s) of a pick with pick-swap conveyance applied, matching
    the live board / frontend `pickOwners`. Used for draft authorization: the raw
    OWNER column does not reflect a conveyed swap, so a swap holder (e.g. WAS on a
    BOS pick that conveys) would otherwise be wrongly rejected. Unlike
    `_broadcast_pick_owners` it does not mask un-announced trades — authorization
    is against true current ownership, so the real owner can always draft."""
    enriched = [pick_to_response(p) for p in picks]
    enrich_swap_conveys(enriched)
    return _broadcast_pick_owners({"trades": []}, enriched, round_num, pick_num)


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

        owners = _owners_swap_aware(picks, body.round, pick_num)

        if not is_privileged:
            if not any(has_role(info, o.lower()) for o in owners):
                raise HTTPException(status_code=403, detail="Not your pick")
            # Floor: the draft must have opened (round 1, pick 1 = noon ET).
            draft_open, _ = get_window(state, 1, 1)
            now = datetime.now(tz=EASTERN)
            if draft_open is None or now < draft_open:
                raise HTTPException(status_code=422, detail="Draft has not started yet")
            # An owner may submit once they're genuinely up — either every pick
            # ahead of them in draft order is in (run ahead of schedule), or
            # their own scheduled window has arrived (so a stuck earlier pick
            # never blocks them forever).
            start, _ = get_window(state, body.round, pick_num)
            window_open = start is not None and now >= start
            earlier_in = all(
                p.get("PLAYER")
                for p in picks
                if p.get("YEAR") and int(p["YEAR"]) == body.year
                and p.get("ROUND") and p.get("PICK")
                and (int(p["ROUND"]), int(p["PICK"])) < (body.round, pick_num)
            )
            if not (window_open or earlier_in):
                raise HTTPException(
                    status_code=422,
                    detail="Not your turn yet — the picks ahead of you aren't in and your window hasn't opened",
                )

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

        # Note: the pick is NOT logged to the event log here. Like trades (which
        # log only on announce), a pick is added to the event log when the
        # presenter reveals it — see reveal_pick.
        save_draft_live(state)

    log_write(info, f"POST draft/pick — {body.year} R{body.round} {orig} → {body.player}")
    return {"ok": True, "year": body.year, "round": body.round, "orig": orig, "player": body.player}


class UnstagePickBody(BaseModel):
    year: int
    round: int
    orig: str


@router.post("/api/draft/pick/unstage")
def unstage_pick(body: UnstagePickBody, info: dict = Depends(get_token_info)):
    """Reverse an early lock-in. An owner may unstage their own pick only while
    their window has not started yet (i.e. it was submitted ahead of schedule);
    once on the clock the pick is final. Restores the player to the front of the
    owner's queue and removes the pick event so it's as if it never happened."""
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
        player = match.get("PLAYER")
        if not player:
            raise HTTPException(status_code=422, detail="Pick is not staged")

        pick_num = int(match["PICK"]) if match.get("PICK") else None
        if pick_num is None:
            raise HTTPException(status_code=422, detail="Pick number not assigned")

        if f"{body.round}-{pick_num}" in state.get("revealed", []):
            raise HTTPException(status_code=422, detail="Pick already revealed — cannot unstage")

        owners = _owners_swap_aware(picks, body.round, pick_num)

        if not is_privileged:
            if not any(has_role(info, o.lower()) for o in owners):
                raise HTTPException(status_code=403, detail="Not your pick")
            start, _ = get_window(state, body.round, pick_num)
            now = datetime.now(tz=EASTERN)
            if start is not None and now >= start:
                raise HTTPException(status_code=422, detail="Your pick window has started — pick is locked")

        match["PLAYER"] = ""
        _save_picks(picks)

        # Restore the player to the front of each owner's queue
        for o in owners:
            raw = state["queue"].get(o)
            q_list = [] if raw is None else (raw if isinstance(raw, list) else [raw])
            if player not in q_list:
                state["queue"][o] = [player] + q_list

        # Drop the matching (unrevealed) pick event so the log stays clean
        events = state.get("events", [])
        for i in range(len(events) - 1, -1, -1):
            ev = events[i]
            if (ev.get("type") == "pick" and ev.get("year") == body.year
                    and ev.get("round") == body.round and ev.get("pick") == pick_num
                    and ev.get("orig") == orig):
                events.pop(i)
                break
        save_draft_live(state)

    log_write(info, f"POST draft/pick/unstage — {body.year} R{body.round} {orig} (was {player})")
    return {"ok": True, "year": body.year, "round": body.round, "orig": orig, "player": player}


class RevealBody(BaseModel):
    round: int
    pick: int


def _ordinal(n: int) -> str:
    """1 -> '1st', 11 -> '11th', 22 -> '22nd', etc."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _broadcast_pick_owners(state: dict, picks: list[dict], round_num: int, pick_num: int) -> list[str]:
    """Resolve the on-air owner(s) of a pick exactly as the live board does
    (draft/live/index.html `displayOwners`): swap-conveyance, the swap-takes-from
    map, and masking of un-announced trades back to the pre-trade owner. `picks`
    is the enriched list (pick_to_response + enrich_swap_conveys). Returns a list
    of team abbreviations."""
    target = next((p for p in picks if p["round"] == round_num and p["pick"] == pick_num), None)
    if not target:
        return []
    orig = (target["orig"] or "").upper()

    # Un-announced trades flip OWNER in the file immediately but the board keeps
    # showing the old owner until the presenter announces — mask back to it.
    for t in state.get("trades", []):
        if t.get("announced"):
            continue
        for r in t.get("reassignments", []):
            if r.get("round") == round_num and (r.get("orig") or "").upper() == orig:
                masked = r.get("from_owner") or ""
                return [x.strip().upper() for x in masked.split("|") if x.strip()] or [orig]

    # swap-takes-from: a conveyed swap hands the swap_owner's own pick to `owner`
    year_picks = [p for p in picks if p["year"] == target["year"]]
    swap_takes_from = {}
    for p in year_picks:
        if p["swap_conveys"] is True and p["swap_owner"]:
            swap_takes_from[(p["round"], p["swap_owner"].upper())] = p["owner"]

    if target["swap_conveys"] is True:
        raw = target["swap_owner"]
    else:
        raw = swap_takes_from.get((round_num, orig), target["owner"])
    if not raw or raw == "?":
        return [orig]
    return [x.strip().upper() for x in str(raw).split("|") if x.strip()] or [orig]


def _build_pick_announcement(state: dict, round_num: int, pick_num: int):
    """Compose the Discord line for a just-revealed pick, or None if anything
    needed is missing. Read-only and fully guarded so a reveal can never fail on
    our account."""
    try:
        picks = [pick_to_response(p) for p in _load_picks()]
        enrich_swap_conveys(picks)
        target = next((p for p in picks if p["round"] == round_num and p["pick"] == pick_num), None)
        if not target or not target["player"]:
            return None
        owners = _broadcast_pick_owners(state, picks, round_num, pick_num)
        owner = owners[0] if owners else (target["orig"] or "")
        team_name = DRAFT_TEAM_NAMES.get(owner, owner)
        bio = _load_json(PLAYER_BIOS_FILE, {}).get(target["player"], {})
        prospect = _display_name(bio.get("name", "")) or target["player"]
        origin = (bio.get("college") or bio.get("country") or "").strip()
        msg = (f"With the {_ordinal(pick_num)} pick in the {target['year']} NBN Draft, "
               f"the {team_name} select {prospect}")
        if origin:
            msg += f", from {origin}"
        return msg + "."
    except Exception as exc:
        logger.warning("Draft announcement build failed for %s-%s: %s", round_num, pick_num, exc)
        return None


def _post_draft_webhook(message: str) -> None:
    """Fire-and-forget Discord post on a daemon thread so the live reveal never
    blocks on (or is broken by) network latency or a Discord outage."""
    def _send():
        try:
            time.sleep(DRAFT_WEBHOOK_DELAY_SECONDS)
            httpx.post(DRAFT_WEBHOOK, json={"content": message}, timeout=5)
        except Exception as exc:
            logger.warning("Draft Discord webhook failed: %s", exc)
    try:
        threading.Thread(target=_send, daemon=True).start()
    except Exception as exc:
        logger.warning("Draft Discord webhook thread failed to start: %s", exc)


@router.post("/api/draft/reveal")
def reveal_pick(body: RevealBody, info: dict = Depends(require_any_role("bod"))):
    key = f"{body.round}-{body.pick}"
    newly_revealed = False
    state = None
    with _draft_lock:
        state = load_draft_live()
        if key not in state["revealed"]:
            state["revealed"].append(key)
            # Log the pick to the event log on reveal (not on submit), mirroring
            # how trades are logged only when announced.
            picks = [pick_to_response(p) for p in _load_picks()]
            enrich_swap_conveys(picks)
            target = next(
                (p for p in picks if p["round"] == body.round and p["pick"] == body.pick),
                None,
            )
            if target and target["player"]:
                owners = _broadcast_pick_owners(state, picks, body.round, body.pick)
                # Idempotent: drop any prior pick event for this slot (e.g. a
                # straggler logged at submit time by older code) before appending.
                state["events"] = [
                    e for e in state.get("events", [])
                    if not (e.get("type") == "pick"
                            and e.get("round") == body.round
                            and e.get("pick") == body.pick)
                ]
                state["events"].append({
                    "type": "pick",
                    "ts": datetime.now(tz=EASTERN).isoformat(),
                    "year": target["year"],
                    "round": body.round,
                    "pick": body.pick,
                    "orig": (target["orig"] or "").upper(),
                    "owner": "|".join(owners),
                    "player": target["player"],
                })
            save_draft_live(state)
            newly_revealed = True
    # Announce exactly once, after the lock is released. Best-effort: any failure
    # is swallowed inside the helpers so the reveal response is unaffected.
    if newly_revealed:
        msg = _build_pick_announcement(state, body.round, body.pick)
        if msg:
            _post_draft_webhook(msg)
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


# ── Draft-day trades ────────────────────────────────────────────────────────────

def _find_pick(picks: list[dict], year: int, round_num: int, orig: str):
    return next(
        (p for p in picks
         if p.get("YEAR") and int(p["YEAR"]) == year
         and p.get("ROUND") and int(p["ROUND"]) == round_num
         and p.get("ORIG", "").upper() == orig.upper()),
        None,
    )


class TradeReassignment(BaseModel):
    round: int
    orig: str       # original pick origin team
    new_owner: str  # team receiving the pick


class TradeBody(BaseModel):
    year: int
    text: str = ""
    reassignments: list[TradeReassignment]


@router.post("/api/draft/trade")
def create_trade(body: TradeBody, info: dict = Depends(require_any_role("bod"))):
    """Stage a multi-pick draft-day trade.

    Ownership flips in the picks file immediately (mechanical-on-entry) so the
    on-the-clock / queue logic stays correct, but the trade is created
    un-announced — the live board masks the change until the presenter announces.
    """
    if not body.reassignments:
        raise HTTPException(status_code=422, detail="Trade needs at least one reassignment")

    norm = []
    for r in body.reassignments:
        orig = r.orig.upper()
        new_owner = r.new_owner.upper()
        if orig not in VALID_TEAMS or new_owner not in VALID_TEAMS:
            raise HTTPException(status_code=404, detail=f"Unknown team ({r.orig} → {r.new_owner})")
        norm.append((r.round, orig, new_owner))

    with _draft_lock:
        picks = _load_picks()
        # Validate every reassignment before mutating anything (atomic)
        matches = []
        for round_num, orig, new_owner in norm:
            match = _find_pick(picks, body.year, round_num, orig)
            if not match:
                raise HTTPException(status_code=404, detail=f"Pick not found: R{round_num} {orig}")
            if match.get("PLAYER"):
                raise HTTPException(status_code=422, detail=f"Pick already submitted — cannot trade R{round_num} {orig}")
            matches.append((match, round_num, orig, new_owner))

        reassignments = []
        for match, round_num, orig, new_owner in matches:
            from_owner = match.get("OWNER") or orig
            match["OWNER"] = new_owner
            reassignments.append({
                "round": round_num,
                "orig": orig,
                "from_owner": from_owner,
                "to_owner": new_owner,
                "pick": int(match["PICK"]) if match.get("PICK") else None,
            })
        _save_picks(picks)

        trade = {
            "id": uuid.uuid4().hex[:8],
            "ts": datetime.now(tz=EASTERN).isoformat(),
            "year": body.year,
            "text": body.text or "",
            "reassignments": reassignments,
            "announced": False,
        }
        state = load_draft_live()
        state.setdefault("trades", []).append(trade)
        save_draft_live(state)

    log_write(info, f"POST draft/trade — {body.year} {len(reassignments)} pick(s) staged ({trade['id']})")
    return {"ok": True, "trade": trade}


@router.post("/api/draft/trade/{trade_id}/announce")
def announce_trade(trade_id: str, info: dict = Depends(require_any_role("bod"))):
    """Reveal a staged trade: mark it announced and log a single Trade event."""
    with _draft_lock:
        state = load_draft_live()
        trade = next((t for t in state.get("trades", []) if t.get("id") == trade_id), None)
        if not trade:
            raise HTTPException(status_code=404, detail="Trade not found")
        if not trade.get("announced"):
            trade["announced"] = True
            state.setdefault("events", []).append({
                "type": "trade",
                "ts": datetime.now(tz=EASTERN).isoformat(),
                "year": trade.get("year"),
                "text": trade.get("text", ""),
                "reassignments": trade.get("reassignments", []),
                "trade_id": trade_id,
            })
            save_draft_live(state)

    log_write(info, f"POST draft/trade/{trade_id}/announce")
    return {"ok": True, "trade": trade}


@router.delete("/api/draft/trade/{trade_id}")
def cancel_trade(trade_id: str, info: dict = Depends(require_any_role("bod"))):
    """Undo a not-yet-announced trade: revert ownership and drop the trade."""
    with _draft_lock:
        state = load_draft_live()
        trades = state.get("trades", [])
        trade = next((t for t in trades if t.get("id") == trade_id), None)
        if not trade:
            raise HTTPException(status_code=404, detail="Trade not found")
        if trade.get("announced"):
            raise HTTPException(status_code=422, detail="Cannot cancel an announced trade")

        picks = _load_picks()
        for r in trade.get("reassignments", []):
            match = _find_pick(picks, trade.get("year"), r["round"], r["orig"])
            # Only revert if the pick still carries the traded-to owner and is unsubmitted
            if (match and not match.get("PLAYER")
                    and (match.get("OWNER") or "").upper() == r["to_owner"].upper()):
                match["OWNER"] = r["from_owner"]
        _save_picks(picks)

        state["trades"] = [t for t in trades if t.get("id") != trade_id]
        save_draft_live(state)

    log_write(info, f"DELETE draft/trade/{trade_id} — cancelled")
    return {"ok": True}


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
        state["trades"] = []
        save_draft_live(state)
    log_write(info, "POST draft/reset — live draft reset to clean slate")
    return {"ok": True}

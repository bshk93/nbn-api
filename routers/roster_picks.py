import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, RULES_DIR, VALID_TEAMS, PICKS_HEADERS, PICKS_FILE, TRADING_BLOCK_FILE,
    TEAM_STATE_FILE, TRADE_EXCEPTIONS_FILE, _rules_lock, _picks_lock, _state_lock,
    _deadcap_lock, _trade_exc_lock,
)
from .storage import read_csv, write_csv, _load_json, _save_json, log_write, _current_season_str, _current_league_year, _season_start
from .auth import get_token_info, has_role, require_role

router = APIRouter()

# ── Team path helper ──────────────────────────────────────────────────────────

def team_path(team: str, kind: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = DATA_DIR / f"{team.lower()}-{kind}.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{kind} file not found")
    return path


# ── Roster ────────────────────────────────────────────────────────────────────

@router.get("/api/roster/{team}")
def get_roster(team: str):
    path = team_path(team, "roster")
    headers, rows = read_csv(path)
    return {"headers": headers, "rows": rows}


@router.put("/api/roster/{team}")
def put_roster(team: str, body: dict, info: dict = Depends(require_role("rosters"))):
    path = team_path(team, "roster")
    existing_headers, _ = read_csv(path)
    headers = body.get("headers") or existing_headers
    rows = body.get("rows", [])
    write_csv(path, headers, rows)
    log_write(info, f"PUT roster/{team.upper()} — {len(rows)} rows")
    return {"ok": True}


# ── Dead Cap ──────────────────────────────────────────────────────────────────

@router.get("/api/deadcap/{team}")
def get_deadcap(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = DATA_DIR / f"{team.lower()}-deadcap.csv"
    if not path.exists():
        return []
    _, rows = read_csv(path)
    return rows


@router.put("/api/deadcap/{team}")
def put_deadcap(
    team: str,
    body: list[dict],
    info: dict = Depends(require_role("rosters")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    season_keys = sorted({
        k for row in body for k in row
        if k != "SLUG" and re.fullmatch(r'\d{2}-\d{2}', k)
    })
    headers = ["SLUG"] + season_keys
    path = DATA_DIR / f"{team.lower()}-deadcap.csv"
    with _deadcap_lock:
        write_csv(path, headers, body)
    log_write(info, f"PUT deadcap/{team} — {len(body)} rows")
    return {"ok": True}


# ── Rules ─────────────────────────────────────────────────────────────────────

RULE_SLUGS = {
    "overview":    "README.md",
    "trades":      "trades.md",
    "free-agency": "free-agency.md",
    "extensions":  "extensions.md",
    "options":     "options.md",
    "releases":    "releases.md",
    "two-way":     "two-way.md",
    "draft":       "draft.md",
}

RULE_LABELS = {
    "overview":    "League Overview",
    "trades":      "Trades",
    "free-agency": "Free Agency",
    "extensions":  "Extensions",
    "options":     "Options",
    "releases":    "Releases / Waivers",
    "two-way":     "Two-Way Contracts",
    "draft":       "Draft Picks",
}


@router.get("/api/rules")
def list_rules():
    return [{"slug": s, "label": RULE_LABELS[s]} for s in RULE_SLUGS]


@router.get("/api/rules/{slug}")
def get_rule(slug: str):
    if slug not in RULE_SLUGS:
        raise HTTPException(status_code=404, detail="Unknown rule slug")
    path = RULES_DIR / RULE_SLUGS[slug]
    if not path.exists():
        return {"slug": slug, "content": ""}
    return {"slug": slug, "content": path.read_text()}


class RuleUpdate(BaseModel):
    content: str


@router.put("/api/rules/{slug}")
def put_rule(slug: str, body: RuleUpdate, info: dict = Depends(require_role("rosters"))):
    if slug not in RULE_SLUGS:
        raise HTTPException(status_code=404, detail="Unknown rule slug")
    path = RULES_DIR / RULE_SLUGS[slug]
    with _rules_lock:
        path.write_text(body.content)
    log_write(info, f"PUT rules/{slug}")
    return {"ok": True}


# ── Picks ─────────────────────────────────────────────────────────────────────

def load_picks() -> list[dict]:
    if not PICKS_FILE.exists():
        return []
    _, rows = read_csv(PICKS_FILE)
    return rows


def save_picks(picks: list[dict]):
    write_csv(PICKS_FILE, PICKS_HEADERS, picks)


def picks_horizon_target_year() -> int:
    """The farthest draft year the picks ledger should currently cover: the
    current league year's start calendar year, plus a fixed 7-year horizon."""
    return 2000 + _season_start(_current_league_year()) + 7


def ensure_picks_horizon() -> list[int]:
    """Idempotently make sure every draft year through the current horizon has
    a full set of self-owned rows for all 30 teams. Only ever creates rows —
    never deletes or modifies an existing one — so this is always safe to
    re-run; retries, process restarts, and manual re-runs all converge to the
    same result. Returns the list of years actually created (empty if the
    ledger already reaches the horizon)."""
    target = picks_horizon_target_year()
    with _picks_lock:
        picks = load_picks()
        existing_years = {int(p["YEAR"]) for p in picks}
        start = max(existing_years, default=target - 1) + 1
        created = list(range(start, target + 1))
        for year in created:
            for team in sorted(VALID_TEAMS):
                for rnd in (1, 2):
                    picks.append({
                        "YEAR": str(year), "ROUND": str(rnd), "ORIG": team,
                        "OWNER": team, "PICK": "", "PLAYER": "",
                        "PROTECTED": "", "SWAP_OWNER": "", "NOTES": "",
                        "FROZEN": "", "FROZEN_REASON": "",
                    })
        if created:
            picks.sort(key=lambda p: (p["YEAR"], p["ROUND"], p["ORIG"]))
            save_picks(picks)
    return created


def pick_to_response(p: dict) -> dict:
    pick      = int(p["PICK"])      if p.get("PICK")      else None
    protected = int(p["PROTECTED"]) if p.get("PROTECTED") else None
    conveys = (pick > protected) if (pick is not None and protected is not None) else None
    swap_owner = p.get("SWAP_OWNER", "").strip() or None
    return {
        "year":          int(p["YEAR"]),
        "round":         int(p["ROUND"]),
        "orig":          p["ORIG"],
        "owner":         p["OWNER"],
        "pick":          pick,
        "player":        p.get("PLAYER", "").strip() or None,
        "protected":     protected,
        "conveys":       conveys,
        "swap_owner":    swap_owner,
        "swap_conveys":  None,   # filled in by enrich_swap_conveys
        "notes":         p.get("NOTES", ""),
        "frozen":        p.get("FROZEN", "").strip().upper() == "TRUE",
        "frozen_reason": p.get("FROZEN_REASON", ""),
    }


def enrich_swap_conveys(picks: list[dict]) -> None:
    # Build (year, round, owner) -> pick number for cross-referencing
    owner_pick = {}
    for p in picks:
        if p["pick"] is not None:
            owner_pick[(p["year"], p["round"], p["owner"])] = p["pick"]
    for p in picks:
        if p["swap_owner"] and p["pick"] is not None:
            swap_num = owner_pick.get((p["year"], p["round"], p["swap_owner"]))
            if swap_num is not None:
                # swap conveys when this pick is better (lower #) than swap_owner's own pick
                p["swap_conveys"] = p["pick"] < swap_num
        # else stays None


@router.get("/api/picks")
def get_all_picks():
    picks = [pick_to_response(p) for p in load_picks()]
    enrich_swap_conveys(picks)
    return picks


@router.get("/api/picks/{team}")
def get_team_picks(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    all_picks = [pick_to_response(p) for p in load_picks()]
    enrich_swap_conveys(all_picks)
    def matches(p):
        owner = p["owner"]
        if owner == "?":
            return p["orig"] == team          # fallback: serve to orig team
        return team in owner.split("|")       # single team or pipe-separated candidates
    return [p for p in all_picks if matches(p)]


class PickUpsert(BaseModel):
    owner: str
    pick: Optional[int] = None
    player: Optional[str] = None
    protected: Optional[int] = None
    swap_owner: Optional[str] = None
    notes: str = ""
    frozen: bool = False
    frozen_reason: str = ""


class PickEntry(BaseModel):
    year: int
    round: str    # "1st" or "2nd"
    team: str = "Own"   # origin from picks CSV (e.g. "Own", "from NYK")
    notes: str = ""


@router.put("/api/picks/{year}/{rnd}/{orig}")
def upsert_pick(
    year: int, rnd: int, orig: str,
    body: PickUpsert,
    info: dict = Depends(require_role("rosters")),
):
    orig = orig.upper()
    if orig not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if rnd not in (1, 2):
        raise HTTPException(status_code=422, detail="Round must be 1 or 2")
    owner_raw = body.owner.strip().upper()
    if owner_raw == "?":
        owner = "?"
    else:
        parts = [p.strip() for p in owner_raw.split("|") if p.strip()]
        bad = [p for p in parts if p not in VALID_TEAMS]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown owner team(s): {', '.join(bad)}")
        owner = "|".join(parts)   # normalized, no spaces around |

    swap_owner = body.swap_owner.upper() if body.swap_owner else ""
    if swap_owner and swap_owner not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown swap_owner team: {swap_owner}")
    updated = {"YEAR": str(year), "ROUND": str(rnd), "ORIG": orig,
               "OWNER":      owner,
               "PICK":       str(body.pick)      if body.pick      is not None else "",
               "PLAYER":     body.player.strip() if body.player    else "",
               "PROTECTED":  str(body.protected) if body.protected is not None else "",
               "SWAP_OWNER": swap_owner,
               "NOTES":      body.notes,
               "FROZEN":        "TRUE" if body.frozen else "",
               "FROZEN_REASON": body.frozen_reason.strip()}

    with _picks_lock:
        picks = load_picks()
        for i, p in enumerate(picks):
            if p.get("YEAR") == str(year) and p.get("ROUND") == str(rnd) and p.get("ORIG", "").upper() == orig:
                old_owner = p.get("OWNER", "")
                picks[i] = updated
                save_picks(picks)
                action = f"traded to {owner}" if old_owner.upper() != owner else "updated"
                log_write(info, f"PUT picks {year} R{rnd} {orig} — {action} pick={body.pick} protected={body.protected} swap_owner={swap_owner or None} notes={body.notes!r} frozen={body.frozen}")
                responses = [pick_to_response(p) for p in picks]
                enrich_swap_conveys(responses)
                return next(r for r in responses if r["orig"] == orig and r["year"] == year and r["round"] == rnd)

        # New pick
        picks.append(updated)
        picks.sort(key=lambda p: (p["YEAR"], p["ROUND"], p["ORIG"]))
        save_picks(picks)
        log_write(info, f"PUT picks {year} R{rnd} {orig} (new) — owner={owner}")
        responses = [pick_to_response(p) for p in picks]
        enrich_swap_conveys(responses)
        return next(r for r in responses if r["orig"] == orig and r["year"] == year and r["round"] == rnd)


@router.delete("/api/picks/{year}/{rnd}/{orig}")
def delete_pick(year: int, rnd: int, orig: str, info: dict = Depends(require_role("rosters"))):
    orig = orig.upper()
    with _picks_lock:
        picks = load_picks()
        new_picks = [p for p in picks
                     if not (p.get("YEAR") == str(year) and p.get("ROUND") == str(rnd)
                             and p.get("ORIG", "").upper() == orig)]
        if len(new_picks) == len(picks):
            raise HTTPException(status_code=404, detail="Pick not found")
        save_picks(new_picks)
    log_write(info, f"DELETE picks {year} R{rnd} {orig}")
    return {"ok": True}


# ── Trading Block ──────────────────────────────────────────────────────────────

def _normalize_team_block(raw) -> dict:
    """Coerce legacy flat-array format to {players, picks} shape."""
    if isinstance(raw, list):
        return {"players": raw, "picks": []}
    return {"players": raw.get("players", []), "picks": raw.get("picks", [])}


def load_trading_block() -> dict:
    if not TRADING_BLOCK_FILE.exists():
        return {t: {"players": [], "picks": []} for t in sorted(VALID_TEAMS)}
    raw = _load_json(TRADING_BLOCK_FILE, {})
    return {team: _normalize_team_block(val) for team, val in raw.items()}


def save_trading_block(data: dict):
    _save_json(TRADING_BLOCK_FILE, data)


class TradingBlockEntry(BaseModel):
    player: str
    notes: str = ""


class TeamTradeBlock(BaseModel):
    players: list[TradingBlockEntry] = []
    picks: list[PickEntry] = []


@router.get("/api/trading-block")
def get_trading_block():
    return load_trading_block()


@router.put("/api/trading-block/{team}")
def put_trading_block(
    team: str,
    body: TeamTradeBlock,
    info: dict = Depends(get_token_info),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if not has_role(info, team.lower()) and not has_role(info, "admin"):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")
    data = load_trading_block()
    data[team] = body.model_dump()
    save_trading_block(data)
    n_players = len(body.players)
    n_picks   = len(body.picks)
    log_write(info, f"PUT trading-block/{team} — {n_players} players, {n_picks} picks")
    return {"ok": True}


# ── Team State ────────────────────────────────────────────────────────────────

DEFAULT_SEASON_STATE: dict = {
    "hard_cap": None, "hard_cap_reason": "", "mle_used": 0, "bae_used": False, "mle_type": None,
}

CAP_RANK = {None: 0, "first_apron": 1, "second_apron": 2}


def load_team_state() -> dict:
    return _load_json(TEAM_STATE_FILE, {})


def save_team_state(data: dict):
    _save_json(TEAM_STATE_FILE, data)


def get_season_state(state: dict, team: str, season: str) -> dict:
    return state.get(team, {}).get(season, dict(DEFAULT_SEASON_STATE))


def _bae_available(state: dict, team: str, cur_season: str) -> bool:
    seasons = sorted(state.get(team, {}).keys())
    prior = [s for s in seasons if s < cur_season]
    if not prior:
        return True
    return not state[team][prior[-1]].get("bae_used", False)


def _maybe_set_hard_cap(ts: dict, new_cap: str, reason: str):
    if CAP_RANK.get(new_cap, 0) > CAP_RANK.get(ts.get("hard_cap"), 0):
        ts["hard_cap"] = new_cap
        ts["hard_cap_reason"] = reason


class TeamSeasonState(BaseModel):
    hard_cap: Optional[str] = None
    hard_cap_reason: str = ""
    mle_used: int = 0
    bae_used: bool = False
    mle_type: Optional[str] = None


@router.get("/api/team-state")
def get_all_team_state():
    state = load_team_state()
    cur = _current_league_year()
    return {
        team: {
            "seasons": state.get(team, {}),
            "current": get_season_state(state, team, cur),
            "bae_available": _bae_available(state, team, cur),
        }
        for team in sorted(VALID_TEAMS)
    }


@router.get("/api/team-state/{team}")
def get_team_state(team: str, season: Optional[str] = None):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    state = load_team_state()
    cur = season or _current_league_year()
    return {
        "season": cur,
        **get_season_state(state, team, cur),
        "bae_available": _bae_available(state, team, cur),
        "seasons": state.get(team, {}),
    }


@router.put("/api/team-state/{team}")
def put_team_state(
    team: str,
    body: TeamSeasonState,
    season: Optional[str] = None,
    info: dict = Depends(require_role("rosters")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if body.hard_cap not in (None, "first_apron", "second_apron"):
        raise HTTPException(status_code=422, detail="hard_cap must be null, 'first_apron', or 'second_apron'")
    if body.mle_type not in (None, "room", "ntmle", "tmle"):
        raise HTTPException(status_code=422, detail="mle_type must be null, 'room', 'ntmle', or 'tmle'")
    cur = season or _current_league_year()
    with _state_lock:
        state = load_team_state()
        if team not in state:
            state[team] = {}
        state[team][cur] = body.model_dump()
        save_team_state(state)
    log_write(info, f"PUT team-state/{team}/{cur} — hard_cap={body.hard_cap} mle_used={body.mle_used} bae_used={body.bae_used} mle_type={body.mle_type}")
    return {"season": cur, **state[team][cur], "bae_available": _bae_available(state, team, cur)}


# ── Trade Exceptions (TPE) ──────────────────────────────────────────────────
# Rulebook § 4.1a. Not itself a tradeable asset — belongs to the team that
# banked it. Creation/consumption is manual for now (entered from the league's
# roster/cap spreadsheet); nothing here computes a TPE from a trade
# transaction or lets the trade builder draw one down.

def load_trade_exceptions() -> dict:
    return _load_json(TRADE_EXCEPTIONS_FILE, {})


def save_trade_exceptions(data: dict):
    _save_json(TRADE_EXCEPTIONS_FILE, data)


def _tpe_response(e: dict) -> dict:
    expired = e["expires_date"] < datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {**e, "expired": expired}


class TradeExceptionCreate(BaseModel):
    amount: int
    acquired_date: Optional[str] = None  # YYYY-MM-DD, defaults to today
    expires_date: Optional[str] = None   # YYYY-MM-DD, defaults to acquired_date + 365 days
    note: str = ""


class TradeExceptionUpdate(BaseModel):
    remaining: Optional[int] = None
    expires_date: Optional[str] = None
    note: Optional[str] = None


@router.get("/api/trade-exceptions")
def get_all_trade_exceptions():
    data = load_trade_exceptions()
    return {team: [_tpe_response(e) for e in data.get(team, [])] for team in sorted(VALID_TEAMS)}


@router.get("/api/trade-exceptions/{team}")
def get_team_trade_exceptions(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    data = load_trade_exceptions()
    return [_tpe_response(e) for e in data.get(team, [])]


@router.post("/api/trade-exceptions/{team}")
def create_trade_exception(
    team: str,
    body: TradeExceptionCreate,
    info: dict = Depends(require_role("rosters")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if body.amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    acquired = body.acquired_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        acquired_dt = datetime.strptime(acquired, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="acquired_date must be YYYY-MM-DD")
    if body.expires_date:
        expires = body.expires_date
    else:
        expires = (acquired_dt + timedelta(days=365)).strftime("%Y-%m-%d")
    entry = {
        "id": uuid.uuid4().hex[:12],
        "amount": body.amount,
        "remaining": body.amount,
        "acquired_date": acquired,
        "expires_date": expires,
        "note": body.note,
    }
    with _trade_exc_lock:
        data = load_trade_exceptions()
        data.setdefault(team, []).append(entry)
        save_trade_exceptions(data)
    log_write(info, f"POST trade-exceptions/{team} — ${body.amount:,} exp {expires}")
    return _tpe_response(entry)


@router.patch("/api/trade-exceptions/{team}/{exc_id}")
def update_trade_exception(
    team: str,
    exc_id: str,
    body: TradeExceptionUpdate,
    info: dict = Depends(require_role("rosters")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    with _trade_exc_lock:
        data = load_trade_exceptions()
        for e in data.get(team, []):
            if e["id"] == exc_id:
                if body.remaining is not None:
                    if body.remaining < 0 or body.remaining > e["amount"]:
                        raise HTTPException(status_code=422, detail="remaining must be between 0 and amount")
                    e["remaining"] = body.remaining
                if body.expires_date is not None:
                    e["expires_date"] = body.expires_date
                if body.note is not None:
                    e["note"] = body.note
                save_trade_exceptions(data)
                log_write(info, f"PATCH trade-exceptions/{team}/{exc_id}")
                return _tpe_response(e)
    raise HTTPException(status_code=404, detail="Trade exception not found")


@router.delete("/api/trade-exceptions/{team}/{exc_id}")
def delete_trade_exception(team: str, exc_id: str, info: dict = Depends(require_role("rosters"))):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    with _trade_exc_lock:
        data = load_trade_exceptions()
        team_list = data.get(team, [])
        new_list = [e for e in team_list if e["id"] != exc_id]
        if len(new_list) == len(team_list):
            raise HTTPException(status_code=404, detail="Trade exception not found")
        data[team] = new_list
        save_trade_exceptions(data)
    log_write(info, f"DELETE trade-exceptions/{team}/{exc_id}")
    return {"ok": True}

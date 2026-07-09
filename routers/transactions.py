import json
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, CAP_LEVELS_FILE, TRANSACTIONS_FILE, VALID_TEAMS,
    _txn_lock, _deadcap_lock, _state_lock, _picks_lock,
    ROSTER_MAX, ROSTER_OFFSEASON_MAX, SALARY_MATCH_TIER1_CAP, SALARY_MATCH_TIER2_CAP,
)
from .storage import read_csv, write_csv, _load_json, log_write, _parse_dollar, _season_start, _season_for_date, _current_league_year
from .auth import require_role
from .players import load_player_bios, save_player_bios, _build_team_map, _scrub_trading_block
from .roster_picks import (
    load_picks, save_picks,
    load_team_state, save_team_state, get_season_state,
    DEFAULT_SEASON_STATE, _maybe_set_hard_cap,
)

router = APIRouter()


# ── CheckResult must come first — forward-referenced by validation helpers ────

class CheckResult(BaseModel):
    check: str
    passed: bool
    level: str = "error"   # "error" blocks; "warning" allows force-through
    message: str


# ── Transaction load/save ─────────────────────────────────────────────────────

def _load_transactions() -> list[dict]:
    return _load_json(TRANSACTIONS_FILE, [])


def _append_transaction(txn: dict):
    txns = _load_transactions()
    txns.append(txn)
    TRANSACTIONS_FILE.write_text(json.dumps(txns, indent=2))


# ── Pydantic models ───────────────────────────────────────────────────────────

class ContractIn(BaseModel):
    type: str = "player"  # "player" or "two-way"
    salaries: dict[str, str] = {}
    cap_holds: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}
    guarantee_schedule: dict[str, list[dict]] = {}


class SignDetails(BaseModel):
    player: str
    team: str
    contract: ContractIn
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None


class PickIn(BaseModel):
    year: int
    round: int
    orig: str
    pick_number: Optional[int] = None


class PickDetails(BaseModel):
    player: str
    team: str
    pick: PickIn
    # A draft pick conveys only the player's draft rights — no contract. The
    # contract is attached later via a separate `sign` step once the cap is
    # finalized. Kept optional for backward compatibility with old payloads.
    contract: Optional[ContractIn] = None


class TransactionIn(BaseModel):
    type: str
    date: str
    description: str = ""
    details: dict
    force: bool = False


class OptionDetails(BaseModel):
    player: str
    decision: str       # "accept" or "decline"
    option_type: str    # "PLAYER_OPT" or "TEAM_OPT"
    year: str           # e.g. "26-27"
    cap_hold_type: str = "UFA"
    cap_hold_amount: Optional[str] = None
    bird_tier: Optional[str] = None  # QVFA, EQVFA, Non-QVFA — only used on decline


class GuaranteeDetails(BaseModel):
    player: str
    year: str           # e.g. "26-27"


class ReleaseDetails(BaseModel):
    player: str


class RenounceDetails(BaseModel):
    player: str


class ConvertTwoWayDetails(BaseModel):
    player: str
    contract: ContractIn


class SignPickDetails(BaseModel):
    # Signs a player whose draft rights a team holds to their first contract.
    player: str
    contract: ContractIn


class TradeAsset(BaseModel):
    type: str                        # "player" or "pick"
    slug: Optional[str] = None
    year: Optional[int] = None
    round: Optional[int] = None
    orig: Optional[str] = None
    protection: Optional[int] = None
    swap_with: Optional[str] = None


class TradeTransfer(BaseModel):
    from_team: str
    to_team: str
    assets: list[TradeAsset]


class TradeIn(BaseModel):
    transfers: list[TradeTransfer]
    legality: str = "tbd"
    # Team abbr -> exception type ("ntmle" | "tmle" | "room_exception") used to
    # absorb that team's incoming salary in lieu of matching outgoing salary.
    # Omit or null for teams matching normally.
    exceptions: dict[str, Optional[str]] = {}


class TradeValidateInput(BaseModel):
    transfers: list[TradeTransfer]
    is_sign_and_trade: bool = False
    exceptions: dict[str, Optional[str]] = {}


class TradeValidationResult(BaseModel):
    legal: bool
    checks: list[CheckResult]
    fact_sheet: dict


# ── Apply helpers ─────────────────────────────────────────────────────────────

def _apply_option(details: OptionDetails, info: dict) -> Optional[str]:
    """Applies an option decision. Returns the player's current team for storage."""
    if details.option_type not in ("PLAYER_OPT", "TEAM_OPT"):
        raise HTTPException(status_code=422, detail="option_type must be PLAYER_OPT or TEAM_OPT")
    if details.decision not in ("accept", "decline"):
        raise HTTPException(status_code=422, detail="decision must be 'accept' or 'decline'")
    if details.cap_hold_type not in ("UFA", "RFA"):
        raise HTTPException(status_code=422, detail="cap_hold_type must be UFA or RFA")

    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    bio = bios[details.player]
    holds = bio.get("cap_holds") or {}

    if holds.get(details.year) != details.option_type:
        raise HTTPException(
            status_code=422,
            detail=f"Player {details.player!r} has no {details.option_type} for year {details.year!r}",
        )

    if details.decision == "accept":
        bio["cap_holds"] = {yr: typ for yr, typ in holds.items()
                            if yr != details.year}
    else:
        key = _season_start(details.year)
        bio["salaries"] = {yr: amt for yr, amt in bio.get("salaries", {}).items()
                           if _season_start(yr) < key}
        if details.cap_hold_amount:
            bio["salaries"][details.year] = details.cap_hold_amount
        bio["cap_holds"] = {yr: typ for yr, typ in holds.items()
                            if yr != details.year and _season_start(yr) < key}
        bio["cap_holds"][details.year] = details.cap_hold_type
        if details.bird_tier:
            bio["bird_tiers"] = {**bio.get("bird_tiers", {}), details.year: details.bird_tier}

    save_player_bios(bios)
    log_write(info, f"TXN option — {details.player} {details.decision} {details.option_type} {details.year}")
    return team


def _apply_guarantee(details: GuaranteeDetails, info: dict) -> Optional[str]:
    """Fully guarantees a single NON_GTD contract year. Sets the full salary as the
    guaranteed amount, clears the NON_GTD cap hold for that year, and drops any
    guarantee date / step schedule so the year reads as fully guaranteed thereafter.
    Returns the player's current team for storage."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    bio = bios[details.player]
    holds = bio.get("cap_holds") or {}
    if holds.get(details.year) != "NON_GTD":
        raise HTTPException(
            status_code=422,
            detail=f"Player {details.player!r} has no NON_GTD year {details.year!r} to guarantee",
        )
    salary = (bio.get("salaries") or {}).get(details.year)
    if not salary:
        raise HTTPException(
            status_code=422,
            detail=f"Player {details.player!r} has no salary recorded for {details.year!r}",
        )

    bio["cap_holds"] = {yr: typ for yr, typ in holds.items() if yr != details.year}
    bio["guaranteed"] = {**bio.get("guaranteed", {}), details.year: salary}
    bio["guarantee_dates"] = {k: v for k, v in bio.get("guarantee_dates", {}).items()
                              if k != details.year}
    bio["guarantee_schedule"] = {k: v for k, v in bio.get("guarantee_schedule", {}).items()
                                 if k != details.year}
    save_player_bios(bios)
    log_write(info, f"TXN guarantee — {details.player} {details.year} ({team})")
    return team


def _dead_cap_from_schedule(schedule: list, salary: str, txn_date: str) -> Optional[str]:
    """Compute dead cap for a NON_GTD year given a multi-step guarantee schedule."""
    def parse_dollar(s) -> float:
        if not s:
            return 0.0
        return float(re.sub(r"[$,\s]", "", str(s)) or 0)

    cumulative = 0.0
    fully_gtd = False
    for step in schedule:
        step_date = step.get("date") or ""
        if step_date and txn_date < step_date:
            continue
        step_amount = step.get("amount")
        if not step_amount:
            fully_gtd = True
            break
        cumulative += parse_dollar(step_amount)

    if fully_gtd:
        return salary
    if cumulative > 0:
        return f"${round(cumulative):,}"
    return None


def _retained_history(bio: dict, cur_season: str):
    """Entries to keep when a new contract is applied to a player: every season
    already played (<= cur_season) plus any future season still guaranteed from a
    prior deal. Earnings history is never silently dropped — only unplayed,
    non-guaranteed future years are released. Returns
    (salaries, guaranteed, guarantee_dates, guarantee_schedule)."""
    prior_gtd = bio.get("guaranteed", {})
    keep = lambda s: s <= cur_season or s in prior_gtd
    return (
        {k: v for k, v in bio.get("salaries", {}).items() if keep(k)},
        {k: v for k, v in bio.get("guaranteed", {}).items() if keep(k)},
        {k: v for k, v in bio.get("guarantee_dates", {}).items() if keep(k)},
        {k: v for k, v in bio.get("guarantee_schedule", {}).items() if keep(k)},
    )


def _apply_release(details: ReleaseDetails, txn_date: str, info: dict) -> tuple[str, dict]:
    """Removes player from roster, converts guaranteed salary to dead cap. Returns (team, dead_cap)."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    cur_season = _season_for_date(txn_date)

    bio = bios[details.player]
    holds_map = bio.get("cap_holds") or {}
    guaranteed = bio.get("guaranteed", {})
    guarantee_dates = bio.get("guarantee_dates", {})

    dead_cap: dict[str, str] = {}
    for season, salary in bio.get("salaries", {}).items():
        if season < cur_season:
            continue
        hold_type = holds_map.get(season)
        if hold_type in ("TEAM_OPT", "UFA", "RFA"):
            continue
        if hold_type == "NON_GTD":
            schedule = bio.get("guarantee_schedule", {}).get(season)
            if schedule:
                dead = _dead_cap_from_schedule(schedule, salary, txn_date)
                if dead:
                    dead_cap[season] = dead
            else:
                gtd_date = guarantee_dates.get(season)
                if gtd_date and txn_date >= gtd_date:
                    dead_cap[season] = salary
                elif guaranteed.get(season):
                    dead_cap[season] = guaranteed[season]
            continue
        dead_cap[season] = guaranteed.get(season, salary)

    # Preserve earnings history: keep every season already played (<= cur_season)
    # plus any future season that's guaranteed (the player still collects it, as
    # the dead-cap amount). Drop only unplayed, non-guaranteed future years.
    def _kept(season: str) -> bool:
        return season <= cur_season or season in dead_cap
    bio["salaries"] = {
        season: (dead_cap[season] if season in dead_cap and season > cur_season else salary)
        for season, salary in bio.get("salaries", {}).items()
        if _kept(season)
    }
    bio["cap_holds"] = {}
    bio["guaranteed"] = {k: v for k, v in guaranteed.items() if _kept(k)}
    bio["guarantee_dates"] = {k: v for k, v in guarantee_dates.items() if _kept(k)}
    bio["guarantee_schedule"] = {k: v for k, v in bio.get("guarantee_schedule", {}).items() if _kept(k)}
    bio["type"] = ""
    save_player_bios(bios)

    # Remove from roster CSV
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    write_csv(path, headers, rows)

    # Write dead cap to team's deadcap CSV
    if dead_cap:
        dc_path = DATA_DIR / f"{team.lower()}-deadcap.csv"
        with _deadcap_lock:
            if dc_path.exists():
                _, dc_rows = read_csv(dc_path)
            else:
                dc_rows = []
            dc_rows = [r for r in dc_rows if r.get("SLUG", "").strip() != details.player]
            dc_rows.append({"SLUG": details.player, **dead_cap})
            season_keys = sorted({
                k for row in dc_rows for k in row
                if k != "SLUG" and re.fullmatch(r'\d{2}-\d{2}', k)
            })
            write_csv(dc_path, ["SLUG"] + season_keys, dc_rows)

    _scrub_trading_block({team: {details.player}}, bios)
    log_write(info, f"TXN release — {details.player} from {team}")
    return team, dead_cap


def _season_after(season: str) -> str:
    """'25-26' -> '26-27'."""
    a, b = season.split("-")
    return f"{(int(a) + 1) % 100:02d}-{(int(b) + 1) % 100:02d}"


def _apply_renounce(details: RenounceDetails, txn_date: str, info: dict) -> str:
    """Renounce a free-agent hold: remove the player from the roster and clear the
    cap hold, turning them into an unsigned free agent. Unlike a release, no dead cap
    is created — a renounced free agent is owed nothing. Earnings history is preserved.
    Returns the player's former team."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    cur_season = _season_for_date(txn_date)
    next_season = _season_after(cur_season)
    bio = bios[details.player]
    holds = bio.get("cap_holds") or {}

    # Must be a clean free-agent hold for the current FA period: the player's
    # earliest cap hold is a UFA/RFA for the upcoming season (their contract has
    # lapsed). Players under contract, or with an unresolved option, are excluded.
    fa_years = sorted(y for y, t in holds.items() if t in ("UFA", "RFA"))
    earliest_hold = min(holds) if holds else None
    if not fa_years or earliest_hold not in fa_years or fa_years[0] > next_season:
        raise HTTPException(
            status_code=422,
            detail=(f"Player {details.player!r} is not a free-agent hold for {next_season}. "
                    "Renounce only applies to UFA/RFA holds — decline any option, or "
                    "use Release to waive a player under contract."),
        )

    # Preserve earnings; drop the hold salary and the renounced cap hold(s).
    past, past_gtd, past_gtd_dates, past_gtd_sched = _retained_history(bio, cur_season)
    bio["salaries"] = past
    bio["guaranteed"] = past_gtd
    bio["guarantee_dates"] = past_gtd_dates
    bio["guarantee_schedule"] = past_gtd_sched
    bio["cap_holds"] = {y: t for y, t in holds.items() if y > next_season}
    save_player_bios(bios)

    # Remove from roster CSV
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    write_csv(path, headers, rows)

    _scrub_trading_block({team: {details.player}}, bios)
    log_write(info, f"TXN renounce — {details.player} from {team}")
    return team


def _apply_convert_twoway(details: ConvertTwoWayDetails, txn_date: str, info: dict, txn_id: Optional[str] = None) -> str:
    """Converts a two-way contract to a standard player contract. Returns the player's team."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    bio = bios[details.player]
    if bio.get("type") != "two-way":
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on a two-way contract")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    bio["type"] = "player"
    cur_season = _season_for_date(txn_date)
    past, past_gtd, past_gtd_dates, past_gtd_sched = _retained_history(bio, cur_season)
    bio["salaries"] = {**past, **details.contract.salaries}
    bio["cap_holds"] = details.contract.cap_holds
    bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
    bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
    bio["guarantee_schedule"] = {**past_gtd_sched, **details.contract.guarantee_schedule}

    bio["contracts"] = (bio.get("contracts") or []) + [{
        "team": team,
        "date": txn_date,
        "signing_method": "convert_twoway",
        "bird_rights_type": None,
        "salaries": details.contract.salaries,
        "guaranteed": details.contract.guaranteed,
        "guarantee_dates": details.contract.guarantee_dates,
        "guarantee_schedule": details.contract.guarantee_schedule,
        "cap_holds": details.contract.cap_holds,
        "txn_id": txn_id,
    }]

    save_player_bios(bios)

    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if roster_path.exists():
        headers, rows = read_csv(roster_path)
        for row in rows:
            if row.get("SLUG") == details.player:
                row["TYPE"] = ""
                break
        write_csv(roster_path, headers, rows)

    log_write(info, f"TXN convert_twoway — {details.player} ({team})")


def _apply_sign_pick(details: SignPickDetails, txn_date: str, info: dict, txn_id: Optional[str] = None) -> str:
    """Signs a held draft pick to their first contract: draft-rights → player/two-way. Returns the team."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    bio = bios[details.player]
    if bio.get("type") != "draft-rights":
        raise HTTPException(
            status_code=422,
            detail=f"Player {details.player!r} does not hold draft rights — only drafted players can be signed this way",
        )

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    new_type = "two-way" if details.contract.type == "two-way" else "player"
    bio["type"] = new_type
    cur_season = _season_for_date(txn_date)
    past, past_gtd, past_gtd_dates, past_gtd_sched = _retained_history(bio, cur_season)
    bio["salaries"] = {**past, **details.contract.salaries}
    bio["cap_holds"] = details.contract.cap_holds
    bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
    bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
    bio["guarantee_schedule"] = {**past_gtd_sched, **details.contract.guarantee_schedule}

    bio["contracts"] = (bio.get("contracts") or []) + [{
        "team": team,
        "date": txn_date,
        "signing_method": "draft_pick",
        "bird_rights_type": None,
        "salaries": details.contract.salaries,
        "guaranteed": details.contract.guaranteed,
        "guarantee_dates": details.contract.guarantee_dates,
        "guarantee_schedule": details.contract.guarantee_schedule,
        "cap_holds": details.contract.cap_holds,
        "txn_id": txn_id,
    }]

    save_player_bios(bios)

    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if roster_path.exists():
        headers, rows = read_csv(roster_path)
        for row in rows:
            if row.get("SLUG") == details.player:
                row["TYPE"] = "two-way" if new_type == "two-way" else ""
                break
        write_csv(roster_path, headers, rows)

    log_write(info, f"TXN sign_pick — {details.player} ({team}) → {new_type}")
    return team


def _apply_sign(details: SignDetails, txn_date: str, info: dict, txn_id: Optional[str] = None):
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team = details.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {team!r}")

    team_map = _build_team_map()
    if details.player in team_map:
        existing = team_map[details.player]
        if existing != team:
            raise HTTPException(status_code=409, detail=f"Player {details.player!r} is already on {existing}")

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Roster file not found for {team}")
    headers, rows = read_csv(path)
    if "SLUG" not in headers:
        raise HTTPException(status_code=422, detail=f"Roster for {team} uses legacy format; migrate before using transactions")

    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    row_type = "two-way" if details.contract.type == "two-way" else ""
    rows.append({"SLUG": details.player, "TYPE": row_type})
    write_csv(path, headers, rows)

    bio = bios[details.player]
    cur_season = _season_for_date(txn_date)
    past, past_gtd, past_gtd_dates, past_gtd_sched = _retained_history(bio, cur_season)
    bio["salaries"] = {**past, **details.contract.salaries}
    bio["cap_holds"] = details.contract.cap_holds
    bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
    bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
    bio["guarantee_schedule"] = {**past_gtd_sched, **details.contract.guarantee_schedule}
    bio["type"] = details.contract.type

    past_bird = {k: v for k, v in bio.get("bird_tiers", {}).items() if k <= cur_season}
    new_bird = {}
    if details.bird_rights_type:
        for season, hold_type in (details.contract.cap_holds or {}).items():
            if hold_type in ("UFA", "RFA"):
                new_bird[season] = details.bird_rights_type
    bio["bird_tiers"] = {**past_bird, **new_bird}

    bio["contracts"] = (bio.get("contracts") or []) + [{
        "team": team,
        "date": txn_date,
        "signing_method": details.signing_method,
        "bird_rights_type": details.bird_rights_type,
        "salaries": details.contract.salaries,
        "guaranteed": details.contract.guaranteed,
        "guarantee_dates": details.contract.guarantee_dates,
        "guarantee_schedule": details.contract.guarantee_schedule,
        "cap_holds": details.contract.cap_holds,
        "txn_id": txn_id,
    }]

    save_player_bios(bios)

    if details.signing_method in ("mle", "ntmle", "tmle", "room_exception", "bae", "cap_space"):
        cur = cur_season
        with _state_lock:
            state = load_team_state()
            if team not in state:
                state[team] = {}
            ts = state[team].get(cur, dict(DEFAULT_SEASON_STATE))

            if details.signing_method == "cap_space":
                if not ts.get("mle_type"):
                    ts["mle_type"] = "room"

            elif details.signing_method in ("mle", "ntmle", "tmle", "room_exception"):
                yr1 = 0
                for yr in sorted(details.contract.salaries.keys()):
                    if yr >= cur:
                        yr1 = int(str(details.contract.salaries[yr])
                                  .replace("$", "").replace(",", "").strip() or 0)
                        break
                ts["mle_used"] = ts.get("mle_used", 0) + yr1

                resolved = details.signing_method
                if resolved == "mle":
                    resolved = ts.get("mle_type") or "ntmle"
                ts["mle_type"] = resolved
                if resolved == "ntmle":
                    _maybe_set_hard_cap(ts, "first_apron", f"NTMLE signing: {details.player}")
                elif resolved == "tmle":
                    _maybe_set_hard_cap(ts, "second_apron", f"TMLE signing: {details.player}")

            elif details.signing_method == "bae":
                ts["bae_used"] = True
                _maybe_set_hard_cap(ts, "first_apron", f"BAE signing: {details.player}")

            state[team][cur] = ts
            save_team_state(state)

    log_write(info, f"TXN sign — {details.player} → {team} [{details.signing_method or 'cap_space'}]")


def _apply_pick(details: PickDetails, txn_date: str, info: dict):
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team = details.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {team!r}")

    team_map = _build_team_map()
    if details.player in team_map:
        existing = team_map[details.player]
        raise HTTPException(status_code=409, detail=f"Player {details.player!r} is already on {existing}")

    orig = details.pick.orig.upper()
    if orig not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown orig team: {orig!r}")
    if details.pick.round not in (1, 2):
        raise HTTPException(status_code=422, detail="Pick round must be 1 or 2")

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Roster file not found for {team}")
    headers, rows = read_csv(path)
    if "SLUG" not in headers:
        raise HTTPException(status_code=422, detail=f"Roster for {team} uses legacy format; migrate before using transactions")

    # A pick conveys draft rights only — the player lands on the roster with no
    # contract. The salary comes later via a separate `sign` step. (Roster CSVs
    # store SLUG,OVR only; the TYPE here is dropped on write — bio.type is the
    # source of truth, read back via the `row.TYPE || bio.type` fallback.)
    rows.append({"SLUG": details.player, "TYPE": "draft-rights"})
    write_csv(path, headers, rows)

    bio = bios[details.player]
    bio["type"] = "draft-rights"
    # Stamp the canonical draft record on the bio. draft_team is the source of
    # truth for "who drafted this player" across the whole site (team Draft
    # History, /draft); the slot fields are the authoritative draft position.
    bio["draft_team"] = team
    bio["draft_year"] = details.pick.year
    bio["draft_round"] = details.pick.round
    if details.pick.pick_number is not None:
        bio["draft_pick"] = details.pick.pick_number
    save_player_bios(bios)

    pick = details.pick
    updated_row = {
        "YEAR": str(pick.year), "ROUND": str(pick.round), "ORIG": orig,
        "OWNER": team,
        "PICK": str(pick.pick_number) if pick.pick_number is not None else "",
        "PLAYER": details.player,
        "PROTECTED": "", "SWAP_OWNER": "", "NOTES": "",
    }
    with _picks_lock:
        picks = load_picks()
        for i, p in enumerate(picks):
            if (p.get("YEAR") == str(pick.year) and p.get("ROUND") == str(pick.round)
                    and p.get("ORIG", "").upper() == orig):
                picks[i] = updated_row
                save_picks(picks)
                log_write(info, f"TXN pick — {details.player} → {team} ({pick.year} R{pick.round} {orig})")
                return
        picks.append(updated_row)
        picks.sort(key=lambda p: (p["YEAR"], p["ROUND"], p["ORIG"]))
        save_picks(picks)

    log_write(info, f"TXN pick — {details.player} → {team} ({pick.year} R{pick.round} {orig} — new row)")


def _apply_trade(details: TradeIn, txn_date: str, info: dict) -> list[str]:
    if len(details.transfers) < 1:
        raise HTTPException(status_code=422, detail="A trade requires at least 1 transfer")

    for xfer in details.transfers:
        xfer.from_team = xfer.from_team.upper()
        xfer.to_team   = xfer.to_team.upper()
        if xfer.from_team not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"Unknown team: {xfer.from_team!r}")
        if xfer.to_team not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"Unknown team: {xfer.to_team!r}")
        for asset in xfer.assets:
            if asset.type not in ("player", "pick"):
                raise HTTPException(status_code=422, detail=f"Unknown asset type: {asset.type!r}")
            if asset.swap_with:
                asset.swap_with = asset.swap_with.upper()
                if asset.swap_with not in VALID_TEAMS:
                    raise HTTPException(status_code=422, detail=f"Unknown swap_with team: {asset.swap_with!r}")

    seen_players: set[str] = set()
    seen_picks: set[tuple] = set()
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                if not asset.slug:
                    raise HTTPException(status_code=422, detail="Player asset missing slug")
                if asset.slug in seen_players:
                    raise HTTPException(status_code=422, detail=f"Player {asset.slug!r} appears in multiple transfers")
                seen_players.add(asset.slug)
            else:
                if not all([asset.year, asset.round, asset.orig]):
                    raise HTTPException(status_code=422, detail="Pick asset missing year/round/orig")
                asset.orig = asset.orig.upper()
                key = (asset.year, asset.round, asset.orig)
                if key in seen_picks:
                    raise HTTPException(status_code=422, detail=f"Pick {asset.year} R{asset.round} {asset.orig} appears in multiple transfers")
                seen_picks.add(key)

    bios     = load_player_bios()
    team_map = _build_team_map()
    picks    = load_picks()
    pick_index = {(int(p["YEAR"]), int(p["ROUND"]), p["ORIG"].upper()): p for p in picks}

    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                if asset.slug not in bios:
                    raise HTTPException(status_code=422, detail=f"Unknown player: {asset.slug!r}")
                actual_team = team_map.get(asset.slug)
                if actual_team != xfer.from_team:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Player {asset.slug!r} is on {actual_team or 'no team'}, not {xfer.from_team}",
                    )
            else:
                key = (asset.year, asset.round, asset.orig)
                pick_row = pick_index.get(key)
                if not pick_row:
                    raise HTTPException(status_code=422, detail=f"Pick {asset.year} R{asset.round} {asset.orig} not found")
                if pick_row["OWNER"].upper() != xfer.from_team:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Pick {asset.year} R{asset.round} {asset.orig} is owned by {pick_row['OWNER']}, not {xfer.from_team}",
                    )
                if pick_row.get("FROZEN", "").strip().upper() == "TRUE":
                    reason = pick_row.get("FROZEN_REASON", "").strip()
                    detail = f"Pick {asset.year} R{asset.round} {asset.orig} is frozen and cannot be traded"
                    if reason:
                        detail += f": {reason}"
                    raise HTTPException(status_code=422, detail=detail)

    roster_moves: list[tuple[str, str, dict]] = []
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                path = DATA_DIR / f"{xfer.from_team.lower()}-roster.csv"
                headers, rows = read_csv(path)
                matching = [r for r in rows if r.get("SLUG", "").strip() == asset.slug]
                if matching:
                    roster_moves.append((xfer.from_team, xfer.to_team, headers, matching[0]))

    roster_cache: dict[str, tuple[list, list]] = {}
    for from_team, to_team, headers, row in roster_moves:
        if from_team not in roster_cache:
            path = DATA_DIR / f"{from_team.lower()}-roster.csv"
            roster_cache[from_team] = list(read_csv(path))
        if to_team not in roster_cache:
            path = DATA_DIR / f"{to_team.lower()}-roster.csv"
            roster_cache[to_team] = list(read_csv(path))

        src_hdrs, src_rows = roster_cache[from_team]
        dst_hdrs, dst_rows = roster_cache[to_team]
        roster_cache[from_team] = (src_hdrs, [r for r in src_rows if r.get("SLUG", "").strip() != row.get("SLUG")])
        dst_rows.append(row)
        roster_cache[to_team] = (dst_hdrs, dst_rows)

    for team, (hdrs, rows) in roster_cache.items():
        write_csv(DATA_DIR / f"{team.lower()}-roster.csv", hdrs, rows)

    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "pick":
                for p in picks:
                    if (int(p["YEAR"]) == asset.year and int(p["ROUND"]) == asset.round
                            and p["ORIG"].upper() == asset.orig):
                        p["OWNER"] = xfer.to_team
                        if asset.protection is not None:
                            p["PROTECTED"] = str(asset.protection)
                        if asset.swap_with is not None:
                            p["SWAP_OWNER"] = asset.swap_with
    save_picks(picks)

    trade_removals: dict[str, set[str]] = {}
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                trade_removals.setdefault(xfer.from_team, set()).add(asset.slug)
    _scrub_trading_block(trade_removals, bios)

    if details.exceptions:
        cur_season = _season_for_date(txn_date)
        _, incoming, _, _ = _trade_flows(details, bios, cur_season)
        with _state_lock:
            state = load_team_state()
            for team, exc_type in details.exceptions.items():
                if not exc_type:
                    continue
                team = team.upper()
                inc = incoming.get(team, 0)
                if inc <= 0:
                    continue
                if team not in state:
                    state[team] = {}
                ts = state[team].get(cur_season, dict(DEFAULT_SEASON_STATE))
                ts["mle_used"] = ts.get("mle_used", 0) + inc
                ts["mle_type"] = exc_type
                if exc_type == "ntmle":
                    _maybe_set_hard_cap(ts, "first_apron", f"NTMLE trade absorption ({txn_date})")
                elif exc_type == "tmle":
                    _maybe_set_hard_cap(ts, "second_apron", f"TMLE trade absorption ({txn_date})")
                state[team][cur_season] = ts
            save_team_state(state)

    teams = sorted({xfer.from_team for xfer in details.transfers} | {xfer.to_team for xfer in details.transfers})
    log_write(info, f"TXN trade — {' / '.join(teams)}: {len(seen_players)} players, {len(seen_picks)} picks")
    return teams


# ── Validation helpers ────────────────────────────────────────────────────────

# Roster-count-exempt bio types: two-way (own cap slot rules), draft-rights (no
# contract yet — just held rights, not a roster spot), dead (a cap hit with no
# active player). Only "player" (and unset "") occupy a standard roster spot.
_ROSTER_EXEMPT_TYPES = {"two-way", "draft-rights", "dead"}


def _is_standard_roster_slot(bio_type: str) -> bool:
    return bio_type not in _ROSTER_EXEMPT_TYPES


def _count_standard_roster(team: str) -> int:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return 0
    _, rows = read_csv(path)
    bios = load_player_bios()
    return sum(
        1 for r in rows
        if r.get("SLUG", "").strip()
        and _is_standard_roster_slot(bios.get(r["SLUG"], {}).get("type", ""))
    )


def _compute_team_salary(team: str, bios: dict, season: str) -> int:
    """Sum all active salary + dead cap for a team in a given season."""
    total = 0
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if path.exists():
        _, rows = read_csv(path)
        for row in rows:
            slug = row.get("SLUG", "").strip()
            bio = bios.get(slug, {})
            total += _parse_dollar((bio.get("salaries") or {}).get(season, ""))
    dc_path = DATA_DIR / f"{team.lower()}-deadcap.csv"
    if dc_path.exists():
        _, dc_rows = read_csv(dc_path)
        for row in dc_rows:
            total += _parse_dollar(row.get(season, ""))
    return total


_FA_HOLD_TYPES = {"UFA", "RFA"}


def _compute_team_salary_ex_holds(team: str, bios: dict, season: str) -> int:
    """Team Salary used for apron-level and league Hard Cap comparisons —
    excludes pure free-agent cap holds (UFA/RFA). A hold is a placeholder that
    reserves a roster spot during free agency, not a real salary obligation, so
    it doesn't count toward which apron tier a team sits in, nor toward the
    league Hard Cap (§ 1.3: "active player salaries plus dead cap" — a hold is
    not an active player salary). Conditional-but-real salary (TEAM_OPT/
    PLAYER_OPT/NON_GTD years) still counts, as does dead cap. The plain Salary
    Cap / cap-room checks (Room Exception eligibility, cap-space signings)
    still use the full `_compute_team_salary` figure — those track actual
    spendable room against the cap, where an unrenounced hold still occupies
    room until cleared."""
    total = 0
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if path.exists():
        _, rows = read_csv(path)
        for row in rows:
            slug = row.get("SLUG", "").strip()
            bio = bios.get(slug, {})
            if (bio.get("cap_holds") or {}).get(season) in _FA_HOLD_TYPES:
                continue
            total += _parse_dollar((bio.get("salaries") or {}).get(season, ""))
    dc_path = DATA_DIR / f"{team.lower()}-deadcap.csv"
    if dc_path.exists():
        _, dc_rows = read_csv(dc_path)
        for row in dc_rows:
            total += _parse_dollar(row.get(season, ""))
    return total


def _hard_cap_check(team: str, projected: int, season: str,
                    team_state: dict, cap_levels: dict) -> Optional[CheckResult]:
    ts  = get_season_state(team_state, team, season)
    hc  = ts.get("hard_cap")
    if not hc:
        return None
    cl  = cap_levels.get(season, {})
    limit = cl.get("apron1") if hc == "first_apron" else cl.get("apron2")
    if limit is None or projected <= limit:
        return None
    label = "First Apron" if hc == "first_apron" else "Second Apron"
    over  = projected - limit
    return CheckResult(
        check=f"hard_cap_{team.lower()}",
        passed=False,
        level="error",
        message=(
            f"{team} would be ${over:,.0f} over their hard cap ({label}: ${limit:,}) "
            f"— projected salary ${projected:,}."
        ),
    )


def _universal_hard_cap_check(team: str, projected: int, season: str,
                               cap_levels: dict) -> Optional[CheckResult]:
    cl    = cap_levels.get(season, {})
    limit = cl.get("hard_cap") or 0
    if not limit or projected <= limit:
        return None
    over = projected - limit
    return CheckResult(
        check=f"hard_cap_league_{team.lower()}",
        passed=False,
        level="error",
        message=(
            f"{team} would be ${over:,.0f} over the league Hard Cap (${limit:,}) "
            f"— projected salary ${projected:,}."
        ),
    )


def _salary_match_limit(outgoing: int) -> int:
    """Max incoming salary allowed under standard (below First Apron) tiered matching (§ 4.2)."""
    if outgoing <= SALARY_MATCH_TIER1_CAP:
        return 2 * outgoing + 250_000
    elif outgoing <= SALARY_MATCH_TIER2_CAP:
        return outgoing + 8_527_000
    else:
        return round(1.25 * outgoing) + 250_000


def _check_contract_raises(
    contract: ContractIn, bird_pct: bool, cur_season: str
) -> Optional[CheckResult]:
    if contract.type == "two-way":
        return None

    pct = 0.08 if bird_pct else 0.05
    pct_label = "8%" if bird_pct else "5%"

    _HOLD_TYPES = {"UFA", "RFA", "PLAYER_OPT", "TEAM_OPT"}
    hold_years = {yr for yr, ht in (contract.cap_holds or {}).items() if ht in _HOLD_TYPES}

    years = sorted(
        (yr for yr in contract.salaries if yr >= cur_season and yr not in hold_years),
        key=lambda y: (_season_start(y), y),
    )
    if len(years) < 2:
        return None

    yr1_sal = _parse_dollar(contract.salaries[years[0]])
    if yr1_sal == 0:
        return None

    max_step = round(pct * yr1_sal)
    violations = []
    for i in range(1, len(years)):
        prev_sal = _parse_dollar(contract.salaries[years[i - 1]])
        this_sal = _parse_dollar(contract.salaries[years[i]])
        diff = abs(this_sal - prev_sal)
        if diff > max_step + 1:
            violations.append(
                f"{years[i]}: ${prev_sal:,} → ${this_sal:,} "
                f"(Δ${diff:,}, max ${max_step:,})"
            )

    if violations:
        return CheckResult(
            check="raise_limit",
            passed=False,
            level="error",
            message=(
                f"Raise/decrease limit violated ({pct_label} of Year 1 = ${max_step:,}/yr): "
                + "; ".join(violations)
            ),
        )
    return None


_MLE_EXCEPTION_LABELS = {
    "ntmle": "Non-Taxpayer MLE",
    "tmle": "Taxpayer MLE",
    "room_exception": "Room Exception",
}


def _check_exception_absorption(
    team: str,
    incoming: int,
    exception_type: str,
    team_salary_before: int,
    team_salary_ex_holds_before: int,
    cl: dict,
    team_state: Optional[dict],
    season: str,
) -> CheckResult:
    """A team may absorb incoming trade salary using its remaining season MLE
    balance in lieu of matching outgoing salary — incoming salary must not
    exceed the exception's remaining amount (outgoing is not a factor). Same
    apron-eligibility restrictions as free-agent signings apply (§ 1.5/§ 1.6,
    § 3.2-§ 3.4): NTMLE unavailable at/above the First Apron, TMLE unavailable
    at/above the Second Apron, Room Exception only below Cap - full NTMLE.

    NTMLE/TMLE eligibility (apron-level checks) use `team_salary_ex_holds_before`
    — pure free-agent cap holds (UFA/RFA) don't count toward apron level. Room
    Exception eligibility is Cap-based, not apron-based, so it still uses the
    full `team_salary_before` (with holds).
    """
    check_name = f"salary_matching_{team.lower()}"
    label = _MLE_EXCEPTION_LABELS.get(exception_type)
    if label is None:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=f"{team}: unknown exception type {exception_type!r}.",
        )

    apron1 = cl.get("apron1")
    apron2 = cl.get("apron2")
    cap = cl.get("cap")
    ntmle_amount = cl.get("ntmle_amount", 0) or 0

    if exception_type == "ntmle":
        eligible = apron1 is not None and team_salary_ex_holds_before < apron1
        threshold_msg = f"the First Apron (${apron1:,}) — § 1.5" if apron1 is not None else "the First Apron"
        salary_shown = team_salary_ex_holds_before
    elif exception_type == "tmle":
        eligible = apron2 is not None and team_salary_ex_holds_before < apron2
        threshold_msg = f"the Second Apron (${apron2:,}) — § 1.6, § 3.4" if apron2 is not None else "the Second Apron"
        salary_shown = team_salary_ex_holds_before
    else:  # room_exception
        room_ceiling = (cap - ntmle_amount) if cap is not None else None
        eligible = room_ceiling is not None and team_salary_before < room_ceiling
        threshold_msg = (f"the Cap minus the full NTMLE amount (${room_ceiling:,}) — § 3.2"
                          if room_ceiling is not None else "the Room Exception eligibility line")
        salary_shown = team_salary_before

    if not eligible:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team} is not eligible for the {label} — Team Salary (${salary_shown:,}) "
                      f"is at/above {threshold_msg}."),
        )

    ts = get_season_state(team_state or {}, team, season)
    amount_key = {"ntmle": "ntmle_amount", "tmle": "tmle_amount", "room_exception": "room_amount"}[exception_type]
    total = cl.get(amount_key, 0) or 0
    used = ts.get("mle_used", 0) or 0
    remaining = max(0, total - used)

    if incoming > remaining:
        over = incoming - remaining
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team}: {label} absorption failed — incoming salary ${incoming:,} exceeds the "
                      f"remaining {label} balance of ${remaining:,} (${total:,} total, ${used:,} used this "
                      f"season) by ${over:,}."),
        )

    return CheckResult(
        check=check_name, passed=True,
        message=(f"{team}: incoming salary ${incoming:,} absorbed via the {label} (${remaining:,} of "
                  f"${total:,} remaining before this trade) — no salary match required."),
    )


def _check_salary_matching(
    team: str,
    outgoing: int,
    incoming: int,
    team_salary_before: int,
    cap_levels: dict,
    season: str,
    exception_type: Optional[str] = None,
    team_state: Optional[dict] = None,
    team_salary_ex_holds_before: Optional[int] = None,
) -> Optional[CheckResult]:
    cl = cap_levels.get(season, {})
    ex_holds = team_salary_ex_holds_before if team_salary_ex_holds_before is not None else team_salary_before

    if exception_type:
        return _check_exception_absorption(team, incoming, exception_type, team_salary_before,
                                            ex_holds, cl, team_state, season)

    if incoming <= outgoing:
        return None

    apron1 = cl.get("apron1")
    if apron1 is None:
        return None

    # ── Cap-room absorption ─────────────────────────────────────────────────
    # A team below the Salary Cap can absorb an incoming player using its own
    # cap room instead of matching salary — this is evaluated live, off the
    # team's actual room at the moment of the trade. NBN has no persistent
    # Traded Player Exceptions (§ 4.1), so this isn't banked for later use;
    # it only has to hold for this trade, using this trade's own numbers.
    cap = cl.get("cap")
    if cap is not None and team_salary_before < cap:
        projected = team_salary_before - outgoing + incoming
        if projected <= cap:
            return CheckResult(
                check=f"salary_matching_{team.lower()}",
                passed=True,
                message=(
                    f"{team} is below the Cap (${team_salary_before:,} < ${cap:,}) and stays "
                    f"at/below it after the trade (${projected:,}) — absorbed via cap room, "
                    "no salary match required."
                ),
            )

    if ex_holds >= apron1:
        limit = outgoing + 250_000
        if incoming > limit:
            over = incoming - limit
            return CheckResult(
                check=f"salary_matching_{team.lower()}",
                passed=False,
                level="error",
                message=(
                    f"{team} is at/above the First Apron (§ 4.3) — incoming salary "
                    f"${incoming:,} exceeds outgoing + $250K limit of ${limit:,} "
                    f"by ${over:,}."
                ),
            )
    else:
        limit = _salary_match_limit(outgoing)
        if incoming > limit:
            over = incoming - limit
            return CheckResult(
                check=f"salary_matching_{team.lower()}",
                passed=False,
                level="error",
                message=(
                    f"{team} salary matching failed (§ 4.2) — incoming ${incoming:,} "
                    f"exceeds the tier limit of ${limit:,} "
                    f"for outgoing salary of ${outgoing:,}. Overage: ${over:,}."
                ),
            )
    return None


# ── Per-type validators ───────────────────────────────────────────────────────

def _validate_sign(details: SignDetails, ctx: dict) -> list[CheckResult]:
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]
    team = details.team.upper()

    current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
    # If the signee already sits on this team's own roster as a cap hold (e.g.
    # a Bird-rights re-signing), that hold's figure is still counted in
    # `current_ex_holds` unless it's a pure UFA/RFA hold (already excluded) —
    # so back it out here to avoid double-counting hold + new contract
    # (rulebook § 3.10: the hold is replaced, not stacked, by the signed
    # contract's Year 1 figure).
    existing_hold = 0
    is_fa_hold = False
    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if roster_path.exists():
        _, roster_rows = read_csv(roster_path)
        if any(r.get("SLUG", "").strip() == details.player for r in roster_rows):
            existing_hold = _parse_dollar((bios.get(details.player, {}).get("salaries") or {}).get(season, ""))
            is_fa_hold = (bios.get(details.player, {}).get("cap_holds") or {}).get(season) in _FA_HOLD_TYPES
    new_sal = _parse_dollar(details.contract.salaries.get(season, ""))
    projected_ex_holds = current_ex_holds - (0 if is_fa_hold else existing_hold) + new_sal
    # Both the apron-triggered hard cap and the league-wide Hard Cap (§ 1.3)
    # are computed on the ex-holds figure — a free-agent hold isn't an active
    # player salary.
    r = _hard_cap_check(team, projected_ex_holds, season,
                        ctx["team_state"], ctx["cap_levels"])
    if r:
        checks.append(r)
    r = _universal_hard_cap_check(team, projected_ex_holds, season, ctx["cap_levels"])
    if r:
        checks.append(r)

    if details.contract.type != "two-way":
        count = _count_standard_roster(team)
        if count >= ROSTER_MAX:
            checks.append(CheckResult(
                check="roster_size",
                passed=False,
                level="error",
                message=(
                    f"{team} already has {count} standard players (max {ROSTER_MAX}); "
                    f"release a player before signing."
                ),
            ))

    bird_pct = details.bird_rights_type in ("QVFA", "EQVFA")
    r = _check_contract_raises(details.contract, bird_pct=bird_pct, cur_season=season)
    if r:
        checks.append(r)

    return checks


def _validate_release(details: ReleaseDetails, ctx: dict) -> list[CheckResult]:
    return []


def _validate_renounce(details: RenounceDetails, ctx: dict) -> list[CheckResult]:
    return []


def _validate_guarantee(details: GuaranteeDetails, ctx: dict) -> list[CheckResult]:
    return []


def _trade_flows(details, bios: dict, season: str) -> tuple[dict, dict, dict, dict]:
    """Per-team salary and player-slug flows for a trade.

    Returns (outgoing_salary, incoming_salary, out_players, in_players), each
    keyed by team abbreviation. Pick assets carry no salary or roster slot, so
    only player assets are accumulated. Shared by the validator and fact sheet
    so the two never diverge on what "this trade moves" means.
    """
    outgoing: dict[str, int] = {}
    incoming: dict[str, int] = {}
    out_players: dict[str, list[str]] = {}
    in_players: dict[str, list[str]] = {}
    for xfer in details.transfers:
        from_t = xfer.from_team.upper()
        to_t   = xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type != "player" or not asset.slug:
                continue
            sal = _parse_dollar((bios.get(asset.slug, {}).get("salaries") or {}).get(season, ""))
            outgoing[from_t] = outgoing.get(from_t, 0) + sal
            incoming[to_t]   = incoming.get(to_t, 0)   + sal
            out_players.setdefault(from_t, []).append(asset.slug)
            in_players.setdefault(to_t, []).append(asset.slug)
    return outgoing, incoming, out_players, in_players


def _validate_trade(details: TradeIn, ctx: dict) -> list[CheckResult]:
    """Canonical trade validation, shared verbatim by the submit endpoint
    (``POST /api/transactions``) and the simulator (``POST /api/validate/trade``).

    Every rule appends an explicit pass *or* fail CheckResult so the result reads
    as a complete rubric — both the simulator and the transaction trade flow render
    the whole scorecard live, and the submit UI only blocks on ``passed == False``
    errors (warnings are force-through-able). The rules, in order:

      • Leg count          — § 4.1 (warning only; single legs are allowed for splits)
      • Salary matching    — § 4.2 (tiered, below apron) / § 4.3 (apron + $250K), or
                              MLE trade absorption if `exceptions[team]` is set (§ 4.2a)
      • 2nd-apron aggreg.  — § 4.4 (warning only; apron-2 teams may not combine salaries)
      • Hard cap           — team apron hard cap (team-state) + league-wide hard cap
      • Roster size        — Article II: 15 in-season, 20 offseason ceiling
    """
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    # ── Leg count (§ 4.1) ──────────────────────────────────────────────────────
    legs = len(details.transfers)
    if legs < 2:
        checks.append(CheckResult(
            check="trade_min_legs",
            passed=False,
            level="warning",
            message=(f"Trade has only {legs} leg(s); a normal trade has 2 or more. "
                     "Submit anyway to record a single-sided leg (e.g. remaining leg after a draft-show split)."),
        ))
    else:
        checks.append(CheckResult(
            check="trade_min_legs", passed=True,
            message=f"{legs} legs exchanged — valid two-or-more-team structure (§ 4.1).",
        ))

    outgoing, incoming, out_players, in_players = _trade_flows(details, bios, season)
    teams = sorted(set(outgoing) | set(incoming) | set(out_players) | set(in_players))

    # ── Salary matching (§ 4.2 / § 4.3) + hard cap ─────────────────────────────
    for team in teams:
        out = outgoing.get(team, 0)
        inc = incoming.get(team, 0)
        current = _compute_team_salary(team, bios, season)
        current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
        delta = inc - out

        if delta > 0:
            projected_ex_holds = current_ex_holds + delta
            # Both the apron-triggered hard cap and the league-wide Hard Cap
            # (§ 1.3: "active player salaries plus dead cap") are computed on
            # the ex-holds figure — a free-agent hold isn't an active salary.
            hc = _hard_cap_check(team, projected_ex_holds, season,
                                 ctx["team_state"], ctx["cap_levels"])
            checks.append(hc or CheckResult(
                check=f"hard_cap_{team.lower()}", passed=True,
                message=f"{team}: projected salary ${projected_ex_holds:,} within hard-cap limits.",
            ))
            lhc = _universal_hard_cap_check(team, projected_ex_holds, season, ctx["cap_levels"])
            if lhc:
                checks.append(lhc)

        if inc > 0:
            exc_type = (details.exceptions or {}).get(team)
            sm = _check_salary_matching(team, out, inc, current, ctx["cap_levels"], season,
                                         exception_type=exc_type, team_state=ctx.get("team_state"),
                                         team_salary_ex_holds_before=current_ex_holds)
            checks.append(sm or CheckResult(
                check=f"salary_matching_{team.lower()}", passed=True,
                message=f"{team}: incoming ${inc:,} matches outgoing ${out:,} (§ 4.2/4.3).",
            ))

        # ── 2nd-apron aggregation (§ 4.4) ──────────────────────────────────────
        # A team whose pre-trade salary is at/above the second apron may not
        # aggregate (combine) two or more outgoing salaries to match incoming
        # salary. Surfaced as a warning for the office to verify — not auto-blocked.
        apron2 = ctx["cap_levels"].get(season, {}).get("apron2")
        out_count = len(out_players.get(team, []))
        if apron2 and current_ex_holds >= apron2 and inc > 0:
            if out_count >= 2:
                checks.append(CheckResult(
                    check=f"apron2_aggregation_{team.lower()}", passed=False, level="warning",
                    message=(f"{team} is at/above the 2nd apron (${current_ex_holds:,} ≥ ${apron2:,}) and is "
                             f"aggregating {out_count} outgoing salaries (§ 4.4) — apron-2 teams may not "
                             "combine salaries to match incoming. Verify this leg manually."),
                ))
            else:
                checks.append(CheckResult(
                    check=f"apron2_aggregation_{team.lower()}", passed=True,
                    message=(f"{team} is at/above the 2nd apron but matches with a single outgoing "
                             "salary — no aggregation (§ 4.4)."),
                ))

    # ── Roster size (Article II) ───────────────────────────────────────────────
    # In-season the standard-roster limit is ROSTER_MAX (15). In the offseason it
    # rises to ROSTER_OFFSEASON_MAX (20); teams must trim back to 15 before the
    # season. So: ≤15 passes, 16–20 is a warning (a net gain into that band is
    # flagged; a balanced swap that leaves an already-over roster unchanged is a
    # quiet note), and anything above 20 is a hard block.
    for team in sorted(set(out_players) | set(in_players)):
        before = _count_standard_roster(team)
        out_std = sum(1 for s in out_players.get(team, [])
                      if _is_standard_roster_slot(bios.get(s, {}).get("type", "")))
        in_std  = sum(1 for s in in_players.get(team, [])
                      if _is_standard_roster_slot(bios.get(s, {}).get("type", "")))
        after = before - out_std + in_std
        key = f"roster_size_{team.lower()}"
        if after > ROSTER_OFFSEASON_MAX:
            checks.append(CheckResult(
                check=key, passed=False, level="error",
                message=(f"{team} would carry {after} standard players, over the "
                         f"{ROSTER_OFFSEASON_MAX}-player offseason maximum — release a player first."),
            ))
        elif after > ROSTER_MAX and after > before:
            checks.append(CheckResult(
                check=key, passed=False, level="warning",
                message=(f"{team} would go from {before} to {after} standard players — over the "
                         f"{ROSTER_MAX}-man regular-season limit (offseason ceiling {ROSTER_OFFSEASON_MAX}); "
                         f"trim to {ROSTER_MAX} before the season."),
            ))
        elif after > ROSTER_MAX:
            checks.append(CheckResult(
                check=key, passed=True,
                message=(f"{team}: {after} standard players after trade — over the {ROSTER_MAX}-man "
                         f"regular-season limit but within the offseason ceiling of {ROSTER_OFFSEASON_MAX} "
                         "(unchanged by this trade); trim before the season."),
            ))
        else:
            checks.append(CheckResult(
                check=key, passed=True,
                message=f"{team}: {after} standard players after trade (max {ROSTER_MAX}).",
            ))

    return checks


def _validate_option(details: OptionDetails, ctx: dict) -> list[CheckResult]:
    return []


def _validate_pick(details: PickDetails, ctx: dict) -> list[CheckResult]:
    # A draft pick conveys draft rights only — no contract, no salary ($0 cap
    # impact), and the player lands as "draft-rights" rather than a standard
    # roster player. Cap and roster-size checks belong to the later `sign_pick`
    # step, not here. `details.contract` is None for picks (see PickDetails).
    return []


def _validate_convert_twoway(details: ConvertTwoWayDetails, ctx: dict) -> list[CheckResult]:
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    team_map = _build_team_map()
    team = team_map.get(details.player)

    old_sal = _parse_dollar((bios.get(details.player, {}).get("salaries") or {}).get(season, ""))
    new_sal = _parse_dollar(details.contract.salaries.get(season, ""))
    delta = new_sal - old_sal

    if delta > 0 and team:
        current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
        projected_ex_holds = current_ex_holds + delta
        r = _hard_cap_check(team, projected_ex_holds, season,
                            ctx["team_state"], ctx["cap_levels"])
        if r:
            checks.append(r)
        r = _universal_hard_cap_check(team, projected_ex_holds, season, ctx["cap_levels"])
        if r:
            checks.append(r)

    if team:
        count = _count_standard_roster(team)
        if count >= ROSTER_MAX:
            checks.append(CheckResult(
                check="roster_size",
                passed=False,
                level="error",
                message=(
                    f"{team} already has {count} standard players (max {ROSTER_MAX}); "
                    f"release a player before converting this two-way contract."
                ),
            ))

    r = _check_contract_raises(details.contract, bird_pct=False, cur_season=season)
    if r:
        checks.append(r)

    return checks


_VALIDATORS = {
    "sign":           _validate_sign,
    "release":        _validate_release,
    "renounce":       _validate_renounce,
    "trade":          _validate_trade,
    "option":         _validate_option,
    "guarantee":      _validate_guarantee,
    "pick":           _validate_pick,
    "convert_twoway": _validate_convert_twoway,
}


def _run_validation(txn_type: str, details, ctx: dict) -> list[CheckResult]:
    fn = _VALIDATORS.get(txn_type)
    return fn(details, ctx) if fn else []


# ── Transaction routes ────────────────────────────────────────────────────────

@router.post("/api/transactions")
def create_transaction(body: TransactionIn, info: dict = Depends(require_role("rosters"))):
    try:
        datetime.strptime(body.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date; use YYYY-MM-DD")

    if body.type not in ("sign", "pick", "option", "guarantee", "release", "renounce", "trade", "convert_twoway", "sign_pick"):
        raise HTTPException(status_code=422, detail=f"Unsupported transaction type: {body.type!r}")

    _detail_models = {
        "sign":           (SignDetails,           "Invalid sign details"),
        "pick":           (PickDetails,           "Invalid pick details"),
        "option":         (OptionDetails,         "Invalid option details"),
        "guarantee":      (GuaranteeDetails,      "Invalid guarantee details"),
        "release":        (ReleaseDetails,        "Invalid release details"),
        "renounce":       (RenounceDetails,       "Invalid renounce details"),
        "trade":          (TradeIn,               "Invalid trade details"),
        "convert_twoway": (ConvertTwoWayDetails,  "Invalid convert_twoway details"),
        "sign_pick":      (SignPickDetails,       "Invalid sign_pick details"),
    }
    model_cls, err_prefix = _detail_models[body.type]
    try:
        parsed_details = model_cls(**body.details)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"{err_prefix}: {e}")

    _val_ctx = {
        "bios":        load_player_bios(),
        "team_state":  load_team_state(),
        "cap_levels":  json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {},
        "cur_season":  _season_for_date(body.date),
    }
    checks = _run_validation(body.type, parsed_details, _val_ctx)
    failed = [c for c in checks if not c.passed]

    if failed and not body.force:
        raise HTTPException(status_code=422, detail={
            "validation": True,
            "checks": [c.model_dump() for c in checks],
            "can_force": True,
        })

    txn_id = secrets.token_hex(8)
    with _txn_lock:
        details = parsed_details
        if body.type == "sign":
            _apply_sign(details, body.date, info, txn_id=txn_id)
            stored_details = details.model_dump()
        elif body.type == "pick":
            _apply_pick(details, body.date, info)
            stored_details = details.model_dump()
        elif body.type == "option":
            team = _apply_option(details, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "guarantee":
            team = _apply_guarantee(details, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "release":
            team, dead_cap = _apply_release(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
            stored_details["dead_cap"] = dead_cap
        elif body.type == "renounce":
            team = _apply_renounce(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "trade":
            teams = _apply_trade(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["teams"] = teams
        elif body.type == "convert_twoway":
            team = _apply_convert_twoway(details, body.date, info, txn_id=txn_id)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "sign_pick":
            team = _apply_sign_pick(details, body.date, info, txn_id=txn_id)
            stored_details = details.model_dump()
            stored_details["team"] = team

        if body.force and failed:
            stored_details["_forced_checks"] = [c.check for c in failed]

        txn = {
            "id": txn_id,
            "type": body.type,
            "date": body.date,
            "created_by": info.get("name", "unknown"),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": body.description,
            "details": stored_details,
        }
        _append_transaction(txn)

    return txn


@router.get("/api/transactions")
def list_transactions(
    team: Optional[str] = None,
    type: Optional[str] = None,
    player: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    txns = _load_transactions()

    if team:
        t_upper = team.upper()
        def _team_match(t):
            d = t.get("details", {})
            if d.get("team", "").upper() == t_upper:
                return True
            return t_upper in [x.upper() for x in d.get("teams", [])]
        txns = [t for t in txns if _team_match(t)]

    if player:
        def _player_match(t):
            d = t.get("details", {})
            if d.get("player") == player:
                return True
            for tr in d.get("transfers", []):
                for asset in tr.get("assets", []):
                    if asset.get("type") == "player" and asset.get("slug") == player:
                        return True
            return False
        txns = [t for t in txns if _player_match(t)]

    if type:
        txns = [t for t in txns if t.get("type") == type]

    txns = sorted(txns, key=lambda t: (t.get("date", ""), t.get("created_at", "")), reverse=True)
    total = len(txns)
    return {"transactions": txns[offset: offset + limit], "total": total}


@router.get("/api/transactions/{txn_id}")
def get_transaction(txn_id: str):
    for t in _load_transactions():
        if t.get("id") == txn_id:
            return t
    raise HTTPException(status_code=404, detail="Transaction not found")


@router.delete("/api/transactions/{txn_id}")
def delete_transaction(txn_id: str, info: dict = Depends(require_role("rosters"))):
    with _txn_lock:
        txns = _load_transactions()
        filtered = [t for t in txns if t.get("id") != txn_id]
        if len(filtered) == len(txns):
            raise HTTPException(status_code=404, detail="Transaction not found")
        TRANSACTIONS_FILE.write_text(json.dumps(filtered, indent=2))
    log_write(info, f"TXN delete — {txn_id}")
    return {"deleted": txn_id}


# ── Trade validation endpoint ─────────────────────────────────────────────────

def _trade_fact_sheet(details, ctx: dict) -> dict:
    """Per-team financial snapshot the simulator renders alongside the checks.
    Reuses _trade_flows so the numbers shown are the exact ones the validator
    judged — no parallel cap math on the client."""
    bios = ctx["bios"]; season = ctx["cur_season"]
    outgoing, incoming, out_players, in_players = _trade_flows(details, bios, season)

    teams: dict[str, dict] = {}
    for team in sorted(set(outgoing) | set(incoming) | set(out_players) | set(in_players)):
        out = outgoing.get(team, 0)
        inc = incoming.get(team, 0)
        before = _count_standard_roster(team)
        out_std = sum(1 for s in out_players.get(team, [])
                      if _is_standard_roster_slot(bios.get(s, {}).get("type", "")))
        in_std  = sum(1 for s in in_players.get(team, [])
                      if _is_standard_roster_slot(bios.get(s, {}).get("type", "")))
        current = _compute_team_salary(team, bios, season)
        current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
        teams[team] = {
            "team": team,
            "current_salary": current,
            "current_salary_ex_holds": current_ex_holds,
            "outgoing_salary": out,
            "incoming_salary": inc,
            "projected_salary": current - out + inc,
            "projected_salary_ex_holds": current_ex_holds - out + inc,
            "players_out": out_players.get(team, []),
            "players_in": in_players.get(team, []),
            "standard_count_before": before,
            "standard_count_after": before - out_std + in_std,
        }

    return {
        "season": season,
        "cap_levels": ctx["cap_levels"].get(season, {}),
        "teams": teams,
    }


@router.post("/api/validate/trade")
def validate_trade(body: TradeValidateInput):
    ctx = {
        "bios":       load_player_bios(),
        "team_state": load_team_state(),
        "cap_levels": json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {},
        "cur_season": _current_league_year(),
    }
    checks = _validate_trade(body, ctx)
    # Warnings (e.g. a single-leg trade) don't make a deal illegal — only errors do,
    # matching the submit path where warnings are force-through-able.
    legal = not any(not c.passed and c.level == "error" for c in checks)
    return TradeValidationResult(
        legal=legal,
        checks=checks,
        fact_sheet=_trade_fact_sheet(body, ctx),
    )

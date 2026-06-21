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
    ROSTER_MAX, SALARY_MATCH_TIER1_CAP, SALARY_MATCH_TIER2_CAP,
)
from .storage import read_csv, write_csv, _load_json, log_write, _parse_dollar, _season_start, _season_for_date
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
    contract: ContractIn


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


class ReleaseDetails(BaseModel):
    player: str


class RenounceDetails(BaseModel):
    player: str


class ConvertTwoWayDetails(BaseModel):
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

    save_player_bios(bios)
    log_write(info, f"TXN option — {details.player} {details.decision} {details.option_type} {details.year}")
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


def _apply_convert_twoway(details: ConvertTwoWayDetails, txn_date: str, info: dict) -> str:
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
    return team


def _apply_sign(details: SignDetails, txn_date: str, info: dict):
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

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Roster file not found for {team}")
    headers, rows = read_csv(path)
    if "SLUG" not in headers:
        raise HTTPException(status_code=422, detail=f"Roster for {team} uses legacy format; migrate before using transactions")

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

    row_type = "two-way" if details.contract.type == "two-way" else ""
    rows.append({"SLUG": details.player, "TYPE": row_type})
    write_csv(path, headers, rows)

    bio = bios[details.player]
    if details.contract.salaries:
        cur_season = _season_for_date(txn_date)
        past, past_gtd, past_gtd_dates, past_gtd_sched = _retained_history(bio, cur_season)
        bio["salaries"] = {**past, **details.contract.salaries}
        bio["cap_holds"] = details.contract.cap_holds
        bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
        bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
        bio["guarantee_schedule"] = {**past_gtd_sched, **details.contract.guarantee_schedule}
    bio["type"] = details.contract.type
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


def _apply_trade(details: TradeIn, info: dict) -> list[str]:
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

    teams = sorted({xfer.from_team for xfer in details.transfers} | {xfer.to_team for xfer in details.transfers})
    log_write(info, f"TXN trade — {' / '.join(teams)}: {len(seen_players)} players, {len(seen_picks)} picks")
    return teams


# ── Validation helpers ────────────────────────────────────────────────────────

def _count_standard_roster(team: str) -> int:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return 0
    _, rows = read_csv(path)
    bios = load_player_bios()
    return sum(
        1 for r in rows
        if r.get("SLUG", "").strip()
        and bios.get(r["SLUG"], {}).get("type", "") != "two-way"
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


def _check_salary_matching(
    team: str,
    outgoing: int,
    incoming: int,
    team_salary_before: int,
    cap_levels: dict,
    season: str,
) -> Optional[CheckResult]:
    if incoming <= outgoing:
        return None

    cl = cap_levels.get(season, {})
    apron1 = cl.get("apron1")
    if apron1 is None:
        return None

    if team_salary_before >= apron1:
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

    current = _compute_team_salary(team, bios, season)
    new_sal = _parse_dollar(details.contract.salaries.get(season, ""))
    projected = current + new_sal
    r = _hard_cap_check(team, projected, season,
                        ctx["team_state"], ctx["cap_levels"])
    if r:
        checks.append(r)
    r = _universal_hard_cap_check(team, projected, season, ctx["cap_levels"])
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


def _validate_trade(details: TradeIn, ctx: dict) -> list[CheckResult]:
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    if len(details.transfers) < 2:
        checks.append(CheckResult(
            check="trade_min_legs",
            passed=False,
            level="warning",
            message=(f"Trade has only {len(details.transfers)} leg(s); a normal trade has 2 or more. "
                     "Submit anyway to record a single-sided leg (e.g. remaining leg after a draft-show split)."),
        ))

    outgoing: dict[str, int] = {}
    incoming: dict[str, int] = {}
    for xfer in details.transfers:
        from_t = xfer.from_team.upper()
        to_t   = xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type != "player" or not asset.slug:
                continue
            sal = _parse_dollar((bios.get(asset.slug, {}).get("salaries") or {}).get(season, ""))
            outgoing[from_t] = outgoing.get(from_t, 0) + sal
            incoming[to_t]   = incoming.get(to_t, 0)   + sal

    for team in set(outgoing) | set(incoming):
        out = outgoing.get(team, 0)
        inc = incoming.get(team, 0)
        current = _compute_team_salary(team, bios, season)

        delta = inc - out
        if delta > 0:
            projected = current + delta
            r = _hard_cap_check(team, projected, season,
                                ctx["team_state"], ctx["cap_levels"])
            if r:
                checks.append(r)
            r = _universal_hard_cap_check(team, projected, season, ctx["cap_levels"])
            if r:
                checks.append(r)

        if inc > 0:
            r = _check_salary_matching(team, out, inc, current, ctx["cap_levels"], season)
            if r:
                checks.append(r)

    return checks


def _validate_option(details: OptionDetails, ctx: dict) -> list[CheckResult]:
    return []


def _validate_pick(details: PickDetails, ctx: dict) -> list[CheckResult]:
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]
    team = details.team.upper()

    current = _compute_team_salary(team, bios, season)
    new_sal = _parse_dollar(details.contract.salaries.get(season, ""))
    projected = current + new_sal
    r = _hard_cap_check(team, projected, season,
                        ctx["team_state"], ctx["cap_levels"])
    if r:
        checks.append(r)
    r = _universal_hard_cap_check(team, projected, season, ctx["cap_levels"])
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
                    f"release a player before signing this pick."
                ),
            ))

    return checks


def _validate_convert_twoway(details: ConvertTwoWayDetails, ctx: dict) -> list[CheckResult]:
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    team_map = _build_team_map()
    team = team_map.get(details.player)

    old_sal = _parse_dollar((bios.get(details.player, {}).get("salaries") or {}).get(season, ""))
    new_sal = _parse_dollar(details.contract.salaries.get(season, ""))
    delta = new_sal - old_sal

    if delta > 0 and team:
        current = _compute_team_salary(team, bios, season)
        projected = current + delta
        r = _hard_cap_check(team, projected, season,
                            ctx["team_state"], ctx["cap_levels"])
        if r:
            checks.append(r)
        r = _universal_hard_cap_check(team, projected, season, ctx["cap_levels"])
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

    if body.type not in ("sign", "pick", "option", "release", "renounce", "trade", "convert_twoway"):
        raise HTTPException(status_code=422, detail=f"Unsupported transaction type: {body.type!r}")

    _detail_models = {
        "sign":           (SignDetails,           "Invalid sign details"),
        "pick":           (PickDetails,           "Invalid pick details"),
        "option":         (OptionDetails,         "Invalid option details"),
        "release":        (ReleaseDetails,        "Invalid release details"),
        "renounce":       (RenounceDetails,       "Invalid renounce details"),
        "trade":          (TradeIn,               "Invalid trade details"),
        "convert_twoway": (ConvertTwoWayDetails,  "Invalid convert_twoway details"),
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

    with _txn_lock:
        details = parsed_details
        if body.type == "sign":
            _apply_sign(details, body.date, info)
            stored_details = details.model_dump()
        elif body.type == "pick":
            _apply_pick(details, body.date, info)
            stored_details = details.model_dump()
        elif body.type == "option":
            team = _apply_option(details, info)
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
            teams = _apply_trade(details, info)
            stored_details = details.model_dump()
            stored_details["teams"] = teams
        elif body.type == "convert_twoway":
            team = _apply_convert_twoway(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team

        if body.force and failed:
            stored_details["_forced_checks"] = [c.check for c in failed]

        txn = {
            "id": secrets.token_hex(8),
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

def _build_trade_context(trade: TradeValidateInput) -> dict:
    bios = load_player_bios()

    players_out: dict[str, list[str]] = {}
    players_in: dict[str, list[str]] = {}
    for xfer in trade.transfers:
        from_team = xfer.from_team.upper()
        to_team = xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type == "player" and asset.slug:
                players_out.setdefault(from_team, []).append(asset.slug)
                players_in.setdefault(to_team, []).append(asset.slug)

    teams: dict[str, dict] = {}
    for team in set(players_out) | set(players_in):
        path = DATA_DIR / f"{team.lower()}-roster.csv"
        rows = read_csv(path)[1] if path.exists() else []

        non_standard_on_roster = {
            r["SLUG"].strip() for r in rows
            if r.get("SLUG", "").strip() and r.get("TYPE", "").strip() == "two-way"
        }
        count_before = sum(
            1 for r in rows
            if r.get("SLUG", "").strip() and r.get("TYPE", "").strip() != "two-way"
        )

        out_slugs = players_out.get(team, [])
        in_slugs = players_in.get(team, [])

        out_standard = sum(1 for s in out_slugs if s not in non_standard_on_roster)
        in_standard = sum(
            1 for s in in_slugs
            if bios.get(s, {}).get("type", "") != "two-way"
        )

        teams[team] = {
            "team": team,
            "standard_count_before": count_before,
            "standard_count_after": count_before - out_standard + in_standard,
            "players_out": out_slugs,
            "players_in": in_slugs,
        }

    return {
        "teams": teams,
        "is_sign_and_trade": trade.is_sign_and_trade,
        "exceptions": trade.exceptions,
    }


def _check_roster_size(ctx: dict) -> CheckResult:
    violations = [
        f"{tc['team']}: would have {tc['standard_count_after']} players (max {ROSTER_MAX})"
        for tc in ctx["teams"].values()
        if tc["standard_count_after"] > ROSTER_MAX
    ]
    if violations:
        return CheckResult(
            check="roster_size",
            passed=False,
            message="Roster limit exceeded — release a player first: " + "; ".join(violations),
        )
    return CheckResult(
        check="roster_size",
        passed=True,
        message=f"All rosters within the {ROSTER_MAX}-player limit",
    )


_TRADE_CHECKS = [
    _check_roster_size,
]


@router.post("/api/validate/trade")
def validate_trade(body: TradeValidateInput):
    ctx = _build_trade_context(body)
    results = [fn(ctx) for fn in _TRADE_CHECKS]
    return TradeValidationResult(
        legal=all(r.passed for r in results),
        checks=results,
        fact_sheet=ctx,
    )

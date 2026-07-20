import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, CAP_LEVELS_FILE, TRANSACTIONS_FILE, VALID_TEAMS,
    _txn_lock, _deadcap_lock, _state_lock, _picks_lock, _trade_exc_lock,
    ROSTER_MAX, ROSTER_OFFSEASON_MAX, SALARY_MATCH_TIER1_CAP, SALARY_MATCH_TIER2_CAP,
)
from .storage import read_csv, write_csv, _load_json, log_write, _parse_dollar, _season_start, _season_for_date, _current_league_year
from .auth import require_role
from .players import load_player_bios, save_player_bios, _build_team_map, _scrub_trading_block
from .roster_picks import (
    load_picks, save_picks,
    load_team_state, save_team_state, get_season_state,
    DEFAULT_SEASON_STATE, _maybe_set_hard_cap, _bae_available,
    load_trade_exceptions, save_trade_exceptions,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _register_trade_protection(pick_key: tuple, from_team: str, to_team: str,
                               threshold: int, txn_id: str | None = None,
                               txn_date: str | None = None) -> None:
    """Build a real conveyance protected-node the moment a trade sets a
    protection — see picks_conveyance/from_trade.py for the direction
    convention. Fails open: this must never block or corrupt the real trade
    write happening around it (save_picks(), just below, is the actual source
    of truth and completes regardless). `txn_id`/`txn_date` let the resulting
    band(s) link back to the real transaction record (teams/team.js's
    tooltip otherwise shows "originating trade(s) not yet linked")."""
    try:
        from picks_conveyance import from_trade
        from_trade.register_protection(pick_key, from_team, to_team, threshold,
                                       txn_id=txn_id, txn_date=txn_date)
    except Exception:
        logger.exception("picks_conveyance: failed to register trade protection "
                         f"for {pick_key} (flat PROTECTED field is unaffected)")


def _register_trade_swap(pick_key: tuple, to_team: str, swap_with: str,
                         picks_snapshot: list[dict], txn_id: str | None = None,
                         txn_date: str | None = None) -> None:
    """Build a real conveyance swap-group the moment a trade sets a swap
    partner. Fails open, same guarantee as above."""
    try:
        from picks_conveyance import from_trade
        from_trade.register_swap(pick_key, to_team, swap_with, picks_snapshot,
                                 txn_id=txn_id, txn_date=txn_date)
    except Exception:
        logger.exception("picks_conveyance: failed to register trade swap "
                         f"for {pick_key} (flat SWAP_OWNER field is unaffected)")


def _register_trade_ladder(pick_key: tuple, from_team: str, to_team: str,
                          protect_top: int, fallback_picks: list[tuple],
                          txn_id: str | None = None, txn_date: str | None = None) -> None:
    """Build a real protection-ladder compensation trigger the moment a trade
    sets ladder_protect_top — see picks_conveyance/from_trade.py for the
    layered-not-replacing convention. Fails open, same guarantee as the
    protection/swap registrations above: the flat trade write completes
    regardless."""
    try:
        from picks_conveyance import from_trade
        from_trade.register_ladder(pick_key, from_team, to_team, protect_top,
                                   fallback_picks, txn_id=txn_id, txn_date=txn_date)
    except Exception:
        logger.exception("picks_conveyance: failed to register trade ladder "
                         f"for {pick_key} (fallback compensation not modeled)")


def _lookup_pick(pick_key: tuple, conveyance_store):
    if conveyance_store is None:
        return None
    return next((p for p in conveyance_store.get("picks", [])
                if (p["year"], p["round"], p["orig"]) == pick_key), None)


def _lookup_conveyance_node(pick_key: tuple, conveyance_store):
    pick = _lookup_pick(pick_key, conveyance_store)
    return pick["conveyance"] if pick else None


def _check_retrade_allowed(pick_key: tuple, asset, from_team: str,
                           conveyance_store) -> None:
    """Validation-pass gate for a pick with existing conveyance structure —
    mirrors the existing flat FROZEN check's shape and placement (before any
    writes happen). Two things can reject the trade here:

    - `legacy`: frozen from re-trade by design (docs/picks-conveyance.md §5)
      until someone manually converts it to real structure — it's tagged
      legacy specifically because nothing here can confidently represent
      what happens to it, so trading it further would just compound that.
      Always checked, regardless of what this trade supplies.

    - Ambiguous leaf: only relevant when this trade ISN'T creating new
      structure (no protection/swap_with supplied) — from_team would be
      conveying whatever claim it holds. If it holds more than one distinct
      claim, `asset.leaf_id` must say which (CUTOVER STEP 9,
      docs/picks-migration-worksheet.md — real leaf-node-id addressing, not
      just the ambiguity-detection stand-in from Step 7). No `leaf_id` and
      more than one claim → rejected with the available leaf_ids listed, so
      the trade can be resubmitted precisely instead of guessed at. A
      supplied `leaf_id` the team doesn't actually hold is rejected too.

    Fails open only on genuine infra issues (store unavailable) — a real
    legacy pick or a real ambiguity must actually block, the same way a real
    FROZEN pick does."""
    try:
        pick = _lookup_pick(pick_key, conveyance_store)
    except Exception:
        logger.exception(f"picks_conveyance: retrade check failed for {pick_key}, "
                         "allowing the trade to proceed (infra issue, not a real pick)")
        return
    if pick is None:
        return
    node = pick["conveyance"]
    if node.get("type") == "legacy":
        raise HTTPException(
            status_code=422,
            detail=f"Pick {asset.year} R{asset.round} {asset.orig} is a legacy "
                   f"pick, frozen from re-trade until manually converted to "
                   f"real structure: {node.get('reason')}",
        )
    if asset.protection is None and asset.swap_with is None:
        try:
            from picks_conveyance import ownership
            leaves = ownership.team_leaves(pick, from_team, conveyance_store)
        except Exception:
            logger.exception(f"picks_conveyance: ambiguity check failed for "
                             f"{pick_key}, allowing the trade to proceed")
            return
        if len(leaves) > 1:
            leaf_id = getattr(asset, "leaf_id", None)
            if leaf_id is None:
                options = "; ".join(f"{l['leaf_id']} ({l['description']})" for l in leaves)
                raise HTTPException(
                    status_code=422,
                    detail=f"Pick {asset.year} R{asset.round} {asset.orig}: "
                           f"{from_team} holds {len(leaves)} distinct claims — "
                           f"specify which with asset.leaf_id. Options: {options}",
                )
            if leaf_id not in {l["leaf_id"] for l in leaves}:
                raise HTTPException(
                    status_code=422,
                    detail=f"Pick {asset.year} R{asset.round} {asset.orig}: "
                           f"{from_team} does not hold leaf_id {leaf_id!r}",
                )


def _handle_pick_retrade(pick_key: tuple, from_team: str, to_team: str,
                         conveyance_store, leaf_id: str | None = None,
                         txn_id: str | None = None, txn_date: str | None = None) -> None:
    """A pick with existing contingent structure (swap/protected/binary) is
    being re-traded with no new protection/swap_with supplied — update the
    registry so that structure reflects the new party instead of going stale
    the way the flat model always did. Fails open: this is a best-effort sync
    of the derived model, not a gate — `_check_retrade_allowed` above is what
    actually blocks a trade when blocking is warranted; by the time this
    runs, the trade has already been decided to proceed. `leaf_id`, when the
    trade supplied one to disambiguate, is passed straight through so the
    mutation targets that exact leaf. `txn_id`/`txn_date` get appended to the
    mutated leaf's `txn_ids` (registry.handle_retrade) — otherwise a
    re-traded leaf's tooltip would keep pointing at only its original
    creating trade forever, silently wrong the moment it moves again."""
    try:
        node = _lookup_conveyance_node(pick_key, conveyance_store)
        if node is None:
            return
        from picks_conveyance import registry
        registry.handle_retrade(pick_key, from_team, to_team, node, leaf_id=leaf_id,
                                txn_id=txn_id, txn_date=txn_date)
    except Exception:
        logger.exception(f"picks_conveyance: retrade sync failed for {pick_key} "
                         "(flat OWNER write is unaffected; model may be stale "
                         "until next manual resync)")


def _load_conveyance_store_for_shadow_check():
    """Best-effort load of the conveyance store for the ownership check below.
    Never raises — a missing/broken store just falls back to the flat check
    for this request; trading never goes fully dark over a file-read issue."""
    try:
        from picks_conveyance import store as conv_store
        return conv_store.load_store()
    except Exception:
        return None


def _flat_owns(pick_row: dict, from_team: str) -> bool:
    owner_raw = pick_row["OWNER"].upper()
    # "?" means unresolved -> falls back to orig team (mirrors GET /api/picks/{team});
    # otherwise OWNER may be a single team or a pipe-separated compound of candidates.
    if owner_raw == "?":
        return pick_row["ORIG"].upper() == from_team
    return from_team in owner_raw.split("|")


def _check_pick_ownership(pick_key: tuple, from_team: str, pick_row: dict,
                          conveyance_store, leaf_id: str | None = None) -> bool:
    """The real ownership gate a trade must pass. CUTOVER STEP 5 (2026-07-19,
    see docs/picks-migration-worksheet.md): the tree-based check is now
    authoritative, replacing the flat OWNER check it superseded — this is the
    whole point of the conveyance model, letting a team re-trade a contingent
    share the flat OWNER field could never represent. Falls back to the flat
    check only if the conveyance store can't be loaded or doesn't have this
    pick — an infrastructure safety net, not a design hedge. Any disagreement
    between the two is logged (not just failures) so real trades keep
    providing evidence about the new check as they happen.

    CUTOVER STEP 9: when `leaf_id` is supplied (a trade disambiguating which
    of several claims it's conveying — see `_check_retrade_allowed`), checks
    that specific leaf (`ownership.team_holds_leaf`) instead of the coarse
    "any claim" check."""
    flat_result = _flat_owns(pick_row, from_team)
    # Explicit revert lever, separate from the infra-failure fallback below:
    # set PICKS_OWNERSHIP_ENFORCE=flat in nbn-api/.env + restart to go back to
    # the old flat-only check instantly, no code change, if this needs to be
    # turned off in a hurry.
    if conveyance_store is None or os.environ.get("PICKS_OWNERSHIP_ENFORCE") == "flat":
        return flat_result
    try:
        from picks_conveyance import ownership
        pick = _lookup_pick(pick_key, conveyance_store)
        if pick is None:
            return flat_result
        if leaf_id is not None:
            return ownership.team_holds_leaf(pick, from_team, leaf_id, conveyance_store)
        tree_result = ownership.team_holds_claim(pick, from_team, conveyance_store)
        if tree_result != flat_result:
            logger.warning(
                "picks_conveyance ownership check diverged from flat: pick=%s "
                "from_team=%s flat_check=%s tree_check=%s (tree_check wins) "
                "node_type=%s", pick_key, from_team, flat_result, tree_result,
                pick["conveyance"].get("type"))
        return tree_result
    except Exception:
        logger.exception(f"picks_conveyance: ownership check failed for "
                         f"{pick_key}, falling back to the flat check")
        return flat_result


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
    # Backfill a trade that predates this transaction log (or whose effects
    # current roster/cap data already reflects) without re-applying it —
    # skips _apply_trade and validation, just logs the record for display.
    historical: bool = False


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
    # Stretch provision (rulebook §5.1 method 2): spread the total remaining
    # dead-cap obligation evenly across this many seasons starting at the
    # release season, instead of the default original-payment-schedule method.
    # The rulebook formula is (2 * remaining contract years) + 1, but this is
    # taken as an explicit input rather than auto-derived, since "remaining
    # years" is ambiguous across NON_GTD/option contracts — the submitter
    # states the stretch length the team actually elected.
    stretch_years: Optional[int] = None


class RenounceDetails(BaseModel):
    player: str


class VoidPlayerDetails(BaseModel):
    player: str
    reason: str = ""


class SetHardCapDetails(BaseModel):
    team: str
    level: str          # "first_apron" | "second_apron" | "default"
    reason: str = ""


class ConvertTwoWayDetails(BaseModel):
    player: str
    contract: ContractIn
    signing_method: Optional[str] = None


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
    # CUTOVER STEP 9 (docs/picks-migration-worksheet.md): disambiguates which
    # of several contingent claims from_team is conveying, when it holds more
    # than one on this pick. Get valid values from GET /api/picks/{team} (a
    # contingent pick's response includes them) or the 422 error this asset
    # would otherwise get, which lists them. Only needed in that ambiguous
    # case — omit for every ordinary trade.
    leaf_id: Optional[str] = None
    # Protection ladder: a compensation trigger layered on top of this pick's
    # own conveyance (which `protection`/`swap_with`/plain re-trade already
    # decide) — not a replacement for it. If the pick doesn't convey to
    # to_team because it lands within ladder_protect_top, to_team is instead
    # owed ladder_fallback (a list of {year, round, orig} pick refs) as
    # compensation. Only set these together; only needed for a "protected,
    # else substitute pick(s)" clause explicitly stated in the trade.
    ladder_protect_top: Optional[int] = None
    ladder_fallback: Optional[list[dict]] = None


class TradeTransfer(BaseModel):
    from_team: str
    to_team: str
    assets: list[TradeAsset]


class TradeIn(BaseModel):
    transfers: list[TradeTransfer]
    legality: str = "tbd"
    # Team abbr -> exception type ("ntmle" | "tmle" | "room_exception" | "bae")
    # used to absorb that team's incoming salary in lieu of matching outgoing
    # salary. Omit or null for teams matching normally. TPE absorption is a
    # separate field (tpe_usage, below) since it needs to name a specific
    # banked exception, not just a type.
    exceptions: dict[str, Optional[str]] = {}
    # Team abbr -> Trade Exception id (see GET /api/trade-exceptions/{team})
    # used to absorb that team's incoming salary (§ 4.1a). Mutually exclusive
    # with `exceptions[team]` for the same team, and with any outgoing salary
    # from that team in this trade — both are rejected by the validator.
    tpe_usage: dict[str, str] = {}
    # Sign-and-trade: whoever submits the trade must know and declare this —
    # nothing in the data lets us infer it (a team signing then trading a
    # player days later can be a coincidence, not a sign-and-trade). When set,
    # every team acquiring a player in this trade is hard-capped at First Apron
    # (rulebook §1.4 row C). sign_and_trade_txn_id optionally links to the
    # paired `sign` transaction's id for traceability from the log.
    is_sign_and_trade: bool = False
    sign_and_trade_txn_id: Optional[str] = None
    # Player slugs whose acquisition is the sign-and-trade portion of this
    # trade. When set, only the team(s) receiving these specific players are
    # hard-capped — needed when a multi-team trade bundles an S&T together
    # with unrelated player-for-player legs, so the cap doesn't spill onto
    # teams that aren't party to the S&T. Omit to fall back to "every team
    # acquiring any player is capped", which is correct for a plain two-team S&T.
    sign_and_trade_players: list[str] = []
    # The proposed new contract(s) being signed as part of the S&T, keyed by
    # player slug. Reuses SignDetails verbatim (player/team/contract/
    # signing_method/bird_rights_type) so the § 3.14 contract-requirement
    # checks (length, raise cap, MLE exclusion, Bird-rights eligibility) can
    # run here even though the real contract is applied via a separate `sign`
    # transaction in practice (see .claude/commands/enter-transaction.md) —
    # this only informs validation, it never itself writes a contract.
    sign_and_trade_signings: list[SignDetails] = []


class TradeValidateInput(BaseModel):
    transfers: list[TradeTransfer]
    is_sign_and_trade: bool = False
    sign_and_trade_players: list[str] = []
    sign_and_trade_signings: list[SignDetails] = []
    exceptions: dict[str, Optional[str]] = {}
    tpe_usage: dict[str, str] = {}


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

    # Stretch provision (§5.1 method 2): re-flatten the same total obligation
    # computed above evenly across `stretch_years` seasons starting at the
    # release season, instead of the original per-season payment schedule.
    if details.stretch_years:
        total = sum(_parse_dollar(v) for v in dead_cap.values())
        if total > 0:
            n = details.stretch_years
            per_year = total // n
            seasons = []
            s = cur_season
            for _ in range(n):
                seasons.append(s)
                s = _season_after(s)
            stretched: dict[str, str] = {}
            running = 0
            for i, season in enumerate(seasons):
                amt = total - running if i == n - 1 else per_year
                running += amt
                stretched[season] = f"${amt:,}"
            dead_cap = stretched

    # Preserve earnings history: keep every season already played (<= cur_season)
    # plus any future season that's guaranteed (the player still collects it, as
    # the dead-cap amount). Drop only unplayed, non-guaranteed future years.
    # Stretch years beyond the player's original contract horizon are added in
    # since they won't already be keys in bio["salaries"].
    def _kept(season: str) -> bool:
        return season <= cur_season or season in dead_cap
    bio["salaries"] = {
        season: (dead_cap[season] if season in dead_cap and season > cur_season else salary)
        for season, salary in bio.get("salaries", {}).items()
        if _kept(season)
    }
    for season, amount in dead_cap.items():
        if season > cur_season and season not in bio["salaries"]:
            bio["salaries"][season] = amount
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
    # The `salaries` entry for the hold season itself (and any season after) is
    # never real pay — it's just the cap-hold number — so the cutoff is the
    # earliest FA hold, not cur_season (which would wrongly keep a hold-season
    # entry that happens to equal the current in-progress season).
    cutoff = fa_years[0]
    keep = lambda s: s < cutoff
    bio["salaries"] = {k: v for k, v in bio.get("salaries", {}).items() if keep(k)}
    bio["guaranteed"] = {k: v for k, v in bio.get("guaranteed", {}).items() if keep(k)}
    bio["guarantee_dates"] = {k: v for k, v in bio.get("guarantee_dates", {}).items() if keep(k)}
    bio["guarantee_schedule"] = {k: v for k, v in bio.get("guarantee_schedule", {}).items() if keep(k)}
    bio["cap_holds"] = {y: t for y, t in holds.items() if y > next_season}
    bio["type"] = ""
    save_player_bios(bios)

    # Remove from roster CSV
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    write_csv(path, headers, rows)

    _scrub_trading_block({team: {details.player}}, bios)
    log_write(info, f"TXN renounce — {details.player} from {team}")
    return team


def _apply_void_player(details: VoidPlayerDetails, txn_date: str, info: dict) -> str:
    """Removes a player from their roster with no dead cap and no remaining
    obligation (rulebook §5.1 void circumstances: real-life retirement, not
    present in the current game build, or an unwanted 2nd-rounder/UDFA before
    the July 31 deadline). Unlike release, no dead_cap CSV entry is written —
    the team owes nothing. Returns the player's former team."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    cur_season = _season_for_date(txn_date)
    bio = bios[details.player]
    past, past_gtd, past_gtd_dates, past_gtd_sched = _retained_history(bio, cur_season)
    bio["salaries"] = past
    bio["guaranteed"] = past_gtd
    bio["guarantee_dates"] = past_gtd_dates
    bio["guarantee_schedule"] = past_gtd_sched
    bio["cap_holds"] = {}
    bio["type"] = ""
    save_player_bios(bios)

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    write_csv(path, headers, rows)

    _scrub_trading_block({team: {details.player}}, bios)
    log_write(info, f"TXN void_player — {details.player} from {team} ({details.reason or 'no reason given'})")
    return team


def _apply_set_hard_cap(details: SetHardCapDetails, txn_date: str, info: dict) -> str:
    """Manually sets or clears a team's hard-cap level for the season, through
    the transaction log instead of the silent PUT /api/team-state side door
    (which leaves no reason/author trail). Unlike _maybe_set_hard_cap — the
    auto-trigger path used by sign/trade, which only ever raises the level —
    this is an explicit override and can also lower or clear it. Returns the
    team abbr."""
    team = details.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {details.team!r}")
    if details.level not in ("first_apron", "second_apron", "default"):
        raise HTTPException(status_code=422, detail="level must be 'first_apron', 'second_apron', or 'default'")

    cur_season = _season_for_date(txn_date)
    new_cap = None if details.level == "default" else details.level
    with _state_lock:
        state = load_team_state()
        ts = state.get(team, {}).get(cur_season, dict(DEFAULT_SEASON_STATE))
        ts["hard_cap"] = new_cap
        ts["hard_cap_reason"] = details.reason
        state.setdefault(team, {})[cur_season] = ts
        save_team_state(state)

    log_write(info, f"TXN set_hard_cap_level — {team} {cur_season} -> {details.level}")
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
        "signing_method": details.signing_method or "convert_twoway",
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

    # A two-way conversion adds real salary against a team's cap exactly like a
    # sign does (rulebook §847: "must have either cap space or a valid
    # exception") -- so it needs the same MLE/BAE bookkeeping and hard-cap
    # trigger as _apply_sign, keyed off the same self-declared signing_method.
    if details.signing_method in ("mle", "ntmle", "tmle", "room_exception", "bae", "cap_space"):
        with _state_lock:
            state = load_team_state()
            if team not in state:
                state[team] = {}
            ts = state[team].get(cur_season, dict(DEFAULT_SEASON_STATE))

            if details.signing_method == "cap_space":
                if not ts.get("mle_type"):
                    ts["mle_type"] = "room"

            elif details.signing_method in ("mle", "ntmle", "tmle", "room_exception"):
                yr1 = 0
                for yr in sorted(details.contract.salaries.keys()):
                    if yr >= cur_season:
                        yr1 = int(str(details.contract.salaries[yr])
                                  .replace("$", "").replace(",", "").strip() or 0)
                        break
                ts["mle_used"] = ts.get("mle_used", 0) + yr1

                resolved = details.signing_method
                if resolved == "mle":
                    resolved = ts.get("mle_type") or "ntmle"
                ts["mle_type"] = resolved
                if resolved == "ntmle":
                    _maybe_set_hard_cap(ts, "first_apron", f"NTMLE two-way conversion: {details.player}")
                elif resolved == "tmle":
                    _maybe_set_hard_cap(ts, "second_apron", f"TMLE two-way conversion: {details.player}")

            elif details.signing_method == "bae":
                ts["bae_used"] = True
                _maybe_set_hard_cap(ts, "first_apron", f"BAE two-way conversion: {details.player}")

            state[team][cur_season] = ts
            save_team_state(state)

    log_write(info, f"TXN convert_twoway — {details.player} ({team}) [{details.signing_method or 'cap_space'}]")
    return team


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

    cur_season = _season_for_date(txn_date)
    team_map = _build_team_map()
    old_hold_team = None
    if details.player in team_map:
        existing = team_map[details.player]
        if existing != team:
            existing_hold = (bios.get(details.player, {}).get("cap_holds") or {}).get(cur_season)
            if existing_hold not in _FA_HOLD_TYPES:
                raise HTTPException(status_code=409, detail=f"Player {details.player!r} is already on {existing}")
            # The old team only carries a free-agent cap hold (UFA/RFA), not an
            # active contract — signing elsewhere supersedes the hold, same as
            # an explicit renounce (rulebook § 3.10), so clean it up here rather
            # than requiring the old team to renounce first.
            old_hold_team = existing

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Roster file not found for {team}")
    headers, rows = read_csv(path)
    if "SLUG" not in headers:
        raise HTTPException(status_code=422, detail=f"Roster for {team} uses legacy format; migrate before using transactions")

    if old_hold_team:
        old_path = DATA_DIR / f"{old_hold_team.lower()}-roster.csv"
        if old_path.exists():
            old_headers, old_rows = read_csv(old_path)
            old_rows = [r for r in old_rows if r.get("SLUG", "").strip() != details.player]
            write_csv(old_path, old_headers, old_rows)
        _scrub_trading_block({old_hold_team: {details.player}}, bios)

    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    row_type = "two-way" if details.contract.type == "two-way" else ""
    rows.append({"SLUG": details.player, "TYPE": row_type})
    write_csv(path, headers, rows)

    bio = bios[details.player]
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


def _apply_trade(details: TradeIn, txn_date: str, info: dict,
                 txn_id: str | None = None) -> list[str]:
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
    conveyance_store = _load_conveyance_store_for_shadow_check()

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
                owned_by_from_team = _check_pick_ownership(
                    key, xfer.from_team, pick_row, conveyance_store, asset.leaf_id)
                if not owned_by_from_team:
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
                _check_retrade_allowed(key, asset, xfer.from_team, conveyance_store)

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
                        pick_key = (asset.year, asset.round, asset.orig)
                        if asset.protection is not None:
                            p["PROTECTED"] = str(asset.protection)
                            _register_trade_protection(pick_key, xfer.from_team,
                                                       xfer.to_team, asset.protection,
                                                       txn_id=txn_id, txn_date=txn_date)
                        if asset.swap_with is not None:
                            p["SWAP_OWNER"] = asset.swap_with
                            _register_trade_swap(pick_key, xfer.to_team,
                                                 asset.swap_with, picks,
                                                 txn_id=txn_id, txn_date=txn_date)
                        if asset.protection is None and asset.swap_with is None:
                            _handle_pick_retrade(pick_key, xfer.from_team,
                                                 xfer.to_team, conveyance_store,
                                                 leaf_id=asset.leaf_id,
                                                 txn_id=txn_id, txn_date=txn_date)
                        if asset.ladder_protect_top is not None:
                            fallback_tuples = [
                                (fb["year"], fb["round"], fb["orig"].upper())
                                for fb in (asset.ladder_fallback or [])
                            ]
                            _register_trade_ladder(pick_key, xfer.from_team,
                                                   xfer.to_team,
                                                   asset.ladder_protect_top,
                                                   fallback_tuples,
                                                   txn_id=txn_id, txn_date=txn_date)
                        p["OWNER"] = xfer.to_team
    save_picks(picks)

    trade_removals: dict[str, set[str]] = {}
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                trade_removals.setdefault(xfer.from_team, set()).add(asset.slug)
    _scrub_trading_block(trade_removals, bios)

    # ── § 4.4 contagion: below-apron-2 aggregation hard-caps the team ──────────
    # Mirrors _validate_trade's aggregation check (per outgoing leg's salary
    # tested independently against total incoming) so the trigger condition
    # never drifts from what the validator already decided was legal.
    cur_season = _season_for_date(txn_date)
    _validation_bios = load_player_bios()
    _cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    outgoing_sal, incoming_sal, out_players_map, _ = _trade_flows(details, _validation_bios, cur_season)
    contagion_teams = []
    for aggr_team, slugs in out_players_map.items():
        if len(slugs) < 2 or incoming_sal.get(aggr_team, 0) <= 0:
            continue
        inc = incoming_sal[aggr_team]
        team_current = _compute_team_salary(aggr_team, _validation_bios, cur_season)
        team_current_ex_holds = _compute_team_salary_ex_holds(aggr_team, _validation_bios, cur_season)
        needs_aggregation = any(
            (lc := _check_salary_matching(
                aggr_team,
                _parse_dollar((_validation_bios.get(slug, {}).get("salaries") or {}).get(cur_season, "")),
                inc, team_current, _cap_levels, cur_season,
                team_state=None, team_salary_ex_holds_before=team_current_ex_holds,
            )) is not None and not lc.passed
            for slug in slugs
        )
        if needs_aggregation:
            contagion_teams.append(aggr_team)

    if contagion_teams:
        with _state_lock:
            state = load_team_state()
            for aggr_team in contagion_teams:
                if aggr_team not in state:
                    state[aggr_team] = {}
                ts = state[aggr_team].get(cur_season, dict(DEFAULT_SEASON_STATE))
                _maybe_set_hard_cap(ts, "second_apron", f"Salary aggregation in trade (§ 4.4 contagion, {txn_date})")
                state[aggr_team][cur_season] = ts
            save_team_state(state)

    if details.exceptions or details.tpe_usage:
        cur_season = _season_for_date(txn_date)
        _, incoming, _, _ = _trade_flows(details, bios, cur_season)

        if details.exceptions:
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
                    if exc_type == "bae":
                        # Boolean-per-season, not a running balance like NTMLE/
                        # TMLE/Room — mirrors the BAE-signing bookkeeping in
                        # _apply_sign (§ 3.5), just triggered from a trade.
                        ts["bae_used"] = True
                        _maybe_set_hard_cap(ts, "first_apron", f"BAE trade absorption ({txn_date})")
                    else:
                        ts["mle_used"] = ts.get("mle_used", 0) + inc
                        ts["mle_type"] = exc_type
                        if exc_type == "ntmle":
                            _maybe_set_hard_cap(ts, "first_apron", f"NTMLE trade absorption ({txn_date})")
                        elif exc_type == "tmle":
                            _maybe_set_hard_cap(ts, "second_apron", f"TMLE trade absorption ({txn_date})")
                    state[team][cur_season] = ts
                save_team_state(state)

        if details.tpe_usage:
            with _trade_exc_lock:
                tpe_data = load_trade_exceptions()
                for team, tpe_id in details.tpe_usage.items():
                    if not tpe_id:
                        continue
                    team = team.upper()
                    inc = incoming.get(team, 0)
                    if inc <= 0:
                        continue
                    for e in tpe_data.get(team, []):
                        if e.get("id") == tpe_id:
                            e["remaining"] = max(0, e.get("remaining", 0) - inc)
                            break
                save_trade_exceptions(tpe_data)

    if details.is_sign_and_trade:
        cur_season = _season_for_date(txn_date)
        if details.sign_and_trade_players:
            acquiring_teams = sorted({
                xfer.to_team for xfer in details.transfers
                if any(a.type == "player" and a.slug in details.sign_and_trade_players
                       for a in xfer.assets)
            })
        else:
            acquiring_teams = sorted({
                xfer.to_team for xfer in details.transfers
                if any(a.type == "player" for a in xfer.assets)
            })
        reason = (f"Sign-and-trade acquisition (txn {details.sign_and_trade_txn_id})"
                  if details.sign_and_trade_txn_id else f"Sign-and-trade acquisition ({txn_date})")
        with _state_lock:
            state = load_team_state()
            for team in acquiring_teams:
                if team not in state:
                    state[team] = {}
                ts = state[team].get(cur_season, dict(DEFAULT_SEASON_STATE))
                _maybe_set_hard_cap(ts, "first_apron", reason)
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
    "bae": "Bi-Annual Exception",
}


def _check_bae_absorption(
    team: str,
    incoming: int,
    team_salary_ex_holds_before: int,
    cl: dict,
    team_state: Optional[dict],
    season: str,
) -> CheckResult:
    """BAE trade absorption (§ 4.2a, extended to include the BAE alongside
    NTMLE/TMLE/Room Exception). Unlike those three, the BAE isn't a running
    dollar balance — it's a once-per-season boolean (`bae_used`), so it can't
    share `_check_exception_absorption`'s amount-key/`mle_used` bookkeeping;
    eligibility mirrors free-agent BAE use instead (§ 3.5): below the First
    Apron, not already used this season, not used in the immediately prior
    season (`_bae_available`), and not already used alongside cap space or the
    Room Exception this same season."""
    check_name = f"salary_matching_{team.lower()}"
    apron1 = cl.get("apron1")
    ts = get_season_state(team_state or {}, team, season)

    if apron1 is None or team_salary_ex_holds_before >= apron1:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team} is not eligible for the Bi-Annual Exception — Team Salary "
                      f"(${team_salary_ex_holds_before:,}) is at/above the First Apron "
                      f"({'$' + format(apron1, ',') if apron1 is not None else 'unset'}) — § 3.5."),
        )
    if ts.get("bae_used"):
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=f"{team} has already used the Bi-Annual Exception this season (§ 3.5).",
        )
    if ts.get("mle_type") == "room":
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team} has already used cap space or the Room Exception this season — "
                      "the BAE cannot also be used the same season (§ 3.5)."),
        )
    if not _bae_available(team_state or {}, team, season):
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=f"{team} used the Bi-Annual Exception last season — unavailable in consecutive years (§ 3.5).",
        )

    total = cl.get("bae_amount", 0) or 0
    if incoming > total:
        over = incoming - total
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team}: Bi-Annual Exception absorption failed — incoming salary ${incoming:,} "
                      f"exceeds the BAE amount of ${total:,} by ${over:,}."),
        )
    return CheckResult(
        check=check_name, passed=True,
        message=(f"{team}: incoming salary ${incoming:,} absorbed via the Bi-Annual Exception "
                  f"(${total:,} available this season) — no salary match required."),
    )


def _check_tpe_absorption(
    team: str,
    incoming: int,
    outgoing: int,
    tpe_id: str,
    trade_exceptions: dict,
    season: str,
) -> CheckResult:
    """Trade Exception (TPE) absorption (§ 4.1a): a team may use a banked TPE
    to acquire a player at or below its remaining balance, without sending
    matching salary back. Cannot be combined with outgoing salary in the same
    acquisition (checked here) or with another exception (checked by the
    caller, which picks this path or the MLE path, never both)."""
    check_name = f"salary_matching_{team.lower()}"
    record = next((e for e in trade_exceptions.get(team, []) if e.get("id") == tpe_id), None)
    if record is None:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=f"{team}: no Trade Exception with id {tpe_id!r} on file.",
        )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if record.get("expires_date", "") < today:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team}: Trade Exception {tpe_id} expired {record['expires_date']} "
                      f"(banked {record['acquired_date']}) — cannot be used (§ 4.1a)."),
        )
    if outgoing > 0:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team}: a Trade Exception cannot be combined with outgoing salary in the "
                      f"same acquisition (§ 4.1a) — this leg sends ${outgoing:,} in outgoing salary."),
        )
    remaining = record.get("remaining", 0)
    if incoming > remaining:
        over = incoming - remaining
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team}: Trade Exception {tpe_id} absorption failed — incoming salary "
                      f"${incoming:,} exceeds the remaining balance of ${remaining:,} "
                      f"(${record['amount']:,} original, expires {record['expires_date']}) by ${over:,}."),
        )
    return CheckResult(
        check=check_name, passed=True,
        message=(f"{team}: incoming salary ${incoming:,} absorbed via Trade Exception {tpe_id} "
                  f"(${remaining:,} of ${record['amount']:,} remaining before this trade, "
                  f"expires {record['expires_date']}) — no salary match required."),
    )


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
    if exception_type == "bae":
        return _check_bae_absorption(team, incoming, team_salary_ex_holds_before, cl, team_state, season)
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
                              MLE trade absorption if `exceptions[team]` is set (§ 4.2a),
                              or Trade Exception absorption if `tpe_usage[team]` is set (§ 4.1a)
      • 2nd-apron aggreg.  — § 4.4 (apron-2 teams may not combine outgoing salaries to
                              match incoming; each outgoing leg must independently clear
                              the match — error if the team is at/above the 2nd apron,
                              contagion warning if aggregating below it)
      • Hard cap           — team apron hard cap (team-state) + league-wide hard cap
      • Roster size        — Article II: 15 in-season, 20 offseason ceiling
      • Sign-and-trade     — § 3.14 contract rules + § 4.3 receiving-team apron limit,
                              only when `is_sign_and_trade` is set
    """
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    # Sign-and-trade: the player's bio doesn't yet reflect the NEW contract
    # being proposed — that's a separate `sign` transaction in the real
    # two-step submission flow (see .claude/commands/enter-transaction.md),
    # which may not have happened yet at validation time. Overlay the
    # proposed contract's current-season salary/cap_holds onto a local copy of
    # `bios` so salary-matching computes against the real proposed terms
    # (mirrors what `_apply_sign` itself would write), not a stale cap-hold
    # placeholder or an expired old contract. ctx["bios"] itself is untouched.
    signings_by_slug = {s.player: s for s in (details.sign_and_trade_signings or [])}
    if signings_by_slug:
        bios = dict(bios)
        for slug, signing in signings_by_slug.items():
            base_bio = dict(bios.get(slug, {}))
            base_bio["salaries"] = {**base_bio.get("salaries", {}), season: signing.contract.salaries.get(season, "")}
            base_bio["cap_holds"] = {k: v for k, v in base_bio.get("cap_holds", {}).items() if k != season}
            bios[slug] = base_bio

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

    # ── Base Year Compensation (§ 4.2, sign-and-trade only) ────────────────────
    # For a sign-and-trade player meeting all four BYC criteria, the SENDING
    # team's outgoing figure for salary-MATCHING purposes only is the greater
    # of the player's previous salary or 50% of the new salary — real
    # cap/hard-cap accounting above still uses the true new salary, since
    # that's what actually leaves the sender's books. Kept as a separate dict
    # (not a mutation of `outgoing`) so the two uses never get conflated.
    # Scope note: only applied to the primary § 4.2 tiered-matching check
    # below, not the § 4.4 aggregation-independence test — the rulebook
    # doesn't specify that interaction and it has no real precedent yet.
    matching_outgoing = dict(outgoing)
    if details.is_sign_and_trade:
        for slug, signing in signings_by_slug.items():
            from_team = next(
                (xfer.from_team.upper() for xfer in details.transfers
                 for a in xfer.assets if a.type == "player" and a.slug == slug),
                None,
            )
            if not from_team:
                continue
            new_sal = _parse_dollar(signing.contract.salaries.get(season, ""))
            orig_bio = ctx["bios"].get(slug, {})
            prior_seasons = sorted(s for s in (orig_bio.get("salaries") or {}) if s < season)
            prev_sal = _parse_dollar(orig_bio["salaries"][prior_seasons[-1]]) if prior_seasons else 0
            bird_ok = signing.bird_rights_type in ("QVFA", "EQVFA")
            raise_pct = ((new_sal - prev_sal) / prev_sal) if prev_sal else 0
            team_salary_after = _compute_team_salary(from_team, bios, season)
            cap = ctx["cap_levels"].get(season, {}).get("cap")
            at_or_above_cap = cap is not None and team_salary_after >= cap
            if bird_ok and new_sal > 0 and prev_sal > 0 and raise_pct > 0.20 and at_or_above_cap:
                byc_credit = max(prev_sal, round(0.5 * new_sal))
                if byc_credit < new_sal:
                    matching_outgoing[from_team] = matching_outgoing.get(from_team, 0) - (new_sal - byc_credit)
                    checks.append(CheckResult(
                        check=f"byc_{slug}", passed=True,
                        message=(f"{from_team}: Base Year Compensation applies to {slug} (§ 4.2) — "
                                  f"outgoing credit for salary matching is ${byc_credit:,} (greater of "
                                  f"previous salary ${prev_sal:,} or 50% of new salary ${round(0.5*new_sal):,}), "
                                  f"not the full new salary ${new_sal:,}."),
                    ))

    # ── Salary matching (§ 4.2 / § 4.3) + hard cap ─────────────────────────────
    for team in teams:
        out = outgoing.get(team, 0)
        match_out = matching_outgoing.get(team, 0)
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
            tpe_id = (details.tpe_usage or {}).get(team)
            if exc_type and tpe_id:
                checks.append(CheckResult(
                    check=f"salary_matching_{team.lower()}", passed=False, level="error",
                    message=(f"{team}: cannot combine a Trade Exception with another exception "
                              "in the same trade (§ 4.1a)."),
                ))
            elif tpe_id:
                checks.append(_check_tpe_absorption(team, inc, out, tpe_id,
                                                    ctx.get("trade_exceptions", {}), season))
            else:
                sm = _check_salary_matching(team, match_out, inc, current, ctx["cap_levels"], season,
                                             exception_type=exc_type, team_state=ctx.get("team_state"),
                                             team_salary_ex_holds_before=current_ex_holds)
                checks.append(sm or CheckResult(
                    check=f"salary_matching_{team.lower()}", passed=True,
                    message=f"{team}: incoming ${inc:,} matches outgoing ${match_out:,} (§ 4.2/4.3).",
                ))

        # ── 2nd-apron aggregation (§ 4.4) ──────────────────────────────────────
        # A team may not aggregate (combine) two or more outgoing salaries to
        # match a single incoming salary while at/above the second apron — each
        # outgoing leg's salary must independently satisfy the match against the
        # same incoming total. Since an apron-2 team is always also above the
        # first apron, that per-leg test is exactly the § 4.3 flat-limit branch
        # of _check_salary_matching, called once per player instead of once with
        # the summed outgoing total — so this reuses the real matching logic
        # rather than a second copy of it. A team below the second apron that
        # needs the sum (i.e. no single leg independently clears the match) is
        # legal now but triggers a Second Apron hard cap for the rest of the
        # season (§ 1.4, § 4.4 contagion) — flagged as a warning here.
        out_slugs = out_players.get(team, [])
        if inc > 0 and len(out_slugs) >= 2:
            leg_salaries = {
                slug: _parse_dollar((bios.get(slug, {}).get("salaries") or {}).get(season, ""))
                for slug in out_slugs
            }
            failing = sorted(
                slug for slug, sal in leg_salaries.items()
                if (lc := _check_salary_matching(team, sal, inc, current, ctx["cap_levels"], season,
                                                  team_state=ctx.get("team_state"),
                                                  team_salary_ex_holds_before=current_ex_holds)) is not None
                and not lc.passed
            )
            apron2 = ctx["cap_levels"].get(season, {}).get("apron2")
            at_apron2 = apron2 is not None and current_ex_holds >= apron2

            if failing and at_apron2:
                checks.append(CheckResult(
                    check=f"apron2_aggregation_{team.lower()}", passed=False, level="error",
                    message=(f"{team} is at/above the 2nd apron (${current_ex_holds:,} ≥ ${apron2:,}) and is "
                             f"aggregating {len(out_slugs)} outgoing salaries to match ${inc:,} incoming "
                             f"(§ 4.4) — combining salaries is prohibited above the 2nd apron, and "
                             f"{', '.join(failing)} would not independently clear the match."),
                ))
            elif failing:
                checks.append(CheckResult(
                    check=f"apron2_aggregation_{team.lower()}", passed=False, level="warning",
                    message=(f"{team} is aggregating {len(out_slugs)} outgoing salaries to match ${inc:,} "
                             "incoming — legal below the 2nd apron, but triggers a Second Apron hard cap "
                             "for the rest of the season (§ 1.4, § 4.4 contagion) once submitted."),
                ))
            else:
                checks.append(CheckResult(
                    check=f"apron2_aggregation_{team.lower()}", passed=True,
                    message=(f"{team} is trading {len(out_slugs)} players — each outgoing salary "
                             "independently satisfies the match, no aggregation needed (§ 4.4)."),
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

    # ── Sign-and-trade (§ 3.14 contract rules + § 4.3 receiving restriction) ───
    # Only runs when the submitter declares is_sign_and_trade — nothing here is
    # inferred. `sign_and_trade_signings` is optional even then (the real
    # two-step submission flow may not have collected contract terms at
    # validate-time); the contract-specific checks below simply don't fire for
    # a signing with no entry, but the receiving-team apron restriction below
    # still runs off `sign_and_trade_players`/`is_sign_and_trade` alone.
    if details.is_sign_and_trade:
        for slug, signing in signings_by_slug.items():
            signing_team = signing.team.upper()
            ts_signer = get_season_state(ctx["team_state"], signing_team, season)

            bird_ok = signing.bird_rights_type in ("QVFA", "EQVFA")
            if bird_ok:
                checks.append(CheckResult(
                    check=f"sat_bird_rights_{slug}", passed=True,
                    message=f"{slug}: has at least Early Bird Rights (EQVFA) with {signing_team} (§ 3.14).",
                ))
            else:
                checks.append(CheckResult(
                    check=f"sat_bird_rights_{slug}", passed=False, level="error",
                    message=(f"{slug}: sign-and-trade requires at least Early Bird Rights (EQVFA) with "
                              f"{signing_team} — declared bird_rights_type is {signing.bird_rights_type!r} (§ 3.14)."),
                ))

            mle_funded = signing.signing_method in ("mle", "ntmle", "tmle")
            if mle_funded:
                checks.append(CheckResult(
                    check=f"sat_no_mle_{slug}", passed=False, level="error",
                    message=(f"{slug}: the MLE may not fund any part of a sign-and-trade contract — "
                              f"signing_method is {signing.signing_method!r} (§ 3.14)."),
                ))
            else:
                checks.append(CheckResult(
                    check=f"sat_no_mle_{slug}", passed=True,
                    message=f"{slug}: contract is not funded by the MLE (§ 3.14).",
                ))

            tmle_locked_out = ts_signer.get("mle_type") == "tmle" and ts_signer.get("mle_used", 0) > 0
            if tmle_locked_out:
                checks.append(CheckResult(
                    check=f"sat_tmle_exclusion_{slug}", passed=False, level="error",
                    message=(f"{signing_team}: has already used the Taxpayer MLE this season — may not "
                              "participate in a sign-and-trade (§ 3.14)."),
                ))
            else:
                checks.append(CheckResult(
                    check=f"sat_tmle_exclusion_{slug}", passed=True,
                    message=f"{signing_team}: has not used the Taxpayer MLE this season, eligible to sign-and-trade (§ 3.14).",
                ))

            hold_types = {"UFA", "RFA", "PLAYER_OPT", "TEAM_OPT"}
            real_years = sorted(
                yr for yr in signing.contract.salaries
                if yr >= season and (signing.contract.cap_holds or {}).get(yr) not in hold_types
            )
            length_ok = len(real_years) in (3, 4)
            if length_ok:
                checks.append(CheckResult(
                    check=f"sat_contract_length_{slug}", passed=True,
                    message=f"{slug}: {len(real_years)}-year contract — valid sign-and-trade length (§ 3.14).",
                ))
            else:
                checks.append(CheckResult(
                    check=f"sat_contract_length_{slug}", passed=False, level="error",
                    message=(f"{slug}: sign-and-trade contracts must be 3 or 4 years — this one is "
                              f"{len(real_years)} years ({', '.join(real_years) or 'none'}) (§ 3.14)."),
                ))

            r = _check_contract_raises(signing.contract, bird_pct=False, cur_season=season)
            if r:
                r.check = f"sat_raise_limit_{slug}"
                checks.append(r)

            for sr in _validate_sign(signing, ctx):
                sr.check = f"sat_{sr.check}_{slug}"
                checks.append(sr)

        apron1 = ctx["cap_levels"].get(season, {}).get("apron1")
        if apron1 is not None:
            if details.sign_and_trade_players:
                receiving_teams = sorted({
                    xfer.to_team.upper() for xfer in details.transfers
                    if any(a.type == "player" and a.slug in details.sign_and_trade_players for a in xfer.assets)
                })
            else:
                receiving_teams = sorted({
                    xfer.to_team.upper() for xfer in details.transfers
                    if any(a.type == "player" for a in xfer.assets)
                })
            for team in receiving_teams:
                current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
                projected = current_ex_holds - outgoing.get(team, 0) + incoming.get(team, 0)
                ok = projected < apron1
                if ok:
                    checks.append(CheckResult(
                        check=f"sat_receiving_apron_{team.lower()}", passed=True,
                        message=(f"{team}: projected salary ${projected:,} is below the First Apron "
                                  f"(${apron1:,}) after the sign-and-trade (§ 4.3)."),
                    ))
                else:
                    checks.append(CheckResult(
                        check=f"sat_receiving_apron_{team.lower()}", passed=False, level="error",
                        message=(f"{team}: sign-and-trade acquisition would leave them at ${projected:,}, "
                                  f"at/above the First Apron (${apron1:,}) — a team may only receive via "
                                  "sign-and-trade if outgoing salary brings them under the threshold (§ 4.3)."),
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


def _append_historical(txn_type: str, stored_details: dict, body: TransactionIn, info: dict) -> dict:
    """Shared by the _create_historical_* helpers below: log a transaction for
    display on player/team pages without touching current roster, cap, or
    team-state data. For backfilling transactions that predate this log, or
    whose effects current data already reflects — replaying them through the
    normal _apply_* path would re-apply moves that have already happened.
    """
    stored_details["historical"] = True
    txn = {
        "id": secrets.token_hex(8),
        "type": txn_type,
        "date": body.date,
        "created_by": info.get("name", "unknown"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": body.description,
        "details": stored_details,
    }
    with _txn_lock:
        _append_transaction(txn)
    return txn


def _create_historical_trade(details: TradeIn, body: TransactionIn, info: dict) -> dict:
    bios = load_player_bios()
    teams_seen: set[str] = set()
    for transfer in details.transfers:
        for team in (transfer.from_team, transfer.to_team):
            if team.upper() not in VALID_TEAMS:
                raise HTTPException(status_code=422, detail=f"Unknown team: {team!r}")
            teams_seen.add(team.upper())
        for asset in transfer.assets:
            if asset.type == "player" and (not asset.slug or asset.slug not in bios):
                raise HTTPException(status_code=422, detail=f"Unknown player slug: {asset.slug!r}")

    stored_details = details.model_dump()
    stored_details["teams"] = sorted(teams_seen)
    return _append_historical("trade", stored_details, body, info)


def _create_historical_sign(details: SignDetails, body: TransactionIn, info: dict) -> dict:
    bios = load_player_bios()
    if not details.player or details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")
    if details.team.upper() not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {details.team!r}")
    return _append_historical("sign", details.model_dump(), body, info)


def _create_historical_option(details: OptionDetails, body: TransactionIn, info: dict) -> dict:
    bios = load_player_bios()
    if not details.player or details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")
    return _append_historical("option", details.model_dump(), body, info)


# ── Transaction routes ────────────────────────────────────────────────────────

@router.post("/api/transactions")
def create_transaction(body: TransactionIn, info: dict = Depends(require_role("rosters"))):
    try:
        datetime.strptime(body.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date; use YYYY-MM-DD")

    if body.type not in ("sign", "pick", "option", "guarantee", "release", "renounce", "trade", "convert_twoway", "sign_pick", "void_player", "set_hard_cap_level"):
        raise HTTPException(status_code=422, detail=f"Unsupported transaction type: {body.type!r}")

    if body.historical and body.type not in ("trade", "sign", "option"):
        raise HTTPException(status_code=422, detail="historical backfill only supports type=trade, sign, option")

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
        "void_player":       (VoidPlayerDetails,  "Invalid void_player details"),
        "set_hard_cap_level": (SetHardCapDetails, "Invalid set_hard_cap_level details"),
    }
    model_cls, err_prefix = _detail_models[body.type]
    try:
        parsed_details = model_cls(**body.details)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"{err_prefix}: {e}")

    if body.historical:
        if body.type == "trade":
            return _create_historical_trade(parsed_details, body, info)
        if body.type == "sign":
            return _create_historical_sign(parsed_details, body, info)
        if body.type == "option":
            return _create_historical_option(parsed_details, body, info)

    _val_ctx = {
        "bios":        load_player_bios(),
        "team_state":  load_team_state(),
        "cap_levels":  json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {},
        "cur_season":  _season_for_date(body.date),
        "trade_exceptions": load_trade_exceptions(),
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
            teams = _apply_trade(details, body.date, info, txn_id=txn_id)
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
        elif body.type == "void_player":
            team = _apply_void_player(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "set_hard_cap_level":
            team = _apply_set_hard_cap(details, body.date, info)
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
            if (d.get("team") or "").upper() == t_upper:
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


class TransactionPatch(BaseModel):
    description: str


@router.patch("/api/transactions/{txn_id}")
def patch_transaction(txn_id: str, body: TransactionPatch, info: dict = Depends(require_role("rosters"))):
    with _txn_lock:
        txns = _load_transactions()
        for t in txns:
            if t.get("id") == txn_id:
                t["description"] = body.description
                TRANSACTIONS_FILE.write_text(json.dumps(txns, indent=2))
                log_write(info, f"TXN note edit — {txn_id}")
                return t
        raise HTTPException(status_code=404, detail="Transaction not found")


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
        "trade_exceptions": load_trade_exceptions(),
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

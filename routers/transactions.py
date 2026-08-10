import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, CAP_LEVELS_FILE, TRANSACTIONS_FILE, VALID_TEAMS, ROOM_ZONE_BASELINE_FILE,
    _txn_lock, _deadcap_lock, _state_lock, _picks_lock, _trade_exc_lock,
    ROSTER_MAX, ROSTER_OFFSEASON_MAX, ROSTER_MIN, ROSTER_CHARGE_MIN, SALARY_MATCH_TIER1_CAP, SALARY_MATCH_TIER2_CAP,
)
from .storage import (
    read_csv, write_csv, _load_json, log_write, _parse_dollar, _season_start, _season_shift,
    _season_for_date, _current_league_year, _league_rollovers, _season_start_date,
)
from .auth import require_role, get_token_info, is_team_owner
from .discord_notify import notify_transaction
from .players import load_player_bios, save_player_bios, _build_team_map, _scrub_trading_block
from .roster_picks import (
    load_picks, save_picks, _all_picks_flat,
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
    # § 3.10: only consulted when the contract's cap_holds imply a trailing
    # UFA/RFA hold whose season needs a Full Bird (QVFA) EAPS comparison and
    # that season has no real EAPS on file yet — see _compute_fa_hold_amount.
    # "above" or "below" (the previous salary, relative to EAPS). Ignored
    # otherwise.
    eaps_assumption: Optional[str] = None


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
    # § 3.10: auto-computed from bird_tier + the last real salary when omitted
    # — see _compute_fa_hold_amount. Pass explicitly only to override.
    cap_hold_amount: Optional[str] = None
    bird_tier: Optional[str] = None  # QVFA, EQVFA, Non-QVFA — only used on decline
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails
    # Only used on the historical=true backfill path (see _create_historical_option)
    # — the live path (_apply_option) always derives team itself from the current
    # roster map and ignores this field.
    team: Optional[str] = None


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


class RescindRenounceDetails(BaseModel):
    txn_id: str   # the renounce being undone; its stored snapshot is the restore source


class OfferSheetDetails(BaseModel):
    """An offer sheet being *extended* (§ 3.15). Deliberately carries no
    `outcome`: extending the offer and the incumbent's decision to match are two
    separate acts by two different teams, and collapsing them into one record
    attributed the incumbent's decision to whoever typed the transaction.
    The outcome now lives on `offer_sheet_decision`.

    Legacy ledger entries from the combined era still carry `outcome` in their
    stored details. Those are read as raw dicts, never through this model, and
    `_open_offer_sheets` treats the presence of `outcome` as "already resolved".
    """
    player: str
    offering_team: str
    contract: ContractIn
    # What actually funds the contract for whichever team ends up signing the
    # player (§ 3.1-3.6 methods; same vocabulary as SignDetails.signing_method).
    # Threaded straight into the internal _apply_sign call so NTMLE/TMLE/BAE
    # usage on an offer sheet gets the same mle_used/hard-cap bookkeeping and
    # § 1.5/§ 1.6 funding-availability validation a plain `sign` gets — before
    # this field existed, an offer sheet had no way to record its funding
    # mechanism at all, so e.g. a Second-Apron team using the NTMLE to sign
    # one went untracked and unvalidated.
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails


class OfferSheetDecisionDetails(BaseModel):
    offer_id: str    # the `offer_sheet` transaction being resolved
    outcome: str     # "matched" (incumbent keeps) or "not_matched" (player leaves)


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
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails


class SignPickDetails(BaseModel):
    # Signs a player whose draft rights a team holds to their first contract.
    player: str
    contract: ContractIn
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails


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
    # Team abbr -> the specific incoming player slugs that team's exception is
    # absorbing (§ 4.2a: "a team uses exactly one of the four for a given
    # incoming player"). The named players are funded by the exception; every
    # other incoming player still has to satisfy ordinary salary matching
    # against outgoing salary. This is what makes a hybrid trade expressible —
    # e.g. two players matched against an outgoing contract while a third is
    # absorbed into the MLE.
    #
    # Omit (or leave a team out) to keep the original all-or-nothing behavior,
    # where the exception is tested against that team's entire incoming total.
    # Every slug must actually be incoming to that team in this trade.
    exception_players: dict[str, list[str]] = {}
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
    exception_players: dict[str, list[str]] = {}
    tpe_usage: dict[str, str] = {}


class TransactionValidationResult(BaseModel):
    """Shape returned by every `/api/validate/*` endpoint: the verdict, the
    full check list (passes included), and a type-specific financial snapshot."""
    legal: bool
    checks: list[CheckResult]
    fact_sheet: dict


# Pre-simulator name, kept so existing callers/tests importing it still work.
TradeValidationResult = TransactionValidationResult


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
        # § 3.10: auto-compute the resulting hold's dollar figure when the
        # submitter didn't give one explicitly, from the last remaining real
        # salary and Bird tier (declared bird_tier, else derived from tenure).
        cap_hold_amount = details.cap_hold_amount
        if not cap_hold_amount:
            prev_entries = sorted(bio["salaries"].items(), key=lambda kv: _season_start(kv[0]))
            prev_salary = prev_entries[-1][1] if prev_entries else None
            if prev_salary:
                cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
                tier = details.bird_tier or _derive_bird_tier(bio, team, details.year, details.player)
                min_amt = _rookie_min_salary(details.year, cap_levels) or None
                max_amt = _max_salary(bio, details.year, cap_levels)
                amount, note = _compute_fa_hold_amount(
                    prev_salary, tier, details.year, cap_levels, details.eaps_assumption, min_amt, max_amt,
                )
                cap_hold_amount = f"${amount:,}"
                if note:
                    bio["cap_hold_notes"] = {**bio.get("cap_hold_notes", {}), details.year: note}
        if cap_hold_amount:
            bio["salaries"][details.year] = cap_hold_amount
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


def _apply_release(details: ReleaseDetails, txn_date: str, info: dict) -> tuple[str, dict, str]:
    """Removes player from roster, converts guaranteed salary to dead cap.
    Returns (team, dead_cap, terminated_salary).

    `terminated_salary` is the release-season salary the terminated contract
    carried — captured here because it is the figure § 1.4 Row D compares
    against the NTMLE when the player later signs elsewhere. Dead cap is not a
    usable substitute: a non-guaranteed contract can terminate with a large
    salary and leave no dead cap at all."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    cur_season = _season_for_date(txn_date)

    bio = bios[details.player]
    terminated_salary = str((bio.get("salaries") or {}).get(cur_season, "") or "")
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
    return team, dead_cap, terminated_salary


def _season_after(season: str) -> str:
    """'25-26' -> '26-27'."""
    a, b = season.split("-")
    return f"{(int(a) + 1) % 100:02d}-{(int(b) + 1) % 100:02d}"


def _renounce_eligibility(player: str, bios: dict, cur_season: str) -> dict:
    """Whether `player` is a renounceable free-agent hold, as
    ``{ok, team, cutoff, hold_type, reason}``.

    The single § 3.10 eligibility test, shared by `_apply_renounce` (which
    hard-fails on it), `_validate_renounce` (which reports it as a check) and
    the fact sheet. Keeping one copy is what stops the simulator or the roster
    page from offering a renounce the apply path would then reject — the same
    reason the signing validators share `_signee_existing_hold`.

    `cutoff` is the earliest UFA/RFA hold season: everything from there on is
    hold bookkeeping rather than real pay, so it's what the apply path trims to.
    """
    out = {"ok": False, "team": None, "cutoff": None, "hold_type": None, "reason": ""}
    if player not in bios:
        out["reason"] = f"Unknown player slug: {player!r}"
        return out

    team = _build_team_map().get(player)
    if not team:
        out["reason"] = f"Player {player!r} is not on any roster"
        return out
    out["team"] = team

    next_season = _season_after(cur_season)
    holds = (bios[player].get("cap_holds") or {})

    # Must be a clean free-agent hold for the current FA period: the player's
    # earliest cap hold is a UFA/RFA for the upcoming season (their contract has
    # lapsed). Players under contract, or with an unresolved option, are excluded.
    fa_years = sorted(y for y, t in holds.items() if t in ("UFA", "RFA"))
    earliest_hold = min(holds) if holds else None
    if not fa_years or earliest_hold not in fa_years or fa_years[0] > next_season:
        out["reason"] = (
            f"Player {player!r} is not a free-agent hold for {next_season}. "
            "Renounce only applies to UFA/RFA holds — decline any option, or "
            "use Release to waive a player under contract."
        )
        return out

    out.update(ok=True, cutoff=fa_years[0], hold_type=holds[fa_years[0]])
    return out


# Bio fields a renounce trims, and therefore exactly what a rescind restores.
_RENOUNCE_SNAPSHOT_FIELDS = (
    "salaries", "guaranteed", "guarantee_dates", "guarantee_schedule", "cap_holds", "type",
)


def _apply_renounce(details: RenounceDetails, txn_date: str, info: dict) -> tuple[str, dict]:
    """Renounce a free-agent hold: remove the player from the roster and clear the
    cap hold, turning them into an unsigned free agent. Unlike a release, no dead cap
    is created — a renounced free agent is owed nothing. Earnings history is preserved.

    Returns ``(team, snapshot)`` — the player's former team, and the pre-renounce
    values of every field this trims. The snapshot is stored on the transaction so
    `rescind_renounce` can put the player back exactly as they were; a renounce
    otherwise destroys contract state with nothing to reconstruct it from.
    """
    bios = load_player_bios()
    elig = _renounce_eligibility(details.player, bios, _season_for_date(txn_date))
    if not elig["ok"]:
        raise HTTPException(status_code=422, detail=elig["reason"])
    team = elig["team"]

    cur_season = _season_for_date(txn_date)
    next_season = _season_after(cur_season)
    bio = bios[details.player]
    holds = bio.get("cap_holds") or {}
    snapshot = {f: json.loads(json.dumps(bio.get(f))) for f in _RENOUNCE_SNAPSHOT_FIELDS if f in bio}

    # Preserve earnings; drop the hold salary and the renounced cap hold(s).
    # The `salaries` entry for the hold season itself (and any season after) is
    # never real pay — it's just the cap-hold number — so the cutoff is the
    # earliest FA hold, not cur_season (which would wrongly keep a hold-season
    # entry that happens to equal the current in-progress season).
    cutoff = elig["cutoff"]
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
    return team, snapshot


def _apply_rescind_renounce(details: "RescindRenounceDetails", txn_date: str, info: dict) -> tuple[str, str]:
    """Restore a renounced free agent to the team that renounced them, from the
    snapshot the renounce recorded. Returns ``(team, player)``.

    Two distinct uses, one mechanism:
      * § 3.10 rescission — a team renounced holds to fund an RFA offer sheet and
        the retaining team matched, so the space was never actually spent.
      * Administrative undo of a mistaken renounce (the case owner self-serve
        makes reachable).

    The § 3.10 (a)/(b) cap restrictions are enforced in `_validate_rescind_renounce`
    rather than here, so a genuine correction can still be forced through by the
    office when the restriction isn't the point.
    """
    txns = _load_transactions()
    src = next((t for t in txns if t.get("id") == details.txn_id), None)
    if src is None:
        raise HTTPException(status_code=422, detail=f"No transaction with id {details.txn_id!r}")
    if src.get("type") != "renounce":
        raise HTTPException(
            status_code=422,
            detail=f"Transaction {details.txn_id!r} is a {src.get('type')!r}, not a renounce",
        )
    snapshot = (src.get("details") or {}).get("_snapshot")
    if not snapshot:
        raise HTTPException(
            status_code=422,
            detail=(f"Renounce {details.txn_id!r} predates snapshotting and can't be rescinded "
                    "automatically — restore the bio by hand."),
        )
    if (src.get("details") or {}).get("_rescinded"):
        raise HTTPException(
            status_code=422,
            detail=f"Renounce {details.txn_id!r} has already been rescinded.",
        )

    player = src["details"]["player"]
    team = (src["details"].get("team") or "").upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Renounce {details.txn_id!r} has no usable team")

    bios = load_player_bios()
    if player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {player!r}")

    # Restoring a player who has since signed somewhere would duplicate them onto
    # two rosters and overwrite a real contract with a stale hold.
    current_team = _build_team_map().get(player)
    if current_team:
        raise HTTPException(
            status_code=422,
            detail=(f"{player!r} is already on {current_team}'s roster — a renounce can only be "
                    "rescinded while the player is still unsigned."),
        )

    bio = bios[player]
    for field in _RENOUNCE_SNAPSHOT_FIELDS:
        if field in snapshot:
            bio[field] = json.loads(json.dumps(snapshot[field]))
        else:
            bio.pop(field, None)
    save_player_bios(bios)

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    if not any(r.get("SLUG", "").strip() == player for r in rows):
        rows.append({h: (player if h == "SLUG" else "") for h in headers})
        write_csv(path, headers, rows)

    # Mark the source renounce as spent, so it can't be rescinded twice (the
    # second restore would re-add a roster row and overwrite whatever contract
    # state the player has picked up since). The transactions page reads this
    # to drop the undo button.
    src["details"]["_rescinded"] = True
    TRANSACTIONS_FILE.write_text(json.dumps(txns, indent=2))

    log_write(info, f"TXN rescind_renounce — {player} restored to {team} (undoes {details.txn_id})")
    return team, player


def _apply_offer_sheet(details: OfferSheetDetails, txn_date: str, info: dict,
                        txn_id: Optional[str] = None) -> tuple[str, str]:
    """Records and immediately applies an RFA offer sheet (§ 3.15) — matched
    means the retaining team signs the player to these exact terms, not
    matched means the offering team does. There's no independent decision
    left once `outcome` is known, so this is one atomic transaction rather
    than a two-step "record the offer, then submit a separate sign" flow: the
    earlier two-step design let an offer_sheet be submitted with no follow-up
    sign, silently leaving the player on nothing but the old RFA hold with no
    error (this bit a real transaction — Dyson Daniels' matched offer sheet
    went in but his LAL contract never updated). Reuses `_apply_sign`'s
    existing old_hold_team logic, which already handles both outcomes
    (same-team re-sign on match, cross-team sign clearing the old hold on a
    non-match) — this function just resolves `team` from `outcome` and calls
    it directly instead of requiring a second API call to do the same thing.
    """
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    offering_team = details.offering_team.upper()
    if offering_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {offering_team!r}")

    team_map = _build_team_map()
    retaining_team = team_map.get(details.player)
    if not retaining_team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")
    if retaining_team == offering_team:
        raise HTTPException(status_code=422, detail="offering_team cannot be the player's own team")

    cur_season = _season_for_date(txn_date)
    rfa_ok, rfa_why = _rfa_eligibility(details.player, bios, cur_season)
    if not rfa_ok:
        raise HTTPException(
            status_code=422,
            detail=(f"Player {details.player!r} is not a restricted free agent ({rfa_why}) — "
                    "offer sheets only apply to RFAs (§ 3.15)."),
        )

    # § 3.15: offer must be for at least 2 guaranteed years. `salaries` year
    # count is a proxy — this doesn't check that each year is actually
    # guaranteed (see `guaranteed`/NON_GTD), same manual-review caveat as the
    # minimum-contract cap-hit check above.
    if len(details.contract.salaries) < 2:
        raise HTTPException(status_code=422, detail="Offer sheet must cover at least 2 guaranteed years (§ 3.15)")

    # Only one live offer per player: § 3.15 gives the incumbent 48 hours to
    # match *an* offer, and two open sheets would make "match" ambiguous while
    # double-charging nobody's books in particular.
    existing = next((o for o in _open_offer_sheets() if o["player"] == details.player), None)
    if existing:
        raise HTTPException(
            status_code=422,
            detail=(f"{details.player!r} already has an open offer sheet from "
                    f"{existing['offering_team']} ({existing['date']}). Resolve it first."),
        )

    log_write(info, (f"TXN offer_sheet — {offering_team} offered {details.player} "
                      f"({retaining_team} RFA); pending {retaining_team}'s decision"))
    return offering_team, retaining_team


def _apply_offer_sheet_decision(details: "OfferSheetDecisionDetails", txn_date: str,
                                 info: dict, txn_id: Optional[str] = None) -> dict:
    """Resolve an open offer sheet (§ 3.15) — the incumbent either matches and
    keeps the player, or passes and the player signs with the offering team.

    This is the step that actually signs anybody. Splitting it back out from the
    combined transaction reintroduces the failure that forced the merge in the
    first place — an offer recorded with no follow-up used to leave the player on
    nothing but their old RFA hold, silently and with no error (it bit Dyson
    Daniels' matched sheet in production). What makes it safe this time is that a
    bare offer is no longer indistinguishable from a finished one:
    `_open_offer_sheets` can enumerate every unresolved offer, the offering team
    is charged a cap hold the whole time it's open, and the UI surfaces it past
    its deadline. Pending is now a state the system knows it's in.
    """
    offer = next((o for o in _open_offer_sheets() if o["id"] == details.offer_id), None)
    if offer is None:
        # Distinguish "never existed" from "already resolved" — they need
        # different fixes and the submitter can't tell them apart otherwise.
        prior = next((t for t in _load_transactions() if t.get("id") == details.offer_id), None)
        if prior is None:
            raise HTTPException(status_code=422, detail=f"No offer sheet with id {details.offer_id!r}")
        if prior.get("type") != "offer_sheet":
            raise HTTPException(
                status_code=422,
                detail=f"Transaction {details.offer_id!r} is a {prior.get('type')!r}, not an offer sheet")
        raise HTTPException(status_code=422, detail=f"Offer sheet {details.offer_id!r} is already resolved")

    if details.outcome not in ("matched", "not_matched"):
        raise HTTPException(status_code=422, detail="outcome must be 'matched' or 'not_matched'")

    offering_team, retaining_team = offer["offering_team"], offer["retaining_team"]
    signing_team = retaining_team if details.outcome == "matched" else offering_team

    contract = ContractIn(**offer["contract"]) if isinstance(offer["contract"], dict) else offer["contract"]
    _apply_sign(
        SignDetails(
            player=offer["player"],
            team=signing_team,
            contract=contract,
            signing_method=offer.get("signing_method"),
            bird_rights_type=offer.get("bird_rights_type"),
        ),
        txn_date, info, txn_id=txn_id,
    )

    # _apply_sign's own contracts[-1].signing_method carries the real funding
    # mechanism (or None), so it drives mle_used/hard-cap the same way a plain
    # sign does. That means it doesn't also encode "this came from an offer
    # sheet" — tag that separately so contract history stays distinguishable
    # from an ordinary re-sign.
    bios_after = load_player_bios()
    contracts = bios_after.get(offer["player"], {}).get("contracts") or []
    if contracts:
        contracts[-1]["offer_sheet_outcome"] = details.outcome
        contracts[-1]["offer_sheet_id"] = details.offer_id
        bios_after[offer["player"]]["contracts"] = contracts
        save_player_bios(bios_after)

    log_write(info, (f"TXN offer_sheet_decision — {retaining_team} "
                      f"{'matched' if details.outcome == 'matched' else 'passed on'} "
                      f"{offering_team}'s offer for {offer['player']}; signed by {signing_team}"))
    return {
        "player": offer["player"],
        "teams": [offering_team, retaining_team],
        "offering_team": offering_team,
        "retaining_team": retaining_team,
        "signing_team": signing_team,
        "outcome": details.outcome,
        "offer_id": details.offer_id,
        "offer_date": offer["date"],
        "contract": offer["contract"],
    }


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

    hold_cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    hold_notes = _autofill_fa_hold_amounts(
        bio, team, details.contract.cap_holds, details.contract.salaries, hold_cap_levels,
        bird_rights_type=details.bird_rights_type, eaps_assumption=details.eaps_assumption,
        slug=details.player,
    )
    if hold_notes:
        bio["cap_hold_notes"] = {**bio.get("cap_hold_notes", {}), **hold_notes}

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

    # Only fires if this rookie-scale contract's cap_holds jumps straight to a
    # UFA/RFA hold rather than the usual TEAM_OPT chain (§ 7.1-7.3) — the
    # final-rookie-year 250%/300% carve-out (§ 3.10) isn't implemented here,
    # so that case still needs a manually-entered cap_hold_amount via `option`.
    hold_cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    hold_notes = _autofill_fa_hold_amounts(
        bio, team, details.contract.cap_holds, details.contract.salaries, hold_cap_levels,
        bird_rights_type=details.bird_rights_type, eaps_assumption=details.eaps_assumption,
        slug=details.player,
    )
    if hold_notes:
        bio["cap_hold_notes"] = {**bio.get("cap_hold_notes", {}), **hold_notes}

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
    # Captured before the new contract overwrites it: after a release the bio
    # keeps the terminated season's salary, so this is the § 1.4 Row D figure
    # for any release logged before `terminated_salary` was stored on the
    # transaction itself.
    prior_season_salary = str((bio.get("salaries") or {}).get(cur_season, "") or "")
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

    # § 3.10: price any trailing UFA/RFA hold this contract implies (the
    # season right after it ends) that the submitter didn't already give a
    # real salary figure for. Runs after the contracts append above so Bird
    # tier can be derived from this contract's own years, not just prior ones.
    hold_cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    hold_notes = _autofill_fa_hold_amounts(
        bio, team, details.contract.cap_holds, details.contract.salaries, hold_cap_levels,
        bird_rights_type=details.bird_rights_type, eaps_assumption=details.eaps_assumption,
        slug=details.player,
    )
    if hold_notes:
        bio["cap_hold_notes"] = {**bio.get("cap_hold_notes", {}), **hold_notes}

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

    # ── § 1.4 Row D: signing a player bought out above the NTMLE ─────────────
    # Fires regardless of how the signing was funded, so it sits outside the
    # signing_method block above — a minimum-salary deal for a player whose
    # terminated contract paid more than the NTMLE triggers the lock just the
    # same. § 1.5.2 separately bars an already-first-apron team from making
    # this signing at all; that gate lives in _validate_sign.
    _cap_levels_sign = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    buyout_salary = _buyout_salary_above_ntmle(
        details.player, txn_date, cur_season, _cap_levels_sign, prior_season_salary,
    )
    if buyout_salary:
        with _state_lock:
            state = load_team_state()
            if team not in state:
                state[team] = {}
            ts = state[team].get(cur_season, dict(DEFAULT_SEASON_STATE))
            _maybe_set_hard_cap(
                ts, "first_apron",
                f"Mid-season buyout signing above NTMLE: {details.player} "
                f"(terminated contract ${buyout_salary:,}, § 1.4 Row D)",
            )
            state[team][cur_season] = ts
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
                # Once a pick has been used to draft a player it's historical
                # record, not a live asset — nothing upstream (ownership,
                # frozen, legacy/leaf checks) inspects PLAYER, so without this
                # a trade could "convey" an already-drafted pick: it'd pass
                # every other check (a drafted pick still resolves to a
                # `settled` node some team technically "owns") and write a
                # transaction for an asset that no longer exists to trade.
                drafted_player = (pick_row.get("PLAYER") or "").strip()
                if drafted_player:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Pick {asset.year} R{asset.round} {asset.orig} was already used "
                               f"to draft {drafted_player} and can no longer be traded",
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

    # ── § 4.3 contagion: below-apron-1 trade with incoming > outgoing+$250K ──
    # Distinct from the § 4.4 block above, which only fires on salary
    # aggregation (2+ outgoing legs) and locks the looser Second Apron ceiling.
    # This is the general rule for ANY trade, including a straight one-for-one
    # swap: a team below the First Apron that takes on more than outgoing +
    # $250,000 is hard-capped at the First Apron for the rest of the season,
    # even though the trade is perfectly legal under standard § 4.2 tiered
    # matching. Skips teams whose incoming salary used an exception (NTMLE/
    # TMLE/BAE/TPE) — those already carry their own dedicated hard-cap trigger
    # above and shouldn't double up on a plain-trade reason string — and skips
    # teams whose incoming salary was genuinely absorbed via cap room (§ 4.2):
    # a plain cap-space acquisition never touches the apron in real NBA rules
    # either, so it gets the same carve-out as the named exceptions rather
    # than being treated as "ordinary matching that happened to clear."
    apron1 = _cap_levels.get(cur_season, {}).get("apron1")
    cap = _cap_levels.get(cur_season, {}).get("cap")
    apron1_contagion_teams = []
    if apron1 is not None:
        exception_teams = {t.upper() for t in (details.exceptions or {})} | {t.upper() for t in (details.tpe_usage or {})}
        for team, inc in incoming_sal.items():
            if team in exception_teams:
                continue
            out = outgoing_sal.get(team, 0)
            if inc <= out + 250_000:
                continue
            # Rosters were already written above, so this figure is post-trade;
            # § 4.3 keys on where the team stood *before* it ("a team currently
            # below the First Apron"). Back the trade out to recover that.
            # Without this, the team the rule most targets — one that starts
            # below the apron and vaults over it by taking on salary — reads as
            # already-above and escapes the contagion lock entirely.
            post = _compute_team_salary_ex_holds(team, _validation_bios, cur_season)
            if post + out - inc < apron1:
                post_with_holds = _compute_team_salary(team, _validation_bios, cur_season)
                before_with_holds = post_with_holds + out - inc
                if _cap_room_absorbed(before_with_holds, out, inc, cap):
                    continue
                apron1_contagion_teams.append(team)

    if apron1_contagion_teams:
        with _state_lock:
            state = load_team_state()
            for team in apron1_contagion_teams:
                if team not in state:
                    state[team] = {}
                ts = state[team].get(cur_season, dict(DEFAULT_SEASON_STATE))
                _maybe_set_hard_cap(ts, "first_apron", f"Trade incoming exceeds outgoing +$250K (§ 4.3 contagion, {txn_date})")
                state[team][cur_season] = ts
            save_team_state(state)

    if details.exceptions or details.tpe_usage:
        cur_season = _season_for_date(txn_date)
        _, incoming, _, in_players = _trade_flows(details, bios, cur_season)

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
                    # Bill the exception only for what it actually absorbed —
                    # with exception_players set that's those players alone, not
                    # the team's whole incoming total (§ 4.2a). Same helper the
                    # validator used, so the checked amount is the billed amount.
                    inc, _matched, _err = _exception_absorption_split(
                        details, team, inc, in_players, bios, cur_season)
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
                        # team_state stores the short form "room" — that's what
                        # _apply_sign writes and what PUT /api/team-state will
                        # accept (it rejects "room_exception" outright). Storing
                        # the raw payload key here left an unreadable value that
                        # every `== "room"` comparison then missed.
                        ts["mle_type"] = "room" if exc_type == "room_exception" else exc_type
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

    # ── § 4.1a: bank a Trade Exception for salary sent out and not matched ───
    # Follows the real CBA, where a TPE is a mechanism *for over-the-cap teams*.
    # A team with cap room absorbs the difference into that room instead (the
    # cap-room absorption path in _check_salary_matching), and sending salary
    # away simply gives it more room — banking an exception on top would let
    # the same space be spent twice. Cap position is measured exactly the way
    # cap-room absorption measures it: full team salary including FA holds,
    # against the plain Salary Cap. Rosters were already written above, so this
    # figure is the post-trade position, which is the one § 4.1a asks about.
    cap = _cap_levels.get(cur_season, {}).get("cap")
    if cap is not None:
        absorbed_teams = {t.upper() for t in (details.exceptions or {})} | {t.upper() for t in (details.tpe_usage or {})}
        banked: list[tuple[str, int]] = []
        for team in sorted(set(outgoing_sal) | set(incoming_sal)):
            out = outgoing_sal.get(team, 0)
            inc = incoming_sal.get(team, 0)
            # Cheap gate first so the roster/bio scan only runs for teams that
            # could actually bank something.
            if out <= inc or team in absorbed_teams:
                continue
            amount = _tpe_bankable(
                out, inc,
                _compute_team_salary(team, _validation_bios, cur_season),
                cap, used_exception=False,
            )
            if amount:
                banked.append((team, amount))
        if banked:
            try:
                acquired_dt = datetime.strptime(txn_date, "%Y-%m-%d")
            except ValueError:
                acquired_dt = datetime.now(timezone.utc)
            expires = (acquired_dt + timedelta(days=365)).strftime("%Y-%m-%d")
            with _trade_exc_lock:
                tpe_data = load_trade_exceptions()
                for team, amount in banked:
                    tpe_data.setdefault(team, []).append({
                        "id": uuid.uuid4().hex[:12],
                        "amount": amount,
                        "remaining": amount,
                        "acquired_date": txn_date,
                        "expires_date": expires,
                        "note": (f"Auto-banked from trade {txn_id}" if txn_id
                                 else f"Auto-banked from trade ({txn_date})"),
                        "txn_id": txn_id,
                    })
                save_trade_exceptions(tpe_data)
            log_write(info, "TXN trade — banked TPE: "
                            + ", ".join(f"{t} ${a:,}" for t, a in banked))

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


def _roster_size_check(team: str, after: int, verb: str) -> "CheckResult | None":
    """§ 2.1 roster-size ceiling, in the same offseason-aware shape the trade
    validator uses (`_validate_trade`'s "Roster size" block): ≤15 passes,
    16-20 is a warning (legal, but flagged to trim before the season), and
    only >20 is a hard block. Signing-side validators (`_validate_sign`,
    `_validate_offer_sheet`, `_validate_offer_sheet_decision`,
    `_validate_convert_twoway`) used to hard-block at 15 unconditionally,
    which meant a team sitting at exactly the in-season limit couldn't even
    make an offseason FA offer — inconsistent with what a trade adding the
    same body would allow.
    """
    if after > ROSTER_OFFSEASON_MAX:
        return CheckResult(
            check="roster_size", passed=False, level="error",
            message=(f"{team} would carry {after} standard players, over the "
                     f"{ROSTER_OFFSEASON_MAX}-player offseason maximum — release a player before {verb}."),
        )
    if after > ROSTER_MAX:
        return CheckResult(
            check="roster_size", passed=False, level="warning",
            message=(f"{team} would carry {after} standard players — over the {ROSTER_MAX}-man "
                     f"regular-season limit (offseason ceiling {ROSTER_OFFSEASON_MAX}); trim to "
                     f"{ROSTER_MAX} before the season."),
        )
    return None


def _rookie_min_salary(season: str, cap_levels: dict) -> int:
    """The 0-years-experience tier of that season's minimum salary scale
    (§ 1.7) — the figure an Empty Roster Charge (§ 2.1a) uses per open slot."""
    return (cap_levels.get(season, {}).get("min_salary_scale") or {}).get("0", 0)


def _min_salary_scale_tier(years_exp: int) -> str:
    """Maps a raw years-of-NBA-experience count to a min_salary_scale key
    ("0".."9","10+")."""
    return "10+" if years_exp >= 10 else str(max(0, years_exp))


def _max_salary_pct(years_exp: int) -> float:
    """§ 3.11 tier: max Year 1 salary as a fraction of that season's Salary Cap,
    by years of NBA experience. Unlike the minimum scale, this is a clean
    percentage of the cap rather than its own negotiated table, so it needs
    no separate per-season entry in cap-levels.json."""
    if years_exp >= 10:
        return 0.35
    if years_exp >= 7:
        return 0.30
    return 0.25


def _max_salary(bio: dict, season: str, cap_levels: dict) -> Optional[int]:
    """§ 3.11: the player's maximum Year 1 salary for `season`, derived from
    the season's cap and the player's real NBA draft_year (same experience
    proxy as _one_year_min_cap_hit). Returns None if experience or the
    season's cap can't be determined (undrafted player, cap not configured
    yet) — callers should treat that as "can't check," not "no cap applies."
    """
    draft_year = bio.get("draft_year")
    cap = (cap_levels.get(season, {}) or {}).get("cap")
    if not draft_year or not cap:
        return None
    years_exp = _season_start(season) + 2000 - int(draft_year)
    return int(cap * _max_salary_pct(years_exp))


_BIRD_HOLD_PCT = {"EQVFA": 1.3, "Non-QVFA": 1.2}


def _rfa_eligibility(player: str, bios: dict, cur_season: str) -> tuple[bool, str]:
    """Whether `player` is a restricted free agent who can receive an offer sheet
    (§ 3.15), as ``(ok, reason)``.

    One rule, shared by `_apply_offer_sheet` and `_validate_offer_sheet`, which
    used to test different things: the apply path asked whether the player's
    *earliest* cap hold was an RFA no later than the upcoming season, while the
    validator asked about `cap_holds[current_season]`.

    The current-season test is the correct one, because an offer sheet is only
    worth extending if it can actually be executed — and `_apply_sign` refuses a
    cross-team signing unless the player carries a UFA/RFA hold **for the current
    season** (anything else means they're still under contract). An "earliest
    hold" reading accepts a player whose deal runs through this season and whose
    RFA year is still ahead of them; the offer would validate, sit on the books
    holding real cap room, and then fail at the decision with "already on ATL".
    """
    holds = ((bios.get(player) or {}).get("cap_holds") or {})
    if not holds:
        return False, "no cap hold on file"
    current = holds.get(cur_season)
    if current == "RFA":
        return True, ""
    if current:
        return False, f"cap hold for {cur_season} is {current}, not RFA"
    upcoming = sorted(y for y, t in holds.items() if t == "RFA" and y > cur_season)
    if upcoming:
        return False, (f"still under contract for {cur_season}; becomes an RFA in "
                       f"{upcoming[0]}, so an offer sheet has to wait for that league year")
    return False, f"no cap hold for {cur_season}"


def _offer_sheet_outcome_team(details: dict) -> Optional[str]:
    """Which team ends up with the player, from a resolved offer sheet's details.

    `teams` is stored ``[offering, retaining]``. A match leaves the player with
    the retaining (incumbent) team; an unmatched offer sends them to the offering
    team. Works on both the legacy combined entry and the split decision, since
    both carry `outcome` plus the same team fields.
    """
    outcome = details.get("outcome")
    if outcome not in ("matched", "not_matched"):
        return None
    teams = [t.upper() for t in (details.get("teams") or []) if t]
    offering = (details.get("offering_team") or (teams[0] if teams else "")).upper()
    retaining = (details.get("retaining_team") or "").upper() or \
        next((t for t in teams if t != offering), "")
    team = retaining if outcome == "matched" else offering
    return team if team in VALID_TEAMS else None


_BIRD_LEDGER_CACHE: dict = {"key": None, "index": {}}
_OPEN_OFFERS_CACHE: dict = {"key": None, "offers": []}


def _open_offer_sheets() -> list[dict]:
    """Offer sheets that have been extended but not yet resolved, newest first.

    Derived from the ledger rather than kept in its own file. A separate store
    would be a second source of truth that can drift out of step with the
    transactions it describes — and "is this offer still open?" is answerable
    exactly, by whether a matching `offer_sheet_decision` exists.

    Each entry is ``{id, date, deadline, player, offering_team, retaining_team,
    contract, hold, submitted_by, description}``. Cached against the ledger's
    (mtime, size) like `_player_acquisition_index`, since cap math consults this
    on every team-salary computation.

    A legacy combined `offer_sheet` (one that carries its own `outcome`) was
    applied at submission and is never open.
    """
    try:
        st = TRANSACTIONS_FILE.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        key = None
    if _OPEN_OFFERS_CACHE["key"] == key and key is not None:
        return _OPEN_OFFERS_CACHE["offers"]

    decided: set[str] = set()
    offers: list[dict] = []
    for txn in _load_transactions():
        d = txn.get("details") or {}
        if txn.get("type") == "offer_sheet_decision":
            if d.get("offer_id"):
                decided.add(d["offer_id"])
        elif txn.get("type") == "offer_sheet" and not d.get("outcome"):
            offering = (d.get("offering_team") or "").upper()
            offers.append({
                "id": txn.get("id"),
                "date": txn.get("date"),
                "deadline": _offer_deadline(txn.get("date")),
                "player": d.get("player"),
                "offering_team": offering,
                "retaining_team": (d.get("retaining_team") or "").upper(),
                "contract": d.get("contract") or {},
                "hold": _offer_hold_amount(d.get("contract") or {}),
                "signing_method": d.get("signing_method"),
                "bird_rights_type": d.get("bird_rights_type"),
                "submitted_by": txn.get("created_by"),
                "description": txn.get("description") or "",
            })

    open_offers = [o for o in offers if o["id"] not in decided]
    open_offers.sort(key=lambda o: o["date"] or "", reverse=True)
    _OPEN_OFFERS_CACHE.update({"key": key, "offers": open_offers})
    return open_offers


def _offer_deadline(date_str: Optional[str]) -> Optional[str]:
    """§ 3.15 gives the retaining team 48 hours to match. Recorded and displayed,
    but never acted on automatically — an offer past its deadline nags rather
    than resolving itself, because silently moving a real player on a timer is
    worse than a late decision."""
    if not date_str:
        return None
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _offer_hold_amount(contract: dict) -> int:
    """§ 3.15's "cap hold equal to the offer sheet value" — read as the offer's
    **Year 1** salary.

    A cap hold is a single-season charge against a single season's cap, so the
    total across all years can't be the figure meant: a 4yr/$200M offer would
    otherwise put a $200M hold on a $165M cap. Year 1 is also what every other
    hold in the system charges, and what the contract's Year 1 would cost if the
    offer converts.
    """
    salaries = (contract or {}).get("salaries") or {}
    years = sorted(y for y in salaries if _YEAR_RE_TXN.match(y))
    return _parse_dollar(salaries[years[0]]) if years else 0


_YEAR_RE_TXN = re.compile(r"^\d{2}-\d{2}$")


def _player_acquisition_index() -> dict[str, list[tuple]]:
    """Per-player acquisition timeline built from the transaction ledger, as
    ``{slug: [(date, kind, team), ...]}`` sorted by date, where `kind` is
    "sign" (starts a new tenure clock), "trade" (carries the clock to a new
    team) or "release" (breaks it).

    Cached against the ledger file's (mtime, size): the simulator revalidates
    on a 250ms debounce while the user types, and re-parsing a ~2MB
    transactions.json per keystroke is pure waste. Any write to the ledger
    changes mtime and invalidates this on the next read.
    """
    try:
        st = TRANSACTIONS_FILE.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        key = None
    if _BIRD_LEDGER_CACHE["key"] == key and key is not None:
        return _BIRD_LEDGER_CACHE["index"]

    index: dict[str, list[tuple]] = {}
    ledger = _load_transactions()
    # Offers indexed first so a decision can be resolved through its `offer_id`
    # rather than trusting the copies stored beside it.
    offers_by_id = {
        t["id"]: (t.get("details") or {})
        for t in ledger if t.get("type") == "offer_sheet" and t.get("id")
    }
    for txn in ledger:
        date = txn.get("date")
        details = txn.get("details") or {}
        if not date:
            continue
        ttype = txn.get("type")
        if ttype in ("sign", "sign_pick"):
            # Historical (backfilled) signings count here. They carry player,
            # team and date — everything a tenure clock needs — even though
            # _append_historical deliberately never wrote them into the bio.
            if details.get("player") and details.get("team"):
                index.setdefault(details["player"], []).append((date, "sign", details["team"].upper()))
        elif ttype == "trade":
            for leg in details.get("transfers") or []:
                to_team = (leg.get("to_team") or "").upper()
                for asset in leg.get("assets") or []:
                    if asset.get("type") == "player" and asset.get("slug") and to_team:
                        index.setdefault(asset["slug"], []).append((date, "trade", to_team))
        elif ttype == "release":
            if details.get("player"):
                index.setdefault(details["player"], []).append((date, "release", (details.get("team") or "").upper()))
        elif ttype in ("offer_sheet", "offer_sheet_decision"):
            # A RESOLVED offer sheet is a signing, and § 3.8 has to see it. It
            # didn't until 2026-08-08, which silently stranded every player who
            # changed teams this way: Mark Williams read as terminal_team=MIN
            # (the team he left) with no rights on record for TOR, the team he
            # actually signed with on an unmatched offer. Only `not_matched`
            # moves a player, which is why the bug hid behind two matched sheets
            # that resolved correctly for unrelated reasons.
            #
            # A split decision is resolved through its `offer_id` rather than the
            # player/team copies stored alongside it — the offer is the record of
            # who was involved, and reading a denormalized copy is how these
            # things silently go stale (see the roster CSV's old OVR column).
            # A legacy combined entry carries `outcome` on itself; a bare offer
            # has none and is still pending, so it conveys nobody.
            src = details
            if ttype == "offer_sheet_decision" and details.get("offer_id"):
                src = {**(offers_by_id.get(details["offer_id"]) or {}),
                       "outcome": details.get("outcome")}
            player = src.get("player")
            if src.get("outcome") and player:
                signing_team = _offer_sheet_outcome_team(src)
                if signing_team:
                    # Matched: the incumbent re-signs their own free agent, which
                    # continues the clock (_bird_tenure treats a re-sign with the
                    # same terminal team as continuous). Not matched: a signing
                    # with a different team, which resets it. Both are "sign".
                    index.setdefault(player, []).append((date, "sign", signing_team))
        # `extension` is deliberately NOT an acquisition event. An extension
        # adds years to a live contract; the player never reaches free agency,
        # so § 3.8 tenure keeps accruing uninterrupted. Committee-confirmed
        # 2026-08-07. Adding it here "for completeness" would reset the Bird
        # clock on every extended player and silently downgrade their tier —
        # see docs/extensions.md. Same reasoning for `option`, `guarantee`
        # and `convert_twoway`: none of them break continuous service.

    for events in index.values():
        events.sort(key=lambda e: e[0])
    _BIRD_LEDGER_CACHE.update({"key": key, "index": index})
    return index


def _bird_tenure(slug: str, team: str, season: str, bio: dict) -> dict:
    """§ 3.8 continuous-service tenure with `team` as of `season`, derived from
    the transaction ledger.

    Returns ``{tier, seasons, basis, evidence, terminal_team}`` where `basis`
    is "ledger" (an acquisition event was found), "draft" (no event, but the
    bio names a drafting team and year) or "unknown" (neither).

    Rights accrue from the most recent **free-agent signing** and carry
    through trades — § 6.2 recognises holding "Bird Rights with the team via
    trade", matching the real CBA — so a trade moves the clock to the new
    team rather than restarting it. A release breaks the chain; the next
    signing starts a fresh one.

    **"unknown" is not Non-QVFA.** A player with no recorded acquisition is
    usually a long-tenured legacy player, i.e. the most likely QVFA of all;
    defaulting them to Non-Bird would invert the truth. Callers must treat
    unknown as "can't say", never as a low tier.
    """
    team = team.upper()
    events = list(_player_acquisition_index().get(slug, []))

    # The draft is an acquisition too, and for a player who has never reached
    # free agency it's the *only* one — so it seeds the timeline rather than
    # acting as a separate fallback. Merging it in (instead of consulting it
    # only when no events exist) is what lets a drafted-then-traded player
    # resolve to the team that actually holds them now.
    d_team, d_year = bio.get("draft_team"), bio.get("draft_year")
    if d_team and d_year:
        events.append((f"{int(d_year)}-07-01", "draft", str(d_team).upper()))
    events.sort(key=lambda e: e[0])

    start_season: Optional[str] = None
    terminal: Optional[str] = None
    basis = "unknown"
    evidence = ""
    for date, kind, ev_team in events:
        if kind in ("sign", "draft"):
            if terminal == ev_team and start_season is not None:
                # Re-signing with your own team CONTINUES the clock. § 3.8 is
                # explicitly about "a team re-signing their own free agents",
                # and rights accrue while the player "remains with the same
                # team" — the disqualifier is "signing as a free agent" with
                # *someone else*. Treating every signing as a reset would mean
                # no player could ever use Bird rights twice, which is the
                # whole mechanism.
                evidence += f", re-signed with {ev_team} on {date}"
            else:
                start_season, terminal = _season_start_of(date), ev_team
                basis = "ledger" if kind == "sign" else "draft"
                evidence = (f"signed with {ev_team} on {date}" if kind == "sign"
                            else f"drafted by {ev_team} in {date[:4]}")
        elif kind == "trade":
            if terminal is not None:
                terminal = ev_team      # clock start unchanged — rights travel (§ 6.2)
                evidence += f", traded to {ev_team} on {date}"
            else:
                # No origin event on file (the player's first signing predates
                # the ledger). The acquiring team still inherits whatever the
                # player had accrued, so time since the trade is a *lower
                # bound* on tenure, never the whole of it. Tracked as its own
                # basis because a floor can only ever support a warning —
                # erroring on it would fail a team for tenure we simply can't
                # see. See _check_bird_rights_declaration.
                start_season, terminal, basis = _season_start_of(date), ev_team, "trade_floor"
                evidence = f"acquired by trade on {date}; no earlier record on file"
        elif kind == "release":
            start_season, terminal = None, None
            evidence = f"released on {date}"

    if start_season is None:
        return {"tier": None, "seasons": None, "basis": "unknown",
                "evidence": evidence or "no signing, trade or draft record on file",
                "terminal_team": None}

    if terminal != team:
        return {"tier": None, "seasons": None, "basis": basis,
                "evidence": evidence or "no acquisition by this team on record",
                "terminal_team": terminal}

    seasons = max(0, _season_start(season) - _season_start(start_season))
    tier = "QVFA" if seasons >= 3 else "EQVFA" if seasons >= 2 else "Non-QVFA"
    return {"tier": tier, "seasons": seasons, "basis": basis,
            "evidence": evidence, "terminal_team": terminal}


def _season_start_of(date: str) -> str:
    """The league-year season string a calendar date falls in. The league year
    turns over on July 1, so anything from July on belongs to YY-(YY+1)."""
    y, m = int(date[:4]), int(date[5:7])
    s = y if m >= 7 else y - 1
    return f"{s % 100:02d}-{(s + 1) % 100:02d}"


_BIRD_TIER_RANK = {"Non-QVFA": 0, "EQVFA": 1, "QVFA": 2}


def _check_bird_rights_declaration(player: str, team: str, declared: Optional[str],
                                   season: str, bios: dict,
                                   method: Optional[str] = None) -> Optional[CheckResult]:
    """§ 3.8: the declared Bird tier must not claim more tenure than the
    ledger supports, and a Bird-funded signing must be of the team's *own*
    free agent.

    Only **over-declaration** is an error, and that asymmetry is deliberate:
    a gap in the backfilled ledger (a signing that was never entered) can only
    make derived tenure look *longer*, never shorter — the most recent signing
    we can see is an older one. So "declared above derived" can't be produced
    by missing data, which makes it safe to block on even though the ledger is
    known to be incomplete. Under-declaring is a team giving up rights it may
    hold; that's their business, not a violation.

    `method` is consulted so that signing_method="bird_rights" is covered even
    when no tier is declared. That combination previously bypassed validation
    entirely: `_check_signing_method_funding` returns early for any method
    outside the cap-space/MLE family, so Bird was an unchecked path to sign
    over the cap while also unlocking § 3.13's 8% raise ceiling.
    """
    bird_funded = (method == "bird_rights")
    if not declared and not bird_funded:
        return None
    check_name = "bird_rights_tenure"
    t = _bird_tenure(player, team, season, bios.get(player, {}) or {})

    # Bird Rights re-sign a team's OWN free agent (§ 3.8). If the ledger
    # positively places the player elsewhere, that's not a Bird signing at
    # all — distinct from merely having no record, handled below.
    if bird_funded and t["tier"] is None and t["terminal_team"] and t["terminal_team"] != team.upper():
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(
                f"{team} cannot use Bird Rights for this player — Bird Rights re-sign a team's own "
                f"free agent, and the ledger has them with {t['terminal_team']} ({t['evidence']}). "
                f"Use cap space, an exception, or the minimum (§ 3.8)."
            ),
        )

    subject = declared or "Bird Rights funding"
    if t["tier"] is None:
        return CheckResult(
            check=check_name, passed=False, level="warning",
            message=(
                f"{subject} is self-declared and couldn't be verified for this player with "
                f"{team} ({t['evidence']}). § 3.8 tenure is unconfirmed — check it by hand."
            ),
        )
    if declared and _BIRD_TIER_RANK[declared] > _BIRD_TIER_RANK[t["tier"]]:
        # A trade-floor tenure is a lower bound, so exceeding it isn't proof of
        # anything — the unseen accrual it inherited could well justify the
        # declaration. Report the floor and let a human judge.
        if t["basis"] == "trade_floor":
            return CheckResult(
                check=check_name, passed=False, level="warning",
                message=(
                    f"{declared} can't be confirmed: {team} has at least {t['seasons']} season(s) "
                    f"with this player ({t['evidence']}), which alone supports only {t['tier']}. "
                    f"Tenure inherited through the trade may still justify {declared} — verify by hand."
                ),
            )
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(
                f"Declared {declared}, but {team} has only {t['seasons']} prior season(s) of "
                f"continuous service with this player ({t['evidence']}) — that is {t['tier']} "
                f"under § 3.8. Full Bird (QVFA) needs 3+, Early Bird (EQVFA) needs 2."
            ),
        )
    return CheckResult(
        check=check_name, passed=True,
        message=(
            f"{subject} is consistent with {t['seasons']} prior season(s) of continuous service "
            f"with {team} ({t['evidence']})."
        ),
    )


def _derive_bird_tier(bio: dict, team: str, hold_season: str, slug: Optional[str] = None) -> str:
    """§ 3.8 Bird tier, best-effort, as a default for when the submitter
    doesn't pass an explicit bird_rights_type/bird_tier. An explicit value
    always wins over this.

    Prefers the transaction ledger (`_bird_tenure`) when `slug` is known,
    since that sees backfilled history and trade continuity. Falls back to
    scanning the bio's own `contracts` entries — which only the apply path
    ever appends to, and which is empty for ~95% of players — so this is
    strictly more informed than the old bio-only scan, never less.
    """
    if slug:
        t = _bird_tenure(slug, team, hold_season, bio)
        if t["tier"]:
            return t["tier"]

    season_team: dict[str, str] = {}
    for c in bio.get("contracts") or []:
        c_team = c.get("team")
        for yr in (c.get("salaries") or {}).keys():
            season_team[yr] = c_team
    tenure = 0
    yr = hold_season
    while True:
        yr = _season_shift(yr, -1)
        if season_team.get(yr) == team:
            tenure += 1
        else:
            break
    if tenure >= 3:
        return "QVFA"
    if tenure >= 2:
        return "EQVFA"
    return "Non-QVFA"


def _compute_fa_hold_amount(
    prev_salary: str,
    bird_tier: str,
    hold_season: str,
    cap_levels: dict,
    eaps_assumption: Optional[str] = None,
    min_amt: Optional[int] = None,
    max_amt: Optional[int] = None,
) -> tuple[int, Optional[str]]:
    """§ 3.10 veteran free-agent hold: `bird_tier`'s percentage of
    `prev_salary` (the last real salary before the hold season), clamped to
    [min_amt, max_amt]. Returns (amount, note) — note is set only when the
    figure rests on an eaps_assumption rather than a real cap number, so
    callers can flag the result as a placeholder pending real EAPS data.

    Doesn't implement the rookie-scale-final-year (250%/300%) or
    coming-off-a-minimum-contract carve-outs in § 3.10 — both still need a
    manually-entered cap_hold_amount.
    """
    prev = _parse_dollar(prev_salary)
    note = None
    if bird_tier == "QVFA":
        eaps = (cap_levels.get(hold_season, {}) or {}).get("eaps") or None
        if eaps:
            pct = 1.5 if prev > eaps else 1.9
        else:
            if eaps_assumption not in ("above", "below"):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Full Bird (QVFA) hold for {hold_season} needs to know whether the "
                        f"previous salary (${prev:,}) is above or below that season's EAPS to "
                        f"pick 150% vs 190% (§ 3.10), and {hold_season} has no EAPS on file yet. "
                        f'Pass eaps_assumption: "above" or "below" to compute a placeholder.'
                    ),
                )
            pct = 1.5 if eaps_assumption == "above" else 1.9
            note = (f"placeholder — {hold_season} EAPS not yet set; assumed previous salary is "
                    f"{eaps_assumption} EAPS ({int(pct * 100)}% of ${prev:,})")
    else:
        pct = _BIRD_HOLD_PCT.get(bird_tier, 1.2)
    amount = round(prev * pct)
    if max_amt:
        amount = min(amount, max_amt)
    if min_amt:
        amount = max(amount, min_amt)
    return amount, note


def _autofill_fa_hold_amounts(
    bio: dict,
    team: str,
    cap_holds: dict,
    explicit_salaries: dict,
    cap_levels: dict,
    bird_rights_type: Optional[str] = None,
    eaps_assumption: Optional[str] = None,
    slug: Optional[str] = None,
) -> dict:
    """Fills in a dollar figure in bio['salaries'] for any UFA/RFA season in
    `cap_holds` that `explicit_salaries` (the just-submitted contract's own
    salaries dict) didn't already price — the trailing free-agent hold a
    contract rolls into once it ends (§ 3.10). Mutates bio['salaries'] in
    place. Returns {season: note} for any season whose figure rests on an
    eaps_assumption placeholder, for the caller to mirror into
    bio['cap_hold_notes'].
    """
    notes = {}
    salaries = bio.get("salaries") or {}
    for season, hold_type in (cap_holds or {}).items():
        if hold_type not in ("UFA", "RFA") or season in explicit_salaries:
            continue
        prev_salary = salaries.get(_season_shift(season, -1))
        if not prev_salary:
            continue  # nothing to base a hold on — e.g. incomplete backfilled history
        tier = bird_rights_type or _derive_bird_tier(bio, team, season, slug)
        min_amt = _rookie_min_salary(season, cap_levels) or None
        max_amt = _max_salary(bio, season, cap_levels)
        amount, note = _compute_fa_hold_amount(
            prev_salary, tier, season, cap_levels, eaps_assumption, min_amt, max_amt,
        )
        salaries[season] = f"${amount:,}"
        if note:
            notes[season] = note
    bio["salaries"] = salaries
    return notes


def _preview_fa_hold(
    bio: dict,
    team: str,
    contract,
    cap_levels: dict,
    bird_rights_type: Optional[str] = None,
    eaps_assumption: Optional[str] = None,
    slug: Optional[str] = None,
) -> Optional[dict]:
    """Price the § 3.10 trailing free-agent hold a *proposed* contract rolls
    into, without applying anything — so a team building a deal can watch the
    figure move instead of being told the office will work it out later.

    Runs `_autofill_fa_hold_amounts` against a throwaway copy of the bio with
    the proposed salaries merged in, so the previewed number comes from the
    exact code path that will write it at signing time rather than a parallel
    reimplementation. Returns None when the contract carries no trailing hold.

    Never raises: `_compute_fa_hold_amount` 422s on a Full Bird hold whose
    season has no EAPS on file and no assumption supplied, which is a normal
    intermediate state in a form that revalidates on every keystroke. That
    becomes `needs_eaps`, not a failed request.
    """
    explicit = contract.salaries or {}
    holds = {s: t for s, t in (contract.cap_holds or {}).items()
             if t in ("UFA", "RFA") and s not in explicit}
    if not holds:
        return None
    season = sorted(holds)[0]
    tier = bird_rights_type or _derive_bird_tier(bio, team, season, slug)
    out = {"season": season, "type": holds[season], "bird_tier": tier,
           "amount": None, "needs_eaps": False, "note": None}

    probe = {**bio, "salaries": {**(bio.get("salaries") or {}), **explicit}}
    out["prior_salary"] = _parse_dollar(probe["salaries"].get(_season_shift(season, -1), "")) or None
    if not out["prior_salary"]:
        out["note"] = (f"no {_season_shift(season, -1)} salary on file to base the hold on "
                       "— the office will price it by hand")
        return out
    try:
        notes = _autofill_fa_hold_amounts(
            probe, team, {season: holds[season]}, explicit, cap_levels,
            bird_rights_type=bird_rights_type, eaps_assumption=eaps_assumption, slug=slug,
        )
    except HTTPException:
        out["needs_eaps"] = True
        return out
    out["amount"] = _parse_dollar(probe["salaries"].get(season, "")) or None
    out["note"] = notes.get(season)
    return out


def _one_year_min_cap_hit(bio: dict, season: str, cap_levels: dict) -> Optional[int]:
    """§ 3.12: a 1-year minimum contract's cap hit is capped at the 2-year
    veteran minimum, regardless of the player's actual NBA experience tier —
    the league reimburses the difference, mirroring the NBA's veteran-minimum
    hardship exception. `draft_year` on the bio is the player's real NBA
    draft year (not the NBN fantasy draft), so it's a reasonable proxy for
    years of NBA experience. Returns None if experience or the season's scale
    can't be determined (e.g. undrafted player, scale not configured yet) —
    callers should treat that as "can't check," not "no cap applies."
    """
    draft_year = bio.get("draft_year")
    scale = (cap_levels.get(season, {}) or {}).get("min_salary_scale") or {}
    if not draft_year or not scale:
        return None
    years_exp = _season_start(season) + 2000 - int(draft_year)
    tier_amt = scale.get(_min_salary_scale_tier(years_exp), 0)
    two_yr_amt = scale.get("2", 0)
    if tier_amt <= 0:
        return None
    # Only 2+ year tiers get capped down — the 0/1-yr tiers already sit at or
    # below the 2-yr number, so there's nothing to reimburse for them.
    if _min_salary_scale_tier(years_exp) in ("0", "1") or not two_yr_amt:
        return tier_amt
    return min(tier_amt, two_yr_amt)


def _empty_roster_charge(standard_count_after: int, season: str, cap_levels: dict) -> tuple[int, int]:
    """Returns (deficiency, charge) for a team projected below the ROSTER_MIN
    (14) standard-roster minimum, for TRADE-LEGALITY purposes only — § 2.1a.
    This is a wider floor than ROSTER_CHARGE_MIN (12), which is where a real,
    persisted Empty Roster Charge actually posts to a team's roster/guaranteed
    salary (see team.js's computeEmptyRosterCharge for that one — it isn't
    computed here, since it isn't specific to trades). This function's charge
    applies immediately and year-round for the hard-cap comparison (not gated
    on the § 2.1 one-week grace period, which separately governs when activity
    strikes escalate for a team that stays non-compliant): a team can't use a
    trade to duck under a hard cap by shedding headcount, since the league
    treats the empty slot(s) up to 14 as costing at least that season's
    rookie minimum, whether or not the team is actually below 12 yet."""
    deficiency = max(0, ROSTER_MIN - standard_count_after)
    if deficiency == 0:
        return 0, 0
    return deficiency, deficiency * _rookie_min_salary(season, cap_levels)


def _pending_offer_hold(team: str, season: str) -> int:
    """§ 3.15: "The offering team has a cap hold equal to the offer sheet value
    placed on their books until the matching period concludes."

    Sum of open offer-sheet holds this team is carrying. Zero unless the team has
    an offer out, so it costs nothing for the other 29 — but it is what stops a
    team floating several offer sheets it could never collectively fund.

    Counted against the plain Cap only, exactly like the UFA/RFA holds it sits
    beside: § 3.10 excludes cap holds from Hard Cap and apron comparisons because
    a hold is a placeholder for a contract that doesn't exist yet, and a pending
    offer is the same species. It becomes real salary in both figures the moment
    the offer converts.
    """
    if season != _current_league_year():
        # The hold exists now, against the current league year's books. Applying
        # it to a future season would charge a team twice for one offer.
        return 0
    return sum(o["hold"] for o in _open_offer_sheets() if o["offering_team"] == team.upper())


def _compute_team_salary(team: str, bios: dict, season: str) -> int:
    """Sum all active salary + dead cap for a team in a given season, plus any
    § 3.15 hold from an offer sheet this team currently has outstanding."""
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
    return total + _pending_offer_hold(team, season)


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


def load_room_zone_baseline() -> dict:
    return _load_json(ROOM_ZONE_BASELINE_FILE, {})


def save_room_zone_baseline(data: dict):
    ROOM_ZONE_BASELINE_FILE.write_text(json.dumps(data, indent=2))


def snapshot_room_zone_baseline(season: str, within_days: int = 7) -> list[str]:
    """§ 3.2: record every team's real Team Salary at the moment `season`'s
    league year actually begins, so Room Exception zone eligibility never has
    to guess or reconstruct after the fact — it just reads what was true on
    the day, the same way `_check_exception_absorption`'s `mle_type` lock
    already avoids re-testing once a team has actually used an exception.

    Idempotent per team/season: a team already recorded for `season` is left
    untouched, so calling this repeatedly (every pass of the scheduler loop
    in `picks_scheduler.py` — on every service start and at every rollover
    boundary) is free once it's done its job.

    Deliberately narrow: only snapshots within `within_days` of the season's
    actual rollover date. That bounds the damage from a service outage
    spanning the exact rollover moment (still caught within a few days of
    the real date) without ever back-dating a season that's genuinely been
    running for weeks — doing that would silently reintroduce a live-salary
    figure mislabeled as "July 1," exactly the bug this replaces. A season
    outside that window with no snapshot (26-27, as of this writing — it had
    already been running for five weeks when this was built) falls back to
    `_team_salary_as_of_league_year_start`'s after-the-fact reconstruction,
    kept around specifically as a bridge for seasons this snapshot missed.
    """
    rollover = _season_start_date(season, _league_rollovers())
    age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - rollover).days
    if age_days < 0 or age_days > within_days:
        return []
    bios = load_player_bios()
    data = load_room_zone_baseline()
    season_data = data.setdefault(season, {})
    snapshotted = []
    for team in sorted(VALID_TEAMS):
        if team in season_data:
            continue
        season_data[team] = {
            "with_holds": _compute_team_salary(team, bios, season),
            "ex_holds": _compute_team_salary_ex_holds(team, bios, season),
            "snapshotted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        snapshotted.append(team)
    if snapshotted:
        save_room_zone_baseline(data)
    return snapshotted


_ZONE_RECON_HANDLED_TYPES = {"trade", "sign", "sign_pick"}
_ZONE_RECON_UNHANDLED_TYPES = {"release", "option", "void_player", "renounce", "rescind_renounce", "convert_twoway", "offer_sheet", "offer_sheet_decision"}


def _team_salary_as_of_league_year_start(
    team: str, season: str, bios: dict
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Bridge for a season whose rollover `snapshot_room_zone_baseline` never
    caught (it didn't exist yet, or the service was down past its freshness
    window) — reconstructs Team Salary (with-holds, ex-holds) as it stood at
    the start of `season`'s league year by starting from the CURRENT computed
    totals and reversing every transaction touching `team` dated after that
    rollover (honoring any BOD override in league-state.json, not just the
    plain July 1 default). `_room_exception_july1_eligible` only calls this
    when no real snapshot is on file — prefer that snapshot wherever one
    exists; this is strictly a fallback, not the primary mechanism.

    Only reverses `trade`, `sign`, and `sign_pick` — the types whose salary
    effect is fully recoverable from the log entry itself. A trade doesn't
    change a player's contract figure, so that player's *current* season
    salary is safe to use for it, unless the same player shows up in more than
    one salary-moving transaction in the reversal window — then a plain
    current-bio lookup can't tell which figure applied at which moment, and
    this abstains rather than guess. It also abstains outright if the team's
    post-rollover history includes a `release`, `option` decision,
    `void_player`, `renounce`, `convert_twoway`, or `offer_sheet` — none of
    those carry enough in the transaction log to reverse confidently (a
    release's real dead-cap split, an option's pre-decision state, a
    renounced hold's original amount aren't recoverable from the entry alone).

    Returns `(with_holds, ex_holds, None)` on success, or `(None, None, reason)`
    if it had to abstain — callers should fall back to their own prior
    behavior in that case, not treat an abstention as a real answer.
    """
    rollover = _season_start_date(season, _league_rollovers())
    rollover_str = rollover.strftime("%Y-%m-%d")

    with_holds = _compute_team_salary(team, bios, season)
    ex_holds = _compute_team_salary_ex_holds(team, bios, season)

    touched: dict[str, int] = {}
    for t in _load_transactions():
        if t.get("date", "") <= rollover_str:
            continue
        ttype = t.get("type")
        d = t.get("details", {}) or {}

        if ttype == "trade":
            if team not in (d.get("teams") or []):
                continue
            for xfer in d.get("transfers", []):
                if xfer.get("from_team") != team and xfer.get("to_team") != team:
                    continue
                sign = -1 if xfer.get("from_team") == team else 1
                for asset in xfer.get("assets", []):
                    if asset.get("type") != "player":
                        continue
                    slug = asset.get("slug")
                    touched[slug] = touched.get(slug, 0) + 1
                    sal = _parse_dollar((bios.get(slug, {}).get("salaries") or {}).get(season, ""))
                    with_holds -= sign * sal
                    ex_holds -= sign * sal
            continue

        if ttype in ("sign", "sign_pick"):
            if d.get("team") != team:
                continue
            slug = d.get("player")
            touched[slug] = touched.get(slug, 0) + 1
            new_sal = _parse_dollar((d.get("contract", {}).get("salaries") or {}).get(season, ""))
            with_holds -= new_sal
            ex_holds -= new_sal
            continue

        if d.get("team") == team or team in (d.get("teams") or []) or d.get("offering_team") == team:
            if ttype in _ZONE_RECON_UNHANDLED_TYPES:
                return None, None, (
                    f"{team}: a {ttype!r} transaction since {rollover_str} ({t.get('id')}) "
                    "can't be reversed confidently"
                )

    dupes = sorted(s for s, c in touched.items() if c > 1)
    if dupes:
        return None, None, (
            f"{team}: {', '.join(dupes)} moved more than once since {rollover_str} — "
            "can't reconstruct a reliable July-1 salary"
        )

    return with_holds, ex_holds, None


def _room_exception_july1_eligible(
    team: str, season: str, bios: dict, cap_levels: dict
) -> Optional[bool]:
    """§ 3.2: was `team` more than the full NTMLE amount below the Cap as of
    `season`'s league-year start — the real July-1 test the Room Exception
    zone assignment is supposed to run. Prefers the real recorded snapshot
    (`snapshot_room_zone_baseline`) when one exists; only falls back to
    after-the-fact reconstruction for a season the snapshot never caught.
    Returns None (couldn't determine) rather than guessing when neither
    source has an answer — callers should fall back to their prior behavior.
    """
    cl = cap_levels.get(season, {})
    cap = cl.get("cap")
    if cap is None:
        return None
    room_ceiling = cap - (cl.get("ntmle_amount", 0) or 0)

    baseline = load_room_zone_baseline().get(season, {}).get(team)
    if baseline is not None:
        return baseline["with_holds"] < room_ceiling

    with_holds, _ex_holds, warning = _team_salary_as_of_league_year_start(team, season, bios)
    if warning:
        logger.info("Room Exception July-1 reconstruction abstained: %s", warning)
        return None
    return with_holds < room_ceiling


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


def _tpe_bankable(
    outgoing: int,
    incoming: int,
    team_salary_after: int,
    cap: Optional[int],
    used_exception: bool,
) -> Optional[int]:
    """§ 4.1a: the Trade Exception this team banks out of a trade, or None.

    Three independent gates, all of which must pass:
      * it sent out more salary than it took back (there is a difference);
      * it did not absorb incoming salary through an exception or another TPE
        in this same trade (can't absorb via one and bank another);
      * it is over the Salary Cap after the trade — a team under the Cap
        absorbs the difference into cap room instead (§ 4.2), and handing it an
        exception on top would let the same space be spent twice.

    `team_salary_after` is the full post-trade team salary *including* cap
    holds, matching how cap-room absorption measures room."""
    if cap is None or used_exception:
        return None
    if outgoing <= incoming:
        return None
    if team_salary_after <= cap:
        return None
    return outgoing - incoming


def _buyout_salary_above_ntmle(
    slug: str,
    txn_date: str,
    season: str,
    cap_levels: dict,
    prior_salary: str = "",
) -> Optional[int]:
    """§ 1.4 Row D / § 1.5.2: did this player reach free agency by having a
    contract terminated in the same season as this signing, carrying a salary
    above the full NTMLE amount? Returns that salary when it qualifies, else
    None.

    "Same Regular Season" is approximated by the league-year season clock
    (`_season_for_date`) — the only season boundary this API models, and the
    same one every other hard-cap trigger keys on. A release and a signing
    landing in the same league year therefore qualify even if one of them is
    technically in the offseason. Erring toward the league year is the safe
    direction: the rule exists to stop teams from scooping up a bought-out
    high-salary player, and that concern doesn't evaporate on the last day of
    the regular season.

    `prior_salary` is the player's release-season salary as it stood *before*
    the new contract overwrote it — needed for releases logged before
    `terminated_salary` was recorded on the transaction (added alongside this
    check), where the stored record has no salary figure of its own."""
    ntmle = (cap_levels.get(season) or {}).get("ntmle_amount")
    if not ntmle:
        return None
    for txn in _load_transactions():
        if txn.get("type") not in ("release", "void_player"):
            continue
        d = txn.get("details") or {}
        if d.get("player") != slug:
            continue
        rel_date = txn.get("date") or ""
        if not rel_date or rel_date > txn_date:
            continue
        if _season_for_date(rel_date) != season:
            continue
        salary = _parse_dollar(d.get("terminated_salary") or "") or _parse_dollar(prior_salary or "")
        if salary > ntmle:
            return salary
    return None


def _check_bae_eligibility(
    team: str,
    team_salary_ex_holds_before: int,
    cl: dict,
    team_state: Optional[dict],
    season: str,
) -> Optional[CheckResult]:
    """Shared BAE eligibility gate (§ 3.5), used by both the free-agent
    signing path and the trade-absorption path: below the First Apron, not
    already used this season, not used in the immediately prior season
    (`_bae_available`), and not already used alongside cap space or the Room
    Exception this same season. Returns None when eligible."""
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
    return None


def _resolve_mle_bucket(method: Optional[str], ts: dict) -> Optional[tuple[str, str, str]]:
    """Which exception bucket an exception-funded `signing_method` actually
    draws on, as ``(resolved_method, cap_levels_amount_key, display_label)``.

    A generic "mle" resolves against whatever bucket the team is already locked
    into this season (`mle_type`), defaulting to the NTMLE. Returns None for
    methods that aren't a running dollar balance (cap space, Bird Rights,
    minimum, BAE, sign-and-trade) — those have dedicated checks.

    Shared by `_check_signing_method_funding` and the simulator's fact sheet so
    the balance shown is the balance the validator charged against.
    """
    resolved = method
    if resolved == "mle":
        resolved = ts.get("mle_type") or "ntmle"
    if resolved == "room":          # mle_type stores the short form
        resolved = "room_exception"
    meta = {
        "ntmle":          ("ntmle_amount", "Non-Taxpayer MLE"),
        "tmle":           ("tmle_amount",  "Taxpayer MLE"),
        "room_exception": ("room_amount",  "Room Exception"),
    }.get(resolved)
    return (resolved, meta[0], meta[1]) if meta else None


def _check_signing_method_funding(
    team: str,
    method: Optional[str],
    new_sal: int,
    team_salary_before: int,
    team_salary_ex_holds_before: int,
    season: str,
    cap_levels: dict,
    team_state: Optional[dict],
    unrenounced_holds: int = 0,
    bios: Optional[dict] = None,
) -> Optional[CheckResult]:
    """§ 3.1–§ 3.6: the declared `signing_method` must actually be available.

    Before this gate, `signing_method` was pure self-declaration — nothing
    compared it against real cap room or the remaining exception balance, so a
    `cap_space` signing by a team tens of millions over the cap passed clean.
    That mattered beyond the one bad record: `_apply_sign` stamps
    `mle_type = "room"` off any cap-space signing, so one wrong declaration
    silently downgraded the team's exception for every later signing that
    season (OKC declared Embiid's 2026-07-20 re-signing as cap space while
    ~$220M over the cap; Hachimura was then charged $11,000,000 against the
    $9,366,000 Room Exception instead of the $15,044,000 NTMLE).

    `cap_space` is measured against the **with-holds** Team Salary — an
    unrenounced hold occupies room until cleared (§ 3.10) — while the § 3.3
    apron test uses the ex-holds figure, matching `_check_bae_eligibility`.
    `bird_rights`, `minimum`, `bae` and `sign_and_trade` are out of scope here:
    Bird Rights carry no amount ceiling of their own, and the other three have
    dedicated checks.

    `bios` (optional) gates a further § 3.2 check: the Room Exception is
    unavailable unless Team Salary was more than the full NTMLE amount below
    the Cap as of the league year's start (unlike NTMLE/TMLE's § 1.5/§ 1.6
    apron bars, which stay live all season and are checked below regardless
    of `bios` — see `_check_exception_absorption` for why those two are
    deliberately not locked the same way). Only the real validation path
    (`_validate_sign`) passes `bios`; callers that construct scalars directly
    (this module's own test suite) skip that gate entirely, exactly as before
    it existed.
    """
    if method not in ("cap_space", "mle", "ntmle", "tmle", "room_exception"):
        return None

    cl = cap_levels.get(season, {})
    check_name = f"signing_method_{team.lower()}"
    ts = get_season_state(team_state or {}, team, season)

    if method == "cap_space":
        cap = cl.get("cap")
        if cap is None:
            return None
        room = cap - team_salary_before
        if new_sal > room:
            message = (
                f"{team} cannot sign for ${new_sal:,} with cap space — Team Salary of "
                f"${team_salary_before:,} (cap holds included) leaves "
                f"{'$' + format(room, ',') if room > 0 else 'no'} room under the "
                f"${cap:,} Salary Cap. Use Bird Rights or an exception (§ 3.1)."
            )
            # Don't just say "no room" when unentered renounces are the likely
            # cause — a hold occupies room only until it's cleared (§ 3.10), and
            # clearing enough of these would fund the signing outright.
            if unrenounced_holds > 0:
                would_clear = " — enough on its own to fund this signing" if \
                    room + unrenounced_holds >= new_sal else ""
                message += (
                    f" Note: {team} still carries ${unrenounced_holds:,} in unrenounced "
                    f"free-agent holds{would_clear}. If those free agents have already been "
                    f"renounced, enter the renounce transactions first (§ 3.10)."
                )
            return CheckResult(check=check_name, passed=False, level="error", message=message)
        return None

    # Exception-funded. A generic "mle" resolves against whatever bucket the
    # team is already locked into this season, exactly as _apply_sign does —
    # so validation and application can't disagree about which one was used.
    bucket = _resolve_mle_bucket(method, ts)
    if bucket is None:
        return None
    resolved, amount_key, label = bucket

    # § 3.3: the NTMLE is unavailable at or above the First Apron.
    if resolved == "ntmle":
        apron1 = cl.get("apron1")
        if apron1 is not None and team_salary_ex_holds_before >= apron1:
            return CheckResult(
                check=check_name, passed=False, level="error",
                message=(
                    f"{team} may not use the Non-Taxpayer MLE — Team Salary "
                    f"(${team_salary_ex_holds_before:,}) is at/above the First Apron "
                    f"(${apron1:,}). The Taxpayer MLE applies instead (§ 3.3)."
                ),
            )

    # § 3.2: the Room Exception zone is locked once assigned, but a team that
    # hasn't used any exception yet this season (`mle_type` unset) still has
    # to actually clear the July-1 line before it counts as assigned.
    if resolved == "room_exception" and ts.get("mle_type") not in ("room", "room_exception") and bios is not None:
        eligible = _room_exception_july1_eligible(team, season, bios, cap_levels)
        if eligible is False:
            room_ceiling = (cl.get("cap") or 0) - (cl.get("ntmle_amount") or 0)
            return CheckResult(
                check=check_name, passed=False, level="error",
                message=(
                    f"{team} may not use the Room Exception — Team Salary was not more than "
                    f"the full NTMLE amount below the Cap (${room_ceiling:,}) as of the start "
                    f"of the league year (§ 3.2)."
                ),
            )

    amount = cl.get(amount_key)
    if amount is None:
        return None
    remaining = amount - (ts.get("mle_used") or 0)
    if new_sal > remaining:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(
                f"{team} cannot sign for ${new_sal:,} using the {label} — only "
                f"${remaining:,} of the ${amount:,} {label} remains this season (§ 3.2)."
            ),
        )
    return None


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
    share `_check_exception_absorption`'s amount-key/`mle_used` bookkeeping."""
    check_name = f"salary_matching_{team.lower()}"
    if (r := _check_bae_eligibility(team, team_salary_ex_holds_before, cl, team_state, season)):
        return r

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


def _exception_absorption_split(
    details,
    team: str,
    incoming_total: int,
    in_players: dict,
    bios: dict,
    season: str,
) -> tuple[int, int, Optional[str]]:
    """How a team's incoming salary divides between its exception and ordinary
    salary matching.

    § 4.2a is per-player — "a team uses exactly one of the four for a given
    incoming player" — so a trade can legitimately match some incoming players
    against outgoing salary while funding another out of an exception. When
    `exception_players[team]` names slugs, only those are absorbed and the rest
    still has to match. When it doesn't, the whole incoming total is treated as
    absorbed, which is the original all-or-nothing behavior.

    Returns (absorbed, matched, error). `error` is a message when the named
    slugs don't correspond to players this team is actually receiving — the
    caller turns that into a failed check rather than silently mis-splitting.

    Shared by the validator and _apply_trade so the amount that gets checked is
    always the amount that gets billed to the exception's balance.
    """
    slugs = [s for s in (getattr(details, "exception_players", None) or {}).get(team, []) if s]
    if not slugs:
        return incoming_total, 0, None

    received = in_players.get(team, [])
    unknown = [s for s in slugs if s not in received]
    if unknown:
        return 0, 0, (
            f"{team}: exception_players names {', '.join(sorted(unknown))}, "
            f"which {'is' if len(unknown) == 1 else 'are'} not incoming to {team} in this trade."
        )

    absorbed = sum(
        _parse_dollar((bios.get(s, {}).get("salaries") or {}).get(season, ""))
        for s in set(slugs)
    )
    return absorbed, max(incoming_total - absorbed, 0), None


def _check_exception_absorption(
    team: str,
    incoming: int,
    exception_type: str,
    team_salary_before: int,
    team_salary_ex_holds_before: int,
    cl: dict,
    team_state: Optional[dict],
    season: str,
    bios: Optional[dict] = None,
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

    `bios` (optional): when the Room Exception has no recorded assignment yet,
    lets the fallback use a real reconstructed July-1 Team Salary instead of
    the live current figure. Only the real validation path (`_validate_trade`)
    passes it; this module's own test suite constructs scalars directly and
    exercises the live-figure fallback exactly as before `bios` existed.
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
        # § 3.2: unlike the NTMLE/TMLE — whose § 1.5/§ 1.6 apron bars are
        # *standing restrictions*, live at the moment of every transaction —
        # the Room Exception assignment is locked on July 1: "A team assigned
        # the Room Exception cannot move out of it during that year, even if
        # they later exceed the cap." So a team that holds it keeps it all
        # year, and re-testing current Team Salary here was wrong. The locked
        # assignment is what team_state's mle_type records.
        # "room" is the canonical short form, but accept the long key too so a
        # record written before that was normalized still reads correctly.
        assigned = get_season_state(team_state or {}, team, season).get("mle_type")
        room_ceiling = (cap - ntmle_amount) if cap is not None else None
        if assigned in ("room", "room_exception"):
            eligible = True
        else:
            # No assignment on record. Prefer a real reconstructed July-1
            # figure when bios is available (the production path); otherwise
            # fall back to the live-salary approximation this used before
            # reconstruction existed.
            july1 = _room_exception_july1_eligible(team, season, bios, cl) if bios is not None else None
            if july1 is not None:
                eligible = july1
            else:
                eligible = room_ceiling is not None and team_salary_before < room_ceiling
        threshold_msg = (f"the Cap minus the full NTMLE amount (${room_ceiling:,}) as of July 1, and "
                          f"holds no recorded Room Exception assignment — § 3.2"
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


def _cap_room_absorbed(team_salary_before: int, outgoing: int, incoming: int, cap: Optional[int]) -> bool:
    """§ 4.2 cap-room absorption: true when `incoming` can be covered purely by
    the team's own room under the Salary Cap, with no salary match required.

    Shared by `_check_salary_matching` and the § 4.3/§ 4.3 contagion checks —
    a trade genuinely funded this way carries no apron consequence, matching
    real NBA behavior: a cap-room acquisition never touches the apron, only
    the named exceptions (NTMLE/TMLE/BAE) and over-the-cap tiered matching do.
    """
    if cap is None or team_salary_before >= cap:
        return False
    return team_salary_before - outgoing + incoming <= cap


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
    bios: Optional[dict] = None,
) -> Optional[CheckResult]:
    cl = cap_levels.get(season, {})
    ex_holds = team_salary_ex_holds_before if team_salary_ex_holds_before is not None else team_salary_before

    if exception_type:
        return _check_exception_absorption(team, incoming, exception_type, team_salary_before,
                                            ex_holds, cl, team_state, season, bios=bios)

    if incoming <= outgoing:
        return None

    apron1 = cl.get("apron1")
    if apron1 is None:
        return None

    # ── Cap-room absorption ─────────────────────────────────────────────────
    # A team below the Salary Cap can absorb an incoming player using its own
    # cap room instead of matching salary — this is evaluated live, off the
    # team's actual room at the moment of the trade. Being under the cap is
    # also exactly why such a team banks no Trade Exception when it sends out
    # more than it takes back (§ 4.1a): the room already absorbs the
    # difference, so this path and a banked TPE are mutually exclusive.
    cap = cl.get("cap")
    if _cap_room_absorbed(team_salary_before, outgoing, incoming, cap):
        projected = team_salary_before - outgoing + incoming
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

def _signee_existing_hold(team: str, player: str, bios: dict, season: str) -> tuple[int, bool]:
    """The signee's own cap hold already on `team`'s books, as
    ``(amount, is_fa_hold)``.

    If the signee already sits on this team's own roster as a cap hold (e.g. a
    Bird-rights re-signing), that hold's figure is still counted in the team's
    ex-holds salary unless it's a pure UFA/RFA hold (already excluded) — so
    callers back it out to avoid double-counting hold + new contract (rulebook
    § 3.10: the hold is replaced, not stacked, by the signed contract's Year 1
    figure).

    Shared by `_validate_sign`, `_validate_offer_sheet` and the simulator's
    fact sheet so all three back out the same figure.
    """
    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not roster_path.exists():
        return 0, False
    _, roster_rows = read_csv(roster_path)
    if not any(r.get("SLUG", "").strip() == player for r in roster_rows):
        return 0, False
    bio = bios.get(player, {})
    return (
        _parse_dollar((bio.get("salaries") or {}).get(season, "")),
        (bio.get("cap_holds") or {}).get(season) in _FA_HOLD_TYPES,
    )


def _validate_sign(details: SignDetails, ctx: dict) -> list[CheckResult]:
    checks = []
    bios = ctx["bios"]; season = ctx["cur_season"]
    team = details.team.upper()

    current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
    existing_hold, is_fa_hold = _signee_existing_hold(team, details.player, bios, season)
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

    if details.signing_method == "bae":
        cl = ctx["cap_levels"].get(season, {})
        r = _check_bae_eligibility(team, current_ex_holds, cl, ctx["team_state"], season)
        if r:
            checks.append(r)

    # § 3.1–§ 3.6: the declared funding method must actually be available.
    # Cap room is measured with holds included (§ 3.10), net of the signee's own
    # hold — the contract replaces that hold rather than stacking on it.
    current_with_holds = _compute_team_salary(team, bios, season)
    # Free-agent holds still on the books for *other* players. Both salary
    # figures include dead cap, so the difference is exactly the UFA/RFA holds.
    # The league sheet routinely runs ahead of this ledger, so a team that has
    # really renounced its way to cap room can read as capped-out here — say so
    # instead of only reporting no room (DET/Gordon 2026-07-26: five renounces
    # applied on the sheet, never entered, made a legal cap-space signing look
    # like it had no funding method at all).
    other_holds = (current_with_holds - current_ex_holds) - (existing_hold if is_fa_hold else 0)
    r = _check_signing_method_funding(
        team, details.signing_method, new_sal,
        current_with_holds - existing_hold,
        current_ex_holds, season, ctx["cap_levels"], ctx["team_state"],
        unrenounced_holds=other_holds, bios=bios,
    )
    if r:
        checks.append(r)

    # ── § 1.5.2: no mid-season buyout signings above the NTMLE ───────────────
    # Only bars teams already at or above the First Apron. Below it the signing
    # is legal and instead triggers the Row D hard cap, applied in _apply_sign.
    # Validation runs before the bio is rewritten, so the player's stored
    # season salary is still the terminated contract's figure.
    apron1_sign = ctx["cap_levels"].get(season, {}).get("apron1")
    if apron1_sign is not None and current_ex_holds >= apron1_sign:
        buyout_salary = _buyout_salary_above_ntmle(
            details.player, ctx.get("txn_date") or "9999-12-31", season, ctx["cap_levels"],
            str((bios.get(details.player, {}).get("salaries") or {}).get(season, "") or ""),
        )
        if buyout_salary:
            ntmle_amt = ctx["cap_levels"].get(season, {}).get("ntmle_amount", 0)
            checks.append(CheckResult(
                check=f"buyout_signing_{team.lower()}",
                passed=False,
                level="error",
                message=(
                    f"{team} is at/above the First Apron (${current_ex_holds:,} ≥ "
                    f"${apron1_sign:,}) and may not sign a player waived this season whose "
                    f"terminated contract paid ${buyout_salary:,}, above the full NTMLE of "
                    f"${ntmle_amt:,} (§ 1.5.2)."
                ),
            ))

    if details.contract.type != "two-way":
        r = _roster_size_check(team, _count_standard_roster(team) + 1, "signing")
        if r:
            checks.append(r)

    # § 3.8. Runs before the raise check below, because declaring a Bird tier
    # is what unlocks the 8% raise ceiling — an unsupported declaration buys
    # both the funding path and the looser ladder.
    r = _check_bird_rights_declaration(details.player, team, details.bird_rights_type, season, bios,
                                       method=details.signing_method)
    if r:
        checks.append(r)

    bird_pct = details.bird_rights_type in ("QVFA", "EQVFA")
    r = _check_contract_raises(details.contract, bird_pct=bird_pct, cur_season=season)
    if r:
        checks.append(r)

    r = _check_signing_eligibility(details.player, team, season, bios)
    if r:
        checks.append(r)

    r = _check_minimum_salary(details.contract, details.player, bios, season, ctx["cap_levels"],
                              txn_date=ctx.get("txn_date"))
    if r:
        checks.append(r)

    r = _check_minimum_contract_cap_hit(details, bios, season, ctx["cap_levels"])
    if r:
        checks.append(r)

    r = _check_max_salary(details, bios, season, ctx["cap_levels"])
    if r:
        checks.append(r)

    return checks


def _check_signing_eligibility(player: str, team: str, season: str,
                               bios: dict) -> Optional[CheckResult]:
    """Who can be signed at all: not retired, and not already under contract
    to somebody else.

    A player carrying a UFA/RFA cap hold is a free agent whose rights their
    team holds (§ 3.10), so re-signing them is exactly what a `sign` is for —
    only a real, non-hold contract elsewhere blocks it.
    """
    bio = bios.get(player) or {}
    if bio.get("retired"):
        return CheckResult(
            check="signing_eligibility", passed=False, level="error",
            message=f"{bio.get('name') or player} is retired and cannot be signed.",
        )
    holder = _build_team_map().get(player)
    if holder and holder.upper() != team.upper():
        hold = (bio.get("cap_holds") or {}).get(season)
        if hold not in _FA_HOLD_TYPES:
            return CheckResult(
                check="signing_eligibility", passed=False, level="error",
                message=(
                    f"{bio.get('name') or player} is under contract to {holder} and is not a free "
                    f"agent — acquire them by trade, or wait for the contract to expire (§ 3.1)."
                ),
            )
    return None


def _min_salary_floor(season: str, cap_levels: dict) -> int:
    """The 0-years tier for `season`, falling back to the latest configured
    season when that one has no scale yet. Minimum scales only ever rise, so
    an older season's figure is a conservative floor for a later one — it can
    under-flag, never produce a false positive."""
    scale = (cap_levels.get(season, {}) or {}).get("min_salary_scale") or {}
    if scale.get("0"):
        return scale["0"]
    for yr in sorted(cap_levels, key=_season_start, reverse=True):
        if _season_start(yr) <= _season_start(season):
            amt = ((cap_levels.get(yr) or {}).get("min_salary_scale") or {}).get("0")
            if amt:
                return amt
    return 0


def _check_minimum_salary(contract, player: str, bios: dict, season: str,
                          cap_levels: dict, txn_date: Optional[str] = None) -> Optional[CheckResult]:
    """§ 3.12: no contract year may pay below that season's minimum salary.

    Proration is the wrinkle. The league prorates in-season minimum signings
    (practice, not yet written into the rulebook — see BACKLOG), so a Year 1
    figure below the full-season minimum can be perfectly legal if the deal
    was signed after the season started. That excuse applies to **Year 1
    only**: every later year of the contract is a full season, so the floor is
    hard there regardless.

    Hence the split:
      * Year 1, signed in-season   -> below floor is a *warning* (likely prorated)
      * Year 1, signed in the offseason, or any later year -> *error*
      * between the league floor and the player's own experience tier ->
        warning, since experience is inferred from `draft_year` rather than
        verified (same treatment § 3.11 gives it)

    Two-way contracts are exempt: they don't pay on the standard scale.
    """
    if contract.type == "two-way":
        return None
    check_name = "minimum_salary"
    # The league year starts July 1; games start in the autumn. A signing in
    # Jul-Sep therefore precedes the season and cannot be prorated. Deliberately
    # a coarse rule — proration isn't in the rulebook yet, so there is no
    # authoritative season-start date to key off.
    in_season = bool(txn_date) and int(txn_date[5:7]) not in (7, 8, 9)

    for yr, raw in sorted((contract.salaries or {}).items()):
        floor = _min_salary_floor(yr, cap_levels)
        if not floor:
            continue        # no scale configured at or before this season
        amount = _parse_dollar(raw)
        if amount < floor:
            prorateable = (yr == season and in_season)
            if prorateable:
                return CheckResult(
                    check=check_name, passed=False, level="warning",
                    message=(
                        f"{yr} salary of ${amount:,} is below the ${floor:,} full-season minimum "
                        f"(§ 3.12). That is legal only if it is a prorated in-season signing — "
                        f"confirm the proration is right, since the system can't verify it."
                    ),
                )
            when = ("signed before the season started, so no proration applies"
                    if yr == season else "a full contract year, which is never prorated")
            return CheckResult(
                check=check_name, passed=False, level="error",
                message=(
                    f"{yr} salary of ${amount:,} is below the {yr} minimum salary of "
                    f"${floor:,} (§ 3.12) — {when}."
                ),
            )
        tier_floor = _min_salary_for(bios.get(player) or {}, yr, cap_levels)
        if tier_floor and amount < tier_floor:
            return CheckResult(
                check=check_name, passed=False, level="warning",
                message=(
                    f"{yr} salary of ${amount:,} is below this player's ${tier_floor:,} "
                    f"minimum for their experience tier (§ 3.12), though above the league "
                    f"floor of ${floor:,}. Experience is inferred from their real NBA draft "
                    f"year — double check before submitting."
                ),
            )
    return CheckResult(
        check=check_name, passed=True,
        message="Every contract year meets the § 3.12 minimum salary.",
    )


def _min_salary_for(bio: dict, season: str, cap_levels: dict) -> Optional[int]:
    """That season's minimum for this player's experience tier, or None when
    experience can't be established (undrafted, or scale not configured)."""
    scale = (cap_levels.get(season, {}) or {}).get("min_salary_scale") or {}
    draft_year = bio.get("draft_year")
    if not scale or not draft_year:
        return None
    years_exp = _season_start(season) + 2000 - int(draft_year)
    return scale.get(_min_salary_scale_tier(years_exp)) or None


def _check_minimum_contract_cap_hit(details: SignDetails, bios: dict, season: str,
                                     cap_levels: dict) -> Optional[CheckResult]:
    """§ 3.12: a 1-year `minimum` signing's cap hit should equal the 2-year
    veteran minimum regardless of the player's actual experience tier. Only
    checked for genuinely 1-year minimum deals (a single salary year) signed
    via signing_method="minimum" — a multi-year minimum contract follows the
    scale per-year instead and isn't checked here. This is advisory (a
    warning, force-through-able) since § 3.12 is manual review, not
    system-enforced — the player's real NBA experience isn't independently
    verified, just inferred from draft_year.
    """
    if details.signing_method != "minimum":
        return None
    salaries = details.contract.salaries or {}
    if len(salaries) != 1 or season not in salaries:
        return None
    expected = _one_year_min_cap_hit(bios.get(details.player, {}), season, cap_levels)
    if expected is None:
        return None
    submitted = _parse_dollar(salaries[season])
    if submitted == expected:
        return CheckResult(
            check="minimum_contract_cap_hit", passed=True,
            message=f"1-yr minimum cap hit (${expected:,}) matches the 2-yr veteran minimum (§ 3.12).",
        )
    return CheckResult(
        check="minimum_contract_cap_hit",
        passed=False,
        level="warning",
        message=(f"Submitted salary (${submitted:,}) doesn't match the § 3.12 1-yr minimum cap hit "
                 f"of ${expected:,} (2-year veteran minimum) inferred from this player's real NBA "
                 f"draft year — double check before submitting."),
    )


def _check_max_salary(details: SignDetails, bios: dict, season: str,
                       cap_levels: dict) -> Optional[CheckResult]:
    """§ 3.11: a signed player's current-season salary shouldn't exceed their
    experience-tier maximum (25/30/35% of that season's Salary Cap). Skipped
    for two-way contracts (no cap salary) and undrafted players (no
    draft_year experience proxy). Advisory only — § 3.11 is manual review,
    not system-enforced, and years of NBA experience is inferred from
    draft_year, not independently verified.
    """
    if details.contract.type == "two-way":
        return None
    salaries = details.contract.salaries or {}
    if season not in salaries:
        return None
    expected = _max_salary(bios.get(details.player, {}), season, cap_levels)
    if expected is None:
        return None
    submitted = _parse_dollar(salaries[season])
    if submitted <= expected:
        return CheckResult(
            check="max_salary", passed=True,
            message=f"Year 1 salary (${submitted:,}) is within the § 3.11 max of ${expected:,}.",
        )
    return CheckResult(
        check="max_salary",
        passed=False,
        level="warning",
        message=(f"Submitted Year 1 salary (${submitted:,}) exceeds the § 3.11 max of ${expected:,} "
                 f"inferred from this player's real NBA draft year — double check before submitting."),
    )


def _validate_release(details: ReleaseDetails, ctx: dict) -> list[CheckResult]:
    return []


def _validate_renounce(details: RenounceDetails, ctx: dict) -> list[CheckResult]:
    """§ 3.10 renounce checks.

    Only one is an error — the eligibility test, shared verbatim with
    `_apply_renounce` via `_renounce_eligibility`. The roster-count consequences
    are warnings on purpose: neither § 2.1 nor § 2.1a forbids renouncing your way
    below the minimum, they just charge you for it, so blocking would invent a
    rule. Surfacing them still matters because the strike and the Empty Roster
    Charge both land on the team days later, well after the click.
    """
    checks: list[CheckResult] = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    elig = _renounce_eligibility(details.player, bios, season)
    if not elig["ok"]:
        checks.append(CheckResult(
            check="renounce_eligible", passed=False, level="error", message=elig["reason"],
        ))
        # Everything below is measured against the team this renounce would hit.
        # Without one there's nothing to measure, and reporting cheerful passes
        # off an unevaluatable transaction is the failure mode _require_validatable
        # exists to prevent.
        return checks

    team = elig["team"]
    name = (bios.get(details.player) or {}).get("name") or details.player
    checks.append(CheckResult(
        check="renounce_eligible", passed=True,
        message=(f"{name} is a {elig['hold_type']} cap hold for {elig['cutoff']} — "
                 f"renounceable under § 3.10."),
    ))

    after = _count_standard_roster(team) - 1
    if after < ROSTER_CHARGE_MIN:
        short = ROSTER_CHARGE_MIN - after
        per = _rookie_min_salary(season, ctx["cap_levels"])
        checks.append(CheckResult(
            check="roster_minimum", passed=False, level="warning",
            message=(f"This leaves {team} with {after} standard players, below the {ROSTER_CHARGE_MIN}-player "
                     f"floor — an Empty Roster Charge of ${per:,} per open slot "
                     f"(${per * short:,} total) applies immediately and counts toward hard cap "
                     f"and apron comparisons (§ 2.1a)."),
        ))
    elif after < ROSTER_MIN:
        checks.append(CheckResult(
            check="roster_minimum", passed=False, level="warning",
            message=(f"This leaves {team} with {after} standard players, below the {ROSTER_MIN}-player "
                     f"minimum. No dollar charge applies above {ROSTER_CHARGE_MIN}, but every FO member "
                     f"takes an activity strike if the shortfall lasts a week (§ 2.1)."),
        ))
    else:
        checks.append(CheckResult(
            check="roster_minimum", passed=True,
            message=f"{team} is left with {after} standard players, at or above the {ROSTER_MIN}-player minimum.",
        ))

    # Bird Rights are forfeited outright (§ 3.10) — the § 3.8 clock this player
    # had accrued with the team is gone, and re-signing them later starts over
    # at Non-QVFA. That cost is invisible on the cap sheet, so it gets said out
    # loud rather than left for the owner to remember.
    bird = _bird_tenure(details.player, team, season, bios.get(details.player) or {})
    if bird["tier"] in ("QVFA", "EQVFA"):
        # A trade_floor basis means the derived figure is a lower bound (no
        # record predates the trade), so the tenure is "at least" this long —
        # which only strengthens the case that real rights are being given up.
        span = (f"at least {bird['seasons']}" if bird["basis"] == "trade_floor" else str(bird["seasons"]))
        checks.append(CheckResult(
            check="bird_rights_forfeited", passed=False, level="warning",
            message=(f"{team} forfeits {name}'s {bird['tier']} Bird Rights "
                     f"({span} seasons of continuous service — {bird['evidence']}). "
                     f"Re-signing them afterwards would be a Non-QVFA signing needing cap space "
                     f"or an exception (§ 3.8, § 3.10)."),
        ))
    elif bird["basis"] == "unknown":
        checks.append(CheckResult(
            check="bird_rights_forfeited", passed=False, level="warning",
            message=(f"{name} has no acquisition on file, so their § 3.8 tenure can't be derived — "
                     f"unknown usually means long-tenured, not Non-QVFA. Assume Bird Rights are "
                     f"being forfeited (§ 3.10)."),
        ))
    else:
        checks.append(CheckResult(
            check="bird_rights_forfeited", passed=True,
            message=(f"{name} holds no Bird Rights with {team} to forfeit "
                     f"({bird['evidence']})."),
        ))

    return checks


def _renounce_fact_sheet(details: RenounceDetails, ctx: dict) -> dict:
    """What the renounce actually does to the team's books.

    Cap figures come from `_compute_team_salary*` — the same helpers the
    validators use — so this can't credit a team with room the validator didn't.
    """
    bios = ctx["bios"]; season = ctx["cur_season"]
    elig = _renounce_eligibility(details.player, bios, season)
    team = elig["team"]
    bio = bios.get(details.player) or {}

    sheet = {
        "season": season,
        "team": team,
        "player": details.player,
        "player_name": bio.get("name") or details.player,
        "eligible": elig["ok"],
        "reason": elig["reason"],
        "hold_type": elig["hold_type"],
        "hold_season": elig["cutoff"],
    }
    if not elig["ok"]:
        return sheet

    cap = (ctx["cap_levels"].get(season) or {}).get("cap")
    hold = _parse_dollar((bio.get("salaries") or {}).get(elig["cutoff"], ""))
    before = _compute_team_salary(team, bios, season)
    before_count = _count_standard_roster(team)
    bird = _bird_tenure(details.player, team, season, bio)

    sheet.update({
        "cap_levels": ctx["cap_levels"].get(season) or {},
        "hold_amount": hold,
        # Renouncing clears the hold, so team salary *including holds* drops by
        # exactly the hold figure. The ex-holds figure (what hard cap and apron
        # compare against, § 1.3) never counted it in the first place — so a
        # renounce buys cap room and moves the apron picture not at all.
        "team_salary_before": before,
        "team_salary_after": before - hold,
        "team_salary_ex_holds": _compute_team_salary_ex_holds(team, bios, season),
        "cap_room_before": (cap - before) if cap else None,
        "cap_room_after": (cap - (before - hold)) if cap else None,
        "standard_count_before": before_count,
        "standard_count_after": before_count - 1,
        "roster_min": ROSTER_MIN,
        "roster_charge_min": ROSTER_CHARGE_MIN,
        "bird_tier": bird["tier"],
        "bird_seasons": bird["seasons"],
        "bird_basis": bird["basis"],
        "bird_evidence": bird["evidence"],
    })
    return sheet


def _validate_rescind_renounce(details: RescindRenounceDetails, ctx: dict) -> list[CheckResult]:
    """§ 3.10 rescission restrictions: a team may not rescind a renouncement if
    doing so would move them from under the cap to over it, or — if already over
    — increase their cap figure further.

    Warnings rather than errors. The restrictions are written for the offer-sheet
    case, where rescinding hands back space the team only ever borrowed; an
    administrative undo of a mistaken renounce is a correction, and refusing to
    let the office restore a player because the restoration costs cap room would
    leave the books wrong in a different way. The hard structural failures (no
    such transaction, no snapshot, player already signed elsewhere) are raised by
    `_apply_rescind_renounce` and are not forceable.
    """
    checks: list[CheckResult] = []
    src = next((t for t in _load_transactions() if t.get("id") == details.txn_id), None)
    if src is None or src.get("type") != "renounce":
        # _apply_rescind_renounce hard-fails these; scoring them here would only
        # produce a verdict on a transaction that doesn't exist.
        return checks

    snap = (src.get("details") or {}).get("_snapshot") or {}
    team = (src["details"].get("team") or "").upper()
    player = src["details"].get("player") or ""
    if team not in VALID_TEAMS or not snap:
        return checks

    bios = ctx["bios"]; season = ctx["cur_season"]
    cap = (ctx["cap_levels"].get(season) or {}).get("cap")
    restored = _parse_dollar((snap.get("salaries") or {}).get(season, ""))
    if not cap or not restored:
        return checks

    before = _compute_team_salary(team, bios, season)
    after = before + restored
    name = (bios.get(player) or {}).get("name") or player

    if before <= cap < after:
        checks.append(CheckResult(
            check="rescind_cap_restriction", passed=False, level="warning",
            message=(f"Restoring {name}'s ${restored:,} hold moves {team} from ${before:,} "
                     f"(under the ${cap:,} cap) to ${after:,} (over it) — § 3.10 bars rescission "
                     f"that crosses the cap."),
        ))
    elif before > cap:
        checks.append(CheckResult(
            check="rescind_cap_restriction", passed=False, level="warning",
            message=(f"{team} is already ${before - cap:,} over the ${cap:,} cap; restoring "
                     f"{name}'s ${restored:,} hold increases that further — § 3.10 bars rescission "
                     f"for a team already above the cap."),
        ))
    else:
        checks.append(CheckResult(
            check="rescind_cap_restriction", passed=True,
            message=(f"{team} stays under the ${cap:,} cap after restoring {name}'s "
                     f"${restored:,} hold (${after:,})."),
        ))
    return checks


def _offer_sheet_signing_team(details: OfferSheetDetails) -> Optional[tuple[str, str]]:
    """``(signing_team, retaining_team)`` for an offer being *extended*.

    At offer time the incumbent hasn't decided yet, so the team whose books this
    has to clear is the **offering** team — they're the one committing the money
    and carrying the § 3.15 hold. If the incumbent matches, their side is judged
    separately by `_validate_offer_sheet_decision`.
    """
    retaining_team = _build_team_map().get(details.player)
    if not retaining_team:
        return None
    team = details.offering_team.upper()
    if team not in VALID_TEAMS:
        return None
    return team, retaining_team


def _validate_offer_sheet(details: OfferSheetDetails, ctx: dict) -> list[CheckResult]:
    """§ 3.15 checks at the moment an offer sheet is *extended*.

    Judged against the offering team: they commit the money and carry the cap
    hold for as long as the offer is open, whatever the incumbent later decides.
    Mirrors `_validate_sign`'s hard-cap and funding checks so a declared
    `signing_method` can't skip the funding gate just because it arrived via an
    offer sheet. Everything else about § 3.15 legality (good-faith fit, Gilbert
    Arenas eligibility) stays manual review per the rulebook.
    """
    checks: list[CheckResult] = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    resolved = _offer_sheet_signing_team(details)
    if resolved is None:
        # The hard-fail checks in _apply_offer_sheet cover these; nothing
        # useful to validate here without a resolvable signing team.
        return checks
    team, retaining = resolved

    if team == retaining:
        checks.append(CheckResult(
            check="offer_sheet_own_player", passed=False, level="error",
            message=(f"{team} already holds this player's RFA rights — a team can't "
                     f"offer-sheet its own restricted free agent (§ 3.15). Re-sign them instead."),
        ))

    # One live offer per player: § 3.15 gives the incumbent 48 hours to match
    # *an* offer, and two open sheets make "match" ambiguous.
    existing = next((o for o in _open_offer_sheets() if o["player"] == details.player), None)
    if existing:
        checks.append(CheckResult(
            check="offer_sheet_already_open", passed=False, level="error",
            message=(f"{(bios.get(details.player) or {}).get('name') or details.player} already has an "
                     f"open offer sheet from {existing['offering_team']} dated {existing['date']} — "
                     f"resolve that one before extending another."),
        ))

    current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
    existing_hold, is_fa_hold = _signee_existing_hold(team, details.player, bios, season)
    new_sal = _parse_dollar(details.contract.salaries.get(season, ""))
    projected_ex_holds = current_ex_holds - (0 if is_fa_hold else existing_hold) + new_sal

    r = _hard_cap_check(team, projected_ex_holds, season, ctx["team_state"], ctx["cap_levels"])
    if r:
        checks.append(r)
    r = _universal_hard_cap_check(team, projected_ex_holds, season, ctx["cap_levels"])
    if r:
        checks.append(r)

    current_with_holds = _compute_team_salary(team, bios, season)
    other_holds = (current_with_holds - current_ex_holds) - (existing_hold if is_fa_hold else 0)
    r = _check_signing_method_funding(
        team, details.signing_method, new_sal,
        current_with_holds - existing_hold,
        current_ex_holds, season, ctx["cap_levels"], ctx["team_state"],
        unrenounced_holds=other_holds, bios=bios,
    )
    if r:
        checks.append(r)

    r = _check_bird_rights_declaration(details.player, team, details.bird_rights_type, season, bios,
                                       method=details.signing_method)
    if r:
        checks.append(r)

    # § 3.15: an offer sheet is a restricted-free-agency instrument. A player
    # who isn't an RFA can't receive one at all — they're either under contract
    # (acquire by trade) or unrestricted (sign them outright, no match right).
    rfa_ok, rfa_why = _rfa_eligibility(details.player, bios, season)
    if not rfa_ok:
        checks.append(CheckResult(
            check="offer_sheet_rfa", passed=False, level="error",
            message=(
                f"{(bios.get(details.player) or {}).get('name') or details.player} is not a "
                f"Restricted Free Agent ({rfa_why}) — only an RFA can receive an "
                f"offer sheet (§ 3.15)."
            ),
        ))

    r = _check_minimum_salary(details.contract, details.player, bios, season, ctx["cap_levels"],
                              txn_date=ctx.get("txn_date"))
    if r:
        checks.append(r)

    if details.contract.type != "two-way":
        # If the incumbent passes, this adds a body to the offering team. Judged
        # now rather than at decision time: a team shouldn't extend an offer it
        # has no room to honour, and the incumbent's choice can't create room.
        r = _roster_size_check(team, _count_standard_roster(team) + 1,
                               "extending an offer sheet they'd have to honour")
        if r:
            checks.append(r)

    return checks


def _validate_offer_sheet_decision(details: OfferSheetDecisionDetails, ctx: dict) -> list[CheckResult]:
    """§ 3.15 checks at the moment an offer sheet is *resolved*.

    The offer itself was already validated against the offering team. What's new
    here is the incumbent's side: matching is how a team keeps its own free agent
    and is allowed over the cap, but a **hard cap is still a hard cap** (§ 1.3),
    so a match that would breach it has to be caught. Nothing checked that before
    the split, because the combined transaction only ever scored the team that
    ended up signing — which on a match was the incumbent, but with terms already
    treated as a fait accompli.
    """
    checks: list[CheckResult] = []
    bios = ctx["bios"]; season = ctx["cur_season"]

    offer = next((o for o in _open_offer_sheets() if o["id"] == details.offer_id), None)
    if offer is None:
        # _apply_offer_sheet_decision hard-fails an unknown or already-resolved
        # offer. Scoring anything here would report a verdict on a transaction
        # that was never evaluated.
        return checks
    if details.outcome not in ("matched", "not_matched"):
        return checks

    team = offer["retaining_team"] if details.outcome == "matched" else offer["offering_team"]
    contract = offer["contract"] or {}
    new_sal = _parse_dollar((contract.get("salaries") or {}).get(season, ""))

    current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
    existing_hold, is_fa_hold = _signee_existing_hold(team, offer["player"], bios, season)
    projected_ex_holds = current_ex_holds - (0 if is_fa_hold else existing_hold) + new_sal

    for check_fn in (
        lambda: _hard_cap_check(team, projected_ex_holds, season, ctx["team_state"], ctx["cap_levels"]),
        lambda: _universal_hard_cap_check(team, projected_ex_holds, season, ctx["cap_levels"]),
    ):
        r = check_fn()
        if r:
            checks.append(r)

    if details.outcome == "not_matched" and (contract.get("type") != "two-way"):
        r = _roster_size_check(team, _count_standard_roster(team) + 1, "adding this player")
        if r:
            checks.append(r)

    if not checks:
        who = (bios.get(offer["player"]) or {}).get("name") or offer["player"]
        checks.append(CheckResult(
            check="offer_sheet_decision", passed=True,
            message=(f"{team} can absorb {who} at ${new_sal:,} for {season} "
                     f"({'match' if details.outcome == 'matched' else 'signing the unmatched offer'})."),
        ))
    return checks


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


def _check_stepien_rule(details: TradeIn, all_picks: list[dict] | None = None) -> list[CheckResult]:
    """§ 7.2 Stepien Rule: a team must retain the ability to make a
    first-round selection at least once every two draft years — a proposed
    trade is illegal if, after it, a team would have no first-round pick in
    two or more consecutive draft years. "Ability to make a selection" counts
    any first-round pick the team holds, own or acquired (not just its own).

    Uses the same resolved, conveyance-aware ownership `GET /api/picks`
    serves (`_all_picks_flat`) — not the raw flat-CSV OWNER column, which
    under the conveyance cutover (PICKS_READ_SOURCE=conveyance) no longer
    reflects final ownership for protected/swap picks. "Has a pick that
    year" mirrors `get_team_picks`'s own matching rule: an exact owner, or
    the orig team when a pick's owner is still fully undetermined ("?").

    Only plain, unconditional first-round retrades are simulated here — a
    protected pick, a swap right, or a protection ladder doesn't hand over a
    definite claim on draft day, so folding those into this year-by-year
    ownership count would be guessing at a still-contingent outcome (see
    picks_conveyance's `leaves` model). Those cases fall back to committee
    manual review, same as the rest of § 7.2's badge in the rulebook.

    A pick with a non-null `group_id` shares its conveyance state with every
    other pick carrying the same `group_id` (a 2+-way swap group or binary
    chain projects the SAME shared priority list / chain node onto each
    member pick's own row — see `registry.handle_retrade` in
    picks_conveyance, which mutates that one shared structure regardless of
    which member pick named the trade). So retrading one member conveys the
    team's single shared claim, not an independent share per row — every
    row in the group must be updated together, or a team with two picks in
    a shared swap group would wrongly look like it has two independent
    claims it can trade away one at a time while "keeping the other."

    A multi-band `protected` pick (no `group_id` — bands aren't a shared
    structure the way swap groups are) can still list several real,
    INDEPENDENT candidates in its pipe-joined `owner` (e.g. a 3-band pick
    with bands going to 3 different teams). Each band is its own claim —
    `registry.handle_retrade` mutates only the specific band the trading
    team occupies, leaving the others untouched — so a plain retrade must
    replace just that one team within the pipe string, never overwrite the
    whole entry down to a single team (which would silently delete every
    other band-holder's real claim).

    A pick with `ladder_fallback_of` set carries a real but easy-to-miss
    claim: its own `owner`/`leaves` look like a plain settled pick, but a
    DIFFERENT pick's `ladder.fallback` names this one as compensation if
    that ladder's protection never lifts — the claimant only appears in
    `ladder_fallback_of.to` on THIS pick, nowhere else. Folded into the
    owner pipe here so that team's real (if contingent) coverage counts;
    left out of retrade simulation below (too indirect to safely mutate
    the same way a normal retrade does — falls to manual review).

    `all_picks` is injectable (defaults to the real `_all_picks_flat()`)
    so tests can exercise this against synthetic pick data instead of
    live production picks — see `tests/test_stepien_rule.py`.
    """
    if all_picks is None:
        all_picks = _all_picks_flat()
    owner_map: dict[tuple[int, str], str] = {}
    group_members: dict[str, list[tuple[int, str]]] = {}
    fallback_keys: set[tuple[int, str]] = set()
    for p in all_picks:
        if p.get("round") != 1 or p.get("player"):
            continue
        key = (p["year"], p["orig"].upper())
        owner = p.get("owner") or "?"
        fallback = p.get("ladder_fallback_of")
        if fallback and fallback.get("to"):
            fallback_keys.add(key)
            fb_team = fallback["to"]
            owner = fb_team if owner == "?" else (
                owner if fb_team in owner.split("|") else f"{owner}|{fb_team}")
        owner_map[key] = owner
        gid = p.get("group_id")
        if gid:
            group_members.setdefault(gid, []).append(key)
    key_to_group = {key: gid for gid, keys in group_members.items() for key in keys}

    def _replace_owner(owner: str, from_team: str, to_team: str) -> str:
        if owner == "?" or from_team not in owner.split("|"):
            return owner
        teams = [to_team if t == from_team else t for t in owner.split("|")]
        deduped = []
        for t in teams:
            if t not in deduped:
                deduped.append(t)
        return "|".join(deduped)

    affected_teams: set[str] = set()
    for xfer in details.transfers:
        from_t = xfer.from_team.upper()
        to_t = xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type != "pick" or asset.round != 1:
                continue
            affected_teams.add(from_t)
            affected_teams.add(to_t)
            if asset.swap_with or asset.protection is not None or asset.ladder_protect_top is not None:
                continue
            key = (asset.year, (asset.orig or "").upper())
            if key not in owner_map or key in fallback_keys:
                continue
            gid = key_to_group.get(key)
            for member_key in (group_members[gid] if gid else [key]):
                owner_map[member_key] = _replace_owner(owner_map[member_key], from_t, to_t)

    if not affected_teams or not owner_map:
        return []

    years = sorted({y for (y, _orig) in owner_map})
    lo, hi = years[0], years[-1]
    checks: list[CheckResult] = []
    for team in sorted(affected_teams):
        have = {y for (y, orig), owner in owner_map.items()
                if (owner == "?" and orig == team) or team in owner.split("|")}
        gap = None
        run_start = None
        for y in range(lo, hi + 1):
            if y in have:
                run_start = None
                continue
            if run_start is None:
                run_start = y
            elif y - run_start >= 1:
                gap = (run_start, y)
                break
        if gap:
            checks.append(CheckResult(
                check=f"stepien_rule_{team.lower()}", passed=False, level="error",
                message=(f"{team} would have no first-round pick in {gap[0]} and {gap[1]} after this "
                         "trade — violates the Stepien Rule (§ 7.2): a team must retain the ability to "
                         "make a first-round selection at least once every two draft years."),
            ))
        else:
            checks.append(CheckResult(
                check=f"stepien_rule_{team.lower()}", passed=True,
                message=f"{team}: retains a first-round pick at least once every two draft years (§ 7.2).",
            ))
    return checks


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
      • Hard cap           — team apron hard cap (team-state) + league-wide hard cap,
                              inclusive of any Empty Roster Charge (below)
      • Roster size        — Article II: 15 in-season, 20 offseason ceiling
      • Empty Roster Charge — § 2.1a: trade legality is judged against the full 14-player
                              minimum (wider than the 12-player floor for a *real*, persisted
                              charge) — a team left below 14 has that gap assumed filled at
                              that season's rookie minimum for hard-cap comparison purposes,
                              folded into the hard-cap figures above, not just reported standalone
      • Sign-and-trade     — § 3.14 contract rules + § 4.3 receiving-team apron limit,
                              only when `is_sign_and_trade` is set
      • Stepien Rule       — § 7.2 (no first-round pick in 2+ consecutive draft years,
                              own or acquired; unconditional pick retrades only — protected/
                              swap/ladder legs are left to manual review)
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

    # ── Roster-after counts + Empty Roster Charge (§ 2.1a) ─────────────────────
    # Computed up front so the hard-cap projections below can already account
    # for a charge — a trade can't duck a hard cap by shedding headcount, since
    # any slot left below the 14-player minimum still costs at least that
    # season's rookie minimum to fill. Reused verbatim by the roster-size
    # section further down so the two never compute "after" differently.
    roster_after: dict[str, int] = {}
    erc_deficiency: dict[str, int] = {}
    erc_charge: dict[str, int] = {}
    for team in sorted(set(out_players) | set(in_players)):
        before = _count_standard_roster(team)
        out_std = sum(1 for s in out_players.get(team, [])
                      if _is_standard_roster_slot(bios.get(s, {}).get("type", "")))
        in_std  = sum(1 for s in in_players.get(team, [])
                      if _is_standard_roster_slot(bios.get(s, {}).get("type", "")))
        after = before - out_std + in_std
        roster_after[team] = after
        erc_deficiency[team], erc_charge[team] = _empty_roster_charge(after, season, ctx["cap_levels"])

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
    charge_decisive: dict[str, bool] = {}
    for team in teams:
        out = outgoing.get(team, 0)
        match_out = matching_outgoing.get(team, 0)
        inc = incoming.get(team, 0)
        current = _compute_team_salary(team, bios, season)
        current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
        delta = inc - out
        charge = erc_charge.get(team, 0)

        if delta > 0 or charge > 0:
            # Empty Roster Charge (§ 2.1a) folds straight into the projected
            # figure — it's guaranteed money the team is on the hook for the
            # moment the roster dips below 14, so it counts toward both the
            # apron-triggered hard cap and the league-wide Hard Cap exactly
            # like a real contract would.
            projected_ex_holds = current_ex_holds + delta + charge
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

            # Was the charge actually decisive — would this team have cleared
            # the hard cap without it? Drives whether the empty_roster_charge_
            # check below reads as a mere assumption or the reason the trade
            # fails (§ 2.1a) — surfaced so the UI doesn't show a plain "passed"
            # checkmark next to the very thing that sank the trade.
            if charge > 0 and (hc is not None or lhc is not None):
                projected_without_charge = current_ex_holds + delta
                hc_wo = _hard_cap_check(team, projected_without_charge, season,
                                        ctx["team_state"], ctx["cap_levels"])
                lhc_wo = _universal_hard_cap_check(team, projected_without_charge, season, ctx["cap_levels"])
                charge_decisive[team] = hc_wo is None and lhc_wo is None

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
                sm = None
                absorbed, matched_inc, split_err = _exception_absorption_split(
                    details, team, inc, in_players, bios, season)
                if split_err:
                    checks.append(CheckResult(
                        check=f"salary_matching_{team.lower()}", passed=False,
                        level="error", message=split_err,
                    ))
                elif exc_type and matched_inc > 0:
                    # Hybrid (§ 4.2a): the named players are funded by the
                    # exception, the remainder still has to match on its own.
                    # Reported as two checks so it's visible which rule each
                    # half of the trade cleared.
                    checks.append(_check_exception_absorption(
                        team, absorbed, exc_type, current, current_ex_holds,
                        ctx["cap_levels"].get(season, {}), ctx.get("team_state"), season, bios=bios))
                    sm = _check_salary_matching(team, match_out, matched_inc, current,
                                                 ctx["cap_levels"], season,
                                                 exception_type=None, team_state=ctx.get("team_state"),
                                                 team_salary_ex_holds_before=current_ex_holds)
                    checks.append(sm or CheckResult(
                        check=f"salary_matching_{team.lower()}", passed=True,
                        message=(f"{team}: remaining incoming ${matched_inc:,} matches outgoing "
                                 f"${match_out:,} (§ 4.2/4.3); ${absorbed:,} absorbed separately."),
                    ))
                else:
                    sm = _check_salary_matching(team, match_out, inc, current, ctx["cap_levels"], season,
                                                 exception_type=exc_type, team_state=ctx.get("team_state"),
                                                 team_salary_ex_holds_before=current_ex_holds, bios=bios)
                    checks.append(sm or CheckResult(
                        check=f"salary_matching_{team.lower()}", passed=True,
                        message=f"{team}: incoming ${inc:,} matches outgoing ${match_out:,} (§ 4.2/4.3).",
                    ))

                # § 4.3 contagion (below First Apron, any trade — not just
                # aggregation): legal under § 4.2 tiered matching, but still
                # locks the team to the First Apron for the rest of the
                # season once submitted. Mirrors the § 4.4 aggregation
                # warning below, one apron tier up and without the 2+-leg
                # requirement. Doesn't apply when an exception funded the
                # acquisition — that path has its own dedicated trigger — nor
                # when the incoming salary was genuinely absorbed via cap room
                # (§ 4.2): real NBA cap-room acquisitions never touch the
                # apron either, so a clean room-based trade gets the same
                # carve-out as the named exceptions.
                if not exc_type and not split_err and (sm is None or sm.passed):
                    apron1 = ctx["cap_levels"].get(season, {}).get("apron1")
                    cap = ctx["cap_levels"].get(season, {}).get("cap")
                    if (apron1 is not None and current_ex_holds < apron1 and inc > match_out + 250_000
                            and not _cap_room_absorbed(current, match_out, inc, cap)):
                        over = inc - match_out - 250_000
                        checks.append(CheckResult(
                            check=f"apron1_contagion_{team.lower()}", passed=False, level="warning",
                            message=(f"{team} is below the First Apron (${current_ex_holds:,} < ${apron1:,}) "
                                     f"but incoming ${inc:,} exceeds outgoing + $250K "
                                     f"(${match_out + 250_000:,}) by ${over:,} — legal under standard tiered "
                                     "matching (§ 4.2), but triggers a First Apron hard cap for the rest of "
                                     "the season (§ 1.4, § 4.3 contagion) once submitted."),
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
        after = roster_after[team]
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

        deficiency, charge = erc_deficiency[team], erc_charge[team]
        if deficiency > 0:
            rookie_min = _rookie_min_salary(season, ctx["cap_levels"])
            real_deficiency = max(0, ROSTER_CHARGE_MIN - after)
            if real_deficiency > 0:
                real_note = (f" {real_deficiency} of those slot(s) fall below the {ROSTER_CHARGE_MIN}-player "
                              "real-charge floor, so that portion also lands on the roster as an actual "
                              "Empty Roster Charge (real guaranteed salary), not just a trade-legality assumption.")
            else:
                real_note = (f" {after} is at/above the {ROSTER_CHARGE_MIN}-player real-charge floor, so nothing "
                              "actually posts to the roster — this is a trade-legality assumption only.")
            decisive = charge_decisive.get(team, False)
            if decisive:
                checks.append(CheckResult(
                    check=f"empty_roster_charge_{team.lower()}", passed=False, level="error",
                    message=(f"{team}: {after} standard players after trade — {deficiency} slot(s) below the "
                             f"{ROSTER_MIN}-player trade-legality floor (§ 2.1a). This is the reason the hard-cap "
                             f"check above fails: without this ${charge:,} assumed charge "
                             f"(${rookie_min:,} × {deficiency}), {team} would clear their hard cap; with it, "
                             f"they don't.{real_note}"),
                ))
            else:
                checks.append(CheckResult(
                    check=f"empty_roster_charge_{team.lower()}", passed=True, level="warning",
                    message=(f"{team}: {after} standard players after trade — {deficiency} slot(s) below the "
                             f"{ROSTER_MIN}-player trade-legality floor (§ 2.1a). For hard-cap comparison purposes "
                             f"only, the trade math assumes those slots get filled at the rookie minimum — a "
                             f"hypothetical ${charge:,} (${rookie_min:,} × {deficiency}), already folded into the "
                             f"hard-cap figures above.{real_note}"),
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

    # ── Stepien Rule (§ 7.2) ────────────────────────────────────────────────────
    checks.extend(_check_stepien_rule(details))

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

    if team and details.signing_method == "bae":
        cl = ctx["cap_levels"].get(season, {})
        ex_holds_now = _compute_team_salary_ex_holds(team, bios, season)
        r = _check_bae_eligibility(team, ex_holds_now, cl, ctx["team_state"], season)
        if r:
            checks.append(r)

    if team:
        r = _roster_size_check(team, _count_standard_roster(team) + 1,
                               "converting this two-way contract")
        if r:
            checks.append(r)

    r = _check_contract_raises(details.contract, bird_pct=False, cur_season=season)
    if r:
        checks.append(r)

    return checks


_VALIDATORS = {
    "sign":           _validate_sign,
    "release":        _validate_release,
    "renounce":       _validate_renounce,
    "rescind_renounce": _validate_rescind_renounce,
    "trade":          _validate_trade,
    "option":         _validate_option,
    "guarantee":      _validate_guarantee,
    "pick":           _validate_pick,
    "convert_twoway": _validate_convert_twoway,
    "offer_sheet":    _validate_offer_sheet,
    "offer_sheet_decision": _validate_offer_sheet_decision,
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
    stored_details = details.model_dump()
    if details.team:
        if details.team.upper() not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"Unknown team: {details.team!r}")
        stored_details["team"] = details.team.upper()
    return _append_historical("option", stored_details, body, info)


# ── Transaction routes ────────────────────────────────────────────────────────

@router.post("/api/transactions")
def create_transaction(body: TransactionIn, info: dict = Depends(require_role("rosters"))):
    try:
        datetime.strptime(body.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date; use YYYY-MM-DD")

    if body.type not in ("sign", "pick", "option", "guarantee", "release", "renounce", "rescind_renounce", "trade", "convert_twoway", "sign_pick", "void_player", "set_hard_cap_level", "offer_sheet", "offer_sheet_decision"):
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
        "rescind_renounce": (RescindRenounceDetails, "Invalid rescind_renounce details"),
        "trade":          (TradeIn,               "Invalid trade details"),
        "convert_twoway": (ConvertTwoWayDetails,  "Invalid convert_twoway details"),
        "sign_pick":      (SignPickDetails,       "Invalid sign_pick details"),
        "void_player":       (VoidPlayerDetails,  "Invalid void_player details"),
        "set_hard_cap_level": (SetHardCapDetails, "Invalid set_hard_cap_level details"),
        "offer_sheet":       (OfferSheetDetails,  "Invalid offer_sheet details"),
        "offer_sheet_decision": (OfferSheetDecisionDetails, "Invalid offer_sheet_decision details"),
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
        "txn_date":    body.date,
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
            team, dead_cap, terminated_salary = _apply_release(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
            stored_details["dead_cap"] = dead_cap
            stored_details["terminated_salary"] = terminated_salary
        elif body.type == "renounce":
            team, snapshot = _apply_renounce(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
            # The restore source for rescind_renounce (§ 3.10). Stored at the
            # moment of the event rather than reconstructed from the ledger
            # later — a renounce erases the very fields a replay would need.
            stored_details["_snapshot"] = snapshot
        elif body.type == "rescind_renounce":
            team, rescinded_player = _apply_rescind_renounce(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
            stored_details["player"] = rescinded_player
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
        elif body.type == "offer_sheet":
            offering_team, retaining_team = _apply_offer_sheet(details, body.date, info, txn_id=txn_id)
            stored_details = details.model_dump()
            stored_details["teams"] = [offering_team, retaining_team]
            # Resolved server-side from the roster, not submitted — the incumbent
            # is a fact about who holds the RFA rights, not a claim the offering
            # team gets to make. _open_offer_sheets reads it back from here.
            stored_details["retaining_team"] = retaining_team
            stored_details["deadline"] = _offer_deadline(body.date)
        elif body.type == "offer_sheet_decision":
            resolved = _apply_offer_sheet_decision(details, body.date, info, txn_id=txn_id)
            stored_details = details.model_dump()
            stored_details.update(resolved)

        forced_checks = [c.check for c in failed] if (body.force and failed) else None
        if forced_checks:
            stored_details["_forced_checks"] = forced_checks

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

    # Outside the lock and off the request thread — the roster write and ledger
    # append are already committed, so a Discord problem must not delay or fail
    # them. Historical backfills never reach here (they return far above).
    notify_transaction(txn, forced_checks)
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
        after = before - out_std + in_std
        deficiency, charge = _empty_roster_charge(after, season, ctx["cap_levels"])
        # Which hard cap (if any) this team is actually locked to (§ 1.4) — the
        # sim needs this to show the *correct* apron, not always "1st Apron"
        # regardless of whether the team is capped at the 1st, the 2nd, or not
        # hard-capped at all (only the league-wide Hard Cap then applies).
        ts = get_season_state(ctx["team_state"], team, season)
        hard_cap_level = ts.get("hard_cap")
        cl_season = ctx["cap_levels"].get(season, {})
        hard_cap_limit = (cl_season.get("apron1") if hard_cap_level == "first_apron"
                           else cl_season.get("apron2") if hard_cap_level == "second_apron"
                           else None)

        # Salary-matching headroom, computed with the same tier branch
        # _check_salary_matching uses (§ 4.2 tiers below the First Apron, the
        # flat outgoing + $250K limit at/above it) so an exported trade sheet
        # can show "Max Incoming" without a second copy of the rule.
        apron1_lvl = cl_season.get("apron1")
        apron2_lvl = cl_season.get("apron2")
        if apron1_lvl is None:
            max_incoming = None
        elif current_ex_holds >= apron1_lvl:
            max_incoming = out + 250_000
        else:
            max_incoming = _salary_match_limit(out)

        # The largest single outgoing salary — what this team could send without
        # aggregating. If the incoming total needs more than that one leg
        # supports, the deal is only possible by combining salaries (§ 4.4).
        leg_salaries = [
            _parse_dollar((bios.get(s, {}).get("salaries") or {}).get(season, ""))
            for s in out_players.get(team, [])
        ]
        unaggregated = max(leg_salaries) if leg_salaries else 0
        if apron1_lvl is None:
            unaggregated_max_incoming = None
            needs_aggregation = False
        else:
            unaggregated_max_incoming = (unaggregated + 250_000
                                          if current_ex_holds >= apron1_lvl
                                          else _salary_match_limit(unaggregated))
            needs_aggregation = len(leg_salaries) >= 2 and inc > unaggregated_max_incoming

        projected_ex_holds = current_ex_holds - out + inc + charge
        apron_after = ("second_apron" if apron2_lvl is not None and projected_ex_holds >= apron2_lvl
                       else "first_apron" if apron1_lvl is not None and projected_ex_holds >= apron1_lvl
                       else None)

        teams[team] = {
            "team": team,
            "current_salary": current,
            "current_salary_ex_holds": current_ex_holds,
            "outgoing_salary": out,
            "incoming_salary": inc,
            # Empty Roster Charge (§ 2.1a) is folded in here too, so the sim's
            # displayed "New salary" / apron-room figures match exactly what
            # _validate_trade judged — no parallel, divergent cap math.
            "projected_salary": current - out + inc + charge,
            "projected_salary_ex_holds": projected_ex_holds,
            "players_out": out_players.get(team, []),
            "players_in": in_players.get(team, []),
            "standard_count_before": before,
            "standard_count_after": after,
            "empty_roster_deficiency": deficiency,
            "empty_roster_charge": charge,
            "hard_cap_level": hard_cap_level,
            "hard_cap_limit": hard_cap_limit,
            "max_incoming": max_incoming,
            "unaggregated_outgoing": unaggregated,
            "unaggregated_max_incoming": unaggregated_max_incoming,
            "needs_aggregation": needs_aggregation,
            "apron_after": apron_after,
        }

    return {
        "season": season,
        "cap_levels": ctx["cap_levels"].get(season, {}),
        "teams": teams,
    }


def _validation_ctx() -> dict:
    """The read-only context every `/api/validate/*` endpoint hands its
    validator. Identical in shape to what the submit path builds, so the
    simulator judges against exactly the same live data — but nothing here
    opens a write path or takes the API lock."""
    return {
        "bios":       load_player_bios(),
        "team_state": load_team_state(),
        "cap_levels": json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {},
        "cur_season": _current_league_year(),
        "txn_date":   datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "trade_exceptions": load_trade_exceptions(),
    }


def _validation_result(checks: list[CheckResult], fact_sheet: dict) -> TransactionValidationResult:
    # Warnings (e.g. a single-leg trade, an advisory § 3.11 max) don't make a
    # transaction illegal — only errors do, matching the submit path where
    # warnings are force-through-able.
    return TransactionValidationResult(
        legal=not any(not c.passed and c.level == "error" for c in checks),
        checks=checks,
        fact_sheet=fact_sheet,
    )


def _require_validatable(team: str, player: str, ctx: dict) -> None:
    """Reject input the validators would silently sail through.

    A validator handed an unknown team reads its salary as $0 and every check
    passes vacuously — the simulator would print a confident "LEGAL" for a
    transaction it never actually evaluated. A wrong verdict is worse than an
    error, so these fail loudly instead of scoring.
    """
    if team.upper() not in VALID_TEAMS:
        raise HTTPException(400, f"Unknown team '{team}'.")
    if player not in ctx["bios"]:
        raise HTTPException(400, f"Unknown player '{player}'.")


def _signing_fact_sheet(team: str, player: str, contract, ctx: dict, *,
                        signing_method: Optional[str],
                        bird_rights_type: Optional[str] = None,
                        eaps_assumption: Optional[str] = None,
                        adds_roster_spot: bool = True) -> dict:
    """Financial snapshot for a signing, rendered by the simulator alongside
    the checks. Every figure here is produced by the same helpers
    `_validate_sign` uses (`_signee_existing_hold`, `_resolve_mle_bucket`,
    `_compute_team_salary*`), so the sheet can't show a team room the
    validator didn't credit it with.
    """
    bios = ctx["bios"]; season = ctx["cur_season"]
    cl = ctx["cap_levels"].get(season, {})

    current = _compute_team_salary(team, bios, season)
    current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
    existing_hold, is_fa_hold = _signee_existing_hold(team, player, bios, season)
    new_sal = _parse_dollar((contract.salaries or {}).get(season, ""))

    projected_ex_holds = current_ex_holds - (0 if is_fa_hold else existing_hold) + new_sal
    # What the § 3.1 cap-room test actually measures against: Team Salary with
    # holds included (§ 3.10), net of the signee's own hold.
    salary_before_net = current - existing_hold
    cap = cl.get("cap")
    cap_room = (cap - salary_before_net) if cap is not None else None
    # Free-agent holds still on the books for *other* players — the figure the
    # funding check cites when it explains that renouncing would create room.
    other_holds = (current - current_ex_holds) - (existing_hold if is_fa_hold else 0)

    ts = get_season_state(ctx["team_state"], team, season)
    hard_cap_level = ts.get("hard_cap")
    hard_cap_limit = (cl.get("apron1") if hard_cap_level == "first_apron"
                      else cl.get("apron2") if hard_cap_level == "second_apron"
                      else None)
    apron1, apron2 = cl.get("apron1"), cl.get("apron2")

    def _apron_at(salary: int) -> Optional[str]:
        if apron2 is not None and salary >= apron2:
            return "second_apron"
        if apron1 is not None and salary >= apron1:
            return "first_apron"
        return None

    # Remaining balance in whichever exception bucket this method draws on.
    exception = None
    bucket = _resolve_mle_bucket(signing_method, ts)
    if bucket:
        _resolved, amount_key, label = bucket
        amount = cl.get(amount_key)
        if amount is not None:
            used = ts.get("mle_used") or 0
            exception = {
                "label": label, "amount": amount,
                "used": used, "remaining": amount - used,
            }

    before = _count_standard_roster(team)
    is_standard = (contract.type != "two-way")
    after = before + (1 if (is_standard and adds_roster_spot) else 0)

    trailing_hold = _preview_fa_hold(
        bios.get(player) or {}, team, contract, ctx["cap_levels"],
        bird_rights_type=bird_rights_type, eaps_assumption=eaps_assumption,
        slug=player,
    )

    return {
        "season": season,
        "cap_levels": cl,
        "team": team,
        "player": player,
        "player_name": (bios.get(player, {}) or {}).get("name", player),
        "signing_method": signing_method,
        "bird_rights_type": bird_rights_type,
        "contract_type": contract.type,
        "salaries": dict(contract.salaries or {}),
        "cap_holds": dict(contract.cap_holds or {}),
        "trailing_hold": trailing_hold,
        "new_salary": new_sal,
        "current_salary": current,
        "current_salary_ex_holds": current_ex_holds,
        "existing_hold": existing_hold,
        "existing_hold_is_fa": is_fa_hold,
        "unrenounced_other_holds": other_holds,
        "salary_before_net_of_hold": salary_before_net,
        "projected_salary_ex_holds": projected_ex_holds,
        "cap_room": cap_room,
        "hard_cap_level": hard_cap_level,
        "hard_cap_limit": hard_cap_limit,
        "apron_before": _apron_at(current_ex_holds),
        "apron_after": _apron_at(projected_ex_holds),
        "exception": exception,
        "standard_count_before": before,
        "standard_count_after": after,
    }


@router.post("/api/validate/trade")
def validate_trade(body: TradeValidateInput):
    ctx = _validation_ctx()
    return _validation_result(_validate_trade(body, ctx), _trade_fact_sheet(body, ctx))


@router.post("/api/validate/sign")
def validate_sign(body: SignDetails):
    """Non-mutating § 3.1–§ 3.13 check of a free-agent signing, for the
    transaction simulator. Shares `_validate_sign` with `POST
    /api/transactions`, so a "legal" verdict here is what the office accepts —
    but this endpoint never writes: no roster, bio, team-state or ledger
    change, and no auth, exactly like `/api/validate/trade`."""
    ctx = _validation_ctx()
    _require_validatable(body.team, body.player, ctx)
    fact_sheet = _signing_fact_sheet(
        body.team.upper(), body.player, body.contract, ctx,
        signing_method=body.signing_method,
        bird_rights_type=body.bird_rights_type,
        eaps_assumption=body.eaps_assumption,
    )
    return _validation_result(_validate_sign(body, ctx), fact_sheet)


@router.get("/api/offer-sheets/open")
def list_open_offer_sheets(team: Optional[str] = None):
    """Offer sheets extended but not yet resolved (§ 3.15). Public.

    This endpoint is the guard that makes splitting the offer from the decision
    safe. Before the split, an offer with no follow-up was indistinguishable from
    a completed one and simply vanished — the player sat on nothing but their old
    RFA hold, silently. Every surface that shows a pending offer reads this.

    `overdue` marks an offer past its 48-hour window. Nothing auto-resolves:
    silently moving a real player on a timer is worse than a late decision, so an
    expired offer nags until a human settles it.
    """
    today = _current_league_year_today()
    offers = _open_offer_sheets()
    if team:
        t = team.upper()
        offers = [o for o in offers if t in (o["offering_team"], o["retaining_team"])]
    bios = load_player_bios()
    return [
        {**o,
         "player_name": (bios.get(o["player"]) or {}).get("name") or o["player"],
         "overdue": bool(o["deadline"] and o["deadline"] < today)}
        for o in offers
    ]


def _current_league_year_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@router.post("/api/validate/offer_sheet_decision")
def validate_offer_sheet_decision(body: OfferSheetDecisionDetails):
    """Non-mutating § 3.15 check of resolving an open offer sheet. Shares its
    validator with the submit path."""
    ctx = _validation_ctx()
    offer = next((o for o in _open_offer_sheets() if o["id"] == body.offer_id), None)
    if offer is None:
        raise HTTPException(400, f"No open offer sheet with id '{body.offer_id}'.")
    return _validation_result(
        _validate_offer_sheet_decision(body, ctx),
        {"offer": offer, "outcome": body.outcome,
         "signing_team": offer["retaining_team"] if body.outcome == "matched" else offer["offering_team"]},
    )


@router.post("/api/validate/renounce")
def validate_renounce(body: RenounceDetails):
    """Non-mutating § 3.10 check of a renounce. Shares `_validate_renounce` and
    `_renounce_eligibility` with `POST /api/transactions`, so the verdict here is
    what the office accepts — and, more to the point, what the roster page's
    confirmation dialog shows an owner before they commit. Never writes, no auth,
    on the same terms as `/api/validate/sign`."""
    ctx = _validation_ctx()
    if body.player not in ctx["bios"]:
        raise HTTPException(400, f"Unknown player '{body.player}'.")
    return _validation_result(_validate_renounce(body, ctx), _renounce_fact_sheet(body, ctx))


class SelfRenounceIn(BaseModel):
    player: str
    description: str = ""


@router.post("/api/self/renounce")
def self_renounce(body: SelfRenounceIn, info: dict = Depends(get_token_info)):
    """Owner-initiated renounce from their own team's roster page (§ 3.10).

    Deliberately narrower than `POST /api/transactions`, which stays on the
    `rosters` role. Three properties make this safe to expose to owners:

      * **The team is derived, never supplied.** It's resolved from the player's
        actual roster row, then matched against the caller's owner tenure — a
        caller cannot name a team to claim authority over.
      * **No force.** Error-level checks are fatal here, full stop. The whole
        point of an owner-facing write is that it can't be pushed past a rule.
      * **Server-stamped date.** No backdating a transaction into a prior league
        year to dodge a cap position.

    Everything else — validation, application, ledger append, snapshot — goes
    through the same code the office path uses.
    """
    player = body.player
    bios = load_player_bios()
    if player not in bios:
        raise HTTPException(status_code=404, detail=f"Unknown player '{player}'.")

    team = _build_team_map().get(player)
    if not team:
        raise HTTPException(status_code=422, detail=f"{player!r} is not on any roster.")
    if not is_team_owner(info, team):
        raise HTTPException(
            status_code=403,
            detail=f"Only {team}'s owner can renounce {team} players.",
        )

    txn_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    details = RenounceDetails(player=player)
    ctx = {
        "bios":       bios,
        "team_state": load_team_state(),
        "cap_levels": json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {},
        "cur_season": _season_for_date(txn_date),
        "txn_date":   txn_date,
        "trade_exceptions": load_trade_exceptions(),
    }
    checks = _validate_renounce(details, ctx)
    if any(not c.passed and c.level == "error" for c in checks):
        raise HTTPException(status_code=422, detail={
            "validation": True,
            "checks": [c.model_dump() for c in checks],
            "can_force": False,
        })

    txn_id = secrets.token_hex(8)
    with _txn_lock:
        applied_team, snapshot = _apply_renounce(details, txn_date, info)
        txn = {
            "id": txn_id,
            "type": "renounce",
            "date": txn_date,
            "created_by": info.get("name", "unknown"),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": body.description or f"{applied_team} renounce",
            "details": {
                "player": player,
                "team": applied_team,
                "_snapshot": snapshot,
                # Marks the transaction as owner self-serve rather than
                # office-entered, so the ledger shows who actually pulled the
                # trigger even though created_by already names them.
                "_source": "owner_self_serve",
            },
        }
        _append_transaction(txn)
    notify_transaction(txn)
    return {"ok": True, "transaction": txn, "checks": [c.model_dump() for c in checks]}


@router.post("/api/validate/offer_sheet")
def validate_offer_sheet(body: OfferSheetDetails):
    """Non-mutating check of an RFA offer sheet being *extended* (§ 3.15).

    Judged against the **offering** team, matching `_validate_offer_sheet`:
    at offer time the incumbent hasn't decided, and the offering team is the
    one committing the money and carrying the pending hold. The incumbent's
    side is judged separately by `/api/validate/offer_sheet_decision`.

    This endpoint used to read `body.outcome`, left over from the era when an
    offer and its decision were one transaction. `OfferSheetDetails` dropped
    that field in the split, so every call raised AttributeError and returned
    500 — the simulator's offer-sheet mode included. Non-mutating on the same
    terms as `/api/validate/sign`.
    """
    ctx = _validation_ctx()
    _require_validatable(body.offering_team, body.player, ctx)
    resolved = _offer_sheet_signing_team(body)
    if resolved is None:
        # No resolvable signing team. _validate_offer_sheet returns no checks
        # here (the submit path hard-fails it instead), so reporting the usual
        # verdict would read as "legal" off zero checks — say plainly that
        # nothing could be evaluated.
        reason = (
            "Can't evaluate this offer sheet: the player must currently sit on a team's "
            "roster as their restricted free agent, and the offering team must be a real team."
        )
        return TransactionValidationResult(
            legal=False,
            checks=[CheckResult(check="offer_sheet_resolvable", passed=False,
                                level="error", message=reason)],
            fact_sheet={"unresolved": True, "reason": reason},
        )
    team, retaining_team = resolved
    fact_sheet = _signing_fact_sheet(
        team, body.player, body.contract, ctx,
        signing_method=body.signing_method,
        bird_rights_type=body.bird_rights_type,
        eaps_assumption=body.eaps_assumption,
        # Mirrors the roster_size branch in _validate_offer_sheet, which counts
        # the body unconditionally: a team shouldn't extend an offer it has no
        # room to honour, and the incumbent's choice can't create room. The
        # fact sheet has to agree with the validator, not second-guess it.
        adds_roster_spot=True,
    )
    fact_sheet.update({
        "offering_team": body.offering_team.upper(),
        "retaining_team": retaining_team,
        "signing_team_role": "offering — pending the incumbent's decision",
    })
    return _validation_result(_validate_offer_sheet(body, ctx), fact_sheet)

"""Draft-pick rules for trades: conveyance registration, ownership, swaps, protections, Stepien, advance limits.

Moved out of routers/transactions.py; see routers/txn/__init__.py."""
import os
import re
from ..roster_picks import _all_picks_flat, _pick_year_horizon, load_picks
from fastapi import HTTPException
from .models import CheckResult, TradeIn
import logging

# Same logger name as before the move, so log filters keep matching.
logger = logging.getLogger("routers.transactions")


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


def _register_pick_swap(asset, from_team: str, to_team: str,
                        txn_id: str | None = None, txn_date: str | None = None) -> None:
    """Write a trade's `swap` asset as a two-team swap group. Not fail-open
    like the registrations above: the group is the whole of a swap asset, so
    losing it would record a trade that did nothing. `_pick_swap_problems`
    has already passed by the time this runs, so a failure here is a bug."""
    from picks_conveyance import from_trade
    try:
        from_trade.register_pick_swap(asset.year, asset.round, from_team, asset.orig,
                                      to_team, asset.swap_orig,
                                      txn_id=txn_id, txn_date=txn_date)
    except Exception as e:
        logger.exception(f"picks_conveyance: failed to register pick swap {asset!r}")
        raise HTTPException(status_code=500,
                            detail=f"Couldn't record the {asset.year} R{asset.round} pick swap: {e}")


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


def _trade_ladder_protection(pick_key: tuple, from_team: str, to_team: str,
                             top: int | None, txn_id: str | None, txn_date: str | None) -> None:
    """Write a ladder keeper's protection trade: removed (the ladder goes,
    via `registry.handle_retrade`) or lowered (`registry.lower_ladder`).
    Not fail-open: the ladder is the whole asset, so a trade that didn't
    change it would record something that never happened."""
    from picks_conveyance import registry
    txn_entry = {"id": txn_id, "date": txn_date} if txn_id else None
    try:
        if top is None:
            registry.handle_retrade(pick_key, from_team, to_team,
                                    {"type": "settled", "team": to_team},
                                    txn_id=txn_id, txn_date=txn_date)
        else:
            registry.lower_ladder(pick_key, top, txn_entry=txn_entry)
    except Exception as e:
        logger.exception(f"picks_conveyance: ladder protection trade failed for {pick_key}")
        raise HTTPException(status_code=500,
                            detail=f"Couldn't record the protection change on {pick_key}: {e}")


def _ladder_keeper(asset, from_team: str, to_team: str):
    """(ladder, step) when this pick asset is a ladder's keeper sending its
    protection to the team owed the pick, else None. That removes the
    protection, or lowers it when the asset carries a smaller `protection`.
    The keeper holds no claim the ownership model sees (a ladder is not a
    leaf), so this is how a trade names it."""
    if getattr(asset, "type", None) != "pick" or not asset.orig:
        return None
    try:
        from picks_conveyance import registry
        found = registry.ladder_for((asset.year, asset.round, asset.orig.upper()))
    except Exception:
        logger.exception("ladder lookup failed")
        return None
    if found and found[0].get("from") == from_team.upper() and found[0].get("to") == to_team.upper():
        return found
    return None


def _normalize_pick_bands(pick_key: tuple) -> bool:
    """Merge a pick's neighbouring bands held by one team, after a trade
    moved or split one (`registry.normalize_protected`). True when one team
    now holds the whole pick, so the protection is gone: that is a
    protection lifted by trade. Fails open like the registrations above."""
    try:
        from picks_conveyance import registry
        return registry.normalize_protected(pick_key) is not None
    except Exception:
        logger.exception(f"picks_conveyance: failed to normalize bands for {pick_key}")
        return False


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


def _trade_leg_ownership_problems(details: TradeIn, bios: dict, team_map: dict,
                                  pick_index: dict, conveyance_store) -> list[str]:
    """Every reason a leg in `details.transfers` can't actually be conveyed by
    its stated `from_team` right now: an unknown/moved player, a pick no
    longer owned by that team, already drafted, frozen, or blocked by
    conveyance structure (`_check_retrade_allowed`). Pure — raises nothing, so
    both `_apply_trade` (fail on the first entry) and TRC's live staleness
    check (report all of them) can use it. This is deliberately NOT part of
    `_validate_trade`/`/api/validate/trade` — that endpoint has never checked
    current asset ownership, only cap/roster legality, and a trade whose
    named player has since left that roster still comes back "legal" there.
    Order matches what `_apply_trade` used to check inline before this call
    replaced it."""
    problems: list[str] = []
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                if asset.slug not in bios:
                    problems.append(f"Unknown player: {asset.slug!r}")
                    continue
                actual_team = team_map.get(asset.slug)
                if actual_team != xfer.from_team:
                    problems.append(
                        f"Player {asset.slug!r} is on {actual_team or 'no team'}, not {xfer.from_team}")
                continue
            if asset.type == "swap":
                continue
            key = (asset.year, asset.round, asset.orig)
            pick_row = pick_index.get(key)
            if not pick_row:
                problems.append(f"Pick {asset.year} R{asset.round} {asset.orig} not found")
                continue
            keeper = _ladder_keeper(asset, xfer.from_team, xfer.to_team)
            owned_by_from_team = keeper is not None or _check_pick_ownership(
                key, xfer.from_team, pick_row, conveyance_store, asset.leaf_id)
            if not owned_by_from_team:
                problems.append(
                    f"Pick {asset.year} R{asset.round} {asset.orig} is owned by "
                    f"{pick_row['OWNER']}, not {xfer.from_team}")
                continue
            # Once a pick has been used to draft a player it's historical
            # record, not a live asset — nothing upstream (ownership, frozen,
            # legacy/leaf checks) inspects PLAYER, so without this a trade
            # could "convey" an already-drafted pick: it'd pass every other
            # check (a drafted pick still resolves to a `settled` node some
            # team technically "owns") and write a transaction for an asset
            # that no longer exists to trade.
            drafted_player = (pick_row.get("PLAYER") or "").strip()
            if drafted_player:
                problems.append(
                    f"Pick {asset.year} R{asset.round} {asset.orig} was already used "
                    f"to draft {drafted_player} and can no longer be traded")
                continue
            if pick_row.get("FROZEN", "").strip().upper() == "TRUE":
                reason = pick_row.get("FROZEN_REASON", "").strip()
                detail = f"Pick {asset.year} R{asset.round} {asset.orig} is frozen and cannot be traded"
                if reason:
                    detail += f": {reason}"
                problems.append(detail)
                continue
            if keeper is not None:
                continue
            try:
                _check_retrade_allowed(key, asset, xfer.from_team, conveyance_store)
            except HTTPException as e:
                problems.append(str(e.detail))
    problems.extend(_pick_swap_problems(details, pick_index, conveyance_store))
    problems.extend(_pick_protection_problems(details))
    return problems


def _swap_assets(details: TradeIn):
    """(from_team, to_team, asset) for every `swap` asset in the trade."""
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "swap":
                yield xfer.from_team.upper(), xfer.to_team.upper(), asset


def _pick_swap_problems(details: TradeIn, pick_index: dict,
                        conveyance_store) -> list[str]:
    """Every reason a `swap` asset can't be written. Each team must hold its
    own pick outright: one owner, not drafted, not frozen, and no structure on
    it already (another swap, a protection, a ladder). A swap layered on a
    contingent pick has no single meaning, so it is refused rather than
    guessed at. Pure, like `_trade_leg_ownership_problems`, which calls it."""
    problems: list[str] = []
    ladder_keys: set[tuple] = set()
    try:
        from picks_conveyance import registry
        for ladder in registry.load_registry().get("ladders", []):
            ladder_keys |= registry._ladder_keys(ladder)
    except Exception:
        logger.exception("pick swap check: could not read ladders")
    for from_t, to_t, asset in _swap_assets(details):
        if not (asset.year and asset.round and asset.orig and asset.swap_orig):
            problems.append("A pick swap needs a year, a round and both picks")
            continue
        a_orig, b_orig = asset.orig.upper(), asset.swap_orig.upper()
        label = f"the {asset.year} R{asset.round} swap ({a_orig} / {b_orig})"
        if a_orig == b_orig:
            problems.append(f"{label} names the same pick twice")
            continue
        for team, orig in ((from_t, a_orig), (to_t, b_orig)):
            key = (asset.year, asset.round, orig)
            row = pick_index.get(key)
            pick_label = f"{asset.year} R{asset.round} {orig}"
            if not row:
                problems.append(f"Pick {pick_label} not found")
                continue
            if (row.get("PLAYER") or "").strip():
                problems.append(f"Pick {pick_label} was already used and can't be swapped")
                continue
            if row.get("FROZEN", "").strip().upper() == "TRUE":
                problems.append(f"Pick {pick_label} is frozen and can't be swapped")
                continue
            node = None
            if conveyance_store is not None:
                try:
                    node = _lookup_conveyance_node(key, conveyance_store)
                except Exception:
                    node = None
            if node is not None:
                outright = node.get("type") == "settled" and node.get("team") == team
            else:
                outright = row.get("OWNER", "").upper() == team or (
                    row.get("OWNER", "") == "?" and orig == team)
            if not outright:
                problems.append(
                    f"{team} doesn't hold pick {pick_label} outright, so it can't be part "
                    f"of {label}. A swap can only be made between two picks that each have "
                    "one owner and no protection or swap on them already")
                continue
            if key in ladder_keys:
                problems.append(f"Pick {pick_label} is tied to a protection ladder and can't be swapped")
    return problems


def _pick_protection_problems(details: TradeIn) -> list[str]:
    """Every reason a new protection on an already-protected pick can't be
    written. That is how a protection is lowered by trade: the team holding
    the protected band sends it with a smaller threshold, keeping the top of
    the band and conveying the rest (`registry.subdivide_protected_band`).
    The split needs that team to hold exactly one band, and the threshold to
    fall inside it. Without this check the registration fails open after
    the trade is applied, and the pick's owner moves with no band behind it.
    A pick with no protection yet takes any threshold, as before."""
    problems: list[str] = []
    try:
        from picks_conveyance import registry
    except Exception:
        return problems
    for xfer in details.transfers:
        from_t = xfer.from_team.upper()
        for asset in xfer.assets:
            if asset.type != "pick" or asset.protection is None or not asset.orig:
                continue
            key = (asset.year, asset.round, asset.orig.upper())
            label = f"{asset.year} R{asset.round} {asset.orig.upper()}"
            keeper = _ladder_keeper(asset, from_t, xfer.to_team)
            if keeper is not None:
                top = int(keeper[1].get("protect_top") or 0)
                if not (1 <= asset.protection < top):
                    problems.append(
                        f"Pick {label} is top-{top} protected, so a lowered protection must be "
                        f"from 1 to {top - 1}, not {asset.protection}")
                continue
            try:
                spec = registry.get_protected_spec(key)
            except Exception:
                logger.exception(f"pick protection check: could not read {key}")
                continue
            if spec is None:
                continue
            mine = [b for b in spec["bands"] if b.get("to") == from_t]
            if len(mine) != 1:
                problems.append(
                    f"{from_t} holds {len(mine)} bands of pick {label}, so it can't set a new "
                    "protection on it. Send the band as it is instead")
                continue
            band = mine[0]
            if not (band["min"] <= asset.protection < band["max"]):
                problems.append(
                    f"Pick {label}: {from_t}'s protection covers picks {band['min']}-{band['max']}, "
                    f"so a new protection must be from {band['min']} to {band['max'] - 1}, "
                    f"not {asset.protection}")
    return problems


def _describe_pick_claims(details: TradeIn) -> None:
    """Set each pick asset's `claim` from the registry as it stands before
    the trade: what the sending team gives up when it holds only one band of
    a protected pick. The team the rest of the pick belongs to receiving the
    band is a protection removed, or lowered when a smaller threshold is
    sent. Anyone else receiving it gets those draft slots. Clears `claim` on
    every other pick asset."""
    try:
        from picks_conveyance import registry
    except Exception:
        registry = None
    for xfer in details.transfers:
        from_t, to_t = xfer.from_team.upper(), xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type != "pick":
                continue
            asset.claim = None
            if registry is None or not asset.orig:
                continue
            keeper = _ladder_keeper(asset, from_t, to_t)
            if keeper is not None:
                asset.claim = ("protection removed" if asset.protection is None
                               else f"protection lowered to top {asset.protection}")
                continue
            try:
                spec = registry.get_protected_spec((asset.year, asset.round, asset.orig.upper()))
            except Exception:
                continue
            if spec is None:
                continue
            mine = [b for b in spec["bands"] if b.get("to") == from_t]
            if len(mine) != 1:
                continue
            band = mine[0]
            rest = {b.get("to") for b in spec["bands"] if b is not band}
            top = asset.protection
            if rest == {to_t}:
                if top is None:
                    asset.claim = "protection removed"
                elif band["min"] in (1, 31) and asset.round == 1:
                    asset.claim = f"protection lowered to top {top}"
                else:
                    asset.claim = f"protection lowered to picks {band['min']}-{top}"
            else:
                lo = band["min"] if top is None else top + 1
                asset.claim = f"picks {lo}-{band['max']}"


def _check_pick_protections(details: TradeIn) -> list[CheckResult]:
    """`_pick_protection_problems` as checks, for /api/validate/trade."""
    return [CheckResult(check="pick_protection", passed=False, level="error", message=m)
            for m in _pick_protection_problems(details)]


def _check_pick_swaps(details: TradeIn) -> list[CheckResult]:
    """`_pick_swap_problems` as checks, so /api/validate/trade (and the trade
    builder behind it) reports a swap that can't be written before anyone
    submits it. Ownership is otherwise left out of that endpoint, but a swap
    means nothing unless each side holds its pick."""
    swaps = list(_swap_assets(details))
    if not swaps:
        return []
    picks = load_picks()
    pick_index = {(int(p["YEAR"]), int(p["ROUND"]), p["ORIG"].upper()): p for p in picks}
    problems = _pick_swap_problems(details, pick_index, _load_conveyance_store_for_shadow_check())
    if problems:
        return [CheckResult(check="pick_swap", passed=False, level="error", message=m)
                for m in problems]
    return [CheckResult(
        check="pick_swap", passed=True,
        message=(f"{to_t} can swap its {a.year} R{a.round} pick ({a.swap_orig.upper()}) for "
                 f"{from_t}'s ({a.orig.upper()}): {to_t} gets the better pick, {from_t} the worse."))
        for from_t, to_t, a in swaps]


_STEPIEN_MAX_SCENARIOS = 4096


def _check_stepien_rule(details: TradeIn, all_picks: list[dict] | None = None) -> list[CheckResult]:
    """§ 7.2 Stepien Rule: a trade is illegal if, after it, a team could end
    up with no first-round pick in two consecutive draft years. "Could" is
    the point: the rule has to hold however every protection resolves, so a
    claim only counts in the outcomes where the team actually gets the pick.
    Any first-round pick counts, own or acquired.

    Ownership is what `GET /api/picks` serves (`_all_picks_flat`), modeled
    per pick as one of:

      - "bands": the pick goes to exactly one team, chosen by where it lands.
        A settled pick is one band (1-60). A protected pick's bands come from
        its `leaves`. A pipe-joined owner with no leaves to say how it splits
        (the flat-CSV fallback) is a band per team at unknown positions.
      - "group": a row with a `group_id` (a swap group or binary chain). Every
        team holding a leaf gets exactly one of the group's picks that year,
        whatever the order, so each counts as certain. Every row of a group is
        the SAME claim (see `registry.handle_retrade`): trading one row conveys
        the team's whole interest in all of them, never "one leg, keeping the
        other".

    A pick with `ladder_fallback_of` goes to `to` when its governing pick
    lands inside `protect_top`, and to its own owner otherwise. Both picks
    resolve from the governing pick's one landing spot, so they are scored as
    one outcome, not independently: a team that either keeps the protected
    pick or keeps the fallback is never charged with losing both.

    The trade is simulated on that model: a plain retrade moves the team's
    claim (or just the `leaf_id` band); `protection` splits the team's band
    at the threshold, the way `from_trade.register_protection` does;
    `swap_with` turns the pick and its counterpart into a two-team group, as
    `register_swap` does; `ladder_fallback` links the fallback picks to this
    one. Then each team in the trade is checked against every combination of
    outcomes that touches it. Past `_STEPIEN_MAX_SCENARIOS` combinations it
    falls back to counting only certain claims, which can only be stricter.

    `all_picks` is injectable (defaults to the real `_all_picks_flat()`)
    so tests can run against synthetic picks — see `tests/test_stepien_rule.py`.
    """
    import itertools
    import re

    if not any(a.type in ("pick", "swap") and a.round == 1
               for x in details.transfers for a in x.assets):
        return []
    if all_picks is None:
        all_picks = _all_picks_flat()

    # key -> {"kind": "bands", "bands": [[lo, hi, team], ...]}
    #      | {"kind": "group", "gid": gid}
    picks: dict[tuple[int, str], dict] = {}
    groups: dict[str, set[str]] = {}          # gid -> teams certain of one pick
    group_members: dict[str, list[tuple[int, str]]] = {}
    fallbacks: dict[tuple[int, str], dict] = {}  # key -> {gov, top, to}
    band_re = re.compile(r"^protected band (\d+)-(\d+)$")

    for p in all_picks:
        if p.get("round") != 1 or p.get("player"):
            continue
        key = (p["year"], p["orig"].upper())
        owner = p.get("owner") or "?"
        teams = [p["orig"].upper()] if owner == "?" else owner.split("|")
        leaves = p.get("leaves") or []
        gid = p.get("group_id")
        if gid:
            leaf_teams = {l["team"] for l in leaves}
            groups.setdefault(gid, set()).update(leaf_teams or teams)
            group_members.setdefault(gid, []).append(key)
            picks[key] = {"kind": "group", "gid": gid}
        else:
            bands = []
            for l in leaves:
                m = band_re.match(l.get("description") or "")
                if m:
                    bands.append([int(m[1]), int(m[2]), l["team"]])
            if not bands:
                if len(teams) == 1:
                    bands = [[1, 60, teams[0]]]
                else:
                    # Who gets which band is unknown: give each its own
                    # made-up slice, so no two can both be counted at once.
                    n = len(teams)
                    bands = [[i * 60 // n + 1, (i + 1) * 60 // n, t] for i, t in enumerate(teams)]
            picks[key] = {"kind": "bands", "bands": sorted(bands)}
        fb = p.get("ladder_fallback_of")
        if fb and fb.get("to"):
            gov = fb.get("governing_pick") or {}
            gov_key = (gov["year"], gov["orig"].upper()) if gov.get("year") else None
            fallbacks[key] = {"gov": gov_key, "top": int(fb.get("protect_top") or 0),
                              "to": fb["to"]}

    def holders(key):
        node = picks[key]
        if node["kind"] == "group":
            return set(groups[node["gid"]])
        return {t for _lo, _hi, t in node["bands"]}

    def retrade(key, from_t, to_t, leaf_id):
        node = picks[key]
        if node["kind"] == "group":
            g = groups[node["gid"]]
            if from_t in g:
                g.discard(from_t)
                g.add(to_t)
            return
        band_idx = None
        if leaf_id:
            m = re.search(r":protected:(\d+)$", leaf_id)
            band_idx = int(m[1]) if m else None
        for i, band in enumerate(node["bands"]):
            if band[2] == from_t and (band_idx is None or i == band_idx):
                band[2] = to_t

    def protect(key, from_t, to_t, top):
        node = picks[key]
        if node["kind"] == "group":
            # A protection on a swap leg: neither side is certain of anything
            # from it any more. Drop from_t's certainty; to_t gains nothing.
            groups[node["gid"]].discard(from_t)
            return
        out = []
        for lo, hi, t in node["bands"]:
            if t != from_t or not (lo <= top < hi):
                out.append([lo, hi, to_t if t == from_t and lo > top else t])
                continue
            out += [[lo, top, from_t], [top + 1, hi, to_t]]
        node["bands"] = out

    def swap(key, from_t, to_t, named):
        counterpart = next((k for k, n in picks.items()
                            if k[0] == key[0] and k[1] != key[1] and n["kind"] == "bands"
                            and [t for _lo, _hi, t in n["bands"]] == [named]), None)
        if counterpart is None or picks[key]["kind"] != "bands":
            retrade(key, from_t, to_t, None)     # apply passes it through flat
            return
        gid = f"_sim_{key[0]}_{key[1]}_{counterpart[1]}"
        groups[gid] = {named, to_t}
        group_members[gid] = [key, counterpart]
        picks[key] = picks[counterpart] = {"kind": "group", "gid": gid}

    def pick_swap(asset, from_t, to_t):
        # Each team is certain of exactly one of the two picks, as in any
        # two-team group. `_pick_swap_problems` refuses anything but two
        # outright picks, so both are plain bands here.
        a = (asset.year, (asset.orig or "").upper())
        b = (asset.year, (asset.swap_orig or "").upper())
        if a not in picks or b not in picks:
            return
        gid = f"_sim_pickswap_{a[0]}_{a[1]}_{b[1]}"
        groups[gid] = {from_t, to_t}
        group_members[gid] = [a, b]
        picks[a] = picks[b] = {"kind": "group", "gid": gid}

    trade_keys = set()
    affected: set[str] = set()
    for xfer in details.transfers:
        from_t, to_t = xfer.from_team.upper(), xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type == "swap" and asset.round == 1:
                affected |= {from_t, to_t}
                pick_swap(asset, from_t, to_t)
                continue
            if asset.type != "pick" or asset.round != 1:
                continue
            affected |= {from_t, to_t}
            key = (asset.year, (asset.orig or "").upper())
            if key not in picks:
                continue
            trade_keys.add(key)
            if asset.swap_with:
                affected.add(asset.swap_with.upper())
                swap(key, from_t, to_t, asset.swap_with.upper())
            elif asset.protection:
                protect(key, from_t, to_t, asset.protection)
            else:
                retrade(key, from_t, to_t, asset.leaf_id)
            for ref in asset.ladder_fallback or []:
                if ref.get("round", 1) != 1:
                    continue
                fkey = (ref["year"], ref["orig"].upper())
                fallbacks[fkey] = {"gov": key, "top": int(asset.ladder_protect_top or 0),
                                   "to": to_t}

    if not affected or not picks:
        return []

    years = sorted({y for (y, _o) in picks})
    lo_y, hi_y = years[0], years[-1]

    def outcomes_for(team):
        """(certain years, [[set of years per outcome], ...]) for one team."""
        certain: set[int] = set()
        # One outcome space per governing pick: its own bands plus every
        # fallback that hangs off it.
        spaces: dict[tuple, list] = {}
        for key, node in picks.items():
            if key in fallbacks:
                continue
            if node["kind"] == "group":
                if team in groups[node["gid"]]:
                    certain.add(key[0])
                continue
            if team in holders(key):
                spaces.setdefault(key, [])
        for fkey, fb in fallbacks.items():
            if fb["to"] == team or team in holders(fkey):
                spaces.setdefault(fb["gov"] or ("fallback", fkey), []).append(fkey)

        variable = []
        for gov, fkeys in spaces.items():
            gov_node = picks.get(gov) if isinstance(gov, tuple) and gov and gov[0] != "fallback" else None
            cuts = {1, 61}
            if gov_node and gov_node["kind"] == "bands":
                for lo, hi, _t in gov_node["bands"]:
                    cuts |= {lo, hi + 1}
            for fk in fkeys:
                cuts.add(fallbacks[fk]["top"] + 1)
            cuts = sorted(c for c in cuts if 1 <= c <= 61)
            spots = cuts[:-1] if gov_node else [None]
            if not gov_node:
                # Governing pick unknown: the fallback could go either way.
                outs = []
                for fk in fkeys:
                    fb = fallbacks[fk]
                    owner = holders(fk)
                    outs.append([{fk[0]} if fb["to"] == team else set(),
                                 {fk[0]} if team in owner else set()])
                variable.extend(outs)
                continue
            outs = []
            for spot in spots:
                got = set()
                if gov_node["kind"] == "bands" and any(
                        lo <= spot <= hi and t == team for lo, hi, t in gov_node["bands"]):
                    if gov not in fallbacks:
                        got.add(gov[0])
                for fk in fkeys:
                    fb = fallbacks[fk]
                    if spot <= fb["top"]:
                        if fb["to"] == team:
                            got.add(fk[0])
                    elif team in holders(fk):
                        got.add(fk[0])
                outs.append(got)
            variable.append(outs)
        return certain, variable

    def first_gap(have):
        for y in range(lo_y, hi_y):
            if y not in have and y + 1 not in have:
                return (y, y + 1)
        return None

    checks: list[CheckResult] = []
    for team in sorted(affected):
        certain, variable = outcomes_for(team)
        n = 1
        for outs in variable:
            n *= max(len(outs), 1)
        gap = None
        if n > _STEPIEN_MAX_SCENARIOS:
            gap = first_gap(certain)
        else:
            for combo in itertools.product(*variable):
                gap = first_gap(certain.union(*combo))
                if gap:
                    break
        if gap:
            best_case = certain.union(*[set().union(*o) for o in variable])
            why = " if its protections fall the wrong way" if first_gap(best_case) is None else ""
            checks.append(CheckResult(
                check=f"stepien_rule_{team.lower()}", passed=False, level="error",
                message=(f"{team} could have no first-round pick in {gap[0]} and {gap[1]} after this "
                         f"trade{why} — violates the Stepien Rule (§ 7.2): a team must keep a "
                         "first-round pick at least once every two draft years, however its "
                         "protections resolve."),
            ))
        else:
            checks.append(CheckResult(
                check=f"stepien_rule_{team.lower()}", passed=True,
                message=f"{team}: keeps a first-round pick at least once every two draft years (§ 7.2).",
            ))
    return checks


def _check_pick_advance_limit(details: TradeIn, cur_season: str) -> list[CheckResult]:
    """§ 7.2 seven-year advance limit: a draft pick may only be traded up to
    7 years ahead of the current league year. Same horizon the picks ledger
    itself is kept populated through (`roster_picks._pick_year_horizon`) — a
    pick further out than that has no real conveyance row to trade in the
    first place, so this is really "the horizon, stated as a rule" rather
    than an independent number that could drift from it.

    Every pick asset in the trade is checked, any round — unlike the Stepien
    Rule this isn't limited to first-rounders. A pick offered only as ladder
    compensation (`ladder_fallback`) is left to manual review, same as the
    Stepien Rule leaves it (see that function's docstring): it's a
    contingent claim, not a pick this trade is itself conveying.
    """
    horizon = _pick_year_horizon(cur_season)
    checks: list[CheckResult] = []
    seen: set[tuple] = set()
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type not in ("pick", "swap") or asset.year is None:
                continue
            key = (asset.year, asset.round, (asset.orig or "").upper())
            if key in seen:
                continue
            seen.add(key)
            label = f"{asset.year} round {asset.round} ({asset.orig or '?'})"
            check_id = f"pick_advance_limit_{asset.year}_{(asset.orig or '?').lower()}"
            if asset.year > horizon:
                checks.append(CheckResult(
                    check=check_id, passed=False, level="error",
                    message=(f"The {label} pick is {asset.year - horizon} year(s) beyond the 7-year "
                             f"advance limit (§ 7.2) — picks may only be traded through {horizon}."),
                ))
            else:
                checks.append(CheckResult(
                    check=check_id, passed=True,
                    message=f"The {label} pick is within the 7-year advance limit (§ 7.2, through {horizon}).",
                ))
    if not checks:
        checks.append(CheckResult(
            check="pick_advance_limit", passed=True,
            message="No draft picks in this trade — § 7.2's 7-year advance limit doesn't apply.",
        ))
    return checks


def _trade_teams(details: dict) -> set[str]:
    teams = {t.upper() for t in (details.get("teams") or []) if t}
    for leg in details.get("transfers") or []:
        for side in ("from_team", "to_team"):
            if leg.get(side):
                teams.add(leg[side].upper())
    return teams

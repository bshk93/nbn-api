"""§ 5.1 waiver wire — the 48-hour claim window a `release` opens.

Design record: nbn-today/docs/waiver-wire-spec.md. Phases 0-1 of that doc's
§ 9 build plan: the ledger model, the claim/resolve endpoints, and the
sweep-on-read priority resolution. Discord (§ 6) and the `/free-agency` UI
(§ 3) are later phases.

**State model** (spec § 4): `_apply_release` is untouched — it still removes
the player from the roster and posts dead cap immediately, exactly as it
should if the window closes unclaimed. This module adds a pending-decision
layer on top, the same shape `offer_sheet` / `offer_sheet_decision` already
uses: a release is "on waivers" for as long as it's enumerable as open (no
`waiver_clear` referencing it yet), and a winning claim reverses the release's
dead cap and re-applies the untouched contract (from the release's `_snapshot`)
to the claiming team via `_apply_sign` — reusing that function's roster/bio
write and its mle-bucket/hard-cap bookkeeping tail rather than duplicating it.

No second store: everything here is derived from `_load_transactions()`, the
same guarantee `_open_offer_sheets` relies on. Four ledger transaction types,
none reachable through the generic `POST /api/transactions` dispatcher
(this feature has its own auth shape per action, like `self_renounce` and the
FA offer endpoints do):

* `waiver_claim` — a team's bid. `POST /api/waivers/{txn_id}/claim`.
* `waiver_claim_withdraw` — pulls an earlier claim before the window closes.
* `waiver_flagged` — a once-only marker for "the § 5's head-to-head tie-break
  is itself tied; this needs a manual PDC call" (spec § 5 step 5). Distinct
  from `waiver_clear` on purpose: a flagged release is NOT resolved, so it
  must not be excluded by the same "already resolved" check.
* `waiver_clear` — the terminal record: `outcome` is `"claimed"` or
  `"unclaimed"`, `resolution` is `"auto"` (the sweep) or `"manual"`
  (`POST /api/waivers/{txn_id}/resolve`, the PDC tie-break escape hatch).

Claims are sealed (spec § 2): nothing here ever tells one team what another
team has bid, or whether anyone else has bid at all.
"""
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, CAP_LEVELS_FILE, TRANSACTIONS_FILE, VALID_TEAMS,
    _txn_lock, _deadcap_lock,
)
from .storage import read_csv, write_csv, _parse_dollar, _season_for_date
from .auth import get_token_info, require_role, has_role, _resolve_token
from .players import load_player_bios
from .roster_picks import load_team_state
from .boxscores import allstats_path
from .transactions import (
    CheckResult, ContractIn, SignDetails,
    _load_transactions, _append_transaction,
    _apply_sign, _compute_team_salary, _compute_team_salary_ex_holds,
    _hard_cap_check, _universal_hard_cap_check, _check_bae_eligibility,
    _check_signing_method_funding, _check_signing_method_declared,
    _roster_size_check, _count_standard_roster, _buyout_salary_above_ntmle,
)

router = APIRouter()

NBN_TODAY_DIR = Path("/home/skim/projects/nbn-today")
STANDINGS_CSV = NBN_TODAY_DIR / "standings" / "standings-history.csv"

WAIVER_WINDOW = timedelta(hours=48)


# ── Pydantic models ───────────────────────────────────────────────────────────

class WaiverClaimDetails(BaseModel):
    team: str
    # Same vocabulary as SignDetails.signing_method (§ 3.1-§ 3.6) — a claim
    # assumes the existing contract rather than negotiating a new one, but the
    # claiming team still has to fund it from something real.
    signing_method: Optional[str] = None
    eaps_assumption: Optional[str] = None


class WaiverResolveDetails(BaseModel):
    # None = "unclaimed" (PDC ruling nobody gets the player). A team abbr must
    # name a team that actually has a claim on file.
    team: Optional[str] = None


# ── time / ledger plumbing ────────────────────────────────────────────────────

def _waiver_deadline(release_txn: dict) -> Optional[datetime]:
    created_at = release_txn.get("created_at")
    if not created_at:
        return None
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) + WAIVER_WINDOW


def _waiver_claims_for(released_txn_id: str, txns: Optional[list[dict]] = None) -> list[dict]:
    """Live (non-withdrawn) claims against one release, oldest first."""
    txns = txns if txns is not None else _load_transactions()
    withdrawn = {(t.get("details") or {}).get("claim_txn_id")
                 for t in txns if t.get("type") == "waiver_claim_withdraw"}
    out = []
    for t in txns:
        if t.get("type") != "waiver_claim":
            continue
        d = t.get("details") or {}
        if d.get("released_txn_id") != released_txn_id or t["id"] in withdrawn:
            continue
        out.append({
            "txn_id": t["id"],
            "team": (d.get("team") or "").upper(),
            "signing_method": d.get("signing_method"),
            "eaps_assumption": d.get("eaps_assumption"),
            "submitted_at": t.get("created_at"),
        })
    return out


def _is_waiver_resolved(released_txn_id: str, txns: list[dict]) -> bool:
    return any((t.get("details") or {}).get("released_txn_id") == released_txn_id
               for t in txns if t.get("type") == "waiver_clear")


def _find_release(txn_id: str, txns: Optional[list[dict]] = None) -> Optional[dict]:
    txns = txns if txns is not None else _load_transactions()
    return next((t for t in txns if t.get("id") == txn_id and t.get("type") == "release"), None)


def _open_waivers(info: Optional[dict] = None) -> list[dict]:
    """Every release still on waivers, newest-deadline-first. Derived straight
    from the ledger — see the module docstring. Releases that predate
    snapshotting (no `_snapshot` on the transaction) never had a window and
    are skipped rather than shown with nothing claimable.

    `info`, if given, adds `has_claimed` for the caller's own team(s) only —
    claims are sealed (spec § 2), so this never reports on any other team."""
    txns = _load_transactions()
    resolved = {(t.get("details") or {}).get("released_txn_id")
                for t in txns if t.get("type") == "waiver_clear"}
    flagged = {(t.get("details") or {}).get("released_txn_id")
               for t in txns if t.get("type") == "waiver_flagged"}
    my_teams = {r.upper() for r in (info or {}).get("roles", []) if r.upper() in VALID_TEAMS}

    out = []
    for t in txns:
        if t.get("type") != "release" or t["id"] in resolved:
            continue
        d = t.get("details") or {}
        if not d.get("_snapshot"):
            continue
        deadline = _waiver_deadline(t)
        claims = _waiver_claims_for(t["id"], txns)
        out.append({
            "txn_id": t["id"],
            "player": d.get("player"),
            "released_by": d.get("team"),
            "date": t.get("date"),
            "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ") if deadline else None,
            "terminated_salary": d.get("terminated_salary"),
            "contract": _forward_contract(d.get("_snapshot") or {}, _season_for_date(t.get("date") or "")),
            "awaiting_pdc": t["id"] in flagged,
            # Which of the caller's *own* teams already has a claim on file —
            # a list, not a bool, so a member holding more than one team role
            # can tell which one (claims are sealed for every other team, § 2).
            "my_claims": sorted(c["team"] for c in claims if c["team"] in my_teams),
        })
    out.sort(key=lambda w: w["deadline"] or "")
    return out


def _forward_contract(snapshot: dict, from_season: str) -> dict:
    """The snapshot's going-forward years only (>= the release's own season) —
    what a `/free-agency` claim card should show as "what you'd be taking on"."""
    keep = lambda d: {k: v for k, v in (d or {}).items() if k >= from_season}
    return {
        "type": snapshot.get("type") or "player",
        "salaries": keep(snapshot.get("salaries")),
        "cap_holds": keep(snapshot.get("cap_holds")),
        "guaranteed": keep(snapshot.get("guaranteed")),
        "guarantee_dates": keep(snapshot.get("guarantee_dates")),
    }


def _val_ctx(date: str) -> dict:
    return {
        "bios": load_player_bios(),
        "team_state": load_team_state(),
        "cap_levels": json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {},
        "cur_season": _season_for_date(date),
        "txn_date": date,
    }


# ── validation (spec § 2: same NTMLE/apron gate a fresh signing gets) ─────────

def _validate_waiver_claim(claim: dict, release_txn: dict, ctx: dict) -> list[CheckResult]:
    """Mirrors `_validate_sign`'s shape, but the "contract" is fixed — the
    untouched deal from the release's `_snapshot`, not something the claiming
    team negotiates. Shared by the claim endpoint, the resolution sweep
    (re-checked per spec § 5 step 6 — a team's room can change in 48 hours),
    and `POST /api/validate/waiver_claim`."""
    checks: list[CheckResult] = []
    team = claim["team"].upper()
    bios = ctx["bios"]; season = ctx["cur_season"]
    player = release_txn["details"]["player"]
    snapshot = release_txn["details"].get("_snapshot") or {}
    contract_type = snapshot.get("type") or "player"
    new_sal = _parse_dollar((snapshot.get("salaries") or {}).get(season, ""))

    current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)
    projected_ex_holds = current_ex_holds + new_sal
    r = _hard_cap_check(team, projected_ex_holds, season, ctx["team_state"], ctx["cap_levels"])
    if r:
        checks.append(r)
    r = _universal_hard_cap_check(team, projected_ex_holds, season, ctx["cap_levels"])
    if r:
        checks.append(r)

    if claim.get("signing_method") == "bae":
        cl = ctx["cap_levels"].get(season, {})
        r = _check_bae_eligibility(team, current_ex_holds, cl, ctx["team_state"], season)
        if r:
            checks.append(r)

    current_with_holds = _compute_team_salary(team, bios, season)
    r = _check_signing_method_funding(
        team, claim.get("signing_method"), new_sal, current_with_holds, current_ex_holds,
        season, ctx["cap_levels"], ctx["team_state"], bios=bios,
    )
    if r:
        checks.append(r)

    # § 1.5.2, extended to claims themselves per the spec's § 2 decision — the
    # rulebook text bars a team from "signing" a player waived this season
    # above the NTMLE while at/above the First Apron; a claim assumes the
    # existing contract rather than negotiating a new one, but the settled
    # answer here is that it's covered by the same restriction anyway.
    apron1 = ctx["cap_levels"].get(season, {}).get("apron1")
    if apron1 is not None and current_ex_holds >= apron1:
        buyout_salary = _buyout_salary_above_ntmle(
            player, ctx["txn_date"], season, ctx["cap_levels"],
            str((bios.get(player, {}).get("salaries") or {}).get(season, "") or ""),
        )
        if buyout_salary:
            ntmle_amt = ctx["cap_levels"].get(season, {}).get("ntmle_amount", 0)
            checks.append(CheckResult(
                check=f"waiver_claim_apron_{team.lower()}", passed=False, level="error",
                message=(
                    f"{team} is at/above the First Apron (${current_ex_holds:,} ≥ "
                    f"${apron1:,}) and may not claim a player waived this season whose "
                    f"terminated contract paid ${buyout_salary:,}, above the full NTMLE of "
                    f"${ntmle_amt:,} (§ 1.5.2, extended to waiver claims)."
                ),
            ))

    if contract_type != "two-way":
        r = _roster_size_check(team, _count_standard_roster(team) + 1, "claiming")
        if r:
            checks.append(r)

    r = _check_signing_method_declared(team, claim.get("signing_method"), contract_type)
    if r:
        checks.append(r)

    return checks


# ── priority resolution (spec § 5) ────────────────────────────────────────────

def _season_before(season: str) -> str:
    a, b = season.split("-")
    return f"{(int(a) - 1) % 100:02d}-{(int(b) - 1) % 100:02d}"


def _relevant_record_season(release_date: str) -> str:
    """Spec § 5 step 2: prior completed season before December 1 of the league
    year the release falls in, current (live-updating) season on/after it."""
    cur = _season_for_date(release_date)
    cutoff = f"20{cur.split('-')[0]}-12-01"
    return _season_before(cur) if release_date < cutoff else cur


def _team_record_pct(team: str, season: str) -> Optional[float]:
    if not STANDINGS_CSV.exists():
        return None
    _, rows = read_csv(STANDINGS_CSV)
    for row in rows:
        if row.get("SEASON") == season and (row.get("TEAM") or "").upper() == team:
            try:
                return float(row.get("PCT") or "")
            except ValueError:
                return None
    return None


def _h2h_record(team_a: str, team_b: str, season: str) -> tuple[int, int]:
    """(wins_a, wins_b) between the two teams in `season`'s regular season.
    Box-score rows are per-player, so games are deduped on (DATE, TEAM) —
    every player on the same team/date/opponent shares one game result."""
    path = allstats_path(season, "REG")
    if not path.exists():
        return 0, 0
    _, rows = read_csv(path)
    seen: set[tuple[str, str]] = set()
    wins_a = wins_b = 0
    for row in rows:
        team = (row.get("TEAM") or "").upper()
        opp = (row.get("OPP") or "").upper()
        if {team, opp} != {team_a, team_b}:
            continue
        key = (row.get("DATE") or "", team)
        if key in seen:
            continue
        seen.add(key)
        if (row.get("WL") or "").upper() != "W":
            continue
        if team == team_a:
            wins_a += 1
        elif team == team_b:
            wins_b += 1
    return wins_a, wins_b


def _priority_order(claims: list[dict], release_date: str) -> dict:
    """Ranks claimants worst-record-first (spec § 5 steps 2-4).

    Returns one of:
      {"status": "no_claims"}
      {"status": "resolved", "order": [claim, ...], "season": "26-27"}
      {"status": "tied", "tied_teams": [...], "season": ..., "h2h": {...} | None}

    A 2-way tie is broken by head-to-head (§ 5 step 4); a 3+-way tie that
    survives the worst-record cut goes straight to PDC rather than guessing at
    a round-robin scheme nobody asked for — see spec § 5 step 5.
    """
    if not claims:
        return {"status": "no_claims"}
    season = _relevant_record_season(release_date)
    # Missing record (no completed season on file at all — only possible in the
    # league's very first season) sorts last: safer than letting an unknown
    # record win a worst-record contest it may not deserve.
    scored = sorted(
        ((c, _team_record_pct(c["team"], season)) for c in claims),
        key=lambda t: t[1] if t[1] is not None else 1.0,
    )
    best = scored[0][1] if scored[0][1] is not None else 1.0
    tied = [c for c, pct in scored if (pct if pct is not None else 1.0) == best]

    if len(tied) == 1:
        return {"status": "resolved", "order": [c for c, _ in scored], "season": season}

    if len(tied) == 2:
        a, b = tied[0]["team"], tied[1]["team"]
        wins_a, wins_b = _h2h_record(a, b, season)
        if wins_a != wins_b:
            first = tied[0] if wins_a < wins_b else tied[1]
            second = tied[1] if first is tied[0] else tied[0]
            rest = [c for c, _ in scored if c not in tied]
            return {"status": "resolved", "order": [first, second] + rest, "season": season}
        return {"status": "tied", "tied_teams": [a, b], "season": season,
                "h2h": {a: wins_a, b: wins_b}}

    return {"status": "tied", "tied_teams": [c["team"] for c in tied], "season": season, "h2h": None}


# ── applying a winning claim ──────────────────────────────────────────────────

def _apply_waiver_transfer(release_txn: dict, claim: dict, txn_date: str, info: dict) -> None:
    """Re-applies the released contract, untouched, onto the claiming team via
    `_apply_sign` (spec § 4 step 4) — reuses its roster/bio write and its
    mle-bucket/hard-cap bookkeeping rather than duplicating that logic. Then
    reverses the releasing team's dead cap: the obligation is now the
    claiming team's, not theirs."""
    d = release_txn["details"]
    player = d["player"]
    old_team = d["team"]
    release_season = _season_for_date(release_txn["date"])
    snapshot = d.get("_snapshot") or {}
    contract = ContractIn(**_forward_contract(snapshot, release_season))

    sign_details = SignDetails(
        player=player, team=claim["team"], contract=contract,
        signing_method=claim.get("signing_method"),
        eaps_assumption=claim.get("eaps_assumption"),
    )
    _apply_sign(sign_details, txn_date, info, txn_id=secrets.token_hex(8))

    dc_path = DATA_DIR / f"{old_team.lower()}-deadcap.csv"
    if dc_path.exists():
        with _deadcap_lock:
            headers, rows = read_csv(dc_path)
            rows = [r for r in rows if r.get("SLUG", "").strip() != player]
            write_csv(dc_path, headers, rows)


def _log_waiver_clear(release_txn: dict, outcome: str, info: dict, *,
                      claimed_by: Optional[str] = None, signing_method: Optional[str] = None,
                      resolution: str = "auto", notes: Optional[str] = None) -> dict:
    txn = {
        "id": secrets.token_hex(8),
        "type": "waiver_clear",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_by": info.get("name", "system"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": "",
        "details": {
            "released_txn_id": release_txn["id"],
            "player": release_txn["details"]["player"],
            "released_by": release_txn["details"]["team"],
            "outcome": outcome,
            "claimed_by": claimed_by,
            "signing_method": signing_method,
            "resolution": resolution,
            "notes": notes,
        },
    }
    _append_transaction(txn)
    return txn


def _log_waiver_flagged(release_txn: dict, priority: dict, info: dict) -> dict:
    txn = {
        "id": secrets.token_hex(8),
        "type": "waiver_flagged",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_by": info.get("name", "system"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": "",
        "details": {
            "released_txn_id": release_txn["id"],
            "player": release_txn["details"]["player"],
            "tied_teams": priority.get("tied_teams"),
            "season": priority.get("season"),
            "h2h": priority.get("h2h"),
        },
    }
    _append_transaction(txn)
    return txn


_SYSTEM_INFO = {"name": "system"}


def _resolve_one_waiver(release_txn: dict, txns: list[dict]) -> Optional[tuple]:
    """Runs under `_txn_lock`, called only from `_sweep_waivers`. Assumes the
    caller has already re-checked this release isn't resolved since `txns` was
    read (idempotency guard lives at the call site, matching `_sweep_ffa_expiry`'s
    double-check-under-lock shape).

    Returns a notification descriptor for the caller to fire *after* the lock
    releases (spec § 6's own rule for the generic dispatcher: Discord must
    never happen while holding the lock), or None when nothing should post —
    a `waiver_flagged` re-check that finds it's already flagged, say."""
    player = release_txn["details"]["player"]
    claims = _waiver_claims_for(release_txn["id"], txns)
    priority = _priority_order(claims, release_txn["date"])

    if priority["status"] == "no_claims":
        _log_waiver_clear(release_txn, "unclaimed", _SYSTEM_INFO)
        return ("closed", player, False)

    if priority["status"] == "tied":
        already_flagged = any(
            (t.get("details") or {}).get("released_txn_id") == release_txn["id"]
            for t in txns if t.get("type") == "waiver_flagged"
        )
        if already_flagged:
            return None
        _log_waiver_flagged(release_txn, priority, _SYSTEM_INFO)
        return ("tied", player, priority.get("tied_teams"), priority.get("h2h"), priority.get("season"))

    ctx = _val_ctx(release_txn["date"])
    winner = None
    for claim in priority["order"]:
        checks = _validate_waiver_claim(claim, release_txn, ctx)
        if not any(not c.passed for c in checks):
            winner = claim
            break

    if winner is None:
        _log_waiver_clear(
            release_txn, "unclaimed", _SYSTEM_INFO,
            notes="Every claim on file failed re-validation at resolution time (spec § 5 step 6).",
        )
        return ("closed", player, False)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _apply_waiver_transfer(release_txn, winner, today, _SYSTEM_INFO)
    _log_waiver_clear(release_txn, "claimed", _SYSTEM_INFO,
                      claimed_by=winner["team"], signing_method=winner.get("signing_method"))
    return ("closed", player, True)


def _fire_waiver_notification(note: tuple) -> None:
    from . import waiver_notify
    if note[0] == "closed":
        _, player, claimed = note
        waiver_notify.notify_window_closed(player, claimed)
    elif note[0] == "tied":
        _, player, tied_teams, h2h, season = note
        waiver_notify.notify_manual_tie(player, tied_teams or [], h2h, season)


def _sweep_waivers() -> None:
    """Sweep-on-read expiry, no scheduler — same shape as `_sweep_ffa_expiry`
    (spec § 7), except this one mutates roster/bio state on a win, so it runs
    under `_txn_lock` with the same double-check-under-lock idempotency guard
    `_apply_release`/`_apply_sign` already serialize behind."""
    txns = _load_transactions()
    now = datetime.now(timezone.utc)
    due_ids = []
    for t in txns:
        if t.get("type") != "release" or not (t.get("details") or {}).get("_snapshot"):
            continue
        if _is_waiver_resolved(t["id"], txns):
            continue
        deadline = _waiver_deadline(t)
        if deadline and now >= deadline:
            due_ids.append(t["id"])
    if not due_ids:
        return

    notes = []
    with _txn_lock:
        txns = _load_transactions()
        for txn_id in due_ids:
            if _is_waiver_resolved(txn_id, txns):
                continue
            release_txn = _find_release(txn_id, txns)
            if release_txn is None:
                continue
            note = _resolve_one_waiver(release_txn, txns)
            if note:
                notes.append(note)
            txns = _load_transactions()  # re-read: the resolution just appended to it

    # Outside the lock, same rule the main dispatcher follows for Discord.
    for note in notes:
        _fire_waiver_notification(note)


# ── endpoints ──────────────────────────────────────────────────────────────────

@router.get("/api/waivers")
def list_open_waivers(authorization: Optional[str] = Header(None)):
    """Public, like `GET /api/fa/board` — every open waiver is visible to
    anyone. `has_claimed` personalizes to the caller's own team(s) only when a
    valid token is presented (`_resolve_token` returns `None` on a missing or
    bad one, same as `/api/news`); claims are sealed, so this never reports on
    any *other* team's claim, with or without a token."""
    _sweep_waivers()
    return _open_waivers(_resolve_token(authorization))


@router.post("/api/validate/waiver_claim")
def validate_waiver_claim(txn_id: str, body: WaiverClaimDetails):
    """Advisory preview, same shape as `/api/validate/sign` — never writes."""
    release_txn = _find_release(txn_id)
    if release_txn is None:
        raise HTTPException(status_code=404, detail="No such release")
    claim = {"team": body.team.upper(), "signing_method": body.signing_method,
             "eaps_assumption": body.eaps_assumption}
    ctx = _val_ctx(release_txn["date"])
    checks = _validate_waiver_claim(claim, release_txn, ctx)
    return {
        "legal": not any(not c.passed for c in checks),
        "checks": [c.model_dump() for c in checks],
        "contract": _forward_contract(release_txn["details"].get("_snapshot") or {},
                                       _season_for_date(release_txn["date"])),
    }


@router.post("/api/waivers/{txn_id}/claim")
def submit_waiver_claim(txn_id: str, body: WaiverClaimDetails, info: dict = Depends(get_token_info)):
    team = body.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=400, detail=f"Unknown team: {team!r}")
    if not (has_role(info, "admin") or has_role(info, team.lower())):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")

    _sweep_waivers()
    txns = _load_transactions()
    release_txn = _find_release(txn_id, txns)
    if release_txn is None:
        raise HTTPException(status_code=404, detail="No such release")
    if release_txn["details"].get("team") == team:
        raise HTTPException(status_code=422, detail="A team may not claim a player it just released")
    deadline = _waiver_deadline(release_txn)
    if _is_waiver_resolved(txn_id, txns) or not deadline or datetime.now(timezone.utc) >= deadline:
        raise HTTPException(status_code=422, detail="This waiver window has closed")

    existing = _waiver_claims_for(txn_id, txns)
    if any(c["team"] == team for c in existing):
        raise HTTPException(status_code=409, detail=f"{team} already has a claim on file for this player")

    claim = {"team": team, "signing_method": body.signing_method, "eaps_assumption": body.eaps_assumption}
    ctx = _val_ctx(release_txn["date"])
    checks = _validate_waiver_claim(claim, release_txn, ctx)
    failed = [c for c in checks if not c.passed]
    if failed:
        raise HTTPException(status_code=422, detail={
            "validation": True, "checks": [c.model_dump() for c in checks],
        })

    txn = {
        "id": secrets.token_hex(8),
        "type": "waiver_claim",
        "date": release_txn["date"],
        "created_by": info.get("name", "unknown"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": "",
        "details": {
            "released_txn_id": txn_id, "player": release_txn["details"]["player"],
            "team": team, "signing_method": body.signing_method,
            "eaps_assumption": body.eaps_assumption,
        },
    }
    with _txn_lock:
        _append_transaction(txn)
    from . import waiver_notify
    waiver_notify.notify_claim_submitted(release_txn["details"]["player"], team, body.signing_method)
    return txn


@router.delete("/api/waivers/{txn_id}/claim")
def withdraw_waiver_claim(txn_id: str, team: str, info: dict = Depends(get_token_info)):
    team = team.upper()
    if not (has_role(info, "admin") or has_role(info, team.lower())):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")

    txns = _load_transactions()
    release_txn = _find_release(txn_id, txns)
    if release_txn is None:
        raise HTTPException(status_code=404, detail="No such release")
    deadline = _waiver_deadline(release_txn)
    if _is_waiver_resolved(txn_id, txns) or not deadline or datetime.now(timezone.utc) >= deadline:
        raise HTTPException(status_code=422, detail="This waiver window has closed")

    claim = next((c for c in _waiver_claims_for(txn_id, txns) if c["team"] == team), None)
    if claim is None:
        raise HTTPException(status_code=404, detail=f"{team} has no claim on file for this player")

    txn = {
        "id": secrets.token_hex(8),
        "type": "waiver_claim_withdraw",
        "date": release_txn["date"],
        "created_by": info.get("name", "unknown"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": "",
        "details": {"claim_txn_id": claim["txn_id"], "released_txn_id": txn_id, "team": team},
    }
    with _txn_lock:
        _append_transaction(txn)
    return txn


@router.post("/api/waivers/{txn_id}/resolve")
def resolve_waiver(txn_id: str, body: WaiverResolveDetails,
                   info: dict = Depends(require_role("fac_head"))):
    """The spec § 5 step 5 escape hatch: a head-to-head tie that's itself tied
    (or a 3+-way tie) doesn't auto-resolve — PDC names a winner, or rules the
    player unclaimed. Gated on `fac_head` for now (same tier that finalizes FA
    ballots); there's no dedicated waivers-committee role yet."""
    txns = _load_transactions()
    release_txn = _find_release(txn_id, txns)
    if release_txn is None:
        raise HTTPException(status_code=404, detail="No such release")

    with _txn_lock:
        txns = _load_transactions()
        if _is_waiver_resolved(txn_id, txns):
            raise HTTPException(status_code=409, detail="This waiver has already been resolved")

        if body.team:
            team = body.team.upper()
            claim = next((c for c in _waiver_claims_for(txn_id, txns) if c["team"] == team), None)
            if claim is None:
                raise HTTPException(status_code=422, detail=f"{team} has no claim on file for this player")
            ctx = _val_ctx(release_txn["date"])
            checks = _validate_waiver_claim(claim, release_txn, ctx)
            failed = [c for c in checks if not c.passed]
            if failed:
                raise HTTPException(status_code=422, detail={
                    "validation": True, "checks": [c.model_dump() for c in checks],
                })
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _apply_waiver_transfer(release_txn, claim, today, info)
            txn = _log_waiver_clear(release_txn, "claimed", info, claimed_by=team,
                                    signing_method=claim.get("signing_method"), resolution="manual")
        else:
            txn = _log_waiver_clear(release_txn, "unclaimed", info, resolution="manual")

    from . import waiver_notify
    waiver_notify.notify_window_closed(release_txn["details"]["player"], bool(body.team))
    return txn

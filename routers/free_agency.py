"""PDC free-agency offer pipeline — see nbn-today/docs/pdc-free-agency-spec.md.

Phases 1–2: the FA pool derivation (§ 7.1) plus the whole offer/ballot API
(§ 4, § 6). Nothing renders any of this yet — every endpoint past the pool is
role-gated and unlinked, which is the point. The pipeline is reviewable and
testable before any owner can reach it.

Two rules shape everything below, and neither is negotiable:

* **`offer` is a verbatim `SignDetails`** (§ 4.2). It is what
  `POST /api/validate/sign` is called with and what a future "sign this offer"
  button would post to `POST /api/transactions` as `details`. Anything the
  committee needs that isn't a contract term (pitch, promises) lives *outside*
  it. Adding a field `SignDetails` doesn't accept turns a ~30-line follow-up
  into a rewrite.
* **Legality has one implementation.** The submit path calls `_validate_sign`,
  the same function `POST /api/transactions` runs — never a second opinion, and
  never with `force`, for the reason `self_renounce` has none: an owner-facing
  write must not be able to push past a rule.
"""
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import fa_notify
from .auth import (get_token_info, has_role, load_members,
                   require_any_role, require_role)
from .constants import (CAP_LEVELS_FILE, FA_BALLOTS_FILE, FA_OFFERS_FILE,
                        FA_STATE_FILE, VALID_TEAMS)
from .players import load_player_bios, _build_team_map
from .proposals import _member_current_team
from .storage import (_current_league_year, _load_json, _parse_dollar,
                      _save_json, log_write)
from .transactions import (ContractIn, SignDetails, _compute_team_salary,
                           _min_salary_for, _require_validatable,
                           _rfa_eligibility, _signee_existing_hold,
                           _signing_fact_sheet, _validate_sign, _validation_ctx)

router = APIRouter()

# One lock for the whole subsystem, where the spec's § 4 sketch said one per
# file. Every write that matters spans two of them — submit stamps the FFA clock
# in fa-state *and* appends to fa-offers; finalize reads offers *and* writes
# ballots — so three locks would always be taken together. That buys no
# concurrency and adds a lock-ordering bug waiting to happen. The write traffic
# here is a committee of a few people.
_fa_lock = threading.RLock()


def _load_cap_levels() -> dict:
    return json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}


def _latest_salary(salaries: dict) -> int:
    if not salaries:
        return 0
    latest_yr = max(salaries.keys())
    return _parse_dollar(salaries[latest_yr])


def _qo_amount(bio: dict, class_year: str, prior_salary: int, cap_levels: dict) -> Optional[int]:
    """§ 3.9 qualifying offer amount — proposed formula, pending BOD confirmation.

    First-round picks still on the rookie scale price off the Year 4 team
    option (§ 7.1), which has no source yet (`/api/rookie-scale` returns `{}`,
    tracked BACKLOG P3 — see docs/extensions.md § 8.3) — so that branch
    returns None rather than guess. Everyone else (2nd round, UDFA, or any
    other player under 4 years of experience) gets the greater of the
    applicable minimum salary scale figure (§ 3.12) or 125% of prior salary.
    """
    if bio.get("draft_round") == 1:
        return None
    min_amt = _min_salary_for(bio, class_year, cap_levels)
    raise_amt = round(prior_salary * 1.25) if prior_salary else None
    candidates = [v for v in (min_amt, raise_amt) if v]
    return max(candidates) if candidates else None


def _is_current_fa(entry: dict, season: str) -> bool:
    """Whether this pool entry is a free agent *this* league year.

    `_fa_pool` returns every player with an actionable cap hold on file, keyed by
    the earliest year that hold lands — so it spans future league years by
    design: it is what `/free-agency`'s year chips are built from, and 26-27's
    tab sitting beside 29-30's is the point of that page.

    It is *not* the set of players who can be signed. Most of the pool is under
    contract; only the earliest class has actually reached free agency. `<=`
    rather than `==` because a hold that was never resolved leaves a player in an
    older class while they are still, plainly, a free agent.

    RENOUNCED and UNSIGNED are current whatever their class year says. They have
    no cap hold at all — that is what put them in those buckets — so they are
    signable right now, and their `class_year` is only the bucket `_fa_pool`
    filed them under (the pool's earliest class), not a date they are waiting on.
    """
    if entry.get("hold_type") in ("RENOUNCED", "UNSIGNED"):
        return True
    return (entry.get("class_year") or "") <= season


def _fa_pool(bios: dict, team_map: dict, season: str, cap_levels: Optional[dict] = None) -> dict:
    """The free-agent pool: `{slug: {class_year, hold_type, prior_salary, rfa, qo_amount}}`.

    Ported from `free-agency/index.html`'s page-JS derivation (§ 7.1) —
    behaviour must stay identical, this just moves it server-side. Each
    player appears in exactly one FA class: the earliest cap_holds year that
    is actionable (not NON_GTD) and has a matching salaries entry. Players
    with no actionable hold at all, no roster, no cap_holds, and no other
    type get bucketed as RENOUNCED (has salary history) or UNSIGNED (never
    signed) into the earliest class year found, or `season` if none exists.

    `cap_levels` takes the real on-disk levels when omitted (the route
    handler's path); callers that already loaded it, or tests, pass it
    explicitly — same split as `_min_salary_for`/`_compute_fa_hold_amount`.
    """
    if cap_levels is None:
        cap_levels = _load_cap_levels()
    pool: dict[str, dict] = {}
    years_seen: set[str] = set()
    renounced: list[str] = []
    unsigned: list[str] = []

    for slug, bio in bios.items():
        cap_map = bio.get("cap_holds") or {}
        salaries = bio.get("salaries") or {}

        actionable = sorted(
            (yr, t) for yr, t in cap_map.items() if t != "NON_GTD" and salaries.get(yr)
        )
        if actionable:
            yr, hold_type = actionable[0]
            years_seen.add(yr)
            prior_salary = _parse_dollar(salaries.get(yr))
            rfa, _ = _rfa_eligibility(slug, bios, yr)
            qo = _qo_amount(bio, yr, prior_salary, cap_levels) if rfa else None
            pool[slug] = {
                "class_year": yr, "hold_type": hold_type,
                "prior_salary": prior_salary, "rfa": rfa, "qo_amount": qo,
            }
            continue

        if bio.get("type") == "" and slug not in team_map and not cap_map and not bio.get("retired"):
            (renounced if salaries else unsigned).append(slug)

    target_year = min(years_seen) if years_seen else season
    for slug in renounced:
        pool[slug] = {
            "class_year": target_year, "hold_type": "RENOUNCED",
            "prior_salary": _latest_salary(bios[slug].get("salaries") or {}),
            "rfa": False, "qo_amount": None,
        }
    for slug in unsigned:
        pool[slug] = {
            "class_year": target_year, "hold_type": "UNSIGNED",
            "prior_salary": 0, "rfa": False, "qo_amount": None,
        }

    # Stamped here so no caller has to re-derive it. Every picker and menu that
    # asks "who can actually be signed" reads this one field, computed by the
    # same helper `_accepts_offers` gates on — the § 6.3 rule, applied to the
    # rule that most needed it.
    for entry in pool.values():
        entry["current"] = _is_current_fa(entry, season)

    return pool


@router.get("/api/fa/pool")
def get_fa_pool():
    bios = load_player_bios()
    team_map = _build_team_map()
    return _fa_pool(bios, team_map, _current_league_year())


def _live_pool() -> dict:
    return _fa_pool(load_player_bios(), _build_team_map(), _current_league_year())


# ══ Phase 2 — state, offers, ballots ═══════════════════════════════════════════

VALID_MODES = {"closed", "rounds", "ffa"}
PLAYER_STATUSES = {"open", "held", "closed"}
LIVE_STATUSES = {"draft", "submitted", "returned"}
# § 4.3b. Deliberately *not* in LIVE_STATUSES: a voided offer leaves play through
# `_is_live`, so every gate that already reads it — the one-live-offer rule, the
# team's exposure, ballot options, conflicts, the review list — drops it without
# a second rule to keep in step. Only `submitted`/`returned` can be voided; a
# draft is the team's own scratch pad and the committee never sees it.
VOIDABLE_STATUSES = {"submitted", "returned"}
PROMISE_ROLES = {"face", "starter", "role_player", "veteran", "none"}

# § 4.1. The window's length is the head's to set (`PUT /api/fa/ffa-window`);
# this is only the default a board starts with. The bounds are not decoration:
# 0 would expire every clock the instant it started, and a window longer than a
# week stops being a clock.
FFA_WINDOW_HOURS = 24
FFA_WINDOW_MIN_HOURS = 1
FFA_WINDOW_MAX_HOURS = 168
BALLOT_TOTAL = 1000
QO = "QO"                    # § 4.4 synthetic ballot option, RFAs only
NO_SIGNING = "NO_SIGNING"    # § 4.4 synthetic ballot option, always offered


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── stores ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    st = _load_json(FA_STATE_FILE, {})
    st.setdefault("seq", 0)          # monotonic offer numbers; never max(existing)
    st.setdefault("mode", "closed")
    st.setdefault("ffa_window_hours", FFA_WINDOW_HOURS)
    st.setdefault("rounds", [])
    st.setdefault("players", {})
    return st


def _save_state(state: dict):
    _save_json(FA_STATE_FILE, state)


def _load_offers() -> list[dict]:
    return _load_json(FA_OFFERS_FILE, [])


def _save_offers(offers: list[dict]):
    _save_json(FA_OFFERS_FILE, offers)


def _load_ballots() -> dict:
    return _load_json(FA_BALLOTS_FILE, {})


def _save_ballots(ballots: dict):
    _save_json(FA_BALLOTS_FILE, ballots)


# ── the FFA clock (§ 4.1) ─────────────────────────────────────────────────────
#
# Expiry is *computed*, never written. A scheduler that dies leaves every player
# silently open forever; a comparison against `deadline` cannot. Nothing here
# resolves an outcome either — expiry closes a submission window and hands the
# player to the committee, exactly as the § 3.15 offer-sheet deadline does.
#
# The *length* of the window is the head's setting, but it is read in exactly one
# place — `_start_ffa_clock`, which stamps a deadline — and nowhere else. That is
# the load-bearing part of making it settable: a running clock keeps the deadline
# it was stamped with, so changing the setting can never move a deadline teams
# are already bidding against, and shortening it can never retroactively close a
# window that is still open. Every reader below goes to the stamp, never to the
# setting; keep it that way.

def _ffa_window_hours(state: dict) -> float:
    hours = state.get("ffa_window_hours")
    return float(hours) if isinstance(hours, (int, float)) and hours > 0 else float(FFA_WINDOW_HOURS)


def ffa_window_label(ffa: Optional[dict]) -> str:
    """"24-hour" — describing the window *this player's clock actually got*,
    derived from its own stamp rather than the current setting.

    Every string the league reads about a clock is built from this, so a window
    that started before the head changed the setting still describes itself
    correctly. Returns **""** when the length is unrecoverable, since a wrong
    number is worse than none — callers drop the adjective and say "the window",
    which is true whatever it ran for.
    """
    ffa = ffa or {}
    hours = ffa.get("window_hours")
    if not isinstance(hours, (int, float)) or hours <= 0:
        # Pre-dates the stamp, or the deadline was moved by hand. Fall back to
        # the span, which is what the window ran either way.
        started, deadline = _parse_ts(ffa.get("started_at")), _parse_ts(ffa.get("deadline"))
        if not started or not deadline:
            return ""
        hours = (deadline - started).total_seconds() / 3600
    if hours <= 0:
        return ""
    shown = int(hours) if abs(hours - round(hours)) < 0.05 else round(hours, 1)
    return f"{shown}-hour"


def _ffa_deadline(entry: dict) -> Optional[datetime]:
    return _parse_ts((entry.get("ffa") or {}).get("deadline"))


def _ffa_expired(entry: dict, now: Optional[datetime] = None) -> bool:
    deadline = _ffa_deadline(entry)
    return bool(deadline and (now or datetime.now(timezone.utc)) >= deadline)


def _player_entry(state: dict, slug: str) -> dict:
    return state["players"].get(slug) or {}


def _accepts_offers(state: dict, slug: str, pool: dict,
                    season: Optional[str] = None) -> tuple[bool, str]:
    """Whether `slug` accepts a *new* offer right now, and the reason if not.

    The reason strings are the ones § 8.1 shows in the disabled ⋯-menu entry, so
    the API and the UI can't drift into explaining this differently.
    """
    if slug not in pool:
        return False, "This player isn't a free agent."
    # Checked before the mode, because it is a fact about the player rather than
    # about the board, and it is the more useful of the two things to be told.
    # Without it, FFA mode ("every player in the pool is offerable") would offer
    # a contract to someone under contract for three more years.
    season = season if season is not None else _current_league_year()
    if not _is_current_fa(pool[slug], season):
        return False, (f"This player doesn't reach free agency until "
                       f"{pool[slug].get('class_year')} — he's under contract for {season}.")
    mode = state["mode"]
    if mode == "closed":
        return False, "Free agency is closed."
    entry = _player_entry(state, slug)
    if mode == "rounds":
        if entry.get("status") != "open":
            return False, "This player isn't open for offers in this round."
        return True, ""
    # FFA: every player in the pool is offerable regardless of `status` — the
    # clock is the only gate, and it's the only clock this system enforces. Its
    # length is named from the player's own stamp, so the refusal describes the
    # window this team was actually bidding into.
    if _ffa_expired(entry):
        deadline = _ffa_deadline(entry)
        lbl = ffa_window_label(entry.get("ffa"))
        return False, (f"The {lbl + ' ' if lbl else ''}FFA window on this player "
                       f"closed at {deadline.isoformat()}.")
    return True, ""


def _current_round(state: dict) -> Optional[dict]:
    open_rounds = [r for r in state["rounds"] if not r.get("closed_at")]
    if open_rounds:
        return open_rounds[-1]
    return state["rounds"][-1] if state["rounds"] else None


def _start_ffa_clock(state: dict, slug: str, offer_id: str, actor: str):
    """Stamp the clock the *first submitted* offer starts. Drafts don't start it,
    and later offers don't extend it (§ 4.1)."""
    entry = state["players"].setdefault(slug, {})
    if entry.get("ffa"):
        return
    started = datetime.now(timezone.utc)
    # The one read of the head's setting. Stamped here — both as the deadline and
    # as the length itself — and never consulted again, which is what keeps a
    # later change off this player's clock. `window_hours` is recorded rather
    # than re-derived from the two timestamps so that the window a player got
    # stays legible even if his deadline is ever adjusted by hand.
    hours = _ffa_window_hours(state)
    entry["ffa"] = {
        "started_at": started.isoformat(),
        "deadline": (started + timedelta(hours=hours)).isoformat(),
        "window_hours": hours,
        "started_by_offer": offer_id,
        "started_by": actor,
    }
    # The FFA session doubles as the round id, so a player reopened for a second
    # FFA window gets a fresh ballot bucket rather than merging into the old one.
    entry["round_id"] = f"ffa-{secrets.token_hex(3)}"
    entry.setdefault("subcommittee", [])
    # `status` is deliberately untouched: in FFA mode it governs sub-committee
    # assignment, not offerability, and it's the head's to set. A submitted offer
    # starting a clock must not quietly reclassify a player the head closed.


def _round_id_for(state: dict, slug: str) -> Optional[str]:
    return _player_entry(state, slug).get("round_id")


def _sweep_ffa_expiry(state: Optional[dict] = None) -> None:
    """Emit the § 9.2 "window closed" post for any clock that has run out.

    Expiry is computed, not scheduled (§ 4.1), so there is no job to hang the
    announcement off — it is emitted by whichever request first *observes* the
    deadline has passed. Called from the read endpoints, which is what the
    dashboards and the public FA page hit; the write paths already refuse an
    expired player through `_accepts_offers`, so they need this for nothing but
    timeliness.

    `closed_posted` on the player's `ffa` object is the once-only guard, and it
    is stamped under the lock *before* anything is sent: two simultaneous
    requests observing the same expiry must produce one post, not two. A clock
    that expired long ago is still stamped here (so it can never surface later)
    but not announced — see `fa_notify.notify_ffa_closed`.
    """
    state = state if state is not None else _load_state()
    if state.get("mode") != "ffa":
        return
    due = [slug for slug, entry in (state.get("players") or {}).items()
           if (entry.get("ffa") or {}).get("deadline")
           and not entry["ffa"].get("closed_posted") and _ffa_expired(entry)]
    if not due:
        return
    posted = []
    with _fa_lock:
        fresh = _load_state()
        for slug in due:
            entry = fresh["players"].get(slug) or {}
            ffa = entry.get("ffa") or {}
            if not ffa.get("deadline") or ffa.get("closed_posted") or not _ffa_expired(entry):
                continue
            ffa["closed_posted"] = _now()
            posted.append((slug, dict(ffa)))
        if posted:
            _save_state(fresh)
    for slug, ffa in posted:
        fa_notify.notify_ffa_closed(slug, ffa)


# ── offers ────────────────────────────────────────────────────────────────────

def _is_live(offer: dict) -> bool:
    """Live = still in play. `archived_at` is stamped only by finalize (§ 4.2),
    which is what frees a team to offer on the same player in a later round. A
    reopen does *not* archive: offers on a player the head reopened without
    finalizing were never resolved and are still the ones under review."""
    return offer.get("archived_at") is None and offer["status"] in LIVE_STATUSES


def _find_offer(offers: list[dict], offer_id: str) -> tuple[int, dict]:
    idx = next((i for i, o in enumerate(offers) if o["id"] == offer_id), None)
    if idx is None:
        raise HTTPException(404, "Offer not found")
    return idx, offers[idx]


def _offers_for(offers: list[dict], slug: str, live_only: bool = True) -> list[dict]:
    return [o for o in offers if o["player"] == slug and (not live_only or _is_live(o))]


def _conflict_team(name: str, slug: str, offers: list[dict]) -> Optional[str]:
    """The member's own team, when it has a live offer on this player (§ 4.6).
    Stamped on ballots and remands as a warning — never a block. In a league this
    size, hard-blocking would make some players unballotable, and holding a team
    role is not by itself evidence of bad faith."""
    team = _member_current_team(name)
    if not team:
        return None
    team = team.upper()
    return team if any(o["team"] == team for o in _offers_for(offers, slug)) else None


def _sign_details(offer: dict) -> SignDetails:
    return SignDetails(**offer["offer"])


def _run_validation(offer: dict, ctx: dict) -> dict:
    """The one legality call (§ 5.1). Same `_validate_sign` as the submit path of
    `POST /api/transactions`, plus the same fact sheet the simulator renders —
    which is why nothing in this module does cap math of its own."""
    details = _sign_details(offer)
    _require_validatable(details.team, details.player, ctx)
    checks = [c.model_dump() for c in _validate_sign(details, ctx)]
    sheet = _signing_fact_sheet(
        details.team.upper(), details.player, details.contract, ctx,
        signing_method=details.signing_method,
        bird_rights_type=details.bird_rights_type,
        eaps_assumption=details.eaps_assumption,
    )
    return {
        "legal": not any(not c["passed"] and c["level"] == "error" for c in checks),
        "checks": checks,
        "fact_sheet": sheet,
        "validated_at": _now(),
        "season": ctx["cur_season"],
    }


# ── visibility (§ 4.5, § 6.1) ─────────────────────────────────────────────────

def _is_head(info: dict) -> bool:
    return has_role(info, "fac_head") or has_role(info, "admin")


def _is_assigned(state: dict, slug: str, name: str) -> bool:
    return name in (_player_entry(state, slug).get("subcommittee") or [])


def _require_reviewer(info: dict, state: dict, slug: str):
    """A sub-committee is transparent to itself and opaque to everyone outside
    it — a `fac` member not assigned to this player sees neither the offers nor
    the ballots, only that the player exists (§ 4.5)."""
    if _is_head(info) or (has_role(info, "fac") and _is_assigned(state, slug, info["name"])):
        return
    raise HTTPException(403, "You aren't on this player's sub-committee")


def _visible_offers(info: dict, state: dict, offers: list[dict]) -> list[dict]:
    """Filtered server-side, per request. The dashboard's grouping renders what
    the endpoint already withheld; it is never a client-side hide."""
    if _is_head(info):
        return list(offers)
    name = info["name"]
    out = []
    for o in offers:
        # A team sees its own offers in every status, drafts included.
        if has_role(info, o["team"].lower()):
            out.append(o)
            continue
        # The committee sees submitted work, never another team's scratch pad —
        # including work it voided, which is the whole point of a void being a
        # status rather than a delete.
        if (has_role(info, "fac") and _is_assigned(state, o["player"], name)
                and o["status"] in ("submitted", "returned", "voided")):
            out.append(o)
    return out


# ── request models ────────────────────────────────────────────────────────────

class PromisesIn(BaseModel):
    mpg: Optional[int] = None
    playoffs: bool = False
    role: str = "none"


class OfferCreate(BaseModel):
    player: str
    team: str
    contract: ContractIn
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None
    pitch: str = ""
    promises: PromisesIn = PromisesIn()


class OfferPatch(BaseModel):
    contract: Optional[ContractIn] = None
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None
    pitch: Optional[str] = None
    promises: Optional[PromisesIn] = None


class ModeIn(BaseModel):
    mode: str


class FfaWindowIn(BaseModel):
    hours: float


class FfaExtendIn(BaseModel):
    hours: float
    reason: str


class RoundIn(BaseModel):
    name: Optional[str] = None
    closes_at: Optional[str] = None    # display label only — nothing acts on it
    close_previous: bool = False


class PlayerStateIn(BaseModel):
    status: Optional[str] = None
    subcommittee: Optional[list[str]] = None
    round_id: Optional[str] = None


class VoidIn(BaseModel):
    reason: str = ""


class RemandIn(BaseModel):
    note: str


class BallotIn(BaseModel):
    balls: dict[str, int]
    note: str = ""


def _clean_promises(p: PromisesIn) -> dict:
    if p.role not in PROMISE_ROLES:
        raise HTTPException(422, f"promises.role must be one of {sorted(PROMISE_ROLES)}")
    if p.mpg is not None and not (0 <= p.mpg <= 48):
        raise HTTPException(422, "promises.mpg must be between 0 and 48")
    return {"mpg": p.mpg, "playoffs": bool(p.playoffs), "role": p.role}


# ── board / state ─────────────────────────────────────────────────────────────

@router.get("/api/fa/board")
def get_board():
    """Public. Mode, rounds, and which players are accepting offers — plus the
    FFA deadlines, which § 9.2 already announces to the league. Deliberately
    carries no contract detail, no offering team, and no offer count: the fact
    that a team is bidding is committee information."""
    state = _load_state()
    _sweep_ffa_expiry(state)
    pool = _live_pool()
    players = {}
    for slug in pool:
        entry = _player_entry(state, slug)
        accepting, reason = _accepts_offers(state, slug, pool)
        # Every pool player is listed, including the ones taking no offers.
        # `reason` is the exact copy § 8.1 puts in the team's disabled ⋯-menu
        # entry, so omitting the closed players would force /free-agency to
        # reinvent those strings client-side and the two would drift. The pool
        # itself is already public, so listing it with a status leaks nothing.
        players[slug] = {
            "status": entry.get("status", "closed"),
            "round_id": entry.get("round_id"),
            "accepting": accepting,
            "reason": None if accepting else reason,
            "ffa_deadline": (entry.get("ffa") or {}).get("deadline"),
        }
    # Public: a team about to bid needs to know how long a clock it is starting,
    # and the length is not committee information the way an offer is.
    return {"mode": state["mode"], "rounds": state["rounds"], "players": players,
            "ffa_window_hours": _ffa_window_hours(state)}


@router.get("/api/fa/state")
def get_state(info: dict = Depends(require_any_role("fac", "poext"))):
    """The committee's view: everything on the board plus the sub-committee
    assignments, which the public board withholds.

    The dashboard's queue sorts by urgency and marks what still needs the
    viewer's attention, so each player also carries `balloted` — *the viewer's
    own* ballot, which leaks nothing — and `finalized`. The count of who else
    has voted is committee information scoped to that player's sub-committee
    (§ 4.5), so it is head-only here; an assigned member reads it from
    `GET /api/fa/players/{slug}/ballots`, which enforces that scope.
    """
    state = _load_state()
    _sweep_ffa_expiry(state)
    offers = _load_offers()
    ballots = _load_ballots()
    head = _is_head(info)
    counts: dict[str, int] = {}
    for o in offers:
        if _is_live(o) and o["status"] != "draft":
            counts[o["player"]] = counts.get(o["player"], 0) + 1
    players = {}
    for slug, entry in state["players"].items():
        node = (ballots.get(slug) or {}).get(entry.get("round_id") or "") or {}
        cast = node.get("ballots") or {}
        players[slug] = {
            **entry,
            "ffa_expired": _ffa_expired(entry),
            "offer_count": counts.get(slug, 0),
            "mine": info["name"] in (entry.get("subcommittee") or []),
            "balloted": info["name"] in cast,
            "finalized": bool(node.get("final")),
            **({"ballots_cast": len(cast)} if head else {}),
        }
    return {"mode": state["mode"], "rounds": state["rounds"], "players": players,
            "ffa_window_hours": _ffa_window_hours(state)}


@router.put("/api/fa/mode")
def set_mode(body: ModeIn, info: dict = Depends(require_role("fac_head"))):
    if body.mode not in VALID_MODES:
        raise HTTPException(422, f"mode must be one of {sorted(VALID_MODES)}")
    with _fa_lock:
        state = _load_state()
        prev = state["mode"]
        state["mode"] = body.mode
        state.setdefault("history", []).append(
            {"at": _now(), "by": info["name"], "action": "mode", "from": prev, "to": body.mode})
        _save_state(state)
    log_write(info, f"PUT fa/mode — {prev} → {body.mode}")
    # Announced outside the lock, after the write is committed — Discord being
    # down must never delay or fail a board change (§ 9).
    fa_notify.notify_mode_change(prev, body.mode, info["name"])
    return {"mode": body.mode, "previous": prev}


@router.put("/api/fa/ffa-window")
def set_ffa_window(body: FfaWindowIn, info: dict = Depends(require_role("fac_head"))):
    """How long an FFA window runs (§ 4.1). The head's setting, alongside mode
    and rounds — the length of the window is board policy, not a league rule, so
    it lives here rather than in the rulebook.

    **It applies to clocks started from now on, and to nothing already running.**
    Every clock carries the deadline it was stamped with, so this cannot move a
    deadline a team is already bidding against, and cannot retroactively close a
    window that is still open. Announced for the same reason a mode change is:
    the league is acting on this clock.
    """
    hours = body.hours
    if not (FFA_WINDOW_MIN_HOURS <= hours <= FFA_WINDOW_MAX_HOURS):
        raise HTTPException(422, f"hours must be between {FFA_WINDOW_MIN_HOURS} and "
                                 f"{FFA_WINDOW_MAX_HOURS}")
    with _fa_lock:
        state = _load_state()
        prev = _ffa_window_hours(state)
        state["ffa_window_hours"] = hours
        state.setdefault("history", []).append(
            {"at": _now(), "by": info["name"], "action": "ffa_window",
             "from": prev, "to": hours})
        _save_state(state)
    log_write(info, f"PUT fa/ffa-window — {prev}h → {hours}h")
    running = sum(1 for e in (state.get("players") or {}).values()
                  if (e.get("ffa") or {}).get("deadline") and not _ffa_expired(e))
    fa_notify.notify_ffa_window_change(prev, hours, info["name"], running)
    return {"ffa_window_hours": hours, "previous": prev, "clocks_unaffected": running}


@router.post("/api/fa/players/{slug}/ffa-extend")
def extend_ffa_window(slug: str, body: FfaExtendIn,
                      info: dict = Depends(require_role("fac_head"))):
    """Push one player's FFA deadline out, keeping everything already bid.

    **This is the deliberate exception to "a setting can never move a running
    deadline" (§ 4.1), and it is safe for the reasons that rule isn't.** That
    rule protects teams from `PUT /api/fa/ffa-window` leaking into clocks they
    are already bidding against — an invisible, global, retroactive change. This
    is none of those: one named player, by the head, with a required reason, and
    announced on both channels before anyone can act on it. `_start_ffa_clock`
    still reads the setting exactly once and still never re-reads it; nothing
    about that changed.

    **It is not the same operation as reopening.** `PUT /api/fa/players/{slug}`
    with `status: "open"` clears `ffa` outright and mints a fresh `round_id`, so
    the next offer starts a brand-new window and ballots already cast land in a
    different bucket — a second window, deliberately. This keeps the same
    window: same `round_id`, same ballots, same offers, more time. The head
    needs both, and picking the wrong one silently discards a round of votes.

    Works on an expired clock as well as a live one, which is the case that
    prompted it: an expired window has no other route back short of a reopen
    that throws the deliberation away. Extending from `max(now, deadline)` is
    what makes the two cases behave the same way — "six more hours" means six
    hours from now on a window that already lapsed, not six hours added to a
    moment in the past.
    """
    if not (0 < body.hours <= FFA_WINDOW_MAX_HOURS):
        raise HTTPException(422, f"hours must be above 0 and at most {FFA_WINDOW_MAX_HOURS}")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "A reason is required — it is shown to every team.")

    with _fa_lock:
        state = _load_state()
        if state.get("mode") != "ffa":
            raise HTTPException(422, f"Free agency is in {state.get('mode')} mode, not FFA — "
                                     f"there is no window to extend.")
        entry = _player_entry(state, slug)
        ffa = entry.get("ffa")
        if not ffa or not ffa.get("deadline"):
            raise HTTPException(422, "No FFA window has started on this player — a window "
                                     "begins when the first offer is submitted.")
        old_deadline = _parse_ts(ffa["deadline"])
        now = datetime.now(timezone.utc)
        reopened = old_deadline <= now
        new_deadline = max(now, old_deadline) + timedelta(hours=body.hours)

        ffa["deadline"] = new_deadline.isoformat()
        started = _parse_ts(ffa.get("started_at"))
        if started:
            # Recomputed, not incremented: `ffa_window_label` describes the
            # window this clock actually ran, and every refusal string and post
            # is built from it. Leaving the original length would have the
            # system telling teams "the 24-hour window" about a 30-hour one.
            ffa["window_hours"] = round((new_deadline - started).total_seconds() / 3600, 2)
        # Reviving a lapsed clock has to clear the announcement guard, or the
        # second expiry passes in silence.
        ffa.pop("closed_posted", None)
        ffa.setdefault("extensions", []).append({
            "at": _now(), "by": info["name"], "hours": body.hours,
            "reason": reason, "from": old_deadline.isoformat(),
            "to": ffa["deadline"], "reopened": reopened,
        })
        state.setdefault("history", []).append(
            {"at": _now(), "by": info["name"], "action": "ffa_extend", "player": slug,
             "from": old_deadline.isoformat(), "to": ffa["deadline"], "reason": reason})
        _save_state(state)
        stamped = dict(ffa)

    log_write(info, f"POST fa/{slug}/ffa-extend — +{body.hours}h "
                    f"({'reopened' if reopened else 'extended'})")
    fa_notify.notify_ffa_extended(slug, stamped, body.hours, reason, info["name"], reopened)
    return {"player": slug, "ffa": stamped, "reopened": reopened,
            "window_label": ffa_window_label(stamped)}


@router.post("/api/fa/rounds")
def open_round(body: RoundIn, info: dict = Depends(require_role("fac_head"))):
    """Opens a round. Opening Round N deliberately does **not** close Round N−1's
    open players (§ 4.1, D11) — `close_previous` only closes the *round record*,
    and even that is opt-in. Players are closed by hand, because the head wanting
    to leave one open across a boundary is worth more than the automation."""
    with _fa_lock:
        state = _load_state()
        number = len(state["rounds"]) + 1
        if body.close_previous:
            for r in state["rounds"]:
                if not r.get("closed_at"):
                    r["closed_at"] = _now()
        rnd = {
            "id": f"r{number}", "number": number,
            "name": body.name or f"Round {number}",
            "opened_at": _now(), "opened_by": info["name"],
            "closed_at": None,
            # Advisory: the dashboard prints it, nothing enforces it.
            "closes_at": body.closes_at,
        }
        state["rounds"].append(rnd)
        _save_state(state)
    log_write(info, f"POST fa/rounds — {rnd['id']}")
    fa_notify.notify_round_opened(rnd, info["name"])
    return rnd


@router.put("/api/fa/players/{slug}")
def set_player_state(slug: str, body: PlayerStateIn,
                     info: dict = Depends(require_role("fac_head"))):
    pool = _live_pool()
    if slug not in pool:
        raise HTTPException(400, f"'{slug}' is not in the free-agent pool")
    season = _current_league_year()
    if not _is_current_fa(pool[slug], season):
        # The head can't open a player who hasn't reached free agency either.
        # `_accepts_offers` would refuse every offer on them anyway, so opening
        # one only produces a player sitting on the board that no team can bid
        # on — and a sub-committee with nothing to review.
        raise HTTPException(422, f"'{slug}' doesn't reach free agency until "
                                 f"{pool[slug].get('class_year')} — the current league year is {season}")
    with _fa_lock:
        state = _load_state()
        offers = _load_offers()
        entry = state["players"].setdefault(
            slug, {"status": "closed", "round_id": None, "subcommittee": [], "ffa": None})
        was = entry.get("status")

        if body.subcommittee is not None:
            members = load_members()
            unknown = [m for m in body.subcommittee if m not in members]
            if unknown:
                raise HTTPException(422, f"Unknown member(s): {unknown}")
            entry["subcommittee"] = list(dict.fromkeys(body.subcommittee))

        if body.status is not None:
            if body.status not in PLAYER_STATUSES:
                raise HTTPException(422, f"status must be one of {sorted(PLAYER_STATUSES)}")
            entry["status"] = body.status
            if body.status == "open":
                # Reopening clears the FFA clock so the next submitted offer
                # starts a fresh one, and mints a fresh round id so the new
                # window's ballots don't merge into the closed window's.
                entry["ffa"] = None
                rnd = _current_round(state)
                if body.round_id:
                    entry["round_id"] = body.round_id
                elif state["mode"] == "rounds":
                    if not rnd:
                        raise HTTPException(422, "Open a round before opening a player in rounds mode")
                    entry["round_id"] = rnd["id"]
                entry["opened_at"] = _now()
                entry["opened_by"] = info["name"]
        elif body.round_id:
            entry["round_id"] = body.round_id

        state.setdefault("history", []).append(
            {"at": _now(), "by": info["name"], "action": "player", "player": slug,
             "from": was, "to": entry.get("status")})
        _save_state(state)
        result = {
            **entry,
            "conflicts": {m: _conflict_team(m, slug, offers)
                          for m in (entry.get("subcommittee") or [])},
        }
    log_write(info, f"PUT fa/players/{slug} — {was} → {entry.get('status')}")
    return result


# ── offers ────────────────────────────────────────────────────────────────────

def _require_team_role(info: dict, team: str):
    """The one gate on an offer (§ 6.0) — any holder of the team's role. Drafting
    and submitting are both team business: a GM or coach who can prepare the whole
    offer can also send it, so front offices aren't blocked on the owner being
    around. The team is always taken from the stored offer, never from the request,
    so this can only ever act on one's own team."""
    if not (has_role(info, "admin") or has_role(info, team.lower())):
        raise HTTPException(403, f"'{team.lower()}' role required")


@router.get("/api/fa/offers")
def list_offers(player: Optional[str] = None, team: Optional[str] = None,
                status: Optional[str] = None, include_archived: bool = False,
                info: dict = Depends(get_token_info)):
    """Scoped per § 6.1. A member with no relevant role gets `[]` — the public
    can't learn even that an offer exists."""
    state = _load_state()
    offers = _load_offers()
    if player:
        offers = [o for o in offers if o["player"] == player]
    if team:
        offers = [o for o in offers if o["team"] == team.upper()]
    if status:
        offers = [o for o in offers if o["status"] == status]
    if not include_archived:
        offers = [o for o in offers if o.get("archived_at") is None]
    return _visible_offers(info, state, offers)


@router.post("/api/fa/offers")
def create_offer(body: OfferCreate, info: dict = Depends(get_token_info)):
    team = body.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(400, f"Unknown team '{team}'")
    _require_team_role(info, team)
    pool = _live_pool()
    if body.player not in pool:
        raise HTTPException(400, f"'{body.player}' is not a free agent")
    promises = _clean_promises(body.promises)

    with _fa_lock:
        state = _load_state()
        accepting, reason = _accepts_offers(state, body.player, pool)
        if not accepting:
            raise HTTPException(422, reason)
        offers = _load_offers()
        if any(o["team"] == team and o["player"] == body.player and _is_live(o) for o in offers):
            # § 4.2 / D5. One live offer per team per player: with no withdrawal
            # (§ 4.3) and no second bid, the draft is a team's only chance to get
            # it right, which is why the draft state is specified as carefully
            # as it is. Finalizing the player archives these and frees the team
            # to bid again in a later round.
            raise HTTPException(409, "Your team already has a live offer on this player")
        state["seq"] += 1
        offer = {
            "id": secrets.token_hex(4),
            "number": state["seq"],
            "player": body.player,
            "team": team,
            "round_id": None,           # stamped at submit — a draft isn't in a round yet
            "status": "draft",
            "version": 1,
            "versions": [],
            "remands": [],
            "created_by": info["name"],
            "submitted_by": None,
            "created_at": _now(), "updated_at": _now(), "submitted_at": None,
            "archived_at": None,
            "void": None,               # § 4.3b {at, by, reason, from_status}
            "offer": {
                "player": body.player, "team": team,
                "contract": body.contract.model_dump(),
                "signing_method": body.signing_method,
                "bird_rights_type": body.bird_rights_type,
                "eaps_assumption": body.eaps_assumption,
            },
            "pitch": body.pitch,
            "promises": promises,
            "validation": None,
            "history": [{"ts": _now(), "actor": info["name"], "from": None, "to": "draft"}],
        }
        offers.append(offer)
        _save_offers(offers)
        _save_state(state)
    log_write(info, f"POST fa/offers — #{offer['number']} {team} → {body.player}")
    return offer


@router.patch("/api/fa/offers/{offer_id}")
def patch_offer(offer_id: str, body: OfferPatch, info: dict = Depends(get_token_info)):
    """Editable while `draft` — and while `returned`, which is the committee's
    own doing (§ 4.3a). A team can never reach for the revision path itself."""
    fields = body.model_dump(exclude_unset=True)
    with _fa_lock:
        offers = _load_offers()
        idx, offer = _find_offer(offers, offer_id)
        _require_team_role(info, offer["team"])
        if not _is_live(offer) or offer["status"] not in ("draft", "returned"):
            raise HTTPException(409, "A submitted offer is final — the committee may send it back, the team may not withdraw it (§ 3.14)")
        if "contract" in fields:
            offer["offer"]["contract"] = body.contract.model_dump()
        for key in ("signing_method", "bird_rights_type", "eaps_assumption"):
            if key in fields:
                offer["offer"][key] = fields[key]
        if "pitch" in fields:
            offer["pitch"] = body.pitch
        if "promises" in fields:
            offer["promises"] = _clean_promises(body.promises)
        offer["updated_at"] = _now()
        offers[idx] = offer
        _save_offers(offers)
    log_write(info, f"PATCH fa/offers/{offer_id}")
    return offer


@router.delete("/api/fa/offers/{offer_id}")
def delete_offer(offer_id: str, info: dict = Depends(get_token_info)):
    with _fa_lock:
        offers = _load_offers()
        idx, offer = _find_offer(offers, offer_id)
        _require_team_role(info, offer["team"])
        if offer["status"] != "draft":
            raise HTTPException(409, "Only a draft can be deleted — a submitted offer is final (§ 3.14)")
        offers.pop(idx)
        _save_offers(offers)
    log_write(info, f"DELETE fa/offers/{offer_id}")
    return {"ok": True}


@router.post("/api/fa/offers/{offer_id}/submit")
def submit_offer(offer_id: str, info: dict = Depends(get_token_info)):
    """Submit, and the resubmit path for a remanded offer — one endpoint, so both
    revalidate, re-snapshot and (from Phase 6) re-notify through one code path.

    Once through, the offer is final at the team's initiative: there is no
    withdraw endpoint and no post-submission edit, mirroring § 3.14's rule for
    sign-and-trades. The only way back is a committee remand.
    """
    pool = _live_pool()
    with _fa_lock:
        offers = _load_offers()
        idx, offer = _find_offer(offers, offer_id)
        _require_team_role(info, offer["team"])
        if not _is_live(offer) or offer["status"] not in ("draft", "returned"):
            raise HTTPException(409, f"Offer is {offer['status']}, not submittable")

        state = _load_state()
        resubmit = offer["status"] == "returned"
        if not resubmit:
            accepting, reason = _accepts_offers(state, offer["player"], pool)
            if not accepting:
                raise HTTPException(422, reason)
        elif offer["player"] not in pool:
            raise HTTPException(422, "This player is no longer a free agent")
        # A resubmission skips the window check on purpose (§ 4.3a): the 24-hour
        # clock governs *new* offers from other teams, and a revision the
        # committee itself asked for is part of its own review.

        ctx = _validation_ctx()
        validation = _run_validation(offer, ctx)
        if not validation["legal"]:
            failing = [c["check"] for c in validation["checks"]
                       if not c["passed"] and c["level"] == "error"]
            # No `force` on this path, for the same reason `self_renounce` has
            # none: an owner-facing write must not be able to push past a rule.
            raise HTTPException(422, {"detail": "Offer fails a rule check", "checks": failing,
                                      "validation": validation})

        # The version being answered, frozen back at the remand (§ 4.3a) — this
        # is what the announcement diffs against, and it has to be captured
        # before the increment or it names the wrong version.
        prev_version = None
        if resubmit:
            prev_version = next(
                (v for v in offer["versions"] if v["version"] == offer["version"]), None)
            offer["version"] += 1

        started_clock = None
        if state["mode"] == "ffa":
            had_clock = bool(_player_entry(state, offer["player"]).get("ffa"))
            _start_ffa_clock(state, offer["player"], offer["id"], info["name"])
            if not had_clock:
                started_clock = _player_entry(state, offer["player"]).get("ffa")
        offer["round_id"] = _round_id_for(state, offer["player"])
        offer["status"] = "submitted"
        offer["submitted_by"] = info["name"]
        offer["submitted_at"] = _now()
        offer["updated_at"] = offer["submitted_at"]
        # Frozen at submit and never recomputed (§ 5.2) — the live picture is
        # recomputed on read instead, so the committee sees both.
        offer["validation"] = validation
        offer["history"].append({"ts": _now(), "actor": info["name"],
                                 "from": "returned" if resubmit else "draft",
                                 "to": "submitted", "version": offer["version"]})
        offers[idx] = offer
        _save_offers(offers)
        _save_state(state)
    log_write(info, f"POST fa/offers/{offer_id}/submit — v{offer['version']}")
    # Both announcements happen outside the lock, after the offer and the state
    # are on disk. The offer comes first: the clock exists *because* of it.
    fa_notify.notify_offer_submitted(offer, prev_version)
    if started_clock:
        fa_notify.notify_ffa_started(offer["player"], started_clock)
    return offer


@router.post("/api/fa/offers/{offer_id}/remand")
def remand_offer(offer_id: str, body: RemandIn, info: dict = Depends(get_token_info)):
    """Send a submitted offer back for revision (§ 4.3a) — the committee's power,
    never the team's. Any assigned sub-committee member may do it (D14): the FAC
    reviews as a group, so a reviewer who wants a term changed shouldn't have to
    route through the head.
    """
    note = body.note.strip()
    if not note:
        # An empty send-back is just a delay. The note is what the team is
        # answering and what the record shows the committee asked for.
        raise HTTPException(422, "A remand requires a note saying what should change")
    with _fa_lock:
        state = _load_state()
        offers = _load_offers()
        idx, offer = _find_offer(offers, offer_id)
        _require_reviewer(info, state, offer["player"])
        if not _is_live(offer) or offer["status"] not in ("submitted", "returned"):
            raise HTTPException(409, f"Offer is {offer['status']} — only a submitted offer can be sent back")
        ballots = _load_ballots()
        node = (ballots.get(offer["player"]) or {}).get(offer["round_id"] or "") or {}
        if node.get("final"):
            # Once the head locks a player the offers on them are closed;
            # reopening the player (§ 4.1) is the escape hatch.
            raise HTTPException(409, "This player is finalized — reopen them before sending an offer back")
        # Freeze the version being sent back *here*, not at resubmit. A returned
        # offer is editable, so by the time it comes back `offer["offer"]` is
        # already the new figures — snapshotting then would record the revision
        # as if it were the thing the committee objected to, and the diff § 4.3a
        # promises would compare v(n) against itself. Guarded by version number
        # so a second, additive remand doesn't freeze the same version twice.
        if not any(v["version"] == offer["version"] for v in offer["versions"]):
            offer["versions"].append({
                "version": offer["version"],
                "offer": json.loads(json.dumps(offer["offer"])),
                "pitch": offer["pitch"],
                "promises": dict(offer["promises"]),
                "validation": offer.get("validation"),
                "submitted_at": offer.get("submitted_at"),
                "submitted_by": offer.get("submitted_by"),
            })
        entry = {
            "at": _now(), "by": info["name"], "note": note,
            "from_version": offer["version"],
            # "Send that rival's offer back" is exactly where the incentive
            # bites, so a conflicted remand is flagged like a conflicted ballot.
            "conflict": _conflict_team(info["name"], offer["player"], offers),
        }
        offer["remands"].append(entry)
        # Additive, not a queue of round-trips: a second member remanding the
        # same offer appends another note and changes nothing else, so the team
        # answers every outstanding ask in one resubmission.
        if offer["status"] == "submitted":
            offer["status"] = "returned"
            offer["history"].append({"ts": _now(), "actor": info["name"],
                                     "from": "submitted", "to": "returned"})
        offer["updated_at"] = _now()
        offers[idx] = offer
        _save_offers(offers)
    log_write(info, f"POST fa/offers/{offer_id}/remand")
    fa_notify.notify_offer_remanded(offer, entry)
    return offer


@router.post("/api/fa/offers/{offer_id}/void")
def void_offer(offer_id: str, body: VoidIn, info: dict = Depends(get_token_info)):
    """Take an offer out of play entirely (§ 4.3b) — the escape hatch a remand
    isn't.

    A remand hands the offer *back* and the team can answer it; the offer is
    still a live bid meanwhile, still on the ballot, still in the team's
    exposure. That is the right tool when the terms need changing and the wrong
    one when the offer should never have counted at all — an offer on the wrong
    player, a team ruled ineligible after the fact, a duplicate submitted around
    an outage. Under a remand those sit `returned` forever, visibly awaiting a
    team that has nothing to say.

    **Head-only, unlike a remand.** Any assigned reviewer may ask for a revision
    (D14) because the team can always answer. Nobody can answer a void, so it
    sits with the head beside the other powers that end things — finalize,
    unlock, assignment. Widening it later is a one-line change; narrowing it
    after teams have lost bids to it is not.

    **It is not a delete.** The offer, its versions, its remands and its
    validation stay exactly where they were; `status` leaves `LIVE_STATUSES` and
    `void` records who, when and why. `restore` puts it back. The § 4.3a rule
    that nothing is overwritten applies here with more force, not less: a bid
    the committee erased is precisely the thing a record has to be able to show.
    """
    if not _is_head(info):
        raise HTTPException(403, "Only the FAC head can void an offer — any reviewer may send one back")
    reason = body.reason.strip()
    if not reason:
        # Same rule as a remand's note, for a stronger reason: this one ends a
        # team's bid with no reply, so "why" is the only thing they get.
        raise HTTPException(422, "A void requires a reason — the team can't answer it")
    with _fa_lock:
        state = _load_state()
        offers = _load_offers()
        idx, offer = _find_offer(offers, offer_id)
        # Archived first, and by name: an archived offer still reads `submitted`,
        # so the generic message would tell a head their submitted offer isn't
        # submitted rather than that the player is already decided.
        if offer.get("archived_at") is not None:
            raise HTTPException(409, "This player is finalized — reopen them before voiding an offer")
        if offer["status"] not in VOIDABLE_STATUSES:
            raise HTTPException(409, f"Offer is {offer['status']} — only a submitted offer can be voided")
        ballots = _load_ballots()
        node = (ballots.get(offer["player"]) or {}).get(offer["round_id"] or "") or {}
        if node.get("final"):
            # Same as a remand: once the head locks a player, the offers on them
            # are decided. Reopening (§ 4.1) is the escape hatch from a lock.
            raise HTTPException(409, "This player is finalized — reopen them before voiding an offer")
        offer["void"] = {"at": _now(), "by": info["name"], "reason": reason,
                         # What it goes back to on a restore. Stored rather than
                         # assumed: a returned offer that gets voided must not
                         # come back as `submitted` with its remands silently
                         # answered.
                         "from_status": offer["status"]}
        offer["history"].append({"ts": offer["void"]["at"], "actor": info["name"],
                                 "from": offer["status"], "to": "voided", "reason": reason})
        offer["status"] = "voided"
        offer["updated_at"] = offer["void"]["at"]
        offers[idx] = offer
        _save_offers(offers)
    log_write(info, f"POST fa/offers/{offer_id}/void")
    fa_notify.notify_offer_voided(offer)
    return offer


@router.post("/api/fa/offers/{offer_id}/restore")
def restore_offer(offer_id: str, info: dict = Depends(get_token_info)):
    """Undo a void (§ 4.3b), back to whatever status it held. `unlock` is to
    `finalize` what this is to `void` — a power that ends something needs a way
    back, or a misclick is the one action on the board nobody can answer.

    Two things can make a restore illegal, and both come from the void having
    worked: the team may have bid again in the meantime (one live offer per team
    per player, § 4.2/D5), and the player may since have been finalized.
    """
    if not _is_head(info):
        raise HTTPException(403, "Only the FAC head can restore a voided offer")
    with _fa_lock:
        offers = _load_offers()
        idx, offer = _find_offer(offers, offer_id)
        if offer["status"] != "voided":
            raise HTTPException(409, f"Offer is {offer['status']}, not voided")
        if offer.get("archived_at") is not None:
            raise HTTPException(409, "This player was finalized after the void — reopen them first")
        back = (offer.get("void") or {}).get("from_status") or "submitted"
        if any(o["team"] == offer["team"] and o["player"] == offer["player"] and _is_live(o)
               for o in offers):
            # The void freed the team to bid again, and it did. Restoring now
            # would put two live offers from one team on one player, which is
            # the invariant `create_offer` exists to hold.
            raise HTTPException(409, f"{offer['team']} has since submitted another offer on this player")
        voided = offer.get("void") or {}
        offer["status"] = back
        offer["void"] = None
        offer["updated_at"] = _now()
        offer["history"].append({"ts": offer["updated_at"], "actor": info["name"],
                                 "from": "voided", "to": back})
        offers[idx] = offer
        _save_offers(offers)
    log_write(info, f"POST fa/offers/{offer_id}/restore")
    fa_notify.notify_offer_restored(offer, voided)
    return offer


# ── review ────────────────────────────────────────────────────────────────────

def _team_commitment(team: str, offers: list[dict], ctx: dict) -> dict:
    """§ 5.3. A pending FA offer holds no cap room — unlike a § 3.15 offer sheet,
    it is a pitch, not an executed instrument. Nothing therefore stops a team
    submitting five max offers it could fund once, so the exposure is disclosed
    rather than blocked (D6).

    Every figure comes from the same helpers `_validate_sign` uses; this module
    does no cap math of its own.
    """
    season = ctx["cur_season"]
    bios = ctx["bios"]
    cap = (ctx["cap_levels"].get(season) or {}).get("cap")
    salary = _compute_team_salary(team, bios, season)
    live = [o for o in offers if o["team"] == team and _is_live(o) and o["status"] != "draft"]
    committed = sum(_parse_dollar((o["offer"]["contract"].get("salaries") or {}).get(season, ""))
                    for o in live)
    # Room measured as if every player this team is bidding on were renounced —
    # otherwise each signee's own FA hold is counted twice, once as a hold and
    # again as the offer meant to replace it.
    own_holds = 0
    for o in live:
        amount, is_fa = _signee_existing_hold(team, o["player"], bios, season)
        if is_fa:
            own_holds += amount
    room = (cap - (salary - own_holds)) if cap is not None else None
    return {
        "team": team, "season": season,
        "live_offers": len(live),
        "committed_year1": committed,
        "room": room,
        # Overcommitted means *bidding* more than you can fund. A team with
        # nothing out is not overcommitted no matter how far over the cap it
        # sits — `committed > room` alone flagged every over-cap team with zero
        # offers, which on the team's own form (§ 8.1) reads as a false alarm on
        # a page that hasn't been used yet. Negative room floors at zero: a team
        # with no room that bids anything at all is exposed by that amount.
        "overcommitted": bool(committed > 0 and room is not None and committed > max(room, 0)),
    }


@router.get("/api/fa/commitment/{team}")
def get_commitment(team: str, info: dict = Depends(get_token_info)):
    """§ 5.3, team side — what this team has already bid against what it can fund.

    The same `_team_commitment` the review page renders for the committee, served
    to the team drafting the offer, so the figure the owner sees on their own
    form and the figure the FAC sees are one computation. A second one here
    would be the disclosure disagreeing with itself.

    Team-role gated rather than owner-gated: a GM drafting the offer needs the
    exposure in front of them, and this is the team's own position, not another
    team's.
    """
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(400, f"Unknown team '{team}'")
    _require_team_role(info, team)
    return _team_commitment(team, _load_offers(), _validation_ctx())


@router.get("/api/fa/players/{slug}/review")
def review_player(slug: str, info: dict = Depends(get_token_info)):
    """Everything the committee needs to compare offers on one player.

    Each offer carries both its frozen submit-time verdict and a live
    revalidation (§ 5.2): a cap position that moved since Monday can make a
    submitted offer illegal, and with no withdrawal path the badge is the only
    way the committee learns of it.
    """
    state = _load_state()
    _require_reviewer(info, state, slug)
    _sweep_ffa_expiry(state)
    pool = _live_pool()
    if slug not in pool:
        raise HTTPException(404, f"'{slug}' is not in the free-agent pool")
    offers = _load_offers()
    live = [o for o in _offers_for(offers, slug) if o["status"] in ("submitted", "returned")]

    ctx = _validation_ctx()
    rendered = []
    for o in live:
        try:
            now_valid = _run_validation(o, ctx)
        except HTTPException as e:
            now_valid = {"legal": False, "checks": [], "error": e.detail}
        was_legal = ((o.get("validation") or {}).get("legal"))
        rendered.append({
            **o,
            "revalidation": now_valid,
            "legality_changed": was_legal is not None and was_legal != now_valid.get("legal"),
            "outstanding_remands": [r for r in o["remands"] if r["from_version"] >= o["version"]],
        })

    # Out of play, so never revalidated and never a ballot option — but listed,
    # because "the committee erased a bid" is exactly the kind of thing a review
    # page has to be able to show. Archived voids belong to a closed round.
    voided = [o for o in offers
              if o["player"] == slug and o["status"] == "voided"
              and o.get("archived_at") is None]

    teams = sorted({o["team"] for o in live})
    entry = _player_entry(state, slug)
    return {
        "player": slug,
        "pool": pool[slug],
        "state": {**entry, "ffa_expired": _ffa_expired(entry)},
        "offers": rendered,
        "voided_offers": voided,
        "commitments": {t: _team_commitment(t, offers, ctx) for t in teams},
        "ballot_options": _ballot_options(slug, live, pool),
        "assignable": _assignable(slug, offers) if _is_head(info) else [],
    }


def _assignable(slug: str, offers: list[dict]) -> list[dict]:
    """The sub-committee picker's roster, with conflicts already resolved.

    § 4.6 wants a conflicted assignee flagged *before* the head confirms, and
    `PUT /api/fa/players/{slug}` only reports conflicts after the fact. The rule
    itself stays where it was — this is the same `_conflict_team` the ballot and
    the remand stamp — so the picker can't come to a different answer about who
    is conflicted than the record does.
    """
    return [
        {"name": name, "team": _member_current_team(name),
         "conflict": _conflict_team(name, slug, offers)}
        for name, m in sorted(load_members().items())
        if has_role({"roles": m.get("roles") or []}, "fac")
    ]


def _ballot_options(slug: str, live: list[dict], pool: dict) -> list[dict]:
    """Offer ids plus the two synthetic options (§ 4.4, D7).

    `NO_SIGNING` is offered for UFAs and RFAs alike: without it a member who
    thinks every offer on the table is bad has no way to say so except by
    picking a least-bad one, which silently converts "nobody should sign him"
    into a signing.
    """
    opts = [{"key": o["id"], "kind": "offer", "team": o["team"], "number": o["number"]}
            for o in live]
    if pool.get(slug, {}).get("rfa"):
        qo = pool[slug].get("qo_amount")
        opts.append({"key": QO, "kind": "qo", "amount": qo,
                     # The § 3.9 formula is marked "proposed, pending BOD
                     # confirmation" in the rulebook itself, so the figure is
                     # labelled rather than presented as settled. The line shows
                     # either way — an RFA's incumbent keeping him on the QO is a
                     # real outcome whether or not we can price it.
                     "estimated": qo is not None})
    opts.append({"key": NO_SIGNING, "kind": "no_signing"})
    return opts


# ── ballots ───────────────────────────────────────────────────────────────────

def _ballot_node(ballots: dict, slug: str, round_id: str, create: bool = False) -> dict:
    per_player = ballots.setdefault(slug, {}) if create else (ballots.get(slug) or {})
    if create:
        return per_player.setdefault(round_id, {"ballots": {}, "final": None})
    return per_player.get(round_id) or {"ballots": {}, "final": None}


@router.get("/api/fa/players/{slug}/ballots")
def get_ballots(slug: str, info: dict = Depends(get_token_info)):
    state = _load_state()
    _require_reviewer(info, state, slug)
    _sweep_ffa_expiry(state)
    round_id = _round_id_for(state, slug)
    if not round_id:
        raise HTTPException(422, "This player hasn't been opened in a round yet")
    node = _ballot_node(_load_ballots(), slug, round_id)
    assigned = _player_entry(state, slug).get("subcommittee") or []
    all_offers = _load_offers()
    revised = [o for o in _offers_for(all_offers, slug)
               if o["status"] in ("submitted", "returned") and o["version"] > 1]
    gone = {o["id"] for o in all_offers if o["player"] == slug and o["status"] == "voided"}

    def _voided_since(cast: dict) -> list[str]:
        """Offers this member put balls on that the head has since voided
        (§ 4.3b).

        The § 4.3a rule holds harder here: a ballot is flagged, never rewritten.
        Redistributing someone's balls because an option left the board is the
        software inventing a vote nobody cast — and the member may well want the
        rest of their ballot to stand exactly as it is. So the balls stay put,
        the member is shown that some of them are on a voided offer, and
        `finalize` reports the same thing to the head.
        """
        return sorted(k for k, n in (cast.get("balls") or {}).items() if n and k in gone)

    def _revised_since(cast: dict) -> list[str]:
        """Offers resubmitted after this ballot was last touched (§ 4.3a).

        A ballot cast against v1 is flagged, never voided — silently discarding
        a member's considered judgment is worse than showing them it may be
        stale. Derived here rather than in the dashboard so there's one rule.
        """
        at = _parse_ts(cast.get("updated_at"))
        if not at:
            return []
        return sorted(o["id"] for o in revised
                      if (_parse_ts(o.get("submitted_at")) or at) > at)

    return {
        "player": slug, "round_id": round_id,
        "subcommittee": assigned,
        # The viewer's own conflict, resolved by the same `_conflict_team` that
        # stamps it onto a cast ballot — so the banner a member sees before they
        # vote and the flag the head sees afterwards can never disagree. Derived
        # here rather than guessed client-side from role names: a conflict comes
        # from an active *tenure*, which the browser has no business reasoning
        # about.
        "your_conflict": _conflict_team(info["name"], slug, all_offers),
        # In-progress ballots are visible inside the sub-committee by design
        # (§ 4.5) — `updated_at` is what lets a member tell a considered ballot
        # from one cast a minute ago in response to theirs.
        "ballots": {name: {**cast, "revised_since": _revised_since(cast),
                           "voided_since": _voided_since(cast)}
                    for name, cast in node["ballots"].items()},
        "outstanding": [m for m in assigned if m not in node["ballots"]],
        "final": node["final"],
    }


@router.put("/api/fa/players/{slug}/ballot")
def cast_ballot(slug: str, body: BallotIn, info: dict = Depends(get_token_info)):
    """Own ballot only, and only if assigned.

    Admin is *not* waved through here, unlike every other check in `auth.py`.
    A ballot is a vote, not an administrative action — there is no such thing as
    casting one you weren't assigned. The head's actual powers over a ballot
    (assign, finalize, unlock) are separate endpoints and admin passes those.
    """
    state = _load_state()
    if not _is_assigned(state, slug, info["name"]):
        raise HTTPException(403, "You aren't on this player's sub-committee")
    round_id = _round_id_for(state, slug)
    if not round_id:
        raise HTTPException(422, "This player hasn't been opened in a round yet")

    pool = _live_pool()
    with _fa_lock:
        ballots = _load_ballots()
        node = _ballot_node(ballots, slug, round_id, create=True)
        # Checked before the options are built, not after: finalize archives the
        # offers, so a late ballot would otherwise be refused as "not an option
        # on this ballot" — true, but not the reason.
        if node.get("final"):
            raise HTTPException(409, "This player is finalized — ballots are locked")

        offers = _load_offers()
        live = [o for o in _offers_for(offers, slug) if o["status"] in ("submitted", "returned")]
        valid_keys = {opt["key"] for opt in _ballot_options(slug, live, pool)}
        unknown = sorted(set(body.balls) - valid_keys)
        if unknown:
            raise HTTPException(422, f"Not options on this ballot: {unknown}")
        if any(v < 0 for v in body.balls.values()):
            raise HTTPException(422, "Ball counts can't be negative")
        total = sum(body.balls.values())
        if total != BALLOT_TOTAL:
            raise HTTPException(422, f"A ballot must total exactly {BALLOT_TOTAL} — this one totals {total}")

        node["ballots"][info["name"]] = {
            "balls": {k: v for k, v in body.balls.items() if v},
            "updated_at": _now(),
            "note": body.note.strip(),
            "conflict": _conflict_team(info["name"], slug, offers),
        }
        _save_ballots(ballots)
    log_write(info, f"PUT fa/players/{slug}/ballot")
    return node["ballots"][info["name"]]


@router.post("/api/fa/players/{slug}/finalize")
def finalize_player(slug: str, info: dict = Depends(require_role("fac_head"))):
    """Locks the ballots and records the totals.

    Unanswered remands warn, never block (D15) — they come back in the response
    so the confirm step can name them. A team that goes quiet can't stall a
    player indefinitely; a head who locks early does so knowingly.

    This is also the point at which offers are archived, which is what frees a
    team to bid on the same player again in a later round (§ 13.1).
    """
    with _fa_lock:
        state = _load_state()
        round_id = _round_id_for(state, slug)
        if not round_id:
            raise HTTPException(422, "This player hasn't been opened in a round yet")
        ballots = _load_ballots()
        node = _ballot_node(ballots, slug, round_id, create=True)
        if node.get("final"):
            raise HTTPException(409, "Already finalized")

        assigned = _player_entry(state, slug).get("subcommittee") or []
        totals: dict[str, int] = {}
        for cast in node["ballots"].values():
            for key, n in cast["balls"].items():
                totals[key] = totals.get(key, 0) + n
        offers = _load_offers()
        live = _offers_for(offers, slug)
        voided = [o for o in offers
                  if o["player"] == slug and o["status"] == "voided"
                  and o.get("archived_at") is None]
        outstanding = [
            {"offer": o["id"], "number": o["number"], "team": o["team"],
             "remands": [r for r in o["remands"] if r["from_version"] >= o["version"]]}
            for o in live if o["status"] == "returned"
        ]
        # Balls cast on an offer voided mid-round. `totals` stays exactly as
        # cast — the record is what the members voted, not what the software
        # thinks they'd have voted — so the head is told instead, alongside the
        # unanswered remands, and reads the totals knowing it.
        stranded = {o["id"]: (o["team"], o["number"]) for o in voided}
        node["final"] = {
            "locked_at": _now(), "locked_by": info["name"],
            # Stored, not recomputed on read: this is the record of what was
            # decided at that moment, and the offers it names are not guaranteed
            # to still be the current picture.
            "totals": totals,
            "voters": sorted(node["ballots"]),
            "abstained": [m for m in assigned if m not in node["ballots"]],
            "outstanding_remands": outstanding,
            "voided_options": [{"offer": oid, "team": team, "number": num,
                                "balls": totals[oid]}
                               for oid, (team, num) in stranded.items() if totals.get(oid)],
            "round_id": round_id,
        }
        # Voided offers are archived with the round too — they belong to it, and
        # leaving them unarchived would carry a dead bid into the next one and
        # keep `restore` open on a decided player.
        for o in live + voided:
            o["archived_at"] = node["final"]["locked_at"]
        state["players"].setdefault(slug, {})["status"] = "closed"
        _save_offers(offers)
        _save_ballots(ballots)
        _save_state(state)
    log_write(info, f"POST fa/players/{slug}/finalize")
    fa_notify.notify_player_finalized(slug, node["final"], live)
    return node["final"]


@router.post("/api/fa/players/{slug}/unlock")
def unlock_player(slug: str, info: dict = Depends(require_role("fac_head"))):
    """Escape hatch. Un-archives the offers this finalize archived — otherwise
    the restored ballots would refer to offers nobody can see."""
    with _fa_lock:
        state = _load_state()
        round_id = _round_id_for(state, slug)
        ballots = _load_ballots()
        node = _ballot_node(ballots, slug, round_id, create=True) if round_id else {}
        final = node.get("final")
        if not final:
            raise HTTPException(409, "Not finalized")
        node["final"] = None
        node.setdefault("unlocks", []).append(
            {"at": _now(), "by": info["name"], "undid": final})
        offers = _load_offers()
        for o in offers:
            if o["player"] == slug and o.get("archived_at") == final["locked_at"]:
                o["archived_at"] = None
        _save_offers(offers)
        _save_ballots(ballots)
        _save_state(state)
    log_write(info, f"POST fa/players/{slug}/unlock")
    return {"ok": True, "round_id": round_id}

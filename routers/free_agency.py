"""PDC free-agency offer pipeline — see nbn-today/docs/pdc-free-agency-spec.md.

Phase 1 only: the FA pool derivation, ported server-side so the board and the
`/free-agency` page can't disagree about who is a free agent. Everything else
in the spec (state, offers, ballots) lands in later phases.
"""
import json
from typing import Optional

from fastapi import APIRouter

from .constants import CAP_LEVELS_FILE
from .players import load_player_bios, _build_team_map
from .storage import _current_league_year
from .transactions import _rfa_eligibility, _min_salary_for

router = APIRouter()


def _load_cap_levels() -> dict:
    return json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}


def _parse_salary(raw) -> int:
    if not raw:
        return 0
    return int(str(raw).replace("$", "").replace(",", "").strip() or 0)


def _latest_salary(salaries: dict) -> int:
    if not salaries:
        return 0
    latest_yr = max(salaries.keys())
    return _parse_salary(salaries[latest_yr])


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
            prior_salary = _parse_salary(salaries.get(yr))
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

    return pool


@router.get("/api/fa/pool")
def get_fa_pool():
    bios = load_player_bios()
    team_map = _build_team_map()
    return _fa_pool(bios, team_map, _current_league_year())

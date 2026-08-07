import itertools
import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .league_time import league_today
from .constants import DATA_DIR, CAP_LEVELS_FILE, VALID_TEAMS
from .storage import read_csv, _parse_dollar, _current_league_year
from .players import load_player_bios, load_ovr
from .roster_picks import load_team_state, get_season_state
from .transactions import (
    TradeAsset, TradeTransfer, TradeValidateInput,
    _validate_trade, _check_salary_matching,
    _compute_team_salary, _compute_team_salary_ex_holds,
    _FA_HOLD_TYPES,
)

# A roster row is excluded from the search entirely (not just from salary
# sums) when it isn't a real, controllable trade asset:
#   - "dead"         — a cap hit with no active player attached
#   - "draft-rights" — an unsigned draft pick; carries no salary (rulebook
#                       § 7.1) and is a distinct asset class from a player
#                       contract (§ 4.6), so it's out of scope for a tool
#                       about swapping rostered players
# UFA/RFA cap holds are handled separately below, keyed off cap_holds[season]
# rather than type, since a player can still read type == "player" while
# only a free-agent hold remains on the books (§ 3.10) — not a tradeable
# asset, so excluded the same way _compute_team_salary_ex_holds does.
_NON_ASSET_TYPES = {"dead", "draft-rights"}

router = APIRouter()

# Search-space safety valves — a ~15-man roster already yields ~2,000 subsets
# up to size 4, so this stays cheap; the cap just guards against pathological
# inputs (e.g. a single minimum-salary player out, which opens a huge legal
# incoming band) rather than reflecting a real rulebook limit.
MAX_PACKAGE_SIZE = 5
DEFAULT_PACKAGE_SIZE = 4
MAX_PACKAGES_PER_TEAM = 10
# Cap on how many cheap-filter survivors get the expensive full _validate_trade
# call per team — that call re-reads roster/deadcap CSVs from disk, so this is
# the real latency knob. Survivors are tried best-OVR-first, so this rarely
# costs us a good result; it just stops us digging through low-value filler
# combos once we already have plenty of candidates.
MAX_VALIDATE_ATTEMPTS_PER_TEAM = 40


class TradeFinderFilters(BaseModel):
    min_ovr: int = 0
    positions: list[str] = Field(default_factory=list)
    max_age: Optional[int] = None


class TradeFinderRequest(BaseModel):
    team: str
    outgoing_slugs: list[str]
    filters: TradeFinderFilters = Field(default_factory=TradeFinderFilters)
    max_package_size: int = DEFAULT_PACKAGE_SIZE


def _age(dob: str, today: Optional[date] = None) -> Optional[int]:
    if not dob:
        return None
    try:
        y, m, d = (int(x) for x in dob.split("-"))
    except ValueError:
        return None
    t = today or league_today()
    years = t.year - y
    if (t.month, t.day) < (m, d):
        years -= 1
    return years


def _load_team_players(team: str, bios: dict, ovr_current: dict, season: str) -> list[dict]:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return []
    _, rows = read_csv(path)
    players = []
    for row in rows:
        slug = row.get("SLUG", "").strip()
        if not slug:
            continue
        bio = bios.get(slug, {})
        if bio.get("type") in _NON_ASSET_TYPES:
            continue
        if (bio.get("cap_holds") or {}).get(season) in _FA_HOLD_TYPES:
            continue
        salary = _parse_dollar((bio.get("salaries") or {}).get(season, ""))
        players.append({
            "slug": slug,
            "name": bio.get("name", slug),
            "pos": bio.get("pos", []),
            "age": _age(bio.get("dob", "")),
            "ovr": ovr_current.get(slug),
            "salary": salary,
        })
    return players


def _passes_filters(player: dict, filters: TradeFinderFilters) -> bool:
    if filters.min_ovr and (player["ovr"] is None or player["ovr"] < filters.min_ovr):
        return False
    if filters.positions and not (set(player["pos"]) & set(filters.positions)):
        return False
    if filters.max_age is not None and (player["age"] is None or player["age"] > filters.max_age):
        return False
    return True


def _team_financial_summary(team: str, current: int, current_ex_holds: int, cap_levels: dict,
                             team_state: dict, season: str) -> dict:
    cl = cap_levels.get(season, {})
    ts = get_season_state(team_state, team, season)
    apron1 = cl.get("apron1")
    apron2 = cl.get("apron2")
    mle_amount = {"ntmle": cl.get("ntmle_amount", 0), "tmle": cl.get("tmle_amount", 0)}.get(
        ts.get("mle_type") or "ntmle", 0
    )
    return {
        "team": team,
        "current_salary": current,
        "current_salary_ex_holds": current_ex_holds,
        "apron1_distance": (apron1 - current_ex_holds) if apron1 is not None else None,
        "apron2_distance": (apron2 - current_ex_holds) if apron2 is not None else None,
        "hard_cap": ts.get("hard_cap"),
        "hard_cap_reason": ts.get("hard_cap_reason", ""),
        "mle_used": ts.get("mle_used", 0),
        "mle_amount": mle_amount,
    }


@router.post("/api/trade-finder/search")
def trade_finder_search(body: TradeFinderRequest):
    """Search every other team's roster for legal 2-team return packages for the
    given outgoing players. All legality is decided by the same _validate_trade
    used by real trade submission and the transaction simulator — this endpoint
    only adds cheap pre-filtering (reusing _check_salary_matching, not a second
    copy of it) so the combinatorial search stays fast, then confirms every
    surviving candidate through the real validator before returning it.
    """
    team = body.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {team!r}")
    if not body.outgoing_slugs:
        raise HTTPException(status_code=422, detail="outgoing_slugs must be non-empty")
    max_size = max(1, min(body.max_package_size or DEFAULT_PACKAGE_SIZE, MAX_PACKAGE_SIZE))

    bios = load_player_bios()
    ovr_history = load_ovr()
    ovr_current = {slug: entries[-1]["ovr"] for slug, entries in ovr_history.items() if entries}
    team_state = load_team_state()
    cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    season = _current_league_year()
    ctx = {"bios": bios, "team_state": team_state, "cap_levels": cap_levels, "cur_season": season}

    my_players = {p["slug"]: p for p in _load_team_players(team, bios, ovr_current, season)}
    missing = [s for s in body.outgoing_slugs if s not in my_players]
    if missing:
        raise HTTPException(status_code=422, detail=f"Players not on {team}'s roster: {missing}")

    outgoing_total = sum(my_players[s]["salary"] for s in body.outgoing_slugs)
    my_current = _compute_team_salary(team, bios, season)
    my_current_ex_holds = _compute_team_salary_ex_holds(team, bios, season)

    results = []
    for other in sorted(VALID_TEAMS - {team}):
        other_players = _load_team_players(other, bios, ovr_current, season)
        by_slug = {p["slug"]: p for p in other_players}
        eligible_slugs = {s for s, p in by_slug.items() if _passes_filters(p, body.filters)}
        if not eligible_slugs:
            continue

        other_current = _compute_team_salary(other, bios, season)
        other_current_ex_holds = _compute_team_salary_ex_holds(other, bios, season)
        roster_slugs = list(by_slug.keys())

        # Pass 1 (cheap): reuse the real matching dispatcher, once per direction,
        # over every combo — pure arithmetic, no I/O, so this stays fast even
        # across ~2,000 combos. A roster with several near-zero-salary filler
        # players can still leave hundreds of combos surviving this pass, so we
        # rank by likely quality before paying for the expensive full validator.
        cheap_survivors = []
        for size in range(1, max_size + 1):
            for combo in itertools.combinations(roster_slugs, size):
                if not (set(combo) & eligible_slugs):
                    continue
                package_salary = sum(by_slug[s]["salary"] for s in combo)

                my_check = _check_salary_matching(
                    team, outgoing_total, package_salary, my_current, cap_levels, season,
                    team_state=team_state, team_salary_ex_holds_before=my_current_ex_holds,
                )
                if my_check is not None and not my_check.passed:
                    continue

                their_check = _check_salary_matching(
                    other, package_salary, outgoing_total, other_current, cap_levels, season,
                    team_state=team_state, team_salary_ex_holds_before=other_current_ex_holds,
                )
                if their_check is not None and not their_check.passed:
                    continue

                cheap_survivors.append((combo, package_salary))

        # Pass 2 (expensive): _validate_trade re-reads roster/deadcap CSVs per
        # call, so only run it — in likely-best-first order — on a bounded
        # number of survivors per team, stopping once enough legal packages
        # are found.
        cheap_survivors.sort(
            key=lambda cs: sum(by_slug[s]["ovr"] or 0 for s in cs[0]), reverse=True
        )

        legal_packages = []
        attempts = 0
        for combo, package_salary in cheap_survivors:
            if len(legal_packages) >= MAX_PACKAGES_PER_TEAM or attempts >= MAX_VALIDATE_ATTEMPTS_PER_TEAM:
                break
            attempts += 1

            details = TradeValidateInput(transfers=[
                TradeTransfer(from_team=team, to_team=other,
                               assets=[TradeAsset(type="player", slug=s) for s in body.outgoing_slugs]),
                TradeTransfer(from_team=other, to_team=team,
                               assets=[TradeAsset(type="player", slug=s) for s in combo]),
            ])
            checks = _validate_trade(details, ctx)
            legal = not any(not c.passed and c.level == "error" for c in checks)
            if not legal:
                continue

            legal_packages.append({
                "players": [by_slug[s] for s in combo],
                "total_salary": package_salary,
                "checks": checks,
            })

        if not legal_packages:
            continue

        truncated = len(cheap_survivors) > attempts or len(legal_packages) >= MAX_PACKAGES_PER_TEAM

        results.append({
            "team": other,
            "financial": _team_financial_summary(other, other_current, other_current_ex_holds,
                                                   cap_levels, team_state, season),
            "packages": legal_packages,
            "truncated": truncated,
        })

    return {
        "team": team,
        "outgoing_slugs": body.outgoing_slugs,
        "outgoing_salary": outgoing_total,
        "season": season,
        "results": results,
    }

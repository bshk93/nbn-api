"""Years of experience, counted the NBN way (league ruling, 2026-11-10).

A player's years of experience are his NBA years before the league began
(2020-21), plus one for every NBN regular season he spent at least one day on
a roster. That is the NBA's own rule (a year of service is a day on a
regular-season roster, played or not), applied to NBN seasons. It feeds every
rule that reads experience when a contract doesn't state it: § 2.2 two-way
eligibility, § 3.1 RFA/UFA, § 3.11 maximum salary, § 3.12 minimum tiers.

Before this, every one of those rules read `season - draft_year`, which is NBA
years: a player who sat a season out of the league, or was never signed after
he was drafted, read years he hadn't served.

There is no per-player roster history (cap history keeps team counts only), so
a roster day is taken from what is on file:

- a regular-season NBN game that season (the box scores, 20-21 on);
- a signing in the ledger that put him on a roster during that regular season
  (dated inside it, or before it with no release before opening night), plus
  each later season the signed contract pays him for, unless he was released
  before that season's opening night;
- being on a roster today, once this season's opening night has passed.

That undercounts a player who sat on a roster all season without playing and
whose signing predates the ledger's contract detail. A contract that states the
player's experience overrides all of this (`_contract_years_exp`), which is how
the office corrects one.

NBA years before 2020 come from the bio's draft year, which is the real draft
year for anyone drafted before the NBN draft existed. An undrafted player has
nothing to count them from, so he reads None: stated on the contract, or
judged by hand.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException

import season_clock
from stats_build.pipeline import player_slug

from . import season_calendar
from .constants import DATA_DIR, TRANSACTIONS_FILE, VALID_TEAMS
from .league_time import league_today_str

FIRST_NBN_YEAR = 2020

# Ledger types that put a player on a roster, and the ones that take him off.
_STARTS = {"sign", "sign_pick", "convert_twoway", "waiver_claim", "accept_qo"}
_ENDS = {"release", "void_player"}
_FA_HOLDS = {"UFA", "RFA"}
_ON_ROSTER_TYPES = {"player", "two-way"}

router = APIRouter()

_lock = threading.Lock()
_cache: dict = {"key": None, "service": {}}


def _start_year(season: str) -> int:
    return 2000 + int(season[:2])


def _holds(raw) -> dict:
    """A contract's cap holds; one old ledger row stores them as "28-29:RFA"."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return dict(p.split(":", 1) for p in raw.split(",") if ":" in p)
    return {}


def _mtime(path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _roster_paths():
    return [DATA_DIR / f"{t.lower()}-roster.csv" for t in VALID_TEAMS]


def _rostered_today() -> set[str]:
    """Slugs on a roster as a player or two-way, not as draft rights."""
    try:
        bios = json.loads((DATA_DIR / "player-bios.json").read_text())
    except (OSError, json.JSONDecodeError):
        bios = {}
    out = set()
    for path in _roster_paths():
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            slug = line.split(",")[0].strip().strip('"')
            if slug and (bios.get(slug) or {}).get("type") in _ON_ROSTER_TYPES:
                out.add(slug)
    return out


def build_service(games, txns: list[dict], rostered: set[str], today: str,
                  bounds_for=season_calendar.regular_season_bounds,
                  season_for_date=season_clock.season_for_date,
                  current_season: Optional[str] = None) -> dict[str, set[str]]:
    """slug -> the NBN seasons he spent a regular-season day on a roster.

    `games` is (slug, season, date) for every regular-season box score line.
    Pure apart from the two calendar lookups, so a test can hand it fixtures."""
    service: dict[str, set[str]] = defaultdict(set)
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for slug, season, date in games:
        service[slug].add(season)
        if date:
            if season not in first or date < first[season]:
                first[season] = date
            if season not in last or date > last[season]:
                last[season] = date

    def bounds(season):
        return bounds_for(season) or (
            (first[season], last[season]) if season in first else None)

    ends: dict[str, list[str]] = defaultdict(list)
    for t in txns:
        if t.get("type") in _ENDS:
            p = (t.get("details") or {}).get("player")
            if p and t.get("date"):
                ends[p].append(t["date"])

    for t in txns:
        if t.get("type") not in _STARTS:
            continue
        d = t.get("details") or {}
        player, signed = d.get("player"), t.get("date")
        if not player or not signed:
            continue
        signed_in = season_for_date(signed)
        contract = d.get("contract") or {}
        holds = _holds(contract.get("cap_holds"))
        later = [s for s in (contract.get("salaries") or {})
                 if holds.get(s) not in _FA_HOLDS and _start_year(s) > _start_year(signed_in)]
        for season in [signed_in, *later]:
            b = bounds(season)
            if not b or b[0] > today:
                continue
            opening, close = b
            if season == signed_in and signed > close:
                continue
            if signed < opening and any(signed < e < opening for e in ends[player]):
                continue
            service[player].add(season)

    if current_season is None:
        current_season = season_for_date(today)
    b = bounds(current_season)
    if b and b[0] <= today:
        for slug in rostered:
            service[slug].add(current_season)
    return service


def _games(index) -> list[tuple[str, str, str]]:
    strs, m = index.strings.values, index.meta
    return [(strs[m["slug"][i]], strs[m["season"][i]], strs[m["date"][i]])
            for i in range(index.n) if not index.playoff[i]]


def service_seasons(slug: str) -> set[str]:
    """The NBN seasons `slug` spent a regular-season day on a roster. Rebuilt
    when a box score, the ledger, a roster or the date changes."""
    from .game_highs import get_index

    index, index_key = get_index()
    today = league_today_str()
    key = (index_key, _mtime(TRANSACTIONS_FILE), _mtime(DATA_DIR / "player-bios.json"),
           tuple(_mtime(p) for p in _roster_paths()), today)
    with _lock:
        if _cache["key"] != key:
            try:
                txns = json.loads(TRANSACTIONS_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                txns = []
            rollovers = season_clock.load_rollovers()
            _cache["service"] = build_service(
                _games(index), txns, _rostered_today(), today,
                season_for_date=lambda d: season_clock.season_for_date(d, rollovers))
            _cache["key"] = key
        return set(_cache["service"].get(slug, ()))


def years_of_service(bio: dict, season: str, slug: Optional[str] = None) -> Optional[int]:
    """Years of experience entering `season`: NBA years before 2020-21 from the
    draft year, plus NBN seasons with a roster day before `season`. None for a
    player with no draft year on file."""
    dy = (bio or {}).get("draft_year")
    if not dy:
        return None
    slug = slug or player_slug(bio.get("name") or "")
    start = _start_year(season)
    pre = max(0, min(FIRST_NBN_YEAR, start) - int(dy))
    nbn = sum(1 for s in service_seasons(slug)
              if FIRST_NBN_YEAR <= _start_year(s) < start)
    return pre + nbn


@router.get("/api/years-of-service")
def get_years_of_service(season: Optional[str] = None):
    """Every player's years of experience entering `season` (default: the
    current league year), for the signing forms to prefill. Players with no
    draft year are left out: there is nothing to count from."""
    season = season or season_clock.current_season()
    if len(season) != 5 or season[2] != "-" or not season.replace("-", "").isdigit():
        raise HTTPException(400, "season must look like 26-27")
    try:
        bios = json.loads((DATA_DIR / "player-bios.json").read_text())
    except (OSError, json.JSONDecodeError):
        bios = {}
    years = {}
    for slug, bio in bios.items():
        n = years_of_service(bio, season, slug)
        if n is not None:
            years[slug] = n
    return {"season": season, "years": years}

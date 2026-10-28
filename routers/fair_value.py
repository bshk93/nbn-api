"""NBN Fair Value — a discounted-cash-flow price for every franchise.

`GET /api/fair-value` and `/api/fair-value/history`, behind the hidden
`/invest/fair/` page. A shadow model: nothing trades on it and nothing pays out.
It runs next to NBN Wall Street so its behaviour can be watched over a season
before anyone decides whether it should replace Wall Street's pricing.

Flavor, like the ledger it is built on (`stats_build/finances.py`, whose
revenue formulas and valuation it reuses). Nothing here touches NB¥ balances,
the cap or any rule.

The model, in full:

  **Value = PV(profit for the rest of this season and the next two)
          + PV(sale price at the end of the third season)**

  * **Strength.** This season: the playoff odds rating, except the roster's OVR
    never counts for less than `PRIOR_FLOOR` of it, so a trade moves the
    rest-of-season projection even in March (the odds model fades the roster to
    18% by game 60, which is right for odds and wrong for a price). Later
    seasons: the OVRs of who is under contract that season, aged by
    `AGE_CURVE`, with empty rotation spots filled at `replacement OVR`, then
    pulled toward the league average by `REVERT` per season.
  * **Draft picks.** A pick drafted before one of the projected seasons adds
    its player to that season's top eight: OVR from the pick number
    (`rookie_ovr`, fit on the last two classes), aged like everyone else, the
    slot from where the pick's team finishes in the simulated season before.
    Every team a contingent pick names gets an equal share. Only the next two
    drafts land inside the horizon, so picks move a price a little.
  * **Injuries.** This season's roster strength counts the injury report
    (`injured_strength`): an Out player is missing until his expected return
    (report date + the middle of 2K's duration), a Hurt one plays at his
    injured OVR, each for the share of the remaining games he misses. A past
    day uses the report as it stood that day (`injuries_as_of`).
  * **Who is under contract.** A season with a salary and no UFA/RFA tag. A
    player whose deal ends into a UFA/RFA hold the team holds can be re-signed
    at the hold amount: best OVR first, while payroll stays under the second
    apron, and past it when his market price (`fa_ask`) is at least his hold,
    since nobody lets a bargain go; never past `RESIGN_MAX_AGE`. The rest walk.
  * **Free agency.** The players who walk, league-wide, are the market
    (`free_agency`). Best first, each signs with whoever offers the most:
    cap room, else the non-taxpayer MLE under the first apron, else the
    taxpayer MLE under the second; between equal offers, the better team. His
    price is what 2026's signings paid for his OVR (`fa_ask`), up to the max
    salary; he takes a bit less (`FA_TAKE_LESS`) rather than not sign, and
    never goes back to the team that let him walk. A signing stays
    on the books, at that salary, for the rest of the projection. Spots still
    empty are filled at replacement level.
  * **Wins and playoffs.** Each season is simulated with the playoff odds
    model (`playoff_odds.simulate`), on this season's schedule. Revenue takes
    the expected value over every finish.
  * **Playoff edge.** Some staffs' teams beat their regular season in the
    playoffs, and keep doing it (`playoff_edges`). Each person on a team's
    staff (owner, GM or coach, from members.json tenures) carries the series
    they were on staff for: won minus what the two teams' point differentials
    predicted, summed and shrunk by `PLAYOFF_EDGE_SHRINK`. A team's edge is
    its current staff's average, worth `PLAYOFF_EDGE_POINTS` a game in
    playoff series only, this season and every projected one.
  * **Revenue.** The ledger's formulas on expected inputs: gate from projected
    win %, stars (players in the league's top `STAR_COUNT` OVRs) and the
    bandwagon from expected finishes; playoff gate at `HOME_PER_SERIES` home
    games a series; concessions on both crowds; merch, media, title bonus.
    Ticket price, base merch and local media scale with the team's market:
    the ledger's this season, then grown from each simulated finish
    (`finances.market_next`, re-centred like the ledger). Ticket price and
    base merch also scale with buzz: today's for the next ten games, then what
    the team's rating sustains (`rest_of_season_buzz`), and a later season's
    rating's.
    NBNTV is left out, as in the ledger's valuation, and so is the tax share,
    which would need every other team's payroll projected too.
  * **Costs.** Payroll is money, not cap (`cap_history._payroll`: contracts and
    dead cap, no holds), plus re-signings and replacement salaries for empty
    roster spots in later seasons; the ledger's tax on it; arena ops.
  * **Sale price.** The ledger's valuation, run on the three projected
    seasons: league multiple for that season × their mean revenue × brand
    (titles in the last six seasons, expected ones counted by probability).
  * **Discounting** at `DISCOUNT_RATE` per season, mid-season convention. This
    season counts only what is left of it. As the season goes by every price
    drifts up a little from discounting alone, so the page also shows each
    team against the league average.
  * **Price** = value ÷ `SHARES`. That is fair value: no noise, it moves only
    when something happened.
  * **Outlook.** The sale price averages the three projected seasons and one
    more, `OUTLOOK_YEARS` past the last: that roster held, everyone aged, every
    pick up to then drafted. A young core or a top pick counts for its rise
    without the far future pulling every team to .500.
  * **Market price** (`market_series`) is what it would trade at: fair value
    × exp(beta·league mood + team mood). Moods are mean-reverting walks with
    seeded fat-tailed shocks and GARCH volatility; news is overdone by
    `OVERREACT` and given back. Recomputed over the season's history on every
    write, so a rebuilt day carries forward.

Run hourly by `snapshot_playoff_odds.py`, after the odds. One row per team per
day is kept in `fair-value-history.jsonl` (a later run replaces the day's row),
so the page can show what moved each day and why. The simulation's seed is
fixed for the season (`season_seed`), so a day's move is what changed and not
sampling noise. After a model change, rebuild the season's rows so the change
does not read as a move: `venv/bin/python rebuild_fair_value.py`. A day is
its game date: a box score that arrives the next morning is recomputed into
its own day by the next run (`stale_days`), not counted on the day it arrived.

    venv/bin/python -m tests.test_fair_value
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from . import game_highs as gh
from . import injuries as inj
from . import playoff_odds as po
from .cap_history import _cap_levels_for, _payroll
from .constants import DATA_DIR, DERIVED_DIR, VALID_TEAMS
from .players import load_ovr, load_player_bios
from .storage import _atomic_write, _load_json, _parse_dollar, read_csv
from stats_build import finances as fin

router = APIRouter()

MODEL = "fv4"

# ── The model's constants. Every figure is NB¥. ──────────────────────────────

DISCOUNT_RATE = 0.08
SHARES = 30_000_000             # so a ¥4.5B franchise prices at ¥150
HORIZON = 3                     # this season and the next two, then the sale

# The outlook a buyer pays for, one season past the horizon: the last projected
# roster held as it is (nobody leaves, so it doesn't drift to .500), everyone
# aged OUTLOOK_YEARS more, every pick up to then in. It is one of the seasons
# the sale price averages, so a young core or a top pick counts for its rise.
OUTLOOK_YEARS = 2

# What a pick turns into: OVR = a + b·ln(pick number), fit on the 2025 and 2026
# classes' first ratings (n = 109, ±1.8), at the class's average age.
ROOKIE_OVR = (80.74, -3.04)
ROOKIE_AGE = {1: 20.2, 2: 22.4}

PRIOR_FLOOR = 0.5               # the roster's least weight in this season's rating
REVERT = 0.75                   # a later season keeps this much of its rating, per season out
SIMS_NOW, SIMS_LATER = 4000, 2000

# Yearly OVR change by age. A first guess; ovr-history.json is about a year
# deep, too thin to fit a curve from yet.
AGE_CURVE = ((22, 3), (24, 2), (26, 1), (29, 0), (31, -1), (33, -2), (99, -3))
RESIGN_MAX_AGE = 34
# A free agent's first-year salary as a share of the cap: ln(share) = a + b·OVR,
# fit on the 2026 offseason's 50 signings above ¥4M (Bird re-signings in,
# minimums out), ±0.44 in the log. A 80 asks ~¥11M, an 84 ~¥18M, an 88 ~¥30M.
FA_PRICE = (-12.946, 0.1278)
FA_MAX_SHARE = 0.35             # § 3.11's top tier; the fit passes 25% only above ~92 OVR
FA_TAKE_LESS = 0.65             # one standard deviation of the fit under his price
FA_EXCEPTIONS = ("ntmle_amount", "tmle_amount")
STAR_COUNT = 15                 # All-NBN is 15 players a season
ROSTER_FILL = 14                # later seasons fill the standard roster to this
HOME_PER_SERIES = 2.6           # measured: 467 playoff games over 90 series, 21 to 26
CAP_GROWTH = 0.05               # aprons for seasons not on file yet, per season

# The playoff edge. Fit on the 90 series 20-21 to 25-26, each scored only on
# the series before it. A person's record is shrunk by n/(n + 8): the fit can't
# tell 3 from 8 (log-likelihood +1.93 against +1.66 over differential alone),
# and 8 keeps one title run from counting as much as five seasons of them. At
# 8, a unit of staff edge moves the series logit 3.96 ± 2.26; it rests mostly
# on PHX, so half is used. The sim's series logit moves 0.194 a point a game,
# so that is 10.2 points a unit: PHX's +0.22 is about +2.2 a game.
PLAYOFF_EDGE_SHRINK = 8
SERIES_LOGIT_PER_DIFF = 0.0575  # what a point of season differential is worth in a series
PLAYOFF_EDGE_POINTS = 10.2
STANDINGS_CSV = DERIVED_DIR / "standings" / "standings-history.csv"
BRACKETS_CSV = DERIVED_DIR / "standings" / "playoff-brackets.csv"

HOLD_TAGS = {"UFA", "RFA"}

CURRENT_FILE = DATA_DIR / "fair-value.json"
HISTORY_FILE = DATA_DIR / "fair-value-history.jsonl"
FINANCES_CSV = DERIVED_DIR / fin.FINANCES_FILE


# ── Seasons ──────────────────────────────────────────────────────────────────

def season_after(season: str, n: int = 1) -> str:
    a, b = int(season[:2]), int(season[3:5])
    return f"{a + n:02d}-{b + n:02d}"


def season_index(season: str) -> int:
    """20-21 is 0: the ledger's valuation multiple is indexed this way."""
    return int(season[:2]) - 20


def age_in(dob: Optional[str], season: str) -> Optional[float]:
    """Age on Feb 1 of the season."""
    try:
        born = date.fromisoformat(dob)
    except (TypeError, ValueError):
        return None
    mid = date(2000 + int(season[3:5]), 2, 1)
    return (mid - born).days / 365.25


def aged_ovr(ovr: float, age: Optional[float], years: int) -> float:
    if age is None:
        return ovr
    for k in range(years):
        a = age + k
        ovr += next(d for limit, d in AGE_CURVE if a < limit + 1)
    return max(40.0, min(99.0, ovr))


def _strength(ovrs: list[float]) -> Optional[float]:
    """The odds model's roster strength: minutes-weighted mean of the top eight."""
    top = sorted(ovrs, reverse=True)[:len(po.MINUTES)]
    if not top:
        return None
    w = po.MINUTES[:len(top)]
    return sum(v * x for v, x in zip(top, w)) / sum(w)


# ── Injuries: who misses how much of what is left ────────────────────────────

INJURY_DAYS_SHORT = 2           # "Day-to-Day"
_DUR = re.compile(r"^(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?\s*(day|week|month)s?\b", re.I)


def injury_days(duration: Optional[str]) -> Optional[float]:
    """The middle of a 2K duration in days: "6-8 weeks" → 49, "Day-To-Day" → 2,
    "Career" → inf. None when it can't be read, and such an injury is left out."""
    d = (duration or "").strip()
    if inj._OPEN_ENDED.search(d):
        return float("inf")
    if inj._SHORT.search(d):
        return INJURY_DAYS_SHORT
    m = _DUR.match(d)
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2) or lo)
    return (lo + hi) / 2 * inj._UNIT_DAYS[m.group(3).lower()]


def injuries_as_of(day: str, history: Optional[list[dict]] = None) -> dict[str, dict]:
    """The injury list at the end of `day`, folded from the change log:
    slug → {status, duration, ovr_delta, date (of the report that last changed it)}."""
    events = inj.load_history() if history is None else history
    out: dict[str, dict] = {}
    for e in sorted((e for e in events if (e.get("date") or "") <= day), key=lambda e: e["date"]):
        if e.get("kind") == "healed":
            out.pop(e["slug"], None)
        elif e.get("after"):
            out[e["slug"]] = {**e["after"], "date": e["date"]}
    return out


def unplayed_games(schedule: list[dict], results: list[dict]) -> list[dict]:
    """The schedule's games with no result yet, matched on date and the two teams."""
    done: dict = {}
    for g in results:
        k = (g["date"], frozenset((g["home"], g["away"])))
        done[k] = done.get(k, 0) + 1
    left = []
    for g in schedule:
        k = (g.get("date"), frozenset((g["home_team"], g["away_team"])))
        if done.get(k):
            done[k] -= 1
        else:
            left.append(g)
    return left


def injured_strength(team: str, ovrs: dict[str, float], injuries: dict[str, dict],
                     left: list[dict]) -> tuple[Optional[float], list[dict]]:
    """A team's strength over the rest of the season, with its injuries in.

    An Out player misses the team's games until his expected return (report
    date + the middle of 2K's duration); a Hurt one plays them at his injured
    OVR. Each costs the strength drop without him, times the share of what is
    left that he misses. Returns (strength, [{slug, status, games, of}])."""
    full = _strength(list(ovrs.values()))
    games = sorted(g.get("date") or "" for g in left if team in (g["home_team"], g["away_team"]))
    if full is None or not games:
        return full, []
    cost, hurt = 0.0, []
    for slug, i in injuries.items():
        if slug not in ovrs:
            continue
        days = injury_days(i.get("duration"))
        if days is None:
            continue
        back = "9999-12-31" if days == float("inf") else \
            (date.fromisoformat(i["date"]) + timedelta(days=round(days))).isoformat()
        missed = sum(d < back for d in games)
        if not missed:
            continue
        rest = dict(ovrs)
        if (i.get("status") or "").lower() == "out":
            del rest[slug]
        else:
            rest[slug] = ovrs[slug] + min(0, i.get("ovr_delta") or 0)
        without = _strength(list(rest.values()))
        if without is None or without >= full:
            continue
        cost += (full - without) * missed / len(games)
        hurt.append({"slug": slug, "status": i.get("status"), "games": missed, "of": len(games),
                     "cost": round((full - without) * missed / len(games), 2)})
    return full - cost, sorted(hurt, key=lambda h: -h["cost"])


# ── Draft picks ──────────────────────────────────────────────────────────────

def rookie_ovr(pick: float) -> float:
    a, b = ROOKIE_OVR
    return a + b * math.log(max(1.0, pick))


def draft_after(year: int) -> str:
    """The season a draft follows: the 2027 draft comes after 26-27."""
    return f"{(year - 1) % 100:02d}-{year % 100:02d}"


def load_picks() -> list[dict]:
    """Every pick as GET /api/picks serves it from the conveyance model."""
    try:
        from picks_conveyance.serve import flat_rows
        return flat_rows()
    except Exception:
        return []


def pick_claims(picks: list[dict], season: str) -> dict[str, list[dict]]:
    """Team -> its share of every pick in a draft still to come.

    Every team a contingent pick names (a swap, a protection band, a ladder)
    gets an equal share of it: the conveyance model says who has a claim, not
    how likely each one is to land it."""
    out: dict[str, list[dict]] = {}
    for p in picks:
        if season_index(draft_after(p["year"])) < season_index(season):
            continue
        parties = list(dict.fromkeys(l["team"] for l in (p.get("leaves") or []) if l.get("team")))
        if not parties:
            owner = p.get("owner") or "?"
            parties = [p["orig"]] if owner == "?" else owner.split("|")
        for t in parties:
            out.setdefault(t, []).append({"year": p["year"], "round": p["round"], "orig": p["orig"],
                                          "share": 1 / len(parties)})
    return out


def draft_slots(row: dict[str, dict]) -> dict[str, int]:
    """Team -> where its own pick lands in a draft, from a simulated season:
    fewest projected wins picks first. The lottery is left out."""
    order = sorted(row, key=lambda t: (row[t]["proj_w"], t))
    return {t: i + 1 for i, t in enumerate(order)}


# ── Who is on the books, season by season ────────────────────────────────────

def _roster(team: str) -> list[str]:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return []
    _, rows = read_csv(path)
    return [(r.get("SLUG") or "").strip() for r in rows if (r.get("SLUG") or "").strip()]


def contract_year(bio: dict, season: str, since: Optional[str] = None) -> Optional[str]:
    """'contract', 'resigned' or None for one player in one season.

    Re-signed: his deal ends into a UFA/RFA hold after `since` (the season the
    projection starts from) and on or before `season`, so the team holds his
    rights and is assumed to use them. A hold in `since` itself is a free agent
    nobody has signed, and an older one belongs to a deal already settled.
    """
    if bio.get("type") in ("draft-rights", "dead"):
        return None
    sal, holds = bio.get("salaries") or {}, bio.get("cap_holds") or {}
    if sal.get(season) and holds.get(season) not in HOLD_TAGS:
        return "contract"
    if since and _resign_hold(bio, season, since):
        return "resigned"
    return None


def _resign_hold(bio: dict, season: str, since: str) -> Optional[str]:
    sal, holds = bio.get("salaries") or {}, bio.get("cap_holds") or {}
    for s in sorted(holds, reverse=True):
        if holds[s] in HOLD_TAGS and since < s <= season:
            last = season_after(s, -1)
            if sal.get(last) and holds.get(last) not in HOLD_TAGS:
                return s
    return None


def hold_amount(bio: dict, season: str, since: str) -> int:
    """What a re-signed player is paid: the hold his deal ended into."""
    s = _resign_hold(bio, season, since)
    return _parse_dollar((bio.get("salaries") or {}).get(s, "")) if s else 0


def books(team: str, season: str, years_out: int, bios: dict, ovr_now: dict,
          contracted: float, ceiling: Optional[float], kept: set,
          gone: frozenset = frozenset(), cap: Optional[float] = None) -> dict:
    """Players on the books for `season`: OVRs (aged), re-signing money, standard count.

    A team re-signs its free agents best OVR first, while payroll (`contracted`
    plus re-signings) stays under `ceiling`, the second apron, or past it for
    a player whose market price on `cap` is at least his hold. The rest walk,
    and are returned as `walked`, the free agency pool. Anyone in `kept`,
    re-signed the season before, stays: that money is committed. Anyone in
    `gone` signed elsewhere in an earlier projected season.
    """
    since = season_after(season, -years_out)
    players, candidates = [], []
    for slug in _roster(team):
        if slug in gone:
            continue
        bio = bios.get(slug) or {}
        kind = contract_year(bio, season, since)
        if kind is None:
            continue
        base = ovr_now.get(slug)
        p = {"slug": slug, "kind": kind, "two_way": bio.get("type") == "two-way",
             "ovr": None if base is None else aged_ovr(base, age_in(bio.get("dob"), since), years_out)}
        if kind == "resigned":
            age = age_in(bio.get("dob"), season)
            if age is not None and age > RESIGN_MAX_AGE and slug not in kept:
                continue
            p["money"] = hold_amount(bio, season, since)
            candidates.append(p)
        else:
            players.append(p)
    resign_money, walked = 0, []
    candidates.sort(key=lambda p: (p["slug"] not in kept, -(p["ovr"] or 0)))
    for p in candidates:
        over = ceiling is not None and contracted + resign_money + p["money"] > ceiling
        bargain = bool(cap and p["ovr"] is not None and fa_ask(p["ovr"], cap) >= p["money"])
        if p["slug"] in kept or not over or bargain:
            resign_money += p["money"]
            players.append(p)
        else:
            walked.append({**p, "from": team})
    return {"players": players, "resign_money": resign_money, "walked": walked,
            "resigned": {p["slug"] for p in players if p["kind"] == "resigned"},
            "standard": sum(1 for p in players if not p["two_way"])}


def _ovr_now(ovr: dict) -> dict:
    return {s: float(h[-1]["ovr"]) for s, h in ovr.items() if h and h[-1].get("ovr") is not None}


def replacement(teams: list[str], bios: dict, ovr_now: dict, season: str) -> tuple[float, int]:
    """(OVR, salary) of a bench player: the league's 6th to 8th men this season."""
    ovrs, pays = [], []
    for t in teams:
        rows = []
        for slug in _roster(t):
            bio = bios.get(slug) or {}
            if contract_year(bio, season) == "contract" and slug in ovr_now and bio.get("type") != "two-way":
                rows.append((ovr_now[slug], _parse_dollar((bio.get("salaries") or {}).get(season, ""))))
        rows.sort(reverse=True)
        for o, p in rows[5:8]:
            ovrs.append(o)
            pays.append(p)
    if not ovrs:
        return 75.0, 5_000_000
    pays.sort()
    return sum(ovrs) / len(ovrs), pays[len(pays) // 2]


def aprons(season: str, base_season: str) -> tuple[Optional[float], Optional[float]]:
    lv = _cap_levels_for(season)
    if lv["apron1"] and lv["apron2"]:
        return lv["apron1"], lv["apron2"]
    base = _cap_levels_for(base_season)
    if not (base["apron1"] and base["apron2"]):
        return None, None
    g = (1 + CAP_GROWTH) ** (season_index(season) - season_index(base_season))
    return base["apron1"] * g, base["apron2"] * g


def cap_level(season: str, base_season: str) -> Optional[float]:
    """The cap, grown like `aprons` when the season isn't on file. The tax's
    brackets are a share of it."""
    cap = _cap_levels_for(season)["cap"]
    if cap:
        return cap
    base = _cap_levels_for(base_season)["cap"]
    if not base:
        return None
    return base * (1 + CAP_GROWTH) ** (season_index(season) - season_index(base_season))


def exceptions(season: str, base_season: str) -> dict[str, float]:
    """The two MLEs, grown like `aprons` when the season isn't on file."""
    from .cap_history import CAP_LEVELS_FILE
    levels = _load_json(CAP_LEVELS_FILE, {})
    on_file = levels.get(season) or {}
    base = levels.get(base_season) or {}
    g = (1 + CAP_GROWTH) ** (season_index(season) - season_index(base_season))
    return {k: float(on_file.get(k) or (base.get(k) or 0) * g) for k in FA_EXCEPTIONS}


# ── Free agency: who signs the players who walk ──────────────────────────────

def fa_ask(ovr: float, cap: float) -> float:
    a, b = FA_PRICE
    return cap * min(FA_MAX_SHARE, math.exp(a + b * ovr))


def fa_budget(payroll: float, cap: Optional[float], apron1: Optional[float],
              apron2: Optional[float], exc: dict[str, float]) -> float:
    """The most a team can pay one free agent: its cap room, else the MLE its
    payroll allows, else nothing."""
    if cap and payroll < cap:
        return max(cap - payroll, exc["ntmle_amount"] if apron1 and payroll < apron1 else 0.0)
    if apron1 and payroll < apron1:
        return exc["ntmle_amount"]
    if apron2 and payroll < apron2:
        return exc["tmle_amount"]
    return 0.0


def free_agency(pool: list[dict], budgets: dict[str, float], open_spots: dict[str, int],
                cap: float, floor_ovr: float, floor_pay: float,
                standing: Optional[dict[str, float]] = None) -> dict[str, list[dict]]:
    """Team -> the free agents it signs, each with what he is paid.

    Best OVR first. Each goes to the team with a roster spot that offers the
    most, at least FA_TAKE_LESS of his price: his price or that team's whole
    budget, whichever is less. Between equal offers he picks the better team
    (`standing`, last season's rating), never the alphabet. Room and an MLE are
    both spent down, so an MLE can be split, as § 3 allows. Nobody above replacement goes back to the team that
    let him walk, and nobody at or below it is worth modelling: the roster fill
    already covers him.
    """
    budgets, open_spots = dict(budgets), dict(open_spots)
    out: dict[str, list[dict]] = {t: [] for t in budgets}
    for p in sorted(pool, key=lambda p: (-(p["ovr"] or 0), p["slug"])):
        if p["ovr"] is None or p["ovr"] <= floor_ovr:
            break
        ask = fa_ask(p["ovr"], cap)
        need = max(floor_pay, FA_TAKE_LESS * ask)
        bidders = [t for t in budgets if t != p["from"] and open_spots[t] > 0 and budgets[t] >= need]
        if not bidders:
            continue
        t = max(bidders, key=lambda t: (min(ask, budgets[t]), (standing or {}).get(t, 0.0), t))
        pay = min(ask, budgets[t])
        out[t].append({**p, "kind": "fa", "money": round(pay)})
        budgets[t] = budgets[t] - pay if budgets[t] > pay else 0.0
        open_spots[t] -= 1
    return out


# ── Playoff edge: staffs whose teams beat their season in the playoffs ───────

def playoff_day(season: str) -> str:
    """The day a season's playoff staff is read on: May 1 of its second year."""
    return f"20{int(season[3:5]):02d}-05-01"


def staff_on(members: dict, team: str, day: str) -> list[str]:
    """Everyone on a team's staff on `day`: owner, GM or coach, all equal."""
    return sorted(name for name, m in members.items() for t in (m.get("tenures") or [])
                  if (t.get("team") or "").upper() == team and (t.get("start") or "9999") <= day
                  <= (t.get("end") or "9999"))


def playoff_records(members: dict, brackets: list[dict], standings: list[dict],
                    before: str) -> dict[str, list[float]]:
    """Person -> each series before `before` he was on staff for: 1 for a win,
    0 for a loss, less the chance the two point differentials gave his team."""
    diff = {(r["SEASON"], r["TEAM"]): _num(r.get("DIFF")) for r in standings}
    out: dict[str, list[float]] = {}
    for b in brackets:
        s = b.get("SEASON") or ""
        if s >= before or (s, b["T1"]) not in diff or (s, b["T2"]) not in diff:
            continue
        p1 = 1 / (1 + math.exp(-SERIES_LOGIT_PER_DIFF * (diff[(s, b["T1"])] - diff[(s, b["T2"])])))
        day = playoff_day(s)
        for team, won, p in ((b["T1"], b["WINNER"] == b["T1"], p1), (b["T2"], b["WINNER"] == b["T2"], 1 - p1)):
            for name in staff_on(members, team, day):
                out.setdefault(name, []).append(float(won) - p)
    return out


def person_edge(record: list[float]) -> float:
    return sum(record) / (len(record) + PLAYOFF_EDGE_SHRINK)


def playoff_edges(season: str, today: str, teams: list[str], members: Optional[dict] = None,
                  brackets: Optional[list[dict]] = None, standings: Optional[list[dict]] = None) -> dict:
    """Team -> {points, edge, staff}: its current staff's average edge, and the
    points a playoff game that is worth. Someone with no playoff series is 0."""
    if members is None:
        from .auth import load_members
        members = load_members()
    if brackets is None:
        brackets = read_csv(BRACKETS_CSV)[1] if BRACKETS_CSV.exists() else []
    if standings is None:
        standings = read_csv(STANDINGS_CSV)[1] if STANDINGS_CSV.exists() else []
    records = playoff_records(members, brackets, standings, season)
    out = {}
    for t in teams:
        staff = [{"name": n, "series": len(records.get(n, [])), "edge": round(person_edge(records.get(n, [])), 4)}
                 for n in staff_on(members, t, today)]
        edge = sum(x["edge"] for x in staff) / len(staff) if staff else 0.0
        out[t] = {"edge": round(edge, 4), "points": round(PLAYOFF_EDGE_POINTS * edge, 2), "staff": staff}
    return out


# ── Revenue on expected inputs ───────────────────────────────────────────────

def finish_probs(row: dict) -> dict[str, float]:
    """A simulated row's chance of each finish the bandwagon pays for."""
    return {
        "Champion": row["title"],
        "Runner-Up": max(0.0, row["finals"] - row["title"]),
        "Conf Finals": max(0.0, row["conf_finals"] - row["finals"]),
        "Second Round": max(0.0, row["second_round"] - row["conf_finals"]),
    }


def expected_bandwagon(finishes: list[dict[str, float]]) -> float:
    """Bandwagon lift from the last three seasons' finishes, most recent first,
    each a {finish: probability} (an actual result is probability 1)."""
    return sum(weight * sum(fin.BANDWAGON.get(f, 0.0) * p for f, p in finish.items())
               for weight, finish in zip(fin.BANDWAGON_FADE, finishes))


def all_finishes(row: dict) -> dict[str, float]:
    """A simulated row's chance of every finish, missing the playoffs included."""
    return {**finish_probs(row),
            "First Round": max(0.0, row["playoffs"] - row["second_round"]),
            "Missed": max(0.0, 1 - row["playoffs"])}


def gate_per_game(win_pct: float, stars: int, away_stars: float, bandwagon: float, market: float = 1.0) -> float:
    fill = (fin.FILL_BASE + fin.FILL_FORM * win_pct + fin.FILL_HOME_STAR * min(fin.STAR_CAP, stars)
            + fin.FILL_AWAY_STAR * away_stars + bandwagon)
    return fin.SEATS * fin.TICKET * market * min(fin.FILL_MAX, max(fin.FILL_MIN, fill))


def playoff_gate(row: dict, market: float = 1.0) -> float:
    reach = {1: row["playoffs"], 2: row["second_round"], 3: row["conf_finals"], 4: row["finals"]}
    return sum(p * HOME_PER_SERIES * fin.SEATS * fin.TICKET * market * fin.PLAYOFF_MULT[r] for r, p in reach.items())


def season_revenue(gate: float, row: dict, stars: int, market: float = 1.0, buzz: float = 0.0) -> dict:
    merch = fin.MERCH_BASE * market * (1 + buzz) + fin.MERCH_STAR * stars + fin.MERCH_TITLE * row["title"]
    title = fin.TITLE_BONUS * row["title"]
    pog = playoff_gate(row, market)
    conc = concessions(gate, row, market * (1 + buzz))
    media = fin.MEDIA_SHARE + fin.LOCAL_MEDIA * market
    return {"gate": gate, "playoff_gate": pog, "concessions": conc, "merch": merch,
            "media": media, "title_bonus": title,
            "revenue": gate + pog + conc + merch + media + title}


def playoff_crowd(row: dict) -> float:
    reach = (row["playoffs"], row["second_round"], row["conf_finals"], row["finals"])
    return sum(p * HOME_PER_SERIES * fin.SEATS for p in reach)


def concessions(gate: float, row: dict, price: float = 1.0) -> float:
    """Spend per head, on the regular-season crowd the gate implies plus a
    sold-out playoff crowd, as the ledger counts it. `price` is the ticket's
    multiplier (market × buzz), to turn the gate back into a crowd."""
    return fin.CONCESSIONS_PER_FAN * (gate / (fin.TICKET * price) + playoff_crowd(row))


def rating_buzz(rating: float) -> float:
    """The buzz a team settles at when it plays to its rating: the ledger's
    buzz is BUZZ_PER_POINT × its average opponent-adjusted margin, and the
    rating is the expected margin against an average team."""
    return min(fin.BUZZ_MAX, max(fin.BUZZ_MIN, fin.BUZZ_PER_POINT * rating))


def rest_of_season_buzz(now: float, rating: float, left: int) -> float:
    """Average buzz over the games left: today's for the next BUZZ_WINDOW,
    then whatever the rating sustains."""
    if left <= 0:
        return now
    near = min(fin.BUZZ_WINDOW, left)
    return (near * now + (left - near) * rating_buzz(rating)) / left


def discount(t: float) -> float:
    return (1 + DISCOUNT_RATE) ** -t


# ── The ledger's own history, for what has already happened ──────────────────

def load_ledger() -> list[dict]:
    if not FINANCES_CSV.exists():
        return []
    _, rows = read_csv(FINANCES_CSV)
    return rows


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ── The run ──────────────────────────────────────────────────────────────────

def _rating_inputs(results: list[dict]) -> tuple[dict, dict]:
    margin, played = {}, {}
    for g in results:
        d = g["home_pts"] - g["away_pts"]
        margin[g["home"]] = margin.get(g["home"], 0) + d
        margin[g["away"]] = margin.get(g["away"], 0) - d
        played[g["home"]] = played.get(g["home"], 0) + 1
        played[g["away"]] = played.get(g["away"], 0) + 1
    return margin, played


def floored_priors(priors: dict, margin: dict, played: dict) -> dict:
    """Priors that make `simulate` rate each team as
    a·prior + (1−a)·margin per game, with a = max(PRIOR_FLOOR, its own fade).

    `simulate` computes (prior·K + margin) / (K + games); this solves that for
    the prior that lands on the floored rating, so the simulator is unchanged.
    """
    K = po.PRIOR_GAMES
    out = {}
    for t, p in priors.items():
        g, m = played.get(t, 0), margin.get(t, 0)
        if not g:
            out[t] = p
            continue
        a = max(PRIOR_FLOOR, K / (K + g))
        target = a * p + (1 - a) * m / g
        out[t] = (target * (K + g) - m) / K
    return out


def season_seed(season: str) -> int:
    """One seed for the whole season, so a day's move is only what changed.

    The simulation's error is ±0.2% of a price on average (up to 0.6%), a third
    of a win's effect. A new seed each day put that error into every daily
    move; with the same seed it is the same error each day and cancels out.
    """
    return 1000 * (season_index(season) + 1)


def compute(season: str, today: str, inputs: Optional[dict] = None) -> dict:
    """Every team's fair value. Pure apart from reading the data dir; writes nothing.

    `inputs` replaces any of the loaded data (tests): bios, ovr, results,
    schedule, ledger.
    """
    inputs = inputs or {}
    bios = inputs.get("bios") or load_player_bios()
    ovr_now = _ovr_now(inputs.get("ovr") or load_ovr())
    if "results" in inputs:
        results = inputs["results"]
    else:
        index, _ = gh.get_index()
        results = po.season_results(index, season)
    schedule = inputs.get("schedule") or po.load_schedule(season)
    ledger = inputs.get("ledger") if "ledger" in inputs else load_ledger()
    teams = sorted(VALID_TEAMS)
    seed = season_seed(season)
    edges = inputs["playoff_edges"] if "playoff_edges" in inputs else playoff_edges(season, today, teams)
    bonus = {t: e["points"] for t, e in edges.items()}

    total = HORIZON
    seasons = [season_after(season, k) for k in range(total)]
    claims = pick_claims(inputs["picks"] if "picks" in inputs else load_picks(), season)

    # This season: the odds model with the roster floored in.
    rostered = {t: {s: ovr_now[s] for s in _roster(t) if s in ovr_now
                    and (bios.get(s) or {}).get("type") not in ("draft-rights", "dead")} for t in teams}
    strength0 = {t: _strength(list(rostered[t].values())) for t in teams}
    # This season's rating is the rest of it, so it counts who is hurt for how much of that.
    injuries = inputs["injuries"] if "injuries" in inputs else injuries_as_of(today)
    left = unplayed_games(schedule, results)
    healthy_or_not = {t: injured_strength(t, rostered[t], injuries, left) for t in teams}
    priors0 = po.roster_priors({t: v[0] for t, v in healthy_or_not.items()})
    margin, played = _rating_inputs(results)
    sims = [po.simulate(results, schedule, floored_priors(priors0, margin, played), sims=SIMS_NOW, seed=seed,
                        playoff_bonus=bonus)]

    # Later seasons: who is on the books, aged, filled and pulled to the middle.
    rep_ovr, rep_pay = replacement(teams, bios, ovr_now, season)
    later_books, strengths = {}, [strength0]
    kept = {t: set() for t in teams}
    signed: dict[str, list[dict]] = {t: [] for t in teams}   # free agents signed so far, with their base OVR
    gone: set[str] = set()
    went: dict[tuple[str, str], str] = {}                    # (season, slug) -> the team that signed him
    slots: dict[int, dict[str, int]] = {}
    drafted: dict[str, list[dict]] = {t: [] for t in teams}

    def filled(ovrs: list[float]) -> Optional[float]:
        return _strength(ovrs + [rep_ovr] * max(0, len(po.MINUTES) - len(ovrs)))

    for k in range(1, total):
        # The draft after season k-1, slotted by that season's simulation.
        slots[k - 1] = draft_slots({t: r for t, r in ((x["team"], x) for x in sims[k - 1]["teams"])})
        s = seasons[k]
        a1, a2 = aprons(s, season)
        cap_s = cap_level(s, season)
        carried = {t: sum(p["money"] for p in signed[t]) for t in teams}
        bk = {t: books(t, s, k, bios, ovr_now, _payroll(t, bios, s)[0] + carried[t], a2, kept[t],
                       frozenset(gone), cap_s) for t in teams}
        kept = {t: bk[t]["resigned"] for t in teams}
        # Free agents signed in an earlier projected season stay, a year older.
        for t in teams:
            for p in signed[t]:
                age = age_in((bios.get(p["slug"]) or {}).get("dob"), season)
                bk[t]["players"].append({**p, "ovr": aged_ovr(p["base"], age, k)})
            bk[t]["standard"] += len(signed[t])
            bk[t]["fa_money"] = carried[t]
            bk[t]["fa_signed"] = []
        if cap_s:
            exc = exceptions(s, season)
            pay_now = {t: _payroll(t, bios, s)[0] + bk[t]["resign_money"] + carried[t] for t in teams}
            budgets = {t: fa_budget(pay_now[t], cap_s, a1, a2, exc) for t in teams}
            spots = {t: max(0, ROSTER_FILL - bk[t]["standard"]) for t in teams}
            pool = [p for t in teams for p in bk[t]["walked"] if p["slug"] not in gone]
            standing = {x["team"]: x["rating"] for x in sims[k - 1]["teams"]}
            for t, got in free_agency(pool, budgets, spots, cap_s, rep_ovr, rep_pay, standing).items():
                for p in got:
                    base = ovr_now.get(p["slug"])
                    signed[t].append({"slug": p["slug"], "kind": "fa", "two_way": False, "base": base,
                                      "money": p["money"], "from": p["from"]})
                    bk[t]["players"].append({**p})
                    bk[t]["standard"] += 1
                    bk[t]["fa_money"] += p["money"]
                    bk[t]["fa_signed"].append({"slug": p["slug"], "ovr": round(p["ovr"], 1),
                                               "salary": p["money"], "from": p["from"]})
                    gone.add(p["slug"])
                    went[(s, p["slug"])] = t
        later_books[s] = bk
        st = {}
        for t in teams:
            ovrs = [p["ovr"] for p in bk[t]["players"] if p["ovr"] is not None and not p["two_way"]]
            base = filled(ovrs)
            # Each pick already drafted by now adds its share of what its
            # player does for the top eight, one at a time.
            gain = 0.0
            for c in claims.get(t, []):
                kd = season_index(draft_after(c["year"])) - season_index(season)
                if kd >= k or kd not in slots:
                    continue
                slot = slots[kd].get(c["orig"], 15) + (30 if c["round"] == 2 else 0)
                ovr = aged_ovr(rookie_ovr(slot), ROOKIE_AGE[c["round"]], k - kd - 1)
                with_him = filled(ovrs + [ovr])
                if base is not None and with_him is not None:
                    # A pick below replacement level is cut for one: never a loss.
                    gain += c["share"] * max(0.0, with_him - base)
                if k == kd + 1:
                    drafted[t].append({**c, "slot": slot, "ovr": round(rookie_ovr(slot), 1)})
            st[t] = None if base is None else base + gain
        strengths.append(st)
        pri = {t: v * REVERT ** k for t, v in po.roster_priors(st).items()}
        sims.append(po.simulate([], schedule, pri, sims=SIMS_LATER, seed=seed + k, playoff_bonus=bonus))
    # The outlook season (see OUTLOOK_YEARS).
    slots[total - 1] = draft_slots({x["team"]: x for x in sims[total - 1]["teams"]})
    k_out = total - 1 + OUTLOOK_YEARS
    outlook_strength = {}
    for t in teams:
        if total > 1:
            held_players = later_books[seasons[-1]][t]["players"]
            ovrs = [aged_ovr(p["ovr"], age_in((bios.get(p["slug"]) or {}).get("dob"), seasons[-1]), OUTLOOK_YEARS)
                    for p in held_players if p["ovr"] is not None and not p["two_way"]]
        else:
            ovrs = list(rostered[t].values())
        base = filled(ovrs)
        gain = 0.0
        for c in claims.get(t, []):
            kd = season_index(draft_after(c["year"])) - season_index(season)
            if kd >= k_out:
                continue
            slot = (slots.get(kd) or slots[max(slots)]).get(c["orig"], 15) + (30 if c["round"] == 2 else 0)
            ovr = aged_ovr(rookie_ovr(slot), ROOKIE_AGE[c["round"]], k_out - kd - 1)
            with_him = filled(ovrs + [ovr])
            if base is not None and with_him is not None:
                gain += c["share"] * max(0.0, with_him - base)
        outlook_strength[t] = None if base is None else base + gain
    pri = {t: v * REVERT ** (total - 1) for t, v in po.roster_priors(outlook_strength).items()}
    outlook_sim = po.simulate([], schedule, pri, sims=SIMS_LATER, seed=seed + k_out, playoff_bonus=bonus)
    outlook_rows = {r["team"]: r for r in outlook_sim["teams"]}
    rows = [{r["team"]: r for r in run["teams"]} for run in sims]

    # Stars: the league's top STAR_COUNT OVRs on the books that season.
    def star_counts(k: int) -> dict[str, int]:
        pool = []
        for t in teams:
            if k == 0:
                pool += [(ovr_now[s], t) for s in _roster(t) if s in ovr_now
                         and contract_year(bios.get(s) or {}, season) == "contract"]
            else:
                pool += [(p["ovr"], t) for p in later_books[seasons[k]][t]["players"] if p["ovr"] is not None]
        pool.sort(reverse=True)
        out = dict.fromkeys(teams, 0)
        for _, t in pool[:STAR_COUNT]:
            out[t] += 1
        return out
    stars = [star_counts(k) for k in range(total)]
    away = [sum(min(fin.STAR_CAP, v) for v in st.values()) / len(teams) for st in stars]

    # What has already happened, from the ledger.
    led = {(r["SEASON"], r["TEAM"]): r for r in ledger}
    league0_rows = [r for r in ledger if r["SEASON"] == "20-21"]
    league0 = (sum(_num(r["REVENUE"]) for r in league0_rows) / len(league0_rows)) if league0_rows else None
    played_home = {t: 0 for t in teams}
    for g in results:
        played_home[g["home"]] += 1
    sched_home = {t: 0 for t in teams}
    for g in schedule:
        if g["home_team"] in sched_home:
            sched_home[g["home_team"]] += 1

    # Market: this season's is the ledger's; each later one grows from the
    # season before's simulated finish and win %, re-centred as the ledger does.
    # Buzz: this season's runs from today's (the ledger's) toward what the
    # rating sustains; a later season's is its rating's. Each season's average
    # feeds the next one's market, as in the ledger.
    buzz_now = {t: _num((led.get((season, t)) or {}).get("BUZZ")) for t in teams}
    buzz_avg_so_far = {t: _num((led.get((season, t)) or {}).get("BUZZ_AVG")) for t in teams}
    buzz = [{} for _ in range(total)]
    for t in teams:
        r0 = rows[0][t]
        games0 = r0["games"] or 82
        played = r0["w"] + r0["l"]
        rest = rest_of_season_buzz(buzz_now[t], r0["rating"], games0 - played)
        buzz[0][t] = {"rest": rest, "season": (played * buzz_avg_so_far[t] + (games0 - played) * rest) / games0}
        for k in range(1, total):
            b = rating_buzz(rows[k][t]["rating"])
            buzz[k][t] = {"rest": b, "season": b}
    mkt = [{t: _num((led.get((season, t)) or {}).get("MARKET")) or 1.0 for t in teams}]
    for k in range(1, total):
        mkt.append(fin.recentre({t: fin.market_next(mkt[k - 1][t], all_finishes(rows[k - 1][t]),
                                                    rows[k - 1][t]["proj_w"] / (rows[k - 1][t]["games"] or 82),
                                                    buzz[k - 1][t]["season"])
                                 for t in teams}))

    mkt_out = fin.recentre({t: fin.market_next(mkt[-1][t], all_finishes(rows[-1][t]),
                                               rows[-1][t]["proj_w"] / (rows[-1][t]["games"] or 82),
                                               buzz[-1][t]["season"]) for t in teams})

    out_teams = []
    for t in teams:
        r0 = rows[0][t]
        games = r0["games"] or 82
        left = games - r0["w"] - r0["l"]
        f = left / games if games else 1.0

        # Bandwagon finishes: actual results before this season, simulated after.
        past = [{(led.get((season_after(season, -b), t)) or {}).get("PLAYOFF_RESULT", ""): 1.0}
                for b in (1, 2, 3)]
        expected_finish = [finish_probs(rows[k][t]) for k in range(total)]

        years = []
        for k, s in enumerate(seasons):
            r = rows[k][t]
            finishes = (expected_finish[:k][::-1] + past)[:3]
            bw = expected_bandwagon(finishes)
            if k == 0:
                rest_wp = (r["proj_w"] - r["w"]) / left if left else 0.5
                ledger_row = led.get((s, t)) or {}
                gate_so_far = (_num(ledger_row.get("GATE")) * _num(ledger_row.get("HOME_GAMES")) / fin.REG_HOME_GAMES
                               if ledger_row else 0.0)
                home_left = max(0, sched_home[t] - played_home[t])
                gate_left = home_left * gate_per_game(rest_wp, stars[0][t], away[0], bw,
                                                      mkt[0][t] * (1 + buzz[0][t]["rest"]))
                gate = gate_so_far + gate_left
            else:
                gate = fin.REG_HOME_GAMES * gate_per_game(r["proj_w"] / (r["games"] or 82), stars[k][t], away[k], bw,
                                                          mkt[k][t] * (1 + buzz[k][t]["rest"]))
            rev = season_revenue(gate, r, stars[k][t], mkt[k][t], buzz[k][t]["season"])

            contracted, dead = _payroll(t, bios, s)
            payroll = float(contracted)
            if k:
                bk = later_books[s][t]
                payroll += bk["resign_money"] + bk["fa_money"] + max(0, ROSTER_FILL - bk["standard"]) * rep_pay
            tax = fin.luxury_tax(payroll, aprons(s, season)[0], cap_level(s, season))
            ops = fin.ARENA_OPS_PER_GAME * fin.REG_HOME_GAMES
            costs = payroll + tax + ops

            if k == 0:
                cash_rev = (gate_left + rev["playoff_gate"] + rev["title_bonus"]
                            + fin.CONCESSIONS_PER_FAN * (gate_left / (fin.TICKET * mkt[0][t] * (1 + buzz[0][t]["rest"]))
                                                         + playoff_crowd(r))
                            + (rev["merch"] + rev["media"]) * f)
                cash_cost = costs * f
                when = f / 2
            else:
                cash_rev, cash_cost = rev["revenue"], costs
                when = f + k - 0.5
            years.append({
                "season": s, "strength": None if strengths[k][t] is None else round(strengths[k][t], 2),
                "rating": r["rating"] if k == 0 else round(r["prior"], 2),
                "proj_w": r["proj_w"], "games": r["games"],
                **{key: r[key] for key in ("playoffs", "second_round", "conf_finals", "finals", "title")},
                "stars": stars[k][t], "bandwagon": round(bw, 4), "market": round(mkt[k][t], 3),
                "buzz": round(buzz[k][t]["season"], 4),
                **{key: round(v) for key, v in rev.items()},
                "payroll": round(payroll), "dead_money": dead, "tax": round(tax), "ops": round(ops),
                "costs": round(costs), "profit": round(rev["revenue"] - costs),
                "cash_in": round(cash_rev), "cash_out": round(cash_cost),
                "pv_in": round(cash_rev * discount(when)), "pv_out": round(cash_cost * discount(when)),
                "players": (len(later_books[s][t]["players"]) if k else None),
                "fa_signed": ([{**p, "name": (bios.get(p["slug"]) or {}).get("name")}
                               for p in later_books[s][t]["fa_signed"]] if k else None),
                "walked": ([{"slug": p["slug"], "name": (bios.get(p["slug"]) or {}).get("name"),
                             "ovr": None if p["ovr"] is None else round(p["ovr"], 1), "to": went.get((s, p["slug"]))}
                            for p in later_books[s][t]["walked"]] if k else None),
            })

        # The sale: the ledger's valuation on the three projected seasons plus
        # the outlook season. Pricing it on the seasons after the horizon, each
        # projected like the others, was tried (2026-10-27) and rejected: the
        # far projection pulls every team to .500, which cut the spread from
        # 2.3x to 1.7-2.0x. The outlook holds the roster instead.
        held = years
        end = seasons[HORIZON - 1]
        titles_before = sum((led.get((season_after(season, -b), t)) or {}).get("PLAYOFF_RESULT") == "Champion"
                            for b in range(1, fin.BRAND_WINDOW - HORIZON + 1))
        exp_titles = titles_before + sum(y["title"] for y in held)
        ro = outlook_rows[t]
        b_out = rating_buzz(ro["rating"])
        bw_out = expected_bandwagon((expected_finish[::-1] + past)[:3])
        gate_out = fin.REG_HOME_GAMES * gate_per_game(ro["proj_w"] / (ro["games"] or 82), stars[-1][t], away[-1],
                                                      bw_out, mkt_out[t] * (1 + b_out))
        rev_out = season_revenue(gate_out, ro, stars[-1][t], mkt_out[t], b_out)["revenue"]
        trailing = (sum(y["revenue"] for y in held) + rev_out) / (len(held) + 1)
        multiple = (fin.START_VALUE / league0) * (1 + fin.VALUE_GROWTH) ** (season_index(end) + 1) if league0 else 0.0
        sale = multiple * trailing * (1 + fin.BRAND_PER_TITLE * exp_titles)
        pv_sale = sale * discount(f + HORIZON - 1)

        pv_in = sum(y["pv_in"] for y in held)
        pv_out = sum(y["pv_out"] for y in held)
        value = pv_in - pv_out + pv_sale
        book = _num((led.get((season, t)) or {}).get("VALUE")) or None
        out_teams.append({
            "team": t, "price": round(value / SHARES, 4), "value": round(value),
            "pv_in": pv_in, "pv_out": pv_out, "pv_sale": round(pv_sale), "sale": round(sale),
            "expected_titles": round(exp_titles, 3), "book_value": book,
            "strength_injured": None if healthy_or_not[t][0] is None else round(healthy_or_not[t][0], 2),
            "buzz": round(buzz_now[t], 4),
            "injuries": [{**i, "name": (bios.get(i["slug"]) or {}).get("name")} for i in healthy_or_not[t][1]],
            "w": r0["w"], "l": r0["l"], "season_left": round(f, 4), "years": years,
            "picks": drafted[t],
            "playoff_edge": edges.get(t),
            "outlook": {"season": season_after(season, k_out),
                        "strength": None if outlook_strength[t] is None else round(outlook_strength[t], 2),
                        "proj_w": ro["proj_w"], "revenue": round(rev_out)},
        })

    mean_price = sum(x["price"] for x in out_teams) / len(out_teams)
    for x in out_teams:
        x["vs_league"] = round(x["price"] / mean_price - 1, 5)
    return {
        "model": MODEL, "season": season, "date": today,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "league": {"mean_price": round(mean_price, 4), "replacement_ovr": round(rep_ovr, 1),
                   "replacement_salary": rep_pay, "discount_rate": DISCOUNT_RATE, "shares": SHARES},
        "teams": out_teams,
    }


# ── The market price ─────────────────────────────────────────────────────────
#
# Fair value is what a franchise is worth; the market price is what it would
# trade at. A real stock wanders around its value: the whole market has moods,
# each name has its own, news is overdone and then given back, and wild days
# come in bunches. The market price is fair value times exp(beta·league mood +
# team mood), every mood a mean-reverting walk with shocks seeded on the
# season, date and team, so a rebuild gives the same series.

MOOD_KEEP = 0.98                # the league's mood keeps this much a day
MOOD_VOL = 0.009                # its daily shock
SENT_KEEP = 0.97                # a team's own mood (half-life ~3 weeks)
SENT_VOL = 0.011                # its daily shock, before clustering
OVERREACT = 0.6                 # a fair-value move is overdone by this much, then fades
VOL_MEMORY = 0.75               # clustering (GARCH): how much of yesterday's variance stays
VOL_REACT = 0.20                # and how much the last shock adds
NEWS_VOL, NEWS_CAP = 0.5, 9.0   # a big fair-value move counts as a shock too, capped
TAIL_P, TAIL_X = 0.03, 3.0      # about one day in 33 is a shock this many times bigger
# Tuned 2026-10-27 on a synthetic 180-day season against real single stocks:
# daily sd 1.8% (real 1.5-2%), kurtosis 5.4 (5-10), |return| autocorrelation
# +0.07 (+0.1-0.3), pairwise correlation +0.22 (+0.2-0.4), no autocorrelation
# in returns, market vs fair value ±6.5% typical.
BETA_RANGE = (0.7, 1.3)


def _shock(*parts) -> float:
    """A standard shock seeded on `parts`: normal, with fat tails, unit variance."""
    d = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    u1 = (int.from_bytes(d[:8], "big") + 1) / (2**64 + 1)
    u2 = int.from_bytes(d[8:16], "big") / 2**64
    u3 = int.from_bytes(d[16:24], "big") / 2**64
    z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
    if u3 < TAIL_P:
        z *= TAIL_X
    return z / math.sqrt(1 - TAIL_P + TAIL_P * TAIL_X ** 2)


def team_beta(season: str, team: str) -> float:
    """How hard a team swings with the league's mood, fixed for a season."""
    d = hashlib.sha256(f"beta|{season}|{team}".encode()).digest()
    lo, hi = BETA_RANGE
    return lo + (hi - lo) * int.from_bytes(d[:8], "big") / 2**64


def market_series(rows: list[dict], shock=None) -> list[dict]:
    """Set every row's `market` price per team, oldest first, in place.

    One season's rows, a day apart. Each team's mood carries over day to day:
    it keeps SENT_KEEP of itself, adds OVERREACT of the day's fair-value move,
    and takes a shock whose size follows the last few days' (VOL_MEMORY)."""
    shock = shock or _shock
    mood, sent, var, last = 0.0, {}, {}, {}
    for row in rows:
        mood = MOOD_KEEP * mood + MOOD_VOL * shock(row["season"], row["date"], "league")
        for t, x in row["teams"].items():
            fv_now = x["price"]
            move = math.log(fv_now / last[t]) if last.get(t) else 0.0
            z = shock(row["season"], row["date"], t)
            v = var.get(t, 1.0)
            e = math.sqrt(v) * z
            sent[t] = SENT_KEEP * sent.get(t, 0.0) + OVERREACT * move + SENT_VOL * e
            # GARCH(1,1) on the team's shock, in units of SENT_VOL: a wild day,
            # or big news, makes the next days wilder; it fades by VOL_MEMORY.
            news = min(NEWS_CAP, (move / SENT_VOL) ** 2)
            var[t] = (1 - VOL_MEMORY - VOL_REACT) + VOL_REACT * (e * e + NEWS_VOL * news) + VOL_MEMORY * v
            last[t] = fv_now
            x["market"] = round(fv_now * math.exp(team_beta(row["season"], t) * mood + sent[t]), 4)
            x["mood"] = round(sent[t], 5)
        row["league_mood"] = round(mood, 5)
    return rows


def write_history(rows: list[dict]) -> None:
    """Save the history, every season's market price recomputed first."""
    rows.sort(key=lambda r: (r["season"], r["date"]))
    for season in {r["season"] for r in rows}:
        market_series([r for r in rows if r["season"] == season])
    _atomic_write(HISTORY_FILE, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


# ── Runs and history ─────────────────────────────────────────────────────────

HISTORY_KEYS = ("price", "vs_league", "pv_in", "pv_out", "pv_sale", "w", "l")


def _history_row(run: dict) -> dict:
    return {"date": run["date"], "season": run["season"], "model": run["model"],
            "teams": {x["team"]: {**{k: x[k] for k in HISTORY_KEYS},
                                  "strength": x["years"][0]["strength"],
                                  "strength_injured": x["strength_injured"],
                                  "proj_w": x["years"][0]["proj_w"],
                                  "title": x["years"][0]["title"],
                                  "payroll": x["years"][0]["payroll"]}
                      for x in run["teams"]}}


def read_history(season: Optional[str] = None) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if season is None or row.get("season") == season:
                out.append(row)
    return out


def save_run(run: dict) -> None:
    """Write the current run and make it the day's row in the history (one row per date)."""
    rows = [r for r in read_history() if not (r["date"] == run["date"] and r["season"] == run["season"])]
    rows.append(_history_row(run))
    write_history(rows)
    _attach_market(run, rows)
    _atomic_write(CURRENT_FILE, json.dumps(run, indent=1))


def _attach_market(run: dict, rows: list[dict]) -> None:
    """Copy the run's day's market price and mood from the history onto the run."""
    day = next((r for r in rows if r["season"] == run["season"] and r["date"] == run["date"]), None)
    if day:
        for x in run["teams"]:
            m = day["teams"].get(x["team"]) or {}
            x["market"], x["mood"] = m.get("market"), m.get("mood")
        run["league_mood"] = day.get("league_mood")


def _refresh_current_market() -> None:
    run = _load_json(CURRENT_FILE, None)
    if run:
        _attach_market(run, read_history(run["season"]))
        _atomic_write(CURRENT_FILE, json.dumps(run, indent=1))


def rebuild_history(season: str, days: list[str], results: Optional[list[dict]] = None) -> list[dict]:
    """Recompute the history rows for `days` with the current model and save them.

    A day's row holds the games played by the end of that day, with buzz
    recounted from them (game date, not
    when the box score was committed), and the ledger's home games to match.
    Rosters, OVRs and contracts are today's, so a row for a day before a move
    shows the move early. A day with no row yet is marked `backfill`; a day
    that already had one keeps its mark.
    """
    if results is None:
        index, _ = gh.get_index()
        results = po.season_results(index, season)
    ledger = load_ledger()
    old = {r["date"]: r for r in read_history(season)}
    new = {}
    for d in days:
        played = [g for g in results if g["date"] <= d]
        home = {}
        for g in played:
            home[g["home"]] = home.get(g["home"], 0) + 1
        # Buzz as it stood that day, from the same games, as the ledger counts it.
        _, b_avg, b_last = fin.buzz_series(sorted(
            ({"season": season, "date": g["date"], "home": g["home"], "away": g["away"],
              "pts": {g["home"]: g["home_pts"], g["away"]: g["away_pts"]}, "wl": {}} for g in played),
            key=lambda g: (g["date"], g["home"], g["away"])))
        led = [dict(r, HOME_GAMES=str(home.get(r["TEAM"], 0)),
                    BUZZ=b_last.get((season, r["TEAM"]), 0.0), BUZZ_AVG=b_avg.get((season, r["TEAM"]), 0.0))
               if r["SEASON"] == season else r for r in ledger]
        row = _history_row(compute(season, d, {"results": played, "ledger": led}))
        if old.get(d, {"backfill": True}).get("backfill"):
            row["backfill"] = True
        new[d] = row
    rows = [r for r in read_history() if not (r["season"] == season and r["date"] in new)] + list(new.values())
    write_history(rows)
    _refresh_current_market()
    return list(new.values())


def stale_days(season: str, today: str, results: list[dict]) -> list[str]:
    """Earlier days whose row is missing or no longer counts every game played
    that day: a box score committed after its game date changes its own day,
    not the day it arrived. The rows' wins add up to the games they counted."""
    rows = {r["date"]: r for r in read_history(season)}
    if not rows:
        return []
    out, d = [], date.fromisoformat(min(rows))
    while d.isoformat() < today:
        day = d.isoformat()
        r = rows.get(day)
        if r is None or sum(t["w"] for t in r["teams"].values()) != sum(g["date"] <= day for g in results):
            out.append(day)
        d += timedelta(days=1)
    return out


@router.get("/api/fair-value")
def get_fair_value():
    """The latest run, plus each team's price on the last earlier day."""
    run = _load_json(CURRENT_FILE, None)
    if not run:
        raise HTTPException(status_code=404, detail="No fair value has been computed yet")
    earlier = [r for r in read_history(run["season"]) if r["date"] < run["date"]]
    prev = earlier[-1] if earlier else None
    return {**run, "prev_date": prev["date"] if prev else None,
            "prev": {t: v["price"] for t, v in prev["teams"].items()} if prev else {},
            "prev_market": {t: v.get("market") for t, v in prev["teams"].items()} if prev else {}}


@router.get("/api/fair-value/history")
def get_fair_value_history(season: Optional[str] = None):
    """One row per day: every team's price and the PV pieces behind it."""
    if season is None:
        run = _load_json(CURRENT_FILE, None)
        season = run["season"] if run else None
    return {"season": season, "days": read_history(season)}

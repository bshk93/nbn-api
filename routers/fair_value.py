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
  * **Injuries.** This season's roster strength counts the injury report
    (`injured_strength`): an Out player is missing until his expected return
    (report date + the middle of 2K's duration), a Hurt one plays at his
    injured OVR, each for the share of the remaining games he misses. A past
    day uses the report as it stood that day (`injuries_as_of`).
  * **Who is under contract.** A season with a salary and no UFA/RFA tag. A
    player whose deal ends into a UFA/RFA hold the team holds can be re-signed
    at the hold amount: best OVR first, while payroll stays under the first
    apron, and not past `RESIGN_MAX_AGE`. The rest walk, and their spots are
    filled at replacement level.
  * **Wins and playoffs.** Each season is simulated with the playoff odds
    model (`playoff_odds.simulate`), on this season's schedule. Revenue takes
    the expected value over every finish.
  * **Revenue.** The ledger's formulas on expected inputs: gate from projected
    win %, stars (players in the league's top `STAR_COUNT` OVRs) and the
    bandwagon from expected finishes; playoff gate at `HOME_PER_SERIES` home
    games a series; concessions on both crowds; merch, media, title bonus.
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
  * **Price** = value ÷ `SHARES`.

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

import json
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

MODEL = "fv1"

# ── The model's constants. Every figure is NB¥. ──────────────────────────────

DISCOUNT_RATE = 0.08
SHARES = 30_000_000             # so a ¥4.5B franchise prices at ¥150
HORIZON = 3                     # this season and the next two, then the sale

PRIOR_FLOOR = 0.5               # the roster's least weight in this season's rating
REVERT = 0.75                   # a later season keeps this much of its rating, per season out
SIMS_NOW, SIMS_LATER = 4000, 2000

# Yearly OVR change by age. A first guess; ovr-history.json is about a year
# deep, too thin to fit a curve from yet.
AGE_CURVE = ((22, 3), (24, 2), (26, 1), (29, 0), (31, -1), (33, -2), (99, -3))
RESIGN_MAX_AGE = 34
STAR_COUNT = 15                 # All-NBN is 15 players a season
ROSTER_FILL = 14                # later seasons fill the standard roster to this
HOME_PER_SERIES = 2.6           # measured: 467 playoff games over 90 series, 21 to 26
CAP_GROWTH = 0.05               # aprons for seasons not on file yet, per season

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
          contracted: float, ceiling: Optional[float], kept: set) -> dict:
    """Players on the books for `season`: OVRs (aged), re-signing money, standard count.

    A team re-signs its free agents best OVR first, while payroll (`contracted`
    plus re-signings) stays under `ceiling`, the first apron. The rest walk.
    Anyone in `kept`, re-signed the season before, stays: that money is
    committed.
    """
    since = season_after(season, -years_out)
    players, candidates = [], []
    for slug in _roster(team):
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
    resign_money = 0
    candidates.sort(key=lambda p: (p["slug"] not in kept, -(p["ovr"] or 0)))
    for p in candidates:
        over = ceiling is not None and contracted + resign_money + p["money"] > ceiling
        if p["slug"] in kept or not over:
            resign_money += p["money"]
            players.append(p)
    return {"players": players, "resign_money": resign_money,
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


def gate_per_game(win_pct: float, stars: int, away_stars: float, bandwagon: float) -> float:
    fill = (fin.FILL_BASE + fin.FILL_FORM * win_pct + fin.FILL_HOME_STAR * min(fin.STAR_CAP, stars)
            + fin.FILL_AWAY_STAR * away_stars + bandwagon)
    return fin.SEATS * fin.TICKET * min(fin.FILL_MAX, max(fin.FILL_MIN, fill))


def playoff_gate(row: dict) -> float:
    reach = {1: row["playoffs"], 2: row["second_round"], 3: row["conf_finals"], 4: row["finals"]}
    return sum(p * HOME_PER_SERIES * fin.SEATS * fin.TICKET * fin.PLAYOFF_MULT[r] for r, p in reach.items())


def season_revenue(gate: float, row: dict, stars: int) -> dict:
    merch = fin.MERCH_BASE + fin.MERCH_STAR * stars + fin.MERCH_TITLE * row["title"]
    title = fin.TITLE_BONUS * row["title"]
    pog = playoff_gate(row)
    conc = concessions(gate, row)
    return {"gate": gate, "playoff_gate": pog, "concessions": conc, "merch": merch,
            "media": fin.MEDIA_SHARE, "title_bonus": title,
            "revenue": gate + pog + conc + merch + fin.MEDIA_SHARE + title}


def playoff_crowd(row: dict) -> float:
    reach = (row["playoffs"], row["second_round"], row["conf_finals"], row["finals"])
    return sum(p * HOME_PER_SERIES * fin.SEATS for p in reach)


def concessions(gate: float, row: dict) -> float:
    """Spend per head, on the regular-season crowd the gate implies plus a
    sold-out playoff crowd, as the ledger counts it."""
    return fin.CONCESSIONS_PER_FAN * (gate / fin.TICKET + playoff_crowd(row))


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

    seasons = [season_after(season, k) for k in range(HORIZON)]

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
    sims = [po.simulate(results, schedule, floored_priors(priors0, margin, played), sims=SIMS_NOW, seed=seed)]

    # Later seasons: who is on the books, aged, filled and pulled to the middle.
    rep_ovr, rep_pay = replacement(teams, bios, ovr_now, season)
    later_books, strengths = {}, [strength0]
    kept = {t: set() for t in teams}
    for k in range(1, HORIZON):
        s = seasons[k]
        a1 = aprons(s, season)[0]
        bk = {t: books(t, s, k, bios, ovr_now, _payroll(t, bios, s)[0], a1, kept[t]) for t in teams}
        kept = {t: bk[t]["resigned"] for t in teams}
        later_books[s] = bk
        st = {}
        for t in teams:
            ovrs = [p["ovr"] for p in bk[t]["players"] if p["ovr"] is not None and not p["two_way"]]
            ovrs += [rep_ovr] * max(0, len(po.MINUTES) - len(ovrs))
            st[t] = _strength(ovrs)
        strengths.append(st)
        pri = {t: v * REVERT ** k for t, v in po.roster_priors(st).items()}
        sims.append(po.simulate([], schedule, pri, sims=SIMS_LATER, seed=seed + k))
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
    stars = [star_counts(k) for k in range(HORIZON)]
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

    out_teams = []
    for t in teams:
        r0 = rows[0][t]
        games = r0["games"] or 82
        left = games - r0["w"] - r0["l"]
        f = left / games if games else 1.0

        # Bandwagon finishes: actual results before this season, simulated after.
        past = [{(led.get((season_after(season, -b), t)) or {}).get("PLAYOFF_RESULT", ""): 1.0}
                for b in (1, 2, 3)]
        expected_finish = [finish_probs(rows[k][t]) for k in range(HORIZON)]

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
                gate_left = home_left * gate_per_game(rest_wp, stars[0][t], away[0], bw)
                gate = gate_so_far + gate_left
            else:
                gate = fin.REG_HOME_GAMES * gate_per_game(r["proj_w"] / (r["games"] or 82), stars[k][t], away[k], bw)
            rev = season_revenue(gate, r, stars[k][t])

            contracted, dead = _payroll(t, bios, s)
            payroll = float(contracted)
            if k:
                bk = later_books[s][t]
                payroll += bk["resign_money"] + max(0, ROSTER_FILL - bk["standard"]) * rep_pay
            tax = fin.luxury_tax(payroll, aprons(s, season)[0], cap_level(s, season))
            ops = fin.ARENA_OPS_PER_GAME * fin.REG_HOME_GAMES
            costs = payroll + tax + ops

            if k == 0:
                cash_rev = (gate_left + rev["playoff_gate"] + rev["title_bonus"]
                            + fin.CONCESSIONS_PER_FAN * (gate_left / fin.TICKET + playoff_crowd(r))
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
                "stars": stars[k][t], "bandwagon": round(bw, 4),
                **{key: round(v) for key, v in rev.items()},
                "payroll": round(payroll), "dead_money": dead, "tax": round(tax), "ops": round(ops),
                "costs": round(costs), "profit": round(rev["revenue"] - costs),
                "cash_in": round(cash_rev), "cash_out": round(cash_cost),
                "pv_in": round(cash_rev * discount(when)), "pv_out": round(cash_cost * discount(when)),
                "players": (len(later_books[s][t]["players"]) if k else None),
            })

        # The sale: the ledger's valuation on the three projected seasons.
        end = seasons[-1]
        titles_before = sum((led.get((season_after(season, -b), t)) or {}).get("PLAYOFF_RESULT") == "Champion"
                            for b in range(1, fin.BRAND_WINDOW - HORIZON + 1))
        exp_titles = titles_before + sum(y["title"] for y in years)
        trailing = sum(y["revenue"] for y in years) / len(years)
        multiple = (fin.START_VALUE / league0) * (1 + fin.VALUE_GROWTH) ** (season_index(end) + 1) if league0 else 0.0
        sale = multiple * trailing * (1 + fin.BRAND_PER_TITLE * exp_titles)
        pv_sale = sale * discount(f + HORIZON - 1)

        pv_in = sum(y["pv_in"] for y in years)
        pv_out = sum(y["pv_out"] for y in years)
        value = pv_in - pv_out + pv_sale
        book = _num((led.get((season, t)) or {}).get("VALUE")) or None
        out_teams.append({
            "team": t, "price": round(value / SHARES, 4), "value": round(value),
            "pv_in": pv_in, "pv_out": pv_out, "pv_sale": round(pv_sale), "sale": round(sale),
            "expected_titles": round(exp_titles, 3), "book_value": book,
            "strength_injured": None if healthy_or_not[t][0] is None else round(healthy_or_not[t][0], 2),
            "injuries": healthy_or_not[t][1],
            "w": r0["w"], "l": r0["l"], "season_left": round(f, 4), "years": years,
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
    _atomic_write(CURRENT_FILE, json.dumps(run, indent=1))
    rows = [r for r in read_history() if not (r["date"] == run["date"] and r["season"] == run["season"])]
    rows.append(_history_row(run))
    rows.sort(key=lambda r: (r["season"], r["date"]))
    _atomic_write(HISTORY_FILE, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def rebuild_history(season: str, days: list[str], results: Optional[list[dict]] = None) -> list[dict]:
    """Recompute the history rows for `days` with the current model and save them.

    A day's row holds the games played by the end of that day (game date, not
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
        led = [dict(r, HOME_GAMES=str(home.get(r["TEAM"], 0))) if r["SEASON"] == season else r for r in ledger]
        row = _history_row(compute(season, d, {"results": played, "ledger": led}))
        if old.get(d, {"backfill": True}).get("backfill"):
            row["backfill"] = True
        new[d] = row
    rows = [r for r in read_history() if not (r["season"] == season and r["date"] in new)] + list(new.values())
    rows.sort(key=lambda r: (r["season"], r["date"]))
    _atomic_write(HISTORY_FILE, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
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
            "prev": {t: v["price"] for t, v in prev["teams"].items()} if prev else {}}


@router.get("/api/fair-value/history")
def get_fair_value_history(season: Optional[str] = None):
    """One row per day: every team's price and the PV pieces behind it."""
    if season is None:
        run = _load_json(CURRENT_FILE, None)
        season = run["season"] if run else None
    return {"season": season, "days": read_history(season)}

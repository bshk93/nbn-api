"""The Front Office Ledger: a fake P&L and valuation for every franchise.

    python3 -m stats_build.finances              # write both CSVs into NBN_OUT_DIR
    python3 -m stats_build.finances --dry-run    # print the summary, write nothing

Flavor only. Nothing here touches NB¥ balances, the cap or any rule; the
figures are denominated in NB¥ at NBA scale so they are easy to think about,
and they are not spendable. Spec: `nbn-today/docs/franchise-finances-spec.md`.

Two outputs, both under `derived/`:

- `data/franchise-finances.csv`: one row per team per season, every line item
  and the valuation.
- `data/franchise-attendance.csv`: one row per game, home team, attendance.

Not part of `pipeline.build` on purpose. The port harness diffs the Python
build's whole output tree against R's, and R never wrote these. `build.sh` runs
this as its own step after either engine, and a failure here does not fail the
stats build.

Everything is derived and deterministic. Randomness is seeded on the game, so a
rebuild never changes history. A formula change does rewrite history, which is
fine for flavor; `MODEL` says which formula a file came from.

Valuation only reads inputs that exist for every season (games, playoff
results, awards). Payroll only exists from 2026-08-25 (`cap-history.jsonl`)
and the stream flag only from 26-27, so both feed profit and never valuation.
That is what keeps the valuation series free of a step where a source begins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

from stats_build import csvio, pipeline
from stats_build.buildargs import DATA_DIR, OUT_DIR, BuildArgs

MODEL = "v3"

# ── The model's constants. Every figure is NB¥. ──────────────────────────────

SEATS = 19_000
TICKET = 120
REG_HOME_GAMES = 41            # used to project an unfinished season

FILL_BASE = 0.70
FILL_FORM = 0.25               # times the home team's win % over its last 10
FILL_HOME_STAR = 0.05          # per star on the home team, up to STAR_CAP
FILL_AWAY_STAR = 0.02          # per star on the visitors, up to STAR_CAP
STAR_CAP = 2
FILL_NOISE = 0.04              # ± this, seeded on the game
FILL_RIVAL = 0.03              # per playoff series the two met in the last RIVAL_WINDOW seasons
RIVAL_CAP = 2
RIVAL_WINDOW = 3
FILL_CHAMP_VISIT = 0.04        # last season's champion is the visiting team
FILL_MIN, FILL_MAX = 0.45, 1.00
FORM_WINDOW = 10

# Last season's finish raises this season's fill, fading over three seasons.
BANDWAGON = {"Champion": 0.10, "Runner-Up": 0.07, "Conf Finals": 0.04, "Second Round": 0.02}
BANDWAGON_FADE = (1.0, 0.5, 0.25)

# Every playoff home game sells out, at this multiple of a regular game's gate.
PLAYOFF_MULT = {1: 2.0, 2: 2.5, 3: 3.0, 4: 4.0}

MERCH_BASE = 40_000_000
MERCH_STAR = 10_000_000
MERCH_MVP = 15_000_000
MERCH_ROTY = 6_000_000
MERCH_TITLE = 30_000_000

MEDIA_SHARE = 150_000_000      # national TV, the same for everyone
LOCAL_MEDIA = 80_000_000       # local TV and sponsors at a market of 1.0

# Market: how much a city spends on its team. Everyone starts at 1.0 in 20-21;
# each finished season moves it by how far the team went and how often it won,
# and the rest fades back toward 1.0 by MARKET_KEEP a season. It is relative:
# the league's mean is held at 1.0 every season, so winners grow at losers'
# expense and the multiple alone carries league-wide growth. It scales the
# ticket price, base merch and local media.
MARKET_STEP = {"Champion": 0.16, "Runner-Up": 0.11, "Conf Finals": 0.07,
               "Second Round": 0.04, "First Round": 0.02, "Missed": -0.03}
MARKET_WIN = 0.30              # times (win % − .500)
MARKET_KEEP = 0.85
MARKET_MIN, MARKET_MAX = 0.5, 2.0
NBNTV_PER_GAME = 1_500_000     # per streamed game played, home or away; 26-27 on
TITLE_BONUS = 100_000_000

CONCESSIONS_PER_FAN = 40       # food, parking and the team store, per head
ARENA_OPS_PER_GAME = 800_000

# The tax, NBA-style: charged in brackets over the first apron (NBN has no
# separate tax line), each steeper than the last. A bracket is a share of the
# cap, so it grows with it. Past the listed rates each bracket adds TAX_STEP.
TAX_BRACKET = 0.035            # of the cap
TAX_RATES = (1.5, 1.75, 2.5, 3.25)
TAX_STEP = 0.5

START_VALUE = 3_000_000_000    # every franchise, opening night 20-21
VALUE_GROWTH = 0.06            # the league-wide multiple, per season
TRAILING = 3                   # seasons of revenue the valuation averages
BRAND_PER_TITLE = 0.04
BRAND_WINDOW = 6

FINANCES_FILE = Path("data") / "franchise-finances.csv"
ATTENDANCE_FILE = Path("data") / "franchise-attendance.csv"

FINANCE_COLS = [
    "TEAM", "SEASON", "MODEL", "IN_PROGRESS", "W", "L", "PLAYOFF_RESULT", "STARS", "MARKET",
    "HOME_GAMES", "AVG_ATTENDANCE", "SELLOUTS", "GATE",
    "PLAYOFF_HOME_GAMES", "PLAYOFF_GATE", "CONCESSIONS", "MERCH", "MEDIA", "NBNTV",
    "TITLE_BONUS", "TAX_SHARE", "REVENUE", "PAYROLL", "DEAD_MONEY", "LUXURY_TAX", "ARENA_OPS", "EXPENSES", "PROFIT",
    "TITLES_6Y", "VALUE", "VALUE_CHANGE", "JERSEY_PATCH",
]
ATTENDANCE_COLS = [
    "SEASON", "DATE", "gametype", "ROUND", "HOME", "AWAY", "HOME_KNOWN",
    "ATTENDANCE", "SELLOUT", "ARENA",
]
# Jersey-patch sponsors. Pure flavor; a team renews last season's deal more
# often than not, seeded like everything else.
SPONSORS = (
    "Tank Commander Energy", "Big Lead Insurance", "Fourth Quarter Collapse Mattresses",
    "Load Management Hotels", "Brick City Masonry", "And-One Dental",
    "Garbage Time Waste Management", "Flop Shop Footwear", "Pump Fake Gas & Go",
    "Full Court Press Dry Cleaning", "Box Out Storage", "Buzzer Beater Alarm Co.",
    "Salary Dump Truck Rental", "Second Apron Bakery", "Cap Space Realty",
    "Hard Cap Hat Co.", "Bird Rights Seed & Feed", "Sign-and-Trade Auto Sales",
    "Draft Night Regret Counseling", "Trade Machine Repair", "Two-Way Street Parking",
    "Moral Victory Brewing", "Trust the Process Credit Union", "Lottery Odds Casino",
    "Free Throw Line Dancing", "Air Ball Balloons", "Technical Foul Plumbing",
    "Bench Mob Furniture", "Paint Protection Auto Detailing", "Rebound Relationship Coaching",
    "Pick-and-Roll Sushi", "Dead Money Funeral Home", "Luxury Tax Accountants LLP",
    "Stream Sniper Webcams", "2K Realism Physical Therapy", "Play-In Tournament Mini Golf",
)
SPONSOR_RENEW = 0.6

OWNERS_FILE = Path("data") / "franchise-owners.csv"
OWNER_COLS = [
    "OWNER", "TEAM", "SEASONS", "FIRST", "LAST", "IN_PROGRESS", "TITLES",
    "START_VALUE", "END_VALUE", "VALUE_ADDED", "VS_LEAGUE",
]


# ── Games ─────────────────────────────────────────────────────────────────────

def _base_season(label: str) -> str:
    return label.replace(" Playoffs", "")


def games(rows: list[dict], playoffs: bool) -> list[dict]:
    """One dict per game, oldest first.

    The away side's `OPP` is `@HOME` (`OPP_RAW` strips it). A game where that
    doesn't pick out exactly one away side (19 regular-season games, 20-21 to
    25-26) is kept with `home_known=False`; its gate is split 50/50 rather than
    guessed.
    """
    by_game: dict[tuple, dict] = {}
    for r in rows:
        team, opp = r.get("TEAM", ""), (r.get("OPP") or "")
        if not team or not opp:
            continue
        other = opp.lstrip("@")
        key = (r["SEASON"], r["DATE"], frozenset((team, other)))
        g = by_game.setdefault(key, {"away": set(), "teams": {}, "round": None})
        if opp.startswith("@"):
            g["away"].add(team)
        g["teams"].setdefault(team, r.get("WL"))
        if playoffs and r.get("ROUND") not in (None, "", "NA"):
            g["round"] = int(r["ROUND"])
    out = []
    for (label, day, pair), g in by_game.items():
        if len(pair) != 2:
            continue
        a, b = sorted(pair)
        if len(g["away"]) == 1:
            away = next(iter(g["away"]))
            home = b if away == a else a
            known = True
        else:
            home, away, known = a, b, False
        out.append({
            "season": _base_season(label), "date": day, "playoffs": playoffs,
            "round": g["round"], "home": home, "away": away, "home_known": known,
            "wl": dict(g["teams"]),
        })
    out.sort(key=lambda g: (g["date"], g["home"], g["away"]))
    return out


def _noise(g: dict) -> float:
    digest = hashlib.sha256(f"{g['date']}|{g['home']}|{g['away']}".encode()).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    return (unit * 2 - 1) * FILL_NOISE


def _unit(*parts) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def jersey_patches(teams: list[str], seasons: list[str]) -> dict[tuple[str, str], str]:
    """(season, team) -> sponsor. No sponsor is on two teams in one season.

    Renewals keep last season's deal (last season had no repeats, so neither do
    they). Everyone else draws from what's left, never the deal they just lost.
    """
    out = {}
    for i, s in enumerate(seasons):
        taken = set()
        shopping = []
        for t in teams:
            if i and _unit("renew", t, s) < SPONSOR_RENEW:
                out[(s, t)] = out[(seasons[i - 1], t)]
                taken.add(out[(s, t)])
            else:
                shopping.append(t)
        for t in shopping:
            lost = out.get((seasons[i - 1], t)) if i else None
            free = [x for x in SPONSORS if x not in taken and x != lost]
            out[(s, t)] = free[int(_unit("sponsor", t, s) * len(free))]
            taken.add(out[(s, t)])
    return out


# ── Awards and playoff finishes ───────────────────────────────────────────────

def _player_teams(reg_rows: list[dict]) -> dict[tuple[str, str], str]:
    """(season, PLAYER upper) -> the team he played the most games for."""
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in reg_rows:
        counts[(r["SEASON"], r["PLAYER"].upper())][r["TEAM"]] += 1
    return {k: max(v.items(), key=lambda kv: (kv[1], kv[0]))[0] for k, v in counts.items()}


STAR_AWARDS = {"All-NBN First Team", "All-NBN Second Team", "All-NBN Third Team"}


def award_counts(award_rows: list[list], reg_rows: list[dict], seasons: list[str]):
    """(season, team) -> {"star": n, "mvp": n, "roty": n}.

    A star is an All-NBN selection, not an All-Star: 25-26 has no All-Star list
    in `awards-history.json`, and All-NBN is named for every season. An award
    goes to the team the player spent most of that season with. A season with no
    All-NBN named yet (the one in progress) borrows the most recent one, counted
    for whoever those players play for now.
    """
    teams_of = _player_teams(reg_rows)
    named: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for _slug, player, season, award in award_rows:
        named[season].append((player.upper(), award))
    kinds = {"Most Valuable Player": "mvp", "Rookie of the Year": "roty",
             **{a: "star" for a in STAR_AWARDS}}
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for i, season in enumerate(seasons):
        source = season
        for back in range(i, -1, -1):
            if any(a in STAR_AWARDS for _, a in named.get(seasons[back], [])):
                source = seasons[back]
                break
        for player, award in named.get(source, []):
            kind = kinds.get(award)
            if not kind or (source != season and kind != "star"):
                continue
            team = teams_of.get((season, player))
            if team:
                out[(season, team)][kind] += 1
    return out


def bandwagon(results: dict[tuple[str, str], str], seasons: list[str], season: str, team: str) -> float:
    i = seasons.index(season)
    lift = 0.0
    for back, weight in enumerate(BANDWAGON_FADE, start=1):
        if i - back >= 0:
            lift += weight * BANDWAGON.get(results.get((seasons[i - back], team), ""), 0.0)
    return lift


def market_next(market: float, result: dict[str, float], win_pct: float) -> float:
    """Next season's market from this one's, before the league is re-centred.

    `result` is {finish: probability}; an actual finish is probability 1, so
    fair value can pass the simulated odds instead."""
    step = sum(MARKET_STEP.get(f, MARKET_STEP["Missed"]) * p for f, p in result.items())
    return min(MARKET_MAX, max(MARKET_MIN, 1 + (market - 1) * MARKET_KEEP + step + MARKET_WIN * (win_pct - 0.5)))


def recentre(markets: dict[str, float]) -> dict[str, float]:
    """Hold the league's mean market at 1.0."""
    mean = sum(markets.values()) / len(markets) if markets else 1.0
    return {t: m / mean for t, m in markets.items()}


def markets(seasons: list[str], teams: list[str], results: dict, records: dict) -> dict[tuple[str, str], float]:
    """(season, team) -> market, from every finished season before it.
    `records` is (season, team) -> win % for finished seasons only."""
    out = {}
    cur = {t: 1.0 for t in teams}
    for s in seasons:
        for t in teams:
            out[(s, t)] = cur[t]
        if not all((s, t) in records for t in teams):
            break
        cur = recentre({t: market_next(cur[t], {results.get((s, t), "Missed"): 1.0}, records[(s, t)])
                        for t in teams})
    for s in seasons:
        for t in teams:
            out.setdefault((s, t), cur[t])
    return out


# ── Inputs that only exist for recent seasons ─────────────────────────────────

def load_streams(data_dir: Path) -> dict[tuple[str, frozenset], bool]:
    """(date, {both teams}) -> streamed, from every `schedule-*.json` on file."""
    out = {}
    for path in sorted(data_dir.glob("schedule-*.json")):
        try:
            sched = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for g in sched.get("games", []):
            key = (g.get("date"), frozenset((g.get("home_team"), g.get("away_team"))))
            out[key] = bool(g.get("stream"))
    return out


def load_arenas(data_dir: Path) -> dict[tuple[str, str], str]:
    """(date, home team) -> the arena the schedule names for that game."""
    out = {}
    for path in sorted(data_dir.glob("schedule-*.json")):
        try:
            sched = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for g in sched.get("games", []):
            if g.get("arena"):
                out[(g.get("date"), g.get("home_team"))] = g["arena"]
    return out


def home_arenas(data_dir: Path) -> dict[str, str]:
    """Team -> its home arena: the one the schedule names most for its home
    games, so a neutral-site game doesn't become the default."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (_, team), arena in load_arenas(data_dir).items():
        counts[team][arena] += 1
    return {t: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] for t, c in counts.items()}


def load_arena_names(data_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Team -> [(first day, name)] for every bought name never pulled, oldest
    first, from `arena-names.json` (routers/arenas.py). A name takes effect
    the UTC day it was bought."""
    try:
        data = json.loads((data_dir / "arena-names.json").read_text())
    except (OSError, ValueError):
        return {}
    return {t: [(e["at"][:10], e["name"]) for e in hist if not e.get("pulled")]
            for t, hist in data.items()}


def arena_for(date: str, home: str, scheduled: dict, defaults: dict, bought: dict) -> str | None:
    """The arena a game was played in, by name. A game the schedule puts
    somewhere other than the home arena (a neutral site) keeps that; otherwise
    the bought name in force that day, else the home arena's own name."""
    sched = scheduled.get((date, home))
    if sched and sched != defaults.get(home):
        return sched
    name = None
    for day, n in bought.get(home, []):
        if day <= date:
            name = n
    return name or defaults.get(home)


def load_books(data_dir: Path) -> dict[tuple[str, str], dict]:
    """(season, team) -> mean in-season payroll, dead money and cap figures.

    Payroll is money, not cap: `payroll` on rows from 2026-10-24, and before
    that `salary_ex_holds - empty_roster_charge`, which is the same figure
    whenever no draft rights carry a hold (true on every day checked). Never
    `salary`, which carries free-agent holds.
    """
    path = data_dir / "cap-history.jsonl"
    acc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("in_season"):
                acc[(row["season"], row["team"])].append(row)
    out = {}
    for key, rows in acc.items():
        def mean(f):
            vals = [f(r) for r in rows]
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None
        payroll = mean(lambda r: r["payroll"] if "payroll" in r
                       else r["salary_ex_holds"] - (r.get("empty_roster_charge") or 0))
        out[key] = {
            "payroll": payroll,
            "dead": mean(lambda r: r.get("dead_cap")),
            "cap": mean(lambda r: r.get("cap")),
            "apron1": mean(lambda r: r.get("apron1")),
        }
    return out


def luxury_tax(payroll: float, apron1: float | None, cap: float | None) -> float:
    """The bill on payroll over the first apron, bracket by bracket."""
    if not apron1 or not cap:
        return 0.0
    over, width = payroll - apron1, TAX_BRACKET * cap
    tax, n = 0.0, 0
    while over > 0:
        rate = TAX_RATES[n] if n < len(TAX_RATES) else TAX_RATES[-1] + TAX_STEP * (n - len(TAX_RATES) + 1)
        tax += rate * min(over, width)
        over -= width
        n += 1
    return tax


# ── The ledger ────────────────────────────────────────────────────────────────

def ledger(data_dir: Path, season: str):
    """(finance rows, attendance rows), each as lists ready for write_csv."""
    reg_rows, playoff_rows = pipeline.prepare(data_dir, season)
    reg = games(reg_rows, playoffs=False)
    po = games(playoff_rows, playoffs=True)
    seasons = sorted({g["season"] for g in reg})
    teams = sorted({g["home"] for g in reg} | {g["away"] for g in reg})
    current = seasons[-1]

    results = pipeline.playoff_results(playoff_rows)
    _, award_rows = pipeline.player_awards(data_dir, playoff_rows)
    awards = award_counts(award_rows, reg_rows, seasons)
    streams = load_streams(data_dir)
    scheduled = load_arenas(data_dir)
    defaults = home_arenas(data_dir)
    bought = load_arena_names(data_dir)

    def arena(g):
        return arena_for(g["date"], g["home"], scheduled, defaults, bought) if g["home_known"] else None
    books = load_books(data_dir)

    def stars(s, t):
        return min(STAR_CAP, awards.get((s, t), {}).get("star", 0))

    # Each team's results in order, for the form term. The opener reads last
    # season's final record; the very first season opens at .500.
    history: dict[tuple[str, str], list[int]] = defaultdict(list)
    final_pct: dict[tuple[str, str], float] = {}
    att_rows = []
    tally: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(float))

    def form(s, t):
        past = history[(s, t)][-FORM_WINDOW:]
        if past:
            return sum(past) / len(past)
        i = seasons.index(s)
        return final_pct.get((seasons[i - 1], t), 0.5) if i else 0.5

    # Bad blood: every playoff series two teams played, by season.
    series = {(g["season"], frozenset((g["home"], g["away"]))) for g in po}

    def rivalry(s, h, a):
        i = seasons.index(s)
        met = sum((seasons[j], frozenset((h, a))) in series
                  for j in range(max(0, i - RIVAL_WINDOW), i))
        return FILL_RIVAL * min(RIVAL_CAP, met)

    def champ_visiting(s, a):
        i = seasons.index(s)
        return FILL_CHAMP_VISIT if i and results.get((seasons[i - 1], a)) == "Champion" else 0.0

    # Market needs each finished season's record before that season's games are
    # priced, so count records first.
    wl: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for g in reg:
        for t in (g["home"], g["away"]):
            wl[(g["season"], t)][0 if g["wl"].get(t) == "W" else 1] += 1
    finished = {k: v[0] / (v[0] + v[1]) for k, v in wl.items()
                if k[0] != current and v[0] + v[1]}
    if all(sum(wl[(current, t)]) >= 82 for t in teams):
        finished.update({(current, t): wl[(current, t)][0] / 82 for t in teams})
    market = markets(seasons, teams, results, finished)

    for i, s in enumerate(seasons):
        for g in (x for x in reg if x["season"] == s):
            h, a = g["home"], g["away"]
            fill = (FILL_BASE + FILL_FORM * form(s, h)
                    + FILL_HOME_STAR * stars(s, h) + FILL_AWAY_STAR * stars(s, a)
                    + bandwagon(results, seasons, s, h) + rivalry(s, h, a)
                    + champ_visiting(s, a) + _noise(g))
            fill = min(FILL_MAX, max(FILL_MIN, fill))
            crowd = round(SEATS * fill)
            gate = crowd * TICKET * market[(s, h)]
            shares = [(h, 1.0)] if g["home_known"] else [(h, 0.5), (a, 0.5)]
            for t, share in shares:
                tt = tally[(s, t)]
                tt["home_games"] += share
                tt["crowd"] += share * crowd
                tt["sellouts"] += share * (fill >= FILL_MAX)
                tt["gate"] += share * gate
            for t in (h, a):
                won = g["wl"].get(t) == "W"
                history[(s, t)].append(1 if won else 0)
                tally[(s, t)]["w" if won else "l"] += 1
                if streams.get((g["date"], frozenset((h, a)))):
                    tally[(s, t)]["streamed"] += 1
            att_rows.append([s, g["date"], "REG", None, h, a, g["home_known"],
                             crowd, fill >= FILL_MAX,
                             arena(g)])
        for t in teams:
            past = history[(s, t)]
            if past:
                final_pct[(s, t)] = sum(past) / len(past)

    for g in po:
        s, h, a = g["season"], g["home"], g["away"]
        gate = SEATS * TICKET * market[(s, h)] * PLAYOFF_MULT.get(g["round"] or 1, 2.0)
        shares = [(h, 1.0)] if g["home_known"] else [(h, 0.5), (a, 0.5)]
        for t, share in shares:
            tally[(s, t)]["po_home"] += share
            tally[(s, t)]["po_gate"] += share * gate
            tally[(s, t)]["po_crowd"] += share * SEATS
        for t in (h, a):
            if streams.get((g["date"], frozenset((h, a)))):
                tally[(s, t)]["streamed"] += 1
        att_rows.append([s, g["date"], "PLAYOFF", g["round"], h, a, g["home_known"],
                         SEATS, True, arena(g)])

    # Revenue per team-season. The season in progress is projected to a full
    # 41 home games, so its valuation isn't dragged down by the calendar.
    revenue_ex_tv: dict[tuple[str, str], float] = {}
    lines: dict[tuple[str, str], dict] = {}
    for s in seasons:
        in_progress = s == current and any(
            tally[(s, t)]["w"] + tally[(s, t)]["l"] < 82 for t in teams)
        league_gate_per_game = (
            sum(tally[(s, t)]["gate"] for t in teams)
            / max(1.0, sum(tally[(s, t)]["home_games"] for t in teams)))
        for t in teams:
            tt = tally[(s, t)]
            gate = tt["gate"]
            crowd = tt["crowd"]
            if in_progress:
                per_game = gate / tt["home_games"] if tt["home_games"] else league_gate_per_game
                gate = per_game * REG_HOME_GAMES
                crowd = gate / (TICKET * market[(s, t)])
            concessions = CONCESSIONS_PER_FAN * (crowd + tt["po_crowd"])
            aw = awards.get((s, t), {})
            champ = results.get((s, t)) == "Champion"
            mk = market[(s, t)]
            media = MEDIA_SHARE + LOCAL_MEDIA * mk
            merch = (MERCH_BASE * mk + MERCH_STAR * aw.get("star", 0)
                     + MERCH_MVP * aw.get("mvp", 0) + MERCH_ROTY * aw.get("roty", 0)
                     + MERCH_TITLE * champ)
            title = TITLE_BONUS if champ else 0
            nbntv = NBNTV_PER_GAME * tt["streamed"]
            rev = gate + tt["po_gate"] + concessions + merch + media + title
            revenue_ex_tv[(s, t)] = rev
            lines[(s, t)] = {"in_progress": in_progress, "gate": gate, "merch": merch, "media": media,
                             "concessions": concessions,
                             "title": title, "nbntv": nbntv, "revenue": rev + nbntv,
                             "champ": champ}
        if in_progress and s != seasons[0]:
            # No playoffs yet. For valuation only, every team is credited the
            # league-average postseason of the last finished season, so the
            # calendar doesn't read as a crash and no team is favoured.
            done = seasons[seasons.index(s) - 1]
            postseason = sum(tally[(done, t)]["po_gate"]
                             + CONCESSIONS_PER_FAN * tally[(done, t)]["po_crowd"]
                             + (TITLE_BONUS + MERCH_TITLE) * (results.get((done, t)) == "Champion")
                             for t in teams) / len(teams)
            for t in teams:
                revenue_ex_tv[(s, t)] += postseason

    # Valuation: a league-wide multiple on trailing revenue, times a brand
    # premium for recent titles. Seeded so 20-21 opens level at START_VALUE.
    league0 = sum(revenue_ex_tv[(seasons[0], t)] for t in teams) / len(teams)
    multiple0 = START_VALUE / league0
    values: dict[tuple[str, str], float] = {}
    for i, s in enumerate(seasons):
        multiple = multiple0 * (1 + VALUE_GROWTH) ** (i + 1)
        for t in teams:
            window = [revenue_ex_tv[(seasons[j], t)] if j >= 0 else league0
                      for j in range(i - TRAILING + 1, i + 1)]
            titles = sum(lines[(seasons[j], t)]["champ"]
                         for j in range(max(0, i - BRAND_WINDOW + 1), i + 1))
            values[(s, t)] = multiple * (sum(window) / len(window)) * (1 + BRAND_PER_TITLE * titles)
            lines[(s, t)]["titles"] = titles

    # The tax is paid out in equal shares to every team that didn't pay any,
    # as the NBA does. Only for a season every team has books for, so the
    # pool is never split on a partial count.
    taxes = {(s, t): luxury_tax(b["payroll"], b["apron1"], b["cap"])
             for (s, t), b in books.items() if b["payroll"] is not None}
    tax_share: dict[tuple[str, str], float] = {}
    for s in seasons:
        if all((s, t) in taxes for t in teams):
            payers = [t for t in teams if taxes[(s, t)] > 0]
            pool = sum(taxes[(s, t)] for t in payers)
            clean = [t for t in teams if t not in payers]
            for t in teams:
                tax_share[(s, t)] = pool / len(clean) if clean and t in clean else 0.0

    patches = jersey_patches(teams, seasons)
    fin_rows = []
    for i, s in enumerate(seasons):
        for t in teams:
            tt, ln = tally[(s, t)], lines[(s, t)]
            book = books.get((s, t))
            payroll = dead = tax = ops = expenses = profit = None
            share = tax_share.get((s, t))
            revenue = ln["revenue"] + (share or 0)
            if (s, t) in taxes:
                payroll = book["payroll"]
                dead = book["dead"]
                tax = taxes[(s, t)]
                ops = ARENA_OPS_PER_GAME * (REG_HOME_GAMES if ln["in_progress"] else tt["home_games"])
                expenses = payroll + tax + ops
                profit = revenue - expenses
            prev = values[(seasons[i - 1], t)] if i else START_VALUE
            fin_rows.append([
                t, s, MODEL, ln["in_progress"], int(tt["w"]), int(tt["l"]),
                results.get((s, t), "Missed" if not ln["in_progress"] else None),
                awards.get((s, t), {}).get("star", 0), round(market[(s, t)], 3),
                _int(tt["home_games"]),
                round(tt["crowd"] / tt["home_games"]) if tt["home_games"] else None,
                _int(tt["sellouts"]), _int(ln["gate"]),
                _int(tt["po_home"]), _int(tt["po_gate"]), _int(ln["concessions"]),
                _int(ln["merch"]), _int(ln["media"]), _int(ln["nbntv"]), ln["title"], _int(share),
                _int(revenue),
                _int(payroll), _int(dead), _int(tax), _int(ops), _int(expenses), _int(profit),
                ln["titles"], _int(values[(s, t)]), _int(values[(s, t)] - prev),
                patches[(s, t)],
            ])
    return fin_rows, att_rows


def _int(x):
    return None if x is None else int(round(x))


def _midseason(season: str):
    """Jan 1 of a season: whoever owns the team then owns its season. A season
    that hasn't reached it yet belongs to whoever owns the team today."""
    from datetime import date
    return min(date(2000 + int(season[3:5]), 1, 1), date.today())


def owner_ledger(fin_rows: list[list], tenures: list[dict]) -> list[list]:
    """One row per owner per team they ran: what the franchise was worth.

    Each season's change in value goes to whoever owned the team at mid-season
    (Jan 1). The playoff stats credit a season to every owner whose tenure
    touched it (`pipeline.tenure_seasons`), which is right for "was there for
    the run" and wrong for money: a summer takeover would count twice.

    `VS_LEAGUE` is the change beyond what the league average did over the same
    seasons, so a long tenure isn't rewarded just for the multiple growing.
    """
    rows = [dict(zip(FINANCE_COLS, r)) for r in fin_rows]
    seasons = sorted({r["SEASON"] for r in rows})
    value = {(r["SEASON"], r["TEAM"]): r["VALUE"] for r in rows}
    league = {s: sum(r["VALUE"] for r in rows if r["SEASON"] == s) / max(1, sum(r["SEASON"] == s for r in rows))
              for s in seasons}
    by_team_row = {(r["SEASON"], r["TEAM"]): r for r in rows}

    def prev(s, t):
        i = seasons.index(s)
        return value[(seasons[i - 1], t)] if i else START_VALUE

    def league_prev(s):
        i = seasons.index(s)
        return league[seasons[i - 1]] if i else START_VALUE

    acc: dict[tuple[str, str], dict] = {}
    for s in seasons:
        mid = _midseason(s)
        for ten in tenures:
            t = ten["TEAM"]
            if (s, t) not in value or not (ten["start"] <= mid <= ten["end"]):
                continue
            a = acc.setdefault((ten["owner"], t), {"seasons": [], "added": 0.0, "vs": 0.0, "titles": 0,
                                                   "start": prev(s, t), "in_progress": False})
            change = value[(s, t)] - prev(s, t)
            expected = prev(s, t) * (league[s] / league_prev(s) - 1)
            a["seasons"].append(s)
            a["added"] += change
            a["vs"] += change - expected
            a["end"] = value[(s, t)]
            a["titles"] += by_team_row[(s, t)]["PLAYOFF_RESULT"] == "Champion"
            a["in_progress"] |= bool(by_team_row[(s, t)]["IN_PROGRESS"])
    out = []
    for (owner, team), a in sorted(acc.items(), key=lambda kv: -kv[1]["vs"]):
        out.append([owner, team, len(a["seasons"]), a["seasons"][0], a["seasons"][-1],
                    a["in_progress"], a["titles"], _int(a["start"]), _int(a["end"]),
                    _int(a["added"]), _int(a["vs"])])
    return out


def write(out_dir: Path, data_dir: Path, season: str) -> list[str]:
    fin, att = ledger(data_dir, season)
    owners = owner_ledger(fin, pipeline.load_owner_tenures(data_dir))
    csvio.write_csv(out_dir / FINANCES_FILE, FINANCE_COLS, fin)
    csvio.write_csv(out_dir / ATTENDANCE_FILE, ATTENDANCE_COLS, att)
    csvio.write_csv(out_dir / OWNERS_FILE, OWNER_COLS, owners)
    return [str(FINANCES_FILE), str(ATTENDANCE_FILE), str(OWNERS_FILE)]


def summary(fin_rows: list[list]) -> str:
    """The latest season's valuations, high to low, plus the spread."""
    col = {c: i for i, c in enumerate(FINANCE_COLS)}
    last = max(r[col["SEASON"]] for r in fin_rows)
    rows = sorted((r for r in fin_rows if r[col["SEASON"]] == last),
                  key=lambda r: -r[col["VALUE"]])
    lines = [f"{last} valuations (model {MODEL}):"]
    for r in rows:
        lines.append(f"  {r[col['TEAM']]}  ¥{r[col['VALUE']] / 1e9:5.2f}B  "
                     f"revenue ¥{r[col['REVENUE']] / 1e6:5.0f}M  titles(6y) {r[col['TITLES_6Y']]}")
    top, bottom = rows[0][col["VALUE"]], rows[-1][col["VALUE"]]
    lines.append(f"spread top/bottom: {top / bottom:.2f}x")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stats_build.finances", description=__doc__.split("\n")[0])
    ap.add_argument("--season", help="e.g. 26-27 (default: today's)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    a = ap.parse_args(argv)
    season = BuildArgs.resolve(a.season, None).season
    if a.dry_run:
        fin, _ = ledger(a.data_dir, season)
        print(summary(fin))
        return 0
    written = write(a.out, a.data_dir, season)
    print(f"finances: wrote {', '.join(written)} (model {MODEL})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

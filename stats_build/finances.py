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

MODEL = "v1"

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

MEDIA_SHARE = 150_000_000
NBNTV_PER_GAME = 1_500_000     # per streamed game played, home or away; 26-27 on
TITLE_BONUS = 100_000_000

ARENA_OPS_PER_GAME = 800_000
TAX_RATE_APRON1 = 1.5          # per NB¥ of payroll over the first apron
TAX_RATE_APRON2 = 1.5          # and again over the second

START_VALUE = 3_000_000_000    # every franchise, opening night 20-21
VALUE_GROWTH = 0.06            # the league-wide multiple, per season
TRAILING = 3                   # seasons of revenue the valuation averages
BRAND_PER_TITLE = 0.04
BRAND_WINDOW = 6

FINANCES_FILE = Path("data") / "franchise-finances.csv"
ATTENDANCE_FILE = Path("data") / "franchise-attendance.csv"

FINANCE_COLS = [
    "TEAM", "SEASON", "MODEL", "IN_PROGRESS", "W", "L", "PLAYOFF_RESULT", "STARS",
    "HOME_GAMES", "AVG_ATTENDANCE", "SELLOUTS", "GATE",
    "PLAYOFF_HOME_GAMES", "PLAYOFF_GATE", "MERCH", "MEDIA", "NBNTV", "TITLE_BONUS",
    "REVENUE", "PAYROLL", "DEAD_MONEY", "LUXURY_TAX", "ARENA_OPS", "EXPENSES", "PROFIT",
    "TITLES_6Y", "VALUE", "VALUE_CHANGE",
]
ATTENDANCE_COLS = [
    "SEASON", "DATE", "gametype", "ROUND", "HOME", "AWAY", "HOME_KNOWN",
    "ATTENDANCE", "SELLOUT", "ARENA",
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
            "apron1": mean(lambda r: r.get("apron1")),
            "apron2": mean(lambda r: r.get("apron2")),
        }
    return out


def luxury_tax(payroll: float, apron1: float | None, apron2: float | None) -> float:
    tax = 0.0
    if apron1:
        tax += TAX_RATE_APRON1 * max(0.0, payroll - apron1)
    if apron2:
        tax += TAX_RATE_APRON2 * max(0.0, payroll - apron2)
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
    arenas = load_arenas(data_dir)
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

    for i, s in enumerate(seasons):
        for g in (x for x in reg if x["season"] == s):
            h, a = g["home"], g["away"]
            fill = (FILL_BASE + FILL_FORM * form(s, h)
                    + FILL_HOME_STAR * stars(s, h) + FILL_AWAY_STAR * stars(s, a)
                    + bandwagon(results, seasons, s, h) + _noise(g))
            fill = min(FILL_MAX, max(FILL_MIN, fill))
            crowd = round(SEATS * fill)
            gate = crowd * TICKET
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
                             arenas.get((g["date"], h)) if g["home_known"] else None])
        for t in teams:
            past = history[(s, t)]
            if past:
                final_pct[(s, t)] = sum(past) / len(past)

    for g in po:
        s, h, a = g["season"], g["home"], g["away"]
        gate = SEATS * TICKET * PLAYOFF_MULT.get(g["round"] or 1, 2.0)
        shares = [(h, 1.0)] if g["home_known"] else [(h, 0.5), (a, 0.5)]
        for t, share in shares:
            tally[(s, t)]["po_home"] += share
            tally[(s, t)]["po_gate"] += share * gate
        for t in (h, a):
            if streams.get((g["date"], frozenset((h, a)))):
                tally[(s, t)]["streamed"] += 1
        att_rows.append([s, g["date"], "PLAYOFF", g["round"], h, a, g["home_known"],
                         SEATS, True, arenas.get((g["date"], h)) if g["home_known"] else None])

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
            if in_progress:
                per_game = gate / tt["home_games"] if tt["home_games"] else league_gate_per_game
                gate = per_game * REG_HOME_GAMES
            aw = awards.get((s, t), {})
            champ = results.get((s, t)) == "Champion"
            merch = (MERCH_BASE + MERCH_STAR * aw.get("star", 0)
                     + MERCH_MVP * aw.get("mvp", 0) + MERCH_ROTY * aw.get("roty", 0)
                     + MERCH_TITLE * champ)
            title = TITLE_BONUS if champ else 0
            nbntv = NBNTV_PER_GAME * tt["streamed"]
            rev = gate + tt["po_gate"] + merch + MEDIA_SHARE + title
            revenue_ex_tv[(s, t)] = rev
            lines[(s, t)] = {"in_progress": in_progress, "gate": gate, "merch": merch,
                             "title": title, "nbntv": nbntv, "revenue": rev + nbntv,
                             "champ": champ}
        if in_progress and s != seasons[0]:
            # No playoffs yet. For valuation only, every team is credited the
            # league-average postseason of the last finished season, so the
            # calendar doesn't read as a crash and no team is favoured.
            done = seasons[seasons.index(s) - 1]
            postseason = sum(tally[(done, t)]["po_gate"]
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

    fin_rows = []
    for i, s in enumerate(seasons):
        for t in teams:
            tt, ln = tally[(s, t)], lines[(s, t)]
            book = books.get((s, t))
            payroll = dead = tax = ops = expenses = profit = None
            if book and book["payroll"] is not None:
                payroll = book["payroll"]
                dead = book["dead"]
                tax = luxury_tax(payroll, book["apron1"], book["apron2"])
                ops = ARENA_OPS_PER_GAME * (REG_HOME_GAMES if ln["in_progress"] else tt["home_games"])
                expenses = payroll + tax + ops
                profit = ln["revenue"] - expenses
            prev = values[(seasons[i - 1], t)] if i else START_VALUE
            fin_rows.append([
                t, s, MODEL, ln["in_progress"], int(tt["w"]), int(tt["l"]),
                results.get((s, t), "Missed" if not ln["in_progress"] else None),
                awards.get((s, t), {}).get("star", 0),
                _int(tt["home_games"]),
                round(tt["crowd"] / tt["home_games"]) if tt["home_games"] else None,
                _int(tt["sellouts"]), _int(ln["gate"]),
                _int(tt["po_home"]), _int(tt["po_gate"]), _int(ln["merch"]), MEDIA_SHARE,
                _int(ln["nbntv"]), ln["title"], _int(ln["revenue"]),
                _int(payroll), _int(dead), _int(tax), _int(ops), _int(expenses), _int(profit),
                ln["titles"], _int(values[(s, t)]), _int(values[(s, t)] - prev),
            ])
    return fin_rows, att_rows


def _int(x):
    return None if x is None else int(round(x))


def write(out_dir: Path, data_dir: Path, season: str) -> list[str]:
    fin, att = ledger(data_dir, season)
    csvio.write_csv(out_dir / FINANCES_FILE, FINANCE_COLS, fin)
    csvio.write_csv(out_dir / ATTENDANCE_FILE, ATTENDANCE_COLS, att)
    return [str(FINANCES_FILE), str(ATTENDANCE_FILE)]


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

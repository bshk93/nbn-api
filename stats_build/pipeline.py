"""The Python stats pipeline — replacing `nbn-today/build/job.R`.

Ported one output file at a time (port spec Phase 2). R stays authoritative
for everything not listed in `PORTED`; each file joins that list only once
`python3 -m stats_build.harness port` shows it byte-identical to R's.

Two rules from the spec govern every line here:

  * **Port bug-for-bug.** Anything that looks wrong is reproduced faithfully
    now and fixed in a separate, later commit with its own reasoning. A diff
    must never mix "ported" with "improved", or every mismatch needs
    adjudication instead of just being a bug.
  * **Byte-identical or it doesn't ship**, via `csvio` — which is why this
    module is stdlib-only. pandas/numpy would bring their own float and NaN
    rendering into the one thing the acceptance test turns on.

Full recompute, always: every run reads every raw row and rewrites its
outputs. That is deliberate and preserved — it is what makes the build
idempotent and self-healing.
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import date, timedelta
from fractions import Fraction
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from stats_build import csvio
from stats_build.rmath import r_mean, r_round

# Output files this module owns. Everything else is still R's.
STAT_FILES = {"p": "P", "r": "R", "a": "A", "s": "S", "b": "B", "3pm": "3PM"}

PORTED = (
    "data/h2h-alltime.csv",
    "data/h2h-playoffs.csv",
    *(f"data/totals-{k}.csv" for k in STAT_FILES),
    *(f"data/game-highs-{k}.csv" for k in STAT_FILES),
    "players/player_awards.csv",
    *(f"data/{t.lower()}-players.csv" for t in (
        "ATL BKN BOS CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL "
        "MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS").split()),
    "data/franchise-records.csv",
    "nbntv-classics/playoff-classics.csv",
    "players/player_seasons.csv",
    "players/player_seasons_playoffs.csv",
    "standings/standings-history.csv",
    "standings/playoff-brackets.csv",
    "data/owner_stats.csv",
    "data/h2h-owners.csv",
    "data/league-history.csv",
    "data/hof.csv",
    "nbntv-classics/playoff-series-margins.csv",
    *(f"data/{t.lower()}-seasons.csv" for t in (
        "ATL BKN BOS CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL "
        "MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS").split()),
)

SEASON_SUMS = (("MIN", "M"), ("PTS", "P"), ("REB", "R"), ("AST", "A"), ("STL", "S"),
               ("BLK", "B"), ("TOV", "TO"), ("PF", "PF"), ("FGM", "FGM"), ("FGA", "FGA"))
SEASON_HIGHS = (("HIGH_P", "P"), ("HIGH_R", "R"), ("HIGH_A", "A"), ("HIGH_S", "S"),
                ("HIGH_B", "B"), ("HIGH_3PM", "3PM"))
SEASON_TAIL_SUMS = (("3PM", "3PM"), ("3PA", "3PA"), ("FTM", "FTM"), ("FTA", "FTA"))
BIO_COLS = ("PHOTO_URL", "DOB", "COLLEGE", "COUNTRY", "NBN_DFT_YR", "NBN_DFT_R", "NBN_DFT_P")

# Franchise record book: five deep, per team, per stat. Sorted by STAT as a
# STRING in the output, so "3PM" leads — digits sort before letters.
FRANCHISE_STATS = ("P", "R", "A", "S", "B", "3PM", "GMSC")

# awards-history.json's keys, and the label each one carries into the CSV, in
# the order job.R binds them -- the file is not sorted, so this order IS the
# output order.
AWARD_LABELS = (
    ("All-Star", "All-Star"),
    ("MVP", "Most Valuable Player"),
    ("DPOY", "Defensive Player of the Year"),
    ("6MOY", "Sixth Man of the Year"),
    ("ROTY", "Rookie of the Year"),
    ("MIP", "Most Improved Player"),
    ("All-NBN-1", "All-NBN First Team"),
    ("All-NBN-2", "All-NBN Second Team"),
    ("All-NBN-3", "All-NBN Third Team"),
    ("All-Defense", "All-Defense"),
    ("All-Rookie", "All-Rookie"),
)

# A playoff team with this many wins has won the title -- there is no bracket
# record in the raw rows, so the count is the evidence.
CHAMPION_WINS = 16

# clean_allstats()'s spelling corrections, then job.R's fix_player_names().
# Both run over the regular season and the playoffs. They are load-bearing:
# career totals group by this string, so an unfixed alias splits a player's
# career in two.
PLAYER_FIXES = {
    "KANTER, ENES": "FREEDOM, ENES",
    "BAMBA, MO": "BAMBA, MOHAMED",
    "CAREY JR., VERNON": "CAREY, VERNON",
    "CHAMAGNIE, JUSTIN": "CHAMPAGNIE, JUSTIN",
    "HAMMONDS, RAYSHON": "HAMMONDS, RAYSHAUN",
    "MATTHEWS, WES": "MATTHEWS, WESLEY",
    "O'NEALE, ROYCE": "ONEALE, ROYCE",
    "PIPPEN, SCOTTIE": "PIPPEN, SCOTTY",
    "ROBINSON, GLENNN": "ROBINSON, GLENN",
    "WHITE, COLBY": "WHITE, COBY",
    "BERTANS,DAVIS": "BERTANS, DAVIS",
    "HIGHSMITH, HAYDEN": "HIGHSMITH, HAYWOOD",
    "THOMAS, CAMERON": "THOMAS, CAM",
    "REDDISH, CAMERON": "REDDISH, CAM",
    "KILLIAN HAYES": "HAYES, KILLIAN",
    "KOBE BROWN": "BROWN, KOBE",
    # Found 2026-08-26 by stats_build/checks.py — which exists because nothing
    # asserted this table was complete. One GSW playoff row (2026-05-11) was
    # minting a `riley-wendell` slug and publishing a player who does not exist
    # into players/player_seasons_playoffs.csv. It is Will Riley: he played 63
    # games, all for GSW, including 2026-05-09 and 2026-05-13 either side of
    # that one, and the two names never appear in the same team-game.
    "RILEY, WENDELL": "RILEY, WILL",
}

_SEASON_IN_FILENAME = re.compile(r"\d{2}\.")


# --------------------------------------------------------------------------
# Loading — mirrors build-utils.R `load_allstats` and job.R's current-season
# injection
# --------------------------------------------------------------------------

def _season_label(filename: str, playoffs: bool) -> str | None:
    """`allstats-20-21.csv` -> "20-21"; `allstats-playoffs-21.csv` -> "20-21 Playoffs".

    R derives this with `str_extract(fp, "\\\\d{2}\\\\.")` — the two digits before
    the extension, i.e. the *end* year — then labels the season as
    `(end-1)-end`. Faithfully reproduced, including that it reads the filename
    rather than the rows: a mislabelled file is mislabelled in the output too.
    """
    m = _SEASON_IN_FILENAME.search(filename)
    if not m:
        return None
    end = int(m.group(0)[:2])
    return f"{end - 1}-{end}" + (" Playoffs" if playoffs else "")


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_allstats(data_dir: Path, playoffs: bool = False) -> list[tuple[str, list[dict[str, str]]]]:
    """Every season file on disk, as (season label, rows), in filename order."""
    pattern = "allstats-playoffs-*.csv" if playoffs else "allstats-[0-9]*.csv"
    out = []
    for path in sorted(data_dir.glob(pattern)):
        season = _season_label(path.name, playoffs)
        if season is None:          # R's `filter(!is.na(SEASON))`
            continue
        out.append((season, _read_rows(path)))
    return out


def load_game_rows(data_dir: Path, season: str, playoffs: bool = False) -> list[dict[str, str]]:
    """Historical files plus the current season, deduplicated as job.R does.

    job.R loads the historical files, `discard`s any whose season equals the
    one being built, then appends the current season read separately — so the
    current season is counted once even though its file matches the glob.
    """
    label = f"{season} Playoffs" if playoffs else season
    rows: list[dict[str, str]] = []
    current: list[dict[str, str]] = []
    for file_season, file_rows in load_allstats(data_dir, playoffs=playoffs):
        target = current if file_season == label else rows
        for r in file_rows:
            r["SEASON"] = file_season
            target.append(r)
    return rows + current


def _stat(row: dict[str, str], col: str) -> float | None:
    """One stat cell as a number, or None for blank/NA (R's `na.rm = TRUE`)."""
    raw = (row.get(col) or "").strip()
    if raw in ("", "NA"):
        return None
    return float(raw)


def prepare(data_dir: Path, season: str) -> tuple[list[dict], list[dict]]:
    """(regular season rows, playoff rows) with the fixes job.R applies.

    clean_allstats ends in `arrange(PLAYER, DATE)`, and that ordering is not
    cosmetic: it is the tie-break for every stable sort downstream, so it is
    reproduced rather than skipped.
    """
    out = []
    for playoffs in (False, True):
        rows = load_game_rows(data_dir, season, playoffs=playoffs)
        for r in rows:
            r["PLAYER"] = PLAYER_FIXES.get(r["PLAYER"], r["PLAYER"])
            r["gametype"] = "PLAYOFF" if playoffs else "REG"
            if not playoffs:
                r["ROUND"] = r["GAME"] = None
        rows.sort(key=lambda r: (r["PLAYER"], r["DATE"]))
        out.append(rows)
    return out[0], out[1]


# --------------------------------------------------------------------------
# Career totals and single-game highs
# --------------------------------------------------------------------------

def career_totals(reg: list[dict], col: str, limit: int = 250):
    """Top `limit` career totals for one stat. Regular season only, as in R.

    Ties keep alphabetical player order: R sums inside `group_by(PLAYER)`,
    which leaves the frame ordered by player, and `arrange(desc(...))` is a
    stable sort over it.
    """
    totals: dict[str, float] = defaultdict(float)
    for r in reg:
        v = _stat(r, col)
        if v is not None:
            totals[r["PLAYER"]] += v
    ranked = sorted(sorted(totals.items()), key=lambda kv: -kv[1])[:limit]
    return (["RANK", "PLAYER", col],
            [[i, player, value] for i, (player, value) in enumerate(ranked, 1)])


GAME_HIGH_COLS = ["DATE", "SEASON", "PLAYER", "TEAM", "OPP", "gametype",
                  "ROUND", "GAME", "P", "R", "A", "S", "B", "3PM"]


def game_highs(all_rows: list[dict], col: str, limit: int = 50):
    """Top `limit` single games for one stat, regular season and playoffs.

    R sorts by `desc(stat), DATE` — the earlier game wins a tie, and beyond
    that the frame's own order (regular season rows then playoff rows, each
    by player then date) decides. A missing value sorts last, as R's `arrange`
    places NA last even under `desc`.
    """
    def key(item):
        _i, r = item
        v = _stat(r, col)
        return (v is None, -(v or 0.0), r["DATE"])

    ranked = sorted(enumerate(all_rows), key=key)[:limit]
    rows = []
    for rank, (_i, r) in enumerate(ranked, 1):
        rows.append([rank] + [r.get(c) if c in ("SEASON", "gametype") else _cell(r, c)
                              for c in GAME_HIGH_COLS])
    return ["RANK"] + GAME_HIGH_COLS, rows


def _cell(row: dict, col: str):
    """A game-high cell: numbers as numbers, text as text, blank as NA."""
    if col in ("P", "R", "A", "S", "B", "3PM"):
        return _stat(row, col)
    v = row.get(col)
    if v in ("", "NA"):
        return None
    return v


# --------------------------------------------------------------------------
# Head-to-head matrices — mirrors build-utils.R `write_h2h_matrix`
# --------------------------------------------------------------------------

def _game_level(rows: Iterable[dict[str, str]]) -> set[tuple[str, str, str, str]]:
    """One entry per (season, date, team, opponent, result), from player rows.

    R: `distinct(SEASON, DATE, TEAM, OPP_CLEAN, WL)` after stripping the `@`
    that marks an away game, dropping rows with no result or no opponent.
    """
    games = set()
    for r in rows:
        wl = (r.get("WL") or "").strip()
        opp = (r.get("OPP") or "").replace("@", "").strip()
        if wl in ("", "NA") or opp in ("", "NA"):
            continue
        games.add((r["SEASON"], r["DATE"], r["TEAM"], opp, wl))
    return games


def _records(games: Iterable[tuple[str, str, str, str, str]]) -> dict[tuple[str, str], list[int]]:
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for _season, _date, team, opp, wl in games:
        if wl == "W":
            counts[(team, opp)][0] += 1
        elif wl == "L":
            counts[(team, opp)][1] += 1
    return counts


def h2h_matrix(games: Iterable[tuple], teams: list[str]) -> tuple[list[str], list[list[Any]]]:
    """Square matrix of `W-L` strings; the diagonal is empty, not `0-0`.

    The empty diagonal comes from R filtering `TEAM != OPP` before pivoting,
    leaving `values_fill = ""`. It is an empty field rather than NA in the
    file, which `csvio` keeps distinct.
    """
    counts = _records(games)
    header = ["TEAM"] + teams
    rows = []
    for team in teams:
        row: list[Any] = [team]
        for opp in teams:
            if team == opp:
                row.append("")
            else:
                w, l = counts.get((team, opp), (0, 0))
                row.append(f"{w}-{l}")
        rows.append(row)
    return header, rows


# --------------------------------------------------------------------------
# Per-team career lines
# --------------------------------------------------------------------------

def game_score(row: dict) -> float | None:
    """clean_allstats' GMSC, rounded to 2 exactly where R rounds it.

    R rounds at load, so every later sum and mean is over the *rounded*
    figure. Rounding later instead would drift by a few tenths across a
    300-game career.
    """
    parts = {k: _stat(row, k) for k in
             ("P", "FGM", "FGA", "FTA", "FTM", "OR", "DR", "S", "A", "B", "PF", "TO")}
    if any(v is None for v in parts.values()):
        return None
    return r_round(
        parts["P"] + 0.4 * parts["FGM"] - 0.7 * parts["FGA"]
        - 0.4 * (parts["FTA"] - parts["FTM"]) + 0.7 * parts["OR"] + 0.3 * parts["DR"]
        + parts["S"] + 0.7 * parts["A"] + 0.7 * parts["B"] - 0.4 * parts["PF"] - parts["TO"],
        2,
    )


def _mean(values: list[float], digits: int) -> float | None:
    """R's `round(mean(x, na.rm = TRUE), digits)`: blanks leave the denominator.

    `r_mean`, not `sum(x)/n`: R's mean makes a second compensating pass, and
    the difference is invisible until a value lands on a rounding boundary and
    the decimal flips. It did, in 11 cells across the 30 team files.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None
    return r_round(r_mean(present), digits)


TEAM_PLAYER_COLS = ["PLAYER", "GP", "GMSC_TOT", "GMSC_AVG", "PPG", "RPG", "APG",
                    "SPG", "BPG", "3PMPG", "SEASONS"]


def team_players(reg: list[dict], team: str, limit: int = 100):
    """One team's all-time per-player career lines, best game score first.

    Regular season only. GP counts rows, so a player who appeared for two
    teams in a season is counted under each — that is what a franchise record
    book means here.
    """
    by_player: dict[str, list[dict]] = defaultdict(list)
    for r in reg:
        if r["TEAM"] == team:
            by_player[r["PLAYER"]].append(r)

    rows = []
    for player, games in sorted(by_player.items()):
        gmsc = [game_score(g) for g in games]
        present = [v for v in gmsc if v is not None]
        rows.append([
            player,
            len(games),
            r_round(math.fsum(present), 1) if present else None,
            _mean(gmsc, 2),
            *(_mean([_stat(g, c) for g in games], 1)
              for c in ("P", "R", "A", "S", "B", "3PM")),
            ", ".join(sorted({g["SEASON"] for g in games})),
        ])
    rows.sort(key=lambda r: -(r[2] or 0.0))
    return TEAM_PLAYER_COLS, rows[:limit]


# --------------------------------------------------------------------------
# Franchise records and playoff classics
# --------------------------------------------------------------------------

FRANCHISE_COLS = ["TEAM", "STAT", "RANK", "VALUE", "PLAYER", "SLUG", "DATE",
                  "SEASON", "OPP", "gametype"]


def franchise_records(all_rows: list[dict], limit: int = 5):
    """Every franchise's own top five single games, per stat.

    League-wide `game-highs-*` only ever shows the same handful of superstars,
    so a team without one never appears in it; this gives all 30 a record book.
    """
    rows = []
    for col in FRANCHISE_STATS:
        by_team: dict[str, list[tuple[float, dict]]] = defaultdict(list)
        for r in all_rows:
            v = game_score(r) if col == "GMSC" else _stat(r, col)
            if v is not None:
                by_team[r["TEAM"]].append((v, r))
        for team, entries in by_team.items():
            best = sorted(enumerate(entries), key=lambda e: (-e[1][0], e[1][1]["DATE"]))[:limit]
            for rank, (_i, (value, r)) in enumerate(best, 1):
                rows.append([team, col, rank, value, r["PLAYER"],
                             player_slug(r["PLAYER"]), r["DATE"], r["SEASON"],
                             r["OPP"], r["gametype"]])
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return FRANCHISE_COLS, rows


CLASSICS_COLS = ["RANK", "SEASON", "DATE", "PLAYER", "TEAM", "OPP", "ROUND", "GAME",
                 "P", "R", "A", "S", "B", "3PM", "FGM", "FGA", "GMSC"]


def playoff_classics(playoff_rows: list[dict], limit: int = 10):
    """The best individual playoff performances, wins only, by game score.

    Wins only is R's filter and it is a judgement about what a "classic" is,
    not a data cleanup: a 60-point loss doesn't make the reel.
    """
    stats = ("P", "R", "A", "S", "B", "3PM", "FGM", "FGA")
    grouped: dict[tuple, dict] = {}
    for r in playoff_rows:
        if (r.get("WL") or "").strip() != "W":
            continue
        key = (r["PLAYER"], r["SEASON"], r["DATE"], r["TEAM"], r["OPP"], r["ROUND"], r["GAME"])
        agg = grouped.setdefault(key, {c: 0.0 for c in (*stats, "GMSC")})
        for c in stats:
            agg[c] += _stat(r, c) or 0.0
        agg["GMSC"] += game_score(r) or 0.0

    ordered = sorted(sorted(grouped.items()), key=lambda kv: -kv[1]["GMSC"])[:limit]
    rows = []
    for rank, (key, agg) in enumerate(ordered, 1):
        player, season, date, team, opp, rnd, game = key
        rows.append([rank, season, date, title_case(player), team,
                     (opp or "").replace("@", ""), rnd, game,
                     *(agg[c] for c in stats[:6]), agg["FGM"], agg["FGA"], agg["GMSC"]])
    return CLASSICS_COLS, rows


# --------------------------------------------------------------------------
# Player seasons
# --------------------------------------------------------------------------

def load_bios(data_dir: Path) -> dict[str, dict]:
    """player-bios.json keyed by UPPERCASE name, which is the join key.

    The name is the join, not the slug: the box scores carry no slug, so a
    bio only meets its stat lines through `LAST, FIRST`. First bio wins on a
    duplicate name, as R's `distinct(NAME_KEY, .keep_all = TRUE)` does.
    """
    path = data_dir / "player-bios.json"
    bios: dict[str, dict] = {}
    if not path.exists():
        return bios
    for bio in json.loads(path.read_text()).values():
        key = (bio.get("name") or "").upper()
        if not key or key in bios:
            continue
        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        bios[key] = {
            "PHOTO_URL": bio.get("photo_url") or "",
            "DOB": bio.get("dob"),
            "COLLEGE": bio.get("college") or "",
            "COUNTRY": bio.get("country") or "",
            "NBN_DFT_YR": _int(bio.get("draft_year")),
            "NBN_DFT_R": _int(bio.get("draft_round")),
            "NBN_DFT_P": _int(bio.get("draft_pick")),
        }
    return bios


PLAYER_SEASON_COLS = (["PLAYER", "SEASON", "TEAM", "G"]
                      + [c for c, _ in SEASON_SUMS]
                      + [c for c, _ in SEASON_HIGHS] + ["HIGH_GMSC"]
                      + [c for c, _ in SEASON_TAIL_SUMS] + ["GMSC", "LAST_DATE"]
                      + list(BIO_COLS) + ["RINGS", "SLUG"])


PLAYOFF_SEASON_COLS = [c for c in PLAYER_SEASON_COLS if c != "RINGS"]


def _rings(playoff_rows: list[dict]) -> dict[str, int]:
    champions = _champion_team_seasons(playoff_rows)
    won: dict[str, set[str]] = defaultdict(set)
    for r in playoff_rows:
        if (r["SEASON"], r["TEAM"]) in champions:
            won[r["PLAYER"]].add(r["SEASON"])
    return {player: len(seasons) for player, seasons in won.items()}


def player_seasons(rows: list[dict], data_dir: Path, playoff_rows: list[dict] | None = None):
    """One row per player per season per team, with a bio snapshot attached.

    A *full* join with the bios, so a drafted player who has never played gets
    a row of NAs — that is how `/players` lists this year's draft class before
    anyone has taken the floor. The other side of the join keeps a player whose
    bio is missing entirely, so stats are never dropped for want of a bio.
    """
    bios = load_bios(data_dir)
    # RINGS and the bio-only rows belong to the regular-season file only. The
    # playoff file is a left join with no ring column: a player who never made
    # the playoffs simply isn't in it, so there is nothing to pad.
    regular = playoff_rows is not None
    rings = _rings(playoff_rows) if regular else {}

    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        grouped[(r["PLAYER"], r["SEASON"], r["TEAM"])].append(r)

    out = []
    seen_players = set()
    for (player, season, team), games in grouped.items():
        seen_players.add(player)
        def total(col):
            return math.fsum(v for g in games if (v := _stat(g, col)) is not None)
        def high(col):
            vals = [v for g in games if (v := _stat(g, col)) is not None]
            return max(vals) if vals else None
        gmsc = [v for g in games if (v := game_score(g)) is not None]
        bio = bios.get(player, {})
        row = ([player, season, team, len(games)]
               + [total(src) for _c, src in SEASON_SUMS]
               + [high(src) for _c, src in SEASON_HIGHS]
               + [max(gmsc) if gmsc else None]
               + [total(src) for _c, src in SEASON_TAIL_SUMS]
               + [math.fsum(gmsc), max(g["DATE"] for g in games)]
               + [bio.get(c) for c in BIO_COLS])
        out.append(row + ([rings.get(player, 0), None] if regular else [None]))

    cols = PLAYER_SEASON_COLS if regular else PLAYOFF_SEASON_COLS
    if regular:
        # Full join: bios with no stat line at all, kept only if they were drafted.
        for name, bio in bios.items():
            if name not in seen_players and bio.get("NBN_DFT_YR") is not None:
                out.append([name] + [None] * (len(cols) - len(BIO_COLS) - 3)
                           + [bio.get(c) for c in BIO_COLS] + [rings.get(name, 0), None])

    for row in out:
        row[0] = title_case(row[0])
        row[-1] = player_slug(row[0])
    out.sort(key=lambda r: (r[0], r[1] is None, r[1] or "", r[26] is None, r[26] or ""))
    return cols, out


# --------------------------------------------------------------------------
# Player awards
# --------------------------------------------------------------------------

def title_case(name: str) -> str:
    """`CURRY, STEPHEN` -> `Curry, Stephen`; capitalise each name part.

    R does this with `tools::toTitleCase`, which is a *book-title* capitaliser
    — hence its list of small words ("of", "the", "per", …) left lowercase.
    That list is off-label here and latently wrong: a player first-named Per
    would come out as `Smith, per`. It is deliberately not reproduced. Every
    name in six seasons of league data capitalises identically either way,
    which the byte-identical gate proves on every run.

    What IS reproduced, because it is real and the gate caught it:

        towns, karl-anthony      -> Towns, Karl-Anthony   (hyphens capitalise)
        o'neale, royce           -> O'neale, Royce        (apostrophes do NOT)
    """
    return re.sub(r"(^|[ -])([a-z])", lambda m: m.group(1) + m.group(2).upper(), name.lower())


def player_slug(name: str) -> str:
    """`Curry, Stephen` -> `curry-stephen`."""
    s = name.lower().replace(", ", "-").replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", s)


def _champion_team_seasons(playoff_rows: list[dict]) -> set[tuple[str, str]]:
    wins: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for r in playoff_rows:
        if (r.get("WL") or "").strip() in ("", "NA"):
            continue
        wins[(r["SEASON"], r["TEAM"])].add((r["DATE"], r["WL"]))
    return {
        key for key, games in wins.items()
        if sum(1 for _date, wl in games if wl == "W") >= CHAMPION_WINS
    }


def player_awards(data_dir: Path, playoff_rows: list[dict]):
    """One row per award per player per season, plus a Champion row each.

    Unsorted by design: the award blocks come out in the order job.R binds
    them, each in awards-history.json's own season and player order, with the
    champions appended last in player order.
    """
    path = data_dir / "awards-history.json"
    history = json.loads(path.read_text()) if path.exists() else {}

    entries: list[tuple[str, str, str]] = []   # (player, season, award)
    for key, label in AWARD_LABELS:
        for season, awards in history.items():
            for player in awards.get(key) or []:
                entries.append((player, season, label))

    champions = _champion_team_seasons(playoff_rows)
    seen = set()
    for r in playoff_rows:
        pair = (r["PLAYER"], r["SEASON"])
        if (r["SEASON"], r["TEAM"]) in champions and pair not in seen:
            seen.add(pair)
            entries.append((r["PLAYER"], r["SEASON"], "Champion"))

    rows = []
    for player, season, award in entries:
        name = title_case(player)
        rows.append([player_slug(name), name, season, award])
    return ["SLUG", "PLAYER", "SEASON", "AWARD"], rows


# --------------------------------------------------------------------------
# Entry point — the contract the harness calls
# --------------------------------------------------------------------------

def build(out_dir: Path, data_dir: Path, args) -> list[str]:
    """Write every ported output into `out_dir`; return their relative paths."""
    out_dir = Path(out_dir)
    data_dir = Path(data_dir)

    reg = load_game_rows(data_dir, args.season, playoffs=False)
    playoffs = load_game_rows(data_dir, args.season, playoffs=True)

    # `teams` comes from the regular season only, as in R.
    teams = sorted({r["TEAM"] for r in reg if r.get("TEAM")})

    csvio.write_csv(out_dir / "data" / "h2h-alltime.csv",
                    *h2h_matrix(_game_level(reg) | _game_level(playoffs), teams))
    csvio.write_csv(out_dir / "data" / "h2h-playoffs.csv",
                    *h2h_matrix(_game_level(playoffs), teams))

    reg_rows, playoff_rows = prepare(data_dir, args.season)
    all_rows = reg_rows + playoff_rows
    for suffix, col in STAT_FILES.items():
        csvio.write_csv(out_dir / "data" / f"totals-{suffix}.csv", *career_totals(reg_rows, col))
        csvio.write_csv(out_dir / "data" / f"game-highs-{suffix}.csv", *game_highs(all_rows, col))

    csvio.write_csv(out_dir / "players" / "player_awards.csv",
                    *player_awards(data_dir, playoff_rows))

    for team in teams:
        csvio.write_csv(out_dir / "data" / f"{team.lower()}-players.csv",
                        *team_players(reg_rows, team))

    csvio.write_csv(out_dir / "data" / "franchise-records.csv", *franchise_records(all_rows))
    csvio.write_csv(out_dir / "nbntv-classics" / "playoff-classics.csv",
                    *playoff_classics(playoff_rows))
    csvio.write_csv(out_dir / "players" / "player_seasons.csv",
                    *player_seasons(reg_rows, data_dir, playoff_rows))
    csvio.write_csv(out_dir / "players" / "player_seasons_playoffs.csv",
                    *player_seasons(playoff_rows, data_dir))

    seasons = sorted({r["SEASON"] for r in reg_rows})
    standings = {s: compute_standings([r for r in reg_rows if r["SEASON"] == s])
                 for s in seasons}
    ratings = team_ratings(reg_rows)
    results = playoff_results(playoff_rows)
    for team in teams:
        csvio.write_csv(out_dir / "data" / f"{team.lower()}-seasons.csv",
                        *team_seasons(standings, ratings, results, team))
    csvio.write_csv(out_dir / "standings" / "standings-history.csv",
                    *standings_history(standings, ratings, results, teams))
    csvio.write_csv(out_dir / "standings" / "playoff-brackets.csv",
                    *playoff_brackets(playoff_rows, standings))
    csvio.write_csv(out_dir / "nbntv-classics" / "playoff-series-margins.csv",
                    *playoff_margins(playoff_rows, standings))
    csvio.write_csv(out_dir / "data" / "owner_stats.csv",
                    *owner_stats(data_dir, reg_rows, playoff_rows, ratings))
    csvio.write_csv(out_dir / "data" / "h2h-owners.csv",
                    *owner_h2h(data_dir, reg_rows, playoff_rows, teams))
    csvio.write_csv(out_dir / "data" / "league-history.csv",
                    *league_history(data_dir, reg_rows, playoff_rows, ratings))
    csvio.write_csv(out_dir / "data" / "hof.csv",
                    *hall_of_fame(data_dir, reg_rows, playoff_rows))
    return list(PORTED)


# --------------------------------------------------------------------------
# Standings, seeding and team ratings
# --------------------------------------------------------------------------

EAST = {"MIL", "IND", "BOS", "BKN", "ATL", "ORL", "MIA", "PHI", "WAS", "TOR",
        "CHI", "CHA", "CLE", "NYK", "DET"}
DIVISIONS = {
    "Atlantic": {"NYK", "TOR", "BOS", "PHI", "BKN"},
    "Central": {"DET", "CLE", "MIL", "CHI", "IND"},
    "Southeast": {"ORL", "ATL", "MIA", "CHA", "WAS"},
    "Northwest": {"OKC", "DEN", "MIN", "UTA", "POR"},
    "Pacific": {"LAL", "PHX", "GSW", "SAC", "LAC"},
    "Southwest": {"SAS", "HOU", "MEM", "DAL", "NOP"},
}
_DIVISION_OF = {team: div for div, teams in DIVISIONS.items() for team in teams}


def conference(team: str) -> str | None:
    return "East" if team in EAST else ("West" if team in _DIVISION_OF else None)


def team_games(season_rows: list[dict]) -> list[dict]:
    """One row per team per game: points for, points against, and the labels.

    Points are summed per (date, team, opponent-as-written) — the `@` is
    stripped only afterwards, exactly as R does, so a home and an away meeting
    on one date stay separate.
    """
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for r in season_rows:
        v = _stat(r, "P")
        if v is not None:
            totals[(r["DATE"], r["TEAM"], r["OPP"])] += v

    scored: dict[tuple[str, str], float] = {}
    for (date, team, _opp), pts in totals.items():
        scored.setdefault((date, team), pts)

    games = []
    for (date, team, opp_raw), pts in totals.items():
        opp = opp_raw.replace("@", "")
        against = scored.get((date, opp))
        games.append({
            "DATE": date, "TEAM": team, "OPP": opp,
            "PTS": pts, "OPP_PTS": against,
            "WIN": against is not None and pts > against,
            "LOSS": against is not None and pts < against,
            "CONF": conference(team), "DIV": _DIVISION_OF.get(team),
            "OPP_CONF": conference(opp), "OPP_DIV": _DIVISION_OF.get(opp),
        })
    return games


def compute_standings(season_rows: list[dict]) -> list[dict]:
    """One season's standings, seeded, with the § tie-break chain applied."""
    games = team_games(season_rows)
    by_team: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        by_team[g["TEAM"]].append(g)

    def pct(w, l):
        return w / (w + l) if (w + l) else None

    table = []
    for team, gs in sorted(by_team.items()):
        w = sum(1 for g in gs if g["WIN"])
        l = sum(1 for g in gs if g["LOSS"])
        conf_w = sum(1 for g in gs if g["WIN"] and g["CONF"] == g["OPP_CONF"])
        conf_l = sum(1 for g in gs if g["LOSS"] and g["CONF"] == g["OPP_CONF"])
        div_w = sum(1 for g in gs if g["WIN"] and g["DIV"] == g["OPP_DIV"])
        div_l = sum(1 for g in gs if g["LOSS"] and g["DIV"] == g["OPP_DIV"])
        ppg = r_mean([g["PTS"] for g in gs])
        oppg = r_mean([g["OPP_PTS"] for g in gs if g["OPP_PTS"] is not None])
        table.append({
            "TEAM": team, "W": w, "L": l, "CONF": gs[0]["CONF"], "DIV": gs[0]["DIV"],
            "CONF_W": conf_w, "CONF_L": conf_l, "DIV_W": div_w, "DIV_L": div_l,
            "PPG": ppg, "OPPG": oppg, "PCT": pct(w, l),
            "CONF_PCT": pct(conf_w, conf_l), "DIV_PCT": pct(div_w, div_l),
            "DIFF": ppg - oppg,
        })

    # Head-to-head win rate, used only to break a tie.
    h2h: dict[tuple[str, str], float] = {}
    pairs: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        if g["WIN"]:
            pairs[(g["TEAM"], g["OPP"])][0] += 1
        elif g["LOSS"]:
            pairs[(g["TEAM"], g["OPP"])][1] += 1
    for key, (w, l) in pairs.items():
        h2h[key] = w / (w + l) if (w + l) else None

    # Division winner: best record in the division, on the same chain.
    by_div: dict[str, list[dict]] = defaultdict(list)
    for row in table:
        by_div[row["DIV"]].append(row)
    winners = set()
    for div, rows in by_div.items():
        best = sorted(rows, key=lambda r: (-(r["PCT"] or 0), -(r["CONF_PCT"] or 0), -r["DIFF"]))[0]
        winners.add(best["TEAM"])
    for row in table:
        row["DIV_WINNER"] = row["TEAM"] in winners

    def resolve(tied: list[dict]) -> list[dict]:
        """R's `resolve_nba_ties`, recursion included.

        Order by head-to-head record among the tied teams, then division
        winner, division record, conference record, point differential. If
        every team ties on all five, the whole group keeps its order; otherwise
        the leader is fixed and the rest are re-resolved *without* it — because
        head-to-head is computed among whoever is still tied, so removing a
        team can change the order of the others.
        """
        if len(tied) == 1:
            return tied
        names = {r["TEAM"] for r in tied}
        h2h_pct = {}
        for row in tied:
            vals = [v for (t, o), v in h2h.items()
                    if t == row["TEAM"] and o in names and v is not None]
            h2h_pct[row["TEAM"]] = r_mean(vals) if vals else 0
        ordering = sorted(tied, key=lambda r: (
            -h2h_pct[r["TEAM"]], not r["DIV_WINNER"], -(r["DIV_PCT"] or 0),
            -(r["CONF_PCT"] or 0), -r["DIFF"]))
        top = ordering[0]
        same = [r for r in ordering
                if h2h_pct[r["TEAM"]] == h2h_pct[top["TEAM"]]
                and r["DIV_WINNER"] == top["DIV_WINNER"]
                and r["DIV_PCT"] == top["DIV_PCT"]
                and r["CONF_PCT"] == top["CONF_PCT"]
                and r["DIFF"] == top["DIFF"]]
        if len(same) == len(ordering):
            return ordering
        return [top] + resolve(ordering[1:])

    resolved: list[dict] = []
    for _key, group in sorted(
            _group(table, lambda r: (r["CONF"], r["W"], r["L"])).items()):
        resolved.extend(resolve(group))

    out = []
    for conf in ("East", "West"):
        rows = [r for r in resolved if r["CONF"] == conf]
        if not rows:                              # a season with no games in one
            continue                              # conference: nothing to seed
        rows.sort(key=lambda r: -r["W"])          # stable: keeps the tie order
        best = max(r["W"] - r["L"] for r in rows)
        for seed, row in enumerate(rows, 1):
            out.append({
                "SEED": f"{conf}-{seed}", "TEAM": row["TEAM"],
                "GB": (best - (row["W"] - row["L"])) / 2,
                "W": row["W"], "L": row["L"],
                "PCT": r_round(row["PCT"], 3), "PPG": r_round(row["PPG"], 1),
                "OPPG": r_round(row["OPPG"], 1), "DIFF": r_round(row["DIFF"], 1),
                "SEED_NUM": seed,
            })
    return out


def _group(rows, key):
    out: dict = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return out


def team_ratings(reg_rows: list[dict]) -> dict[tuple[str, str], tuple[float, float]]:
    """OFF_RTG / DEF_RTG per team-season: scoring against what the opponent
    had been allowing, and vice versa, both on running averages.

    Each game is measured against the opponent's *cumulative* form up to that
    point in the season, not their final numbers — so beating a team that was
    good at the time counts as it looked at the time.
    """
    per_game: dict[tuple[str, str, str], float] = defaultdict(float)
    for r in reg_rows:
        v = _stat(r, "P")
        if v is not None:
            per_game[(r["SEASON"], r["TEAM"], r["DATE"], r["OPP"].replace("@", ""))] = \
                per_game[(r["SEASON"], r["TEAM"], r["DATE"], r["OPP"].replace("@", ""))] + v

    rows = [{"SEASON": s, "TEAM": t, "DATE": d, "OPP": o, "P": p}
            for (s, t, d, o), p in per_game.items()]

    # Exact rationals through the running averages: every input is an integer
    # point total over an integer game count, so the whole chain is exact until
    # the single conversion at the end. R accumulates in long double, which is
    # very nearly the same thing; doing it in plain doubles drifts in the last
    # bits and shows up in a 16-digit rating.
    for (_season, _team), group in _group(rows, lambda r: (r["TEAM"], r["SEASON"])).items():
        group.sort(key=lambda r: r["DATE"])
        running = Fraction(0)
        for i, r in enumerate(group, 1):
            running += Fraction(r["P"])
            r["CUM_PPG"] = running / i
    points = {(r["TEAM"], r["DATE"]): r for r in rows}

    for (_team, _season), group in _group(rows, lambda r: (r["TEAM"], r["SEASON"])).items():
        group.sort(key=lambda r: r["DATE"])
        running = Fraction(0)
        n = 0
        for r in group:
            opp = points.get((r["OPP"], r["DATE"]))
            if opp is None:
                continue
            n += 1
            running += Fraction(opp["P"])
            r["CUM_ALLOWED"] = running / n

    out: dict[tuple[str, str], tuple[float, float]] = {}
    for (team, season), group in _group(rows, lambda r: (r["TEAM"], r["SEASON"])).items():
        offs, defs = [], []
        for r in group:
            opp = points.get((r["OPP"], r["DATE"]))
            if opp is None or "CUM_ALLOWED" not in opp or "CUM_PPG" not in opp:
                continue
            offs.append(float(Fraction(r["P"]) - opp["CUM_ALLOWED"]))
            defs.append(float(opp["CUM_PPG"] - Fraction(opp["P"])))
        if offs:
            out[(team, season)] = (r_mean(offs), r_mean(defs))
    return out


# Awarded by vote, not derived — the only two league honours the raw rows
# cannot produce. Kept as data rather than a lookup file because that is where
# job.R keeps them; they move to the awards store when someone adds a UI.
FOTY = {("ATL", "20-21"), ("NOP", "21-22"), ("SAS", "22-23"), ("UTA", "23-24"), ("MEM", "24-25")}
COTY = {("SAC", "20-21"), ("IND", "21-22"), ("SAS", "22-23"), ("UTA", "23-24"), ("MEM", "24-25")}

PLAYOFF_RESULTS = ((16, "Champion"), (12, "Runner-Up"), (8, "Conf Finals"), (4, "Second Round"))

TEAM_SEASON_COLS = ["SEASON", "W", "L", "PCT", "PPG", "OPPG", "DIFF", "SEED", "SEED_NUM",
                    "OFF_RTG", "DEF_RTG", "PLAYOFF_RESULT", "FOTY", "COTY"]


def playoff_results(playoff_rows: list[dict]) -> dict[tuple[str, str], str]:
    """How far each team got, from how many playoff games it won.

    There is no bracket in the raw rows, so the win count *is* the round: 4 to
    get out of the first round, 16 to win it all.
    """
    wins: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    seen: set[tuple[str, str]] = set()
    for r in playoff_rows:
        season = r["SEASON"].replace(" Playoffs", "")
        seen.add((season, r["TEAM"]))
        if (r.get("WL") or "").strip() not in ("", "NA"):
            wins[(season, r["TEAM"])].add((r["DATE"], r["WL"]))
    out = {}
    for key in seen:
        won = sum(1 for _d, wl in wins.get(key, ()) if wl == "W")
        out[key] = next((label for threshold, label in PLAYOFF_RESULTS if won >= threshold),
                        "First Round")
    return out


def team_seasons(standings: dict[str, list[dict]], ratings, results, team: str):
    """One team's season-by-season history."""
    rows = []
    for season in sorted(standings):
        row = next((r for r in standings[season] if r["TEAM"] == team), None)
        if row is None:
            continue
        off, dfn = ratings.get((team, season), (None, None))
        rows.append([season, row["W"], row["L"], row["PCT"], row["PPG"], row["OPPG"],
                     row["DIFF"], row["SEED"], row["SEED_NUM"], off, dfn,
                     results.get((season, team), "Missed"),
                     (team, season) in FOTY, (team, season) in COTY])
    return TEAM_SEASON_COLS, rows


def standings_history(standings, ratings, results, teams: list[str]):
    """Every team-season in one table, ordered as the standings page reads it."""
    rows = []
    for team in teams:
        _cols, team_rows = team_seasons(standings, ratings, results, team)
        rows.extend(row + [team] for row in team_rows)
    rows.sort(key=lambda r: (r[0], r[8]))
    return TEAM_SEASON_COLS + ["TEAM"], rows


# --------------------------------------------------------------------------
# Playoff brackets and series margins
# --------------------------------------------------------------------------

BRACKET_COLS = ["SEASON", "ROUND", "T1", "T2", "T1_W", "T2_W", "WINNER",
                "T1_SEED", "T1_SEED_NUM", "T2_SEED", "T2_SEED_NUM"]
MARGIN_COLS = ["SEASON", "ROUND", "T1", "T2", "GAMES", "AVG_MARGIN", "T1_W", "T2_W",
               "WINNER", "T1_SEED", "T1_SEED_NUM", "T2_SEED", "T2_SEED_NUM"]


def _series_games(playoff_rows: list[dict]):
    """Playoff games keyed by series, with the two teams in a fixed order.

    A series is identified by the alphabetically-first team as T1, so both
    sides' rows collapse onto one series rather than two mirrored ones.
    """
    games: dict[tuple[str, str, str, str], set] = defaultdict(set)
    for r in playoff_rows:
        rnd = r.get("ROUND")
        opp = (r.get("OPP_TEAM") or "").strip()
        if rnd in (None, "", "NA") or opp in ("", "NA"):
            continue
        season = r["SEASON"].replace(" Playoffs", "")
        t1, t2 = min(r["TEAM"], opp), max(r["TEAM"], opp)
        games[(season, rnd, t1, t2)].add(
            (r["DATE"], r["TEAM"], r.get("WL"), r.get("TEAM_PTS"), r.get("OPP_TEAM_PTS")))
    return games


def playoff_brackets(playoff_rows: list[dict], standings: dict[str, list[dict]]):
    """Every playoff series: who played, who won how many, and their seeds."""
    seeds = {(season, row["TEAM"]): (row["SEED"], row["SEED_NUM"])
             for season, rows in standings.items() for row in rows}
    out = []
    # Sorted by series key first: R groups by (season, round, T1, T2), so that
    # is the order the final sort breaks ties against. Two series can share a
    # season, round and T1 seed number — one per conference.
    for (season, rnd, t1, t2), games in sorted(_series_games(playoff_rows).items()):
        t1_w = len({date for date, team, wl, _p, _o in games if team == t1 and wl == "W"})
        t2_w = len({date for date, team, wl, _p, _o in games if team == t2 and wl == "W"})
        s1 = seeds.get((season, t1), (None, None))
        s2 = seeds.get((season, t2), (None, None))
        out.append([season, rnd, t1, t2, t1_w, t2_w, t1 if t1_w >= t2_w else t2,
                    s1[0], s1[1], s2[0], s2[1]])
    out.sort(key=lambda r: (r[0], r[1], r[8] if r[8] is not None else 0))
    return BRACKET_COLS, out


def playoff_margins(playoff_rows: list[dict], standings: dict[str, list[dict]],
                    min_wins: int = 4):
    """Average margin per completed series, closest first.

    Only series someone actually won (4+ wins) — an unfinished or forfeited
    series has no margin worth ranking, and this feeds a "best games" page.
    """
    _cols, brackets = playoff_brackets(playoff_rows, standings)
    by_key = {(b[0], b[1], b[2], b[3]): b for b in brackets}

    out = []
    for key, games in sorted(_series_games(playoff_rows).items()):
        margins: dict[str, float] = {}
        for date, _team, _wl, pts, opp_pts in games:
            if pts in (None, "", "NA") or opp_pts in (None, "", "NA"):
                continue
            margins[date] = abs(float(pts) - float(opp_pts))
        bracket = by_key.get(key)
        if not margins or bracket is None or max(bracket[4], bracket[5]) < min_wins:
            continue
        out.append([key[0], key[1], key[2], key[3], len(margins),
                    r_round(r_mean(list(margins.values())), 1),
                    bracket[4], bracket[5], bracket[6],
                    bracket[7], bracket[8], bracket[9], bracket[10]])
    out.sort(key=lambda r: r[5])
    return MARGIN_COLS, out


# --------------------------------------------------------------------------
# Owners
# --------------------------------------------------------------------------

OWNER_COLS = ["owner", "teams", "seasons", "best_reg_season", "best_reg_pct",
              "worst_reg_season", "worst_reg_pct", "reg_w", "reg_l", "reg_pct",
              "playoff_w", "playoff_l", "playoff_pct", "total_w", "total_l", "total_pct",
              "playoff_appearances", "po_r2", "po_conf_finals", "po_finals",
              "championships", "off_rtg", "def_rtg"]

PLAYOFF_DEPTH = (("po_r2", 4), ("po_conf_finals", 8), ("po_finals", 12), ("championships", 16))


def load_owner_tenures(data_dir: Path, today: date | None = None) -> list[dict]:
    """owners.csv as dated tenures: each owner holds a team until the next starts.

    The file records only start dates, so an end date is the day before the
    next owner of that team took over, and the current owner runs to today.
    """
    today = today or datetime.now(LEAGUE_TZ).date() if False else (today or date.today())
    path = data_dir / "owners.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            month, day, year = (int(x) for x in r["start_date"].split("/"))
            rows.append({"owner": r["owner"], "TEAM": r["team"].upper(),
                         "start": date(year, month, day)})
    rows.sort(key=lambda r: (r["TEAM"], r["start"]))
    for i, r in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        r["end"] = (nxt["start"] - timedelta(days=1)
                    if nxt and nxt["TEAM"] == r["TEAM"] else today)
    return rows


def tenure_seasons(tenure: dict) -> list[str]:
    """Every league year a tenure touches, by R's `get_owners()` rule.

    Deliberately coarser than game-day attribution: a season counts if the
    tenure overlapped it *at all*, because the cutover is June and a GM who
    takes over in the summer owns that season's playoff run even though they
    were not there for the games. Two owners can therefore both be credited
    with one season's playoff depth.
    """
    def season_year(d: date) -> int:
        return d.year - (1 if d.month < 6 else 0)
    return [f"{y % 100:02d}-{(y + 1) % 100:02d}"
            for y in range(season_year(tenure["start"]), season_year(tenure["end"]) + 1)]


def _season_of(day: date) -> str:
    """The league year a date belongs to: June starts the new one."""
    y = day.year if day.month >= 6 else day.year - 1
    return f"{y % 100:02d}-{(y + 1) % 100:02d}"


def _owner_games(tenures: list[dict], games: list[dict]) -> dict[str, list[dict]]:
    """Games attributed to whoever owned that team on the day it was played."""
    by_team: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        by_team[g["TEAM"]].append(g)
    out: dict[str, list[dict]] = defaultdict(list)
    for t in tenures:
        for g in by_team.get(t["TEAM"], ()):
            if t["start"] <= g["DAY"] <= t["end"]:
                out[t["owner"]].append(g)
    return out


def _distinct_games(rows: list[dict], gametype: str) -> list[dict]:
    seen = set()
    out = []
    for r in rows:
        wl = (r.get("WL") or "").strip()
        if wl in ("", "NA"):
            continue
        key = (r["TEAM"], r["DATE"], r["SEASON"], wl, gametype)
        if key in seen:
            continue
        seen.add(key)
        y, m, d = (int(x) for x in r["DATE"].split("-"))
        out.append({"TEAM": r["TEAM"], "DATE": r["DATE"], "DAY": date(y, m, d),
                    "SEASON": r["SEASON"], "WL": wl, "TYPE": gametype})
    return out


def owner_stats(data_dir: Path, reg_rows: list[dict], playoff_rows: list[dict],
                ratings: dict[tuple[str, str], tuple[float, float]]):
    """Career totals per GM, attributed by who owned the team on game day."""
    tenures = load_owner_tenures(data_dir)
    games = _distinct_games(reg_rows, "REG") + _distinct_games(playoff_rows, "PLAYOFF")
    per_owner = _owner_games(tenures, games)

    # Games played per team-season, to weight the ratings by workload.
    counts: dict[tuple[str, str], set] = defaultdict(set)
    for r in reg_rows:
        counts[(r["TEAM"], r["SEASON"])].add((r["OPP"].replace("@", ""), r["DATE"]))

    # A season is credited to whoever owned the team at its midpoint (Jan 1).
    owner_rtg: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for (team, season), (off, dfn) in ratings.items():
        midpoint = date(2000 + int(season[-2:]), 1, 1)
        for t in tenures:
            if t["TEAM"] == team and t["start"] <= midpoint <= t["end"]:
                owner_rtg[t["owner"]].append((off, dfn, len(counts[(team, season)])))

    # Playoff depth is only counted for seasons that actually finished.
    po_wins: dict[tuple[str, str], int] = defaultdict(int)
    for g in games:
        if g["TYPE"] == "PLAYOFF" and g["WL"] == "W":
            po_wins[(g["TEAM"], g["SEASON"].replace(" Playoffs", ""))] += 1
    completed = {season for (_t, season), wins in po_wins.items() if wins >= 16}
    participants = {(g["TEAM"], g["SEASON"].replace(" Playoffs", ""))
                    for g in games if g["TYPE"] == "PLAYOFF"}

    rows = []
    for owner in sorted({t["owner"] for t in tenures}):
        mine = per_owner.get(owner, [])
        reg = [g for g in mine if g["TYPE"] == "REG"]
        po = [g for g in mine if g["TYPE"] == "PLAYOFF"]

        by_season: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for g in reg:
            by_season[_season_of(g["DAY"])][0 if g["WL"] == "W" else 1] += 1
        seasons = {s: (w, l) for s, (w, l) in sorted(by_season.items()) if w + l}
        pcts = {s: w / (w + l) for s, (w, l) in seasons.items()}
        best = max(pcts, key=lambda s: pcts[s]) if pcts else None
        worst = min(pcts, key=lambda s: pcts[s]) if pcts else None

        reg_w = sum(1 for g in reg if g["WL"] == "W")
        reg_l = sum(1 for g in reg if g["WL"] == "L")
        po_w = sum(1 for g in po if g["WL"] == "W")
        po_l = sum(1 for g in po if g["WL"] == "L")

        depth = dict.fromkeys((name for name, _ in PLAYOFF_DEPTH), 0)
        owned = {(t["TEAM"], season) for t in tenures if t["owner"] == owner
                 for season in tenure_seasons(t)}
        for team, season in sorted(owned & participants):
            if season not in completed:
                continue
            for name, threshold in PLAYOFF_DEPTH:
                if po_wins[(team, season)] >= threshold:
                    depth[name] += 1

        weighted = owner_rtg.get(owner, [])
        total_games = sum(n for _o, _d, n in weighted)
        off = r_round(math.fsum(o * n for o, _d, n in weighted) / total_games, 2) if total_games else None
        dfn = r_round(math.fsum(d * n for _o, d, n in weighted) / total_games, 2) if total_games else None

        rows.append([
            owner, ", ".join(sorted({g["TEAM"] for g in mine})),
            len(seasons) or None,      # R leaves this NA for a GM with no games
            f"{seasons[best][0]}-{seasons[best][1]}" if best else None,
            pcts.get(best) if best else None,
            f"{seasons[worst][0]}-{seasons[worst][1]}" if worst else None,
            pcts.get(worst) if worst else None,
            reg_w, reg_l, r_round(reg_w / (reg_w + reg_l), 3) if reg_w + reg_l else None,
            po_w, po_l, r_round(po_w / (po_w + po_l), 3) if po_w + po_l else None,
            reg_w + po_w, reg_l + po_l,
            r_round((reg_w + po_w) / (reg_w + po_w + reg_l + po_l), 3)
            if (reg_w + po_w + reg_l + po_l) else None,
            len({g["SEASON"].replace(" Playoffs", "") for g in po}),
            *(depth[name] for name, _ in PLAYOFF_DEPTH), off, dfn,
        ])
    rows.sort(key=lambda r: (-(r[15] or 0), -r[13]))
    return OWNER_COLS, rows


def owner_h2h(data_dir: Path, reg_rows: list[dict], playoff_rows: list[dict],
              teams: list[str]):
    """Each GM's record against each franchise, regular season and playoffs."""
    tenures = load_owner_tenures(data_dir)
    games = _distinct_games(reg_rows, "REG") + _distinct_games(playoff_rows, "PLAYOFF")
    opponents: dict[tuple[str, str, str], str] = {}
    for r in reg_rows + playoff_rows:
        opponents[(r["TEAM"], r["DATE"], r["SEASON"])] = r["OPP"].replace("@", "")

    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for t in tenures:
        for g in games:
            if g["TEAM"] != t["TEAM"] or not (t["start"] <= g["DAY"] <= t["end"]):
                continue
            opp = opponents.get((g["TEAM"], g["DATE"], g["SEASON"]))
            if not opp:
                continue
            counts[(t["owner"], opp)][0 if g["WL"] == "W" else 1] += 1

    header = ["OWNER"] + teams
    rows = []
    for owner in sorted({t["owner"] for t in tenures}):
        row: list[Any] = [owner]
        for team in teams:
            w, l = counts.get((owner, team), (0, 0))
            row.append(f"{w}-{l}")
        rows.append(row)
    return header, rows


# --------------------------------------------------------------------------
# League history
# --------------------------------------------------------------------------

# Finalists are not derivable: the raw rows record a team's playoff wins, so
# the champion falls out at 16, but "who lost the final" needs the bracket.
RUNNERS_UP = {
    "20-21": ("DAL", "MIL", "DEN"), "21-22": ("NOP", "WAS", "GSW"),
    "22-23": ("CLE", "BKN", "DEN"), "23-24": ("PHX", "NYK", "UTA"),
    "24-25": ("MIL", "ATL", "OKC"),
}
COTY_NAMES = {"20-21": ("That1gal", "SAC"), "21-22": ("Kid Monotone", "IND"),
              "22-23": ("bryn and Q", "SAS"), "23-24": ("Schu", "UTA"),
              "24-25": ("CF", "MEM")}

HISTORY_COLS = ["SEASON", "CHAMPION", "RUNNER_UP", "EAST_RUNNER_UP", "WEST_RUNNER_UP",
                "MVP", "DPOY", "ROTY", "MIP", "FOTY", "COTY",
                "PTS_LEADER", "REB_LEADER", "AST_LEADER", "STL_LEADER", "BLK_LEADER",
                "TPM_LEADER", "BEST_OFF", "BEST_DEF", "BEST_OVERALL"]
HISTORY_LEADERS = (("PTS_LEADER", "P"), ("REB_LEADER", "R"), ("AST_LEADER", "A"),
                   ("STL_LEADER", "S"), ("BLK_LEADER", "B"), ("TPM_LEADER", "3PM"))


def league_history(data_dir: Path, reg_rows: list[dict], playoff_rows: list[dict],
                   ratings: dict[tuple[str, str], tuple[float, float]]):
    """One row per season: champion, award winners, stat and rating leaders."""
    history = json.loads((data_dir / "awards-history.json").read_text()) \
        if (data_dir / "awards-history.json").exists() else {}

    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in reg_rows:
        for col in ("P", "R", "A", "S", "B", "3PM"):
            v = _stat(r, col)
            if v is not None:
                totals[(r["SEASON"], r["PLAYER"])][col] += v

    champions = {season for (season, _team) in _champion_team_seasons(playoff_rows)}
    champion_of = {season.replace(" Playoffs", ""): team
                   for (season, team) in _champion_team_seasons(playoff_rows)}

    rows = []
    for season in sorted({r["SEASON"] for r in reg_rows}):
        runner = RUNNERS_UP.get(season, (None, None, None))
        awards = history.get(season, {})

        def first(key):
            values = awards.get(key) or []
            return values[0] if values else None

        leaders = []
        for _col, stat in HISTORY_LEADERS:
            candidates = sorted((p, vals[stat]) for (s, p), vals in totals.items() if s == season)
            best = max(candidates, key=lambda pv: pv[1], default=None)
            leaders.append(f"{best[0]} ({csvio.format_field(best[1])})" if best else None)

        season_ratings = [(team, off, dfn) for (team, s), (off, dfn) in ratings.items()
                          if s == season]
        rating_cells = []
        for pick in (lambda t: t[1], lambda t: t[2], lambda t: t[1] + t[2]):
            if season_ratings:
                best = max(season_ratings, key=pick)
                rating_cells.append(f"{best[0]} ({pick(best):+.2f})")
            else:
                rating_cells.append(None)

        coty = COTY_NAMES.get(season)
        rows.append([season, champion_of.get(season), *runner,
                     first("MVP"), first("DPOY"), first("ROTY"), first("MIP"),
                     next((t for (t, s) in FOTY if s == season), None),
                     f"{coty[0]} ({coty[1]})" if coty else None,
                     *leaders, *rating_cells])
    return HISTORY_COLS, rows


# --------------------------------------------------------------------------
# Hall of Fame
# --------------------------------------------------------------------------

# What a Hall of Fame case is made of, and what each piece is worth. Weighted
# game score carries the volume; the rest is what the league voted on.
HOF_WEIGHTS = (("RINGS", 10), ("PLAYOFF_APPS", 1), ("MVP", 8), ("DPOY", 5),
               ("ALLSTARS", 3), ("ALL_NBN_1", 4), ("ALL_NBN_2", 3), ("ALL_NBN_3", 2),
               ("ALL_DEF", 2), ("SIX_MOY", 3), ("ROY", 3), ("MIP", 2))
HOF_AWARD_KEYS = {"ALLSTARS": "All-Star", "ALL_NBN_1": "All-NBN-1", "ALL_NBN_2": "All-NBN-2",
                  "ALL_NBN_3": "All-NBN-3", "MVP": "MVP", "DPOY": "DPOY",
                  "ALL_DEF": "All-Defense", "SIX_MOY": "6MOY", "ROY": "ROTY", "MIP": "MIP"}
HOF_COLS = ["PLAYER", "TEAMS", "HOF_POINTS", "RINGS", "PLAYOFF_APPS", "ALLSTARS",
            "ALL_NBN_1", "ALL_NBN_2", "ALL_NBN_3", "MVP", "DPOY", "ALL_DEF",
            "SIX_MOY", "ROY", "MIP", "G", "M", "P", "R", "A", "S", "B", "ACTIVE"]

# A playoff game counts for more the deeper the round, and a short series
# counts for more per game: 5.5 is the average length of a best-of-seven, so a
# sweep's four games carry the weight of a full series.
ROUND_MULTIPLIER = {"1": 2, "2": 4, "3": 8, "4": 16}
AVERAGE_SERIES_GAMES = 5.5


def hall_of_fame(data_dir: Path, reg_rows: list[dict], playoff_rows: list[dict],
                 limit: int = 250):
    """Career HOF scores: weighted game score, plus what the league voted on."""
    history = json.loads((data_dir / "awards-history.json").read_text()) \
        if (data_dir / "awards-history.json").exists() else {}
    awards: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _season, season_awards in history.items():
        for col, key in HOF_AWARD_KEYS.items():
            for player in season_awards.get(key) or []:
                awards[player][col] += 1

    all_rows = reg_rows + playoff_rows
    # Series length, per team-season-round, so a sweep is not punished for it.
    series_dates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for r in all_rows:
        series_dates[(r["SEASON"], r["TEAM"], r.get("ROUND"))].add(r["DATE"])

    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    teams_of: dict[str, set[str]] = defaultdict(set)
    for r in all_rows:
        player = r["PLAYER"]
        rnd = r.get("ROUND")
        gametype_weight = ROUND_MULTIPLIER.get(str(rnd), 1)
        length_weight = (1 if gametype_weight == 1
                         else AVERAGE_SERIES_GAMES / len(series_dates[(r["SEASON"], r["TEAM"], rnd)]))
        gmsc = game_score(r)
        if gmsc is not None:
            totals[player]["GMSC_WEIGHTED"] += (
                gmsc * (1.25 if (r.get("WL") or "").strip() == "W" else 0.75)
                * gametype_weight * length_weight)
        totals[player]["G"] += 1
        for col in ("M", "P", "R", "A", "S", "B"):
            v = _stat(r, col)
            if v is not None:
                totals[player][col] += v

    rings = _rings(playoff_rows)
    apps: dict[str, set[str]] = defaultdict(set)
    for r in playoff_rows:
        apps[r["PLAYER"]].add(r["SEASON"])
    for r in reg_rows:
        teams_of[r["PLAYER"]].add(r["TEAM"])
    latest = max((r["SEASON"] for r in reg_rows), default=None)
    active = {r["PLAYER"] for r in reg_rows if r["SEASON"] == latest}
    # NA, not 0, for a player with no regular-season games at all: R derives
    # this from the regular-season frame, so a playoff-only player has nothing
    # to be 0 about.
    played_regular = {r["PLAYER"] for r in reg_rows}

    rows = []
    for player, stats in sorted(totals.items()):
        counts = {"RINGS": rings.get(player, 0), "PLAYOFF_APPS": len(apps.get(player, ())),
                  **{col: awards[player].get(col, 0) for col in HOF_AWARD_KEYS}}
        points = r_round(stats["GMSC_WEIGHTED"] / 100
                         + math.fsum(counts[col] * weight for col, weight in HOF_WEIGHTS), 1)
        rows.append([player, ",".join(sorted(teams_of.get(player, ()))), points,
                     *(counts[col] for col in HOF_COLS[3:15]),
                     stats["G"], *(stats[c] for c in ("M", "P", "R", "A", "S", "B")),
                     1 if player in active else (0 if player in played_regular else None)])
    rows.sort(key=lambda r: -r[2])
    return HOF_COLS, rows[:limit]

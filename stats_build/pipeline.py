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
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from stats_build import csvio

# Output files this module owns. Everything else is still R's.
STAT_FILES = {"p": "P", "r": "R", "a": "A", "s": "S", "b": "B", "3pm": "3PM"}

PORTED = (
    "data/h2h-alltime.csv",
    "data/h2h-playoffs.csv",
    *(f"data/totals-{k}.csv" for k in STAT_FILES),
    *(f"data/game-highs-{k}.csv" for k in STAT_FILES),
    "players/player_awards.csv",
)

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
    return list(PORTED)

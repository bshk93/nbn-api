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
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from stats_build import csvio

# Output files this module owns. Everything else is still R's.
PORTED = ("data/h2h-alltime.csv", "data/h2h-playoffs.csv")

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
    return list(PORTED)

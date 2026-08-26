"""Where a season's raw box score file lives, and how the first one gets made.

The raw files are append-only and unrebuildable, so nothing creates one casually
— but a season *has* to get its first file from somewhere, and until 2026-08-26
nothing did. The season clock rolls on July 1; from that moment `stats_build`
resolved the new season, found no `allstats-{season}.csv`, and exited 2. Under
`set -e` that took `build.sh` down before `link-public.sh`, so **every build
failed** from the rollover onward — silently, because nothing triggers a build in
the offseason. `POST /api/boxscore/commit` 404'd for the same reason, so the
first game of the season could not have been committed either. It went unnoticed
for five days and would have surfaced as game one never appearing on the site.

Creating the file by hand each July is not a fix; it is the same trap with a
calendar reminder in front of it. So this module does it, once, on demand.

**What must be preserved is the refusal that guard was really making.** Its
comment named two cases: a wrong season was resolved, or the data directory is
not mounted. In both, aggregating zero rows would overwrite 86 good files with
empty ones. The signal that separates those from an ordinary rollover is the
*previous* season's file:

  * previous season present, this one absent → a rollover. Create it.
  * previous season absent too → nothing is mounted, or the season resolved to
    something that never existed. Refuse, exactly as before.

The remaining case — the file existed and was deleted mid-season — reads the
same as a rollover here, and deliberately is not this module's problem.
`check_stats_integrity.py` runs weekly, holds a manifest of every file's row
count and hash, and reports precisely that: `GONE — was N rows, no file on disk
now`, to Discord and as a non-zero exit. Duplicating it here would mean the build
refusing to run on a judgement the integrity check has already made better.

The header is copied **verbatim from the previous season's file** rather than
written from a constant. Older seasons genuinely differ (no `OPP_RAW` before
24-25, some carry `SEASON`, the blank column is `...27` in places), and
`allstats_guard` refuses any write that would drop a column that is on disk. A
file created from a constant that has since moved on would refuse its own first
append.
"""
from __future__ import annotations

import shutil
from pathlib import Path


class RawFileMissing(RuntimeError):
    """A season's raw box score file is absent and cannot be created safely."""


def season_file_name(season: str, game_type: str = "REG") -> str:
    """`allstats-25-26.csv`, or `allstats-playoffs-26.csv` for the postseason.

    Mirrors `routers.boxscores.allstats_path`, which is where this naming is
    used from the API side; kept here too so the build does not have to import a
    FastAPI router to know what a season's file is called.
    """
    if game_type.upper() == "PLAYOFF":
        return f"allstats-playoffs-{season.split('-')[-1]}.csv"
    return f"allstats-{season}.csv"


def previous_season(season: str) -> str:
    """`26-27` → `25-26`. Raises ValueError on anything not `YY-YY`."""
    start, end = season.split("-")
    if len(start) != 2 or len(end) != 2 or not (start.isdigit() and end.isdigit()):
        raise ValueError(f"not a YY-YY season: {season!r}")
    return f"{int(start) - 1:02d}-{int(end) - 1:02d}"


def check_can_create(data_dir: Path, season: str, game_type: str = "REG") -> str:
    """The header row a new `season` file would be given, or raise.

    Split out from `ensure_season_file` so a caller that must not write —
    `stats_build --dry-run` — can still report exactly what a real run would do,
    and still fail on the cases that would fail for real.
    """
    prev = Path(data_dir) / season_file_name(previous_season(season), game_type)
    if not prev.exists():
        raise RawFileMissing(
            f"no raw box scores for {season}, and none for "
            f"{previous_season(season)} at {prev} either. A season file is only "
            f"created when the one before it is on disk — two missing in a row "
            f"means the data directory is not mounted, or {season} is not a "
            f"season this league ever played."
        )

    with prev.open("r", newline="") as fh:
        header = fh.readline()
    if not header.strip():
        raise RawFileMissing(f"{prev} has no header row to copy")
    return header if header.endswith("\n") else header + "\n"


def ensure_season_file(data_dir: Path, season: str,
                       game_type: str = "REG") -> tuple[Path, bool]:
    """The season's raw file, created empty from last season's header if absent.

    Returns `(path, created)`. Raises `RawFileMissing` when the file is absent
    and the previous season's is too — the unmounted-directory and wrong-season
    cases, where creating anything would be the wrong move.

    Only ever creates a header row. The caller appends games through
    `allstats_guard.write_allstats` as usual; a header-only file is a valid
    input to the build, which writes the same 86 outputs from it as it does from
    a season that has not started.
    """
    path = Path(data_dir) / season_file_name(season, game_type)
    if path.exists():
        return path, False

    header = check_can_create(data_dir, season, game_type)

    # Write beside the target and move into place, so a reader can never catch a
    # zero-byte file mid-write: the build reads an empty file as a season with
    # no games, which is exactly the wrong reading of a half-written one.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as fh:
        fh.write(header)
    shutil.move(str(tmp), str(path))
    return path, True

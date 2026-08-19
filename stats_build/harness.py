"""Run-both-and-diff harness for the R -> Python stats port.

The port is correct when the Python pipeline writes *the same bytes* the R
build writes, for every output file, from the same inputs. Nothing weaker is
acceptable: `build/smoke_test.py` asserts schema only (required columns,
minimum row counts) and no values at all, so there is no other oracle.

Two rules this module exists to enforce, both from the spec:

  * **Fix the writer, don't relax the test.** Float formatting, rounding and
    column order are where "identical" fights hardest. When R writes `0.5` and
    Python writes `0.50`, the Python writer changes. So `Comparison.identical`
    is byte equality and nothing else. The report *classifies* a difference as
    formatting-only when both sides parse to the same number, because knowing
    that shortens the fix -- but it never counts as a pass.
  * **Port bug-for-bug.** A difference is a defect until proven to be an R
    formatting artifact, never an improvement smuggled into a port diff.

Everything here is read-only against `NBS_DATA_DIR`: the R build is invoked
directly (not through `build.sh`, which also runs `sync_owners.py` and
`link-public.sh`, both of which write into the live data directory), with
`NBN_OUT_DIR` pointed at a scratch tree.

CLI:

    python3 -m stats_build.harness run-r [--out DIR]
    python3 -m stats_build.harness run-py [--out DIR]
    python3 -m stats_build.harness diff DIR_A DIR_B
    python3 -m stats_build.harness determinism      # R twice, then diff
    python3 -m stats_build.harness port             # R vs Python, then diff
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

# The R build still lives in the site repo; the API already reaches across for
# it (`BUILD_SCRIPT` in routers/build.py). That path disappears at cutover.
REPO_ROOT = Path(os.environ.get("NBN_SITE_REPO", "/home/skim/projects/nbn-today"))
BUILD_DIR = Path(os.environ.get("NBN_BUILD_DIR", REPO_ROOT / "build"))
DATA_DIR = Path(os.environ.get("NBS_DATA_DIR", "/var/lib/nothing-but-stats"))

# Scratch output lives outside the data directory and off /tmp (a 1GB tmpfs
# here, with a quota that has bitten before). Two full output trees are ~4MB.
SCRATCH_ROOT = Path(os.environ.get("NBN_HARNESS_DIR", Path.home() / ".cache" / "nbn-stats-harness"))

LEAGUE_TZ = ZoneInfo("America/New_York")

MANIFEST_NAME = "_manifest.json"

# Cells where R's answer cannot be reproduced without emulating its long-double
# accumulation, and we have decided not to. Each is a per-game game-score
# average landing exactly on a .xx5 boundary, where R's mean lands on the other
# side of the tie in its last bit; the two answers differ by 0.01. Every one is
# a fringe player with a 4-to-20-game career for that franchise.
#
# Listed cell by cell ON PURPOSE rather than as a rule. A rule ("adjacent
# values are fine") would swallow a genuine off-by-one bug; this cannot, and
# any seventh case, or any of these six moving, fails the gate and gets looked
# at by a person.
# The only columns compared with a tolerance rather than exactly, and the only
# place in the port where a tolerance exists at all.
#
# OFF_RTG/DEF_RTG are means over per-game running averages — hundreds of
# additions deep — and R accumulates them in long double. Python has no long
# double, and computing the chain in exact rationals lands no closer (tried:
# 237 differing cells against 223), because R's answer is not the exact value
# either. The two agree to 13-14 significant digits; the measured worst
# relative difference across all 465 rating cells is 2.8e-14, and the site
# renders these to one decimal place.
#
# The bound is set three orders above what was measured and eight below
# anything a logic error could produce: a rating computed wrongly is out by
# 1e-3 at least, never 1e-11. Scoped by column name so it cannot leak into any
# other figure.
RATING_COLUMNS = {"OFF_RTG", "DEF_RTG"}
RATING_TOLERANCE = 1e-11


def rating_noise(column: str, a: str, b: str) -> bool:
    if column not in RATING_COLUMNS:
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if fa == fb:
        return True
    return abs(fa - fb) / max(abs(fa), abs(fb)) <= RATING_TOLERANCE


# Whole files where R's output is wrong and the port's is right, so a
# cell-by-cell comparison cannot even align. Each needs its own real check in
# tests/test_stats_pipeline.py -- being listed here removes it from the gate,
# so it must not be the only thing looking at the file.
KNOWN_FIXED_FILES = {
    "data/league-history.csv":
        "R counts playoff wins over PLAYER rows instead of games, so any team "
        "with about two playoff wins clears the 16-win champion test: 64 rows "
        "for 6 seasons, 11 'champions' in 20-21 alone. season-summary already "
        "works around it by deduplicating and taking the champion from the "
        "bracket ('CSV join artifacts'). The port counts games; every other "
        "cell matches R exactly, and the champion matches the finals winner.",
}

KNOWN_FIXES = {
    # R writes these names wrong, and the port deliberately does not.
    # job.R capitalises with tools::toTitleCase, a *book-title* function whose
    # small-word list ("of", "the", "will", "so", …) is meant for prose. Three
    # real players are first-named Will, so the live site currently shows
    # "Barton, will". The port capitalises every name part instead.
    # Slugs are unaffected -- they lowercase anyway -- so nothing that links by
    # slug changes. Delete this block when R goes; then it is simply correct.
    ("players/player_seasons.csv", "PLAYER", "Barton, will", "Barton, Will"),
    ("players/player_seasons.csv", "PLAYER", "Richard, will", "Richard, Will"),
    ("players/player_seasons.csv", "PLAYER", "Riley, will", "Riley, Will"),
    ("players/player_seasons_playoffs.csv", "PLAYER", "Barton, will", "Barton, Will"),
    ("players/player_seasons_playoffs.csv", "PLAYER", "Richard, will", "Richard, Will"),
    ("players/player_seasons_playoffs.csv", "PLAYER", "Riley, will", "Riley, Will"),
}

KNOWN_TIES = {
    ("data/cle-players.csv", "CHRISTOPHER, JOSH", "GMSC_AVG", "1.28", "1.27"),
    ("data/cle-players.csv", "REDDISH, CAM", "GMSC_AVG", "0.58", "0.57"),
    ("data/mia-players.csv", "HARKLESS, MAURICE", "GMSC_AVG", "1.25", "1.24"),
    ("data/nop-players.csv", "JONES, DAMIAN", "GMSC_AVG", "4.02", "4.03"),
    ("data/okc-players.csv", "RONDO, RAJON", "GMSC_AVG", "3.57", "3.58"),
    ("data/por-players.csv", "LOWRY, KYLE", "GMSC_AVG", "4.92", "4.93"),
}


# --------------------------------------------------------------------------
# Build inputs: resolved once, recorded, and passed explicitly
# --------------------------------------------------------------------------

def resolve_season(today: date | None = None) -> str:
    """Current season, mirroring build.sh's Sep 30 cutoff in league time.

    Deliberately duplicated from build.sh rather than shelled out to: the
    harness must be able to pin a season explicitly, and build.sh cannot run
    without also writing to the live data directory. Both disappear into one
    Python function at cutover.
    """
    today = today or datetime.now(LEAGUE_TZ).date()
    if today.month <= 9:
        y1, y2 = today.year - 1, today.year
    else:
        y1, y2 = today.year, today.year + 1
    return f"{y1 % 100:02d}-{y2 % 100:02d}"


def resolve_playoffs_from(season: str) -> str:
    conf = BUILD_DIR / "seasons.conf"
    if not conf.exists():
        return ""
    for line in conf.read_text().splitlines():
        if line.startswith(f"{season}="):
            return line.split("=", 1)[1].strip()
    return ""


@dataclass(frozen=True)
class BuildArgs:
    """The three arguments job.R takes, always stated rather than defaulted.

    `through` matters: job.R defaults it to `Sys.Date()`, so an unpinned build
    can produce different output on a different day. Two runs being compared
    must agree on it, or the diff is measuring the calendar.
    """

    season: str
    playoffs_from: str
    through: str

    @classmethod
    def resolve(cls, season: str | None = None, through: str | None = None) -> "BuildArgs":
        season = season or resolve_season()
        return cls(
            season=season,
            playoffs_from=resolve_playoffs_from(season),
            through=through or datetime.now(LEAGUE_TZ).date().isoformat(),
        )


# --------------------------------------------------------------------------
# Running each side
# --------------------------------------------------------------------------

def run_r(out_dir: Path, args: BuildArgs, quiet: bool = True) -> float:
    """Run the R build into `out_dir`. Returns wall-clock seconds."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "NBS_DATA_DIR": str(DATA_DIR),
        "NBN_OUT_DIR": str(out_dir),
        "NBN_BUILD_DIR": str(BUILD_DIR),
    }
    start = time.monotonic()
    proc = subprocess.run(
        ["Rscript", str(BUILD_DIR / "job.R"), args.season, args.playoffs_from, args.through],
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-4000:] + "\n")
        raise RuntimeError(f"R build failed ({proc.returncode})")
    if not quiet:
        sys.stderr.write(proc.stderr)
    _write_manifest(out_dir, "R", args, elapsed)
    return elapsed


def run_python(out_dir: Path, args: BuildArgs, quiet: bool = True) -> tuple[float, list[str]]:
    """Run the Python pipeline into `out_dir`. Returns (seconds, files written).

    Phase 2 fills this in one aggregation at a time; until then it is honest
    about not existing rather than writing an empty tree that would diff as
    "86 files missing" and look like a broken run.
    """
    try:
        from stats_build import pipeline  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - until Phase 2
        raise NotImplementedError(
            "stats_build.pipeline does not exist yet (port spec Phase 2). "
            "The harness is usable now for R-vs-R determinism and for diffing "
            "any two output trees."
        ) from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    written = pipeline.build(out_dir=out_dir, data_dir=DATA_DIR, args=args)
    elapsed = time.monotonic() - start
    _write_manifest(out_dir, "python", args, elapsed)
    return elapsed, list(written)


def _write_manifest(out_dir: Path, engine: str, args: BuildArgs, elapsed: float) -> None:
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "engine": engine,
                "season": args.season,
                "playoffs_from": args.playoffs_from,
                "through": args.through,
                "seconds": round(elapsed, 2),
                "run_at": datetime.now(LEAGUE_TZ).isoformat(timespec="seconds"),
                "data_dir": str(DATA_DIR),
            },
            indent=2,
        )
        + "\n"
    )


# --------------------------------------------------------------------------
# Comparing
# --------------------------------------------------------------------------

def snapshot(root: Path) -> dict[str, str]:
    """relative path -> sha256, for every output file under `root`."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@dataclass
class CellDiff:
    row: int          # 1-based data row (header is row 0)
    column: str
    a: str
    b: str
    formatting_only: bool


@dataclass
class FileDiff:
    path: str
    header_a: list[str] = field(default_factory=list)
    header_b: list[str] = field(default_factory=list)
    rows_a: int = 0
    rows_b: int = 0
    cells: list[CellDiff] = field(default_factory=list)
    cells_total: int = 0
    rendering_only: int = 0   # same double, readr printed it longer (see above)
    known_ties: int = 0       # listed in KNOWN_TIES: R's mean won a .xx5 tie
    known_fixes: int = 0      # listed in KNOWN_FIXES: R is wrong, we are not
    rating_noise: int = 0     # OFF_RTG/DEF_RTG, equal to 13+ significant digits
    note: str = ""

    @property
    def formatting_only(self) -> bool:
        """Every difference is a number rendered differently.

        Advisory. A file that is formatting-only is still a failure -- it means
        the Python writer needs fixing, which is exactly the spec's rule.
        """
        return (
            self.header_a == self.header_b
            and self.rows_a == self.rows_b
            and self.cells_total > 0
            and all(c.formatting_only for c in self.cells)
        )


@dataclass
class Comparison:
    only_in_a: list[str]
    only_in_b: list[str]
    identical: list[str]
    differing: list[FileDiff]
    # Files whose bytes differ ONLY by the readr rendering quirk. Reported, not
    # failed -- every number in them is the same number.
    quirk_only: list[FileDiff] = field(default_factory=list)
    # Files listed in KNOWN_FIXED_FILES: R is wrong, the port is right, and the
    # difference is too structural to compare cell by cell.
    fixed_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.only_in_a or self.only_in_b or self.differing)

    @property
    def known_tie_cells(self) -> int:
        return sum(d.known_ties for d in self.differing + self.quirk_only)

    @property
    def known_fix_cells(self) -> int:
        return sum(d.known_fixes for d in self.differing + self.quirk_only)

    @property
    def rating_noise_cells(self) -> int:
        return sum(d.rating_noise for d in self.differing + self.quirk_only)

    @property
    def rendering_only_cells(self) -> int:
        return sum(d.rendering_only for d in self.differing) + sum(
            d.rendering_only for d in self.quirk_only
        )


def _numeric_equal(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _significant_digits(text: str) -> int:
    return len(text.lstrip("-").replace(".", "").lstrip("0"))


def same_double_rendered_differently(a: str, b: str) -> bool:
    """The one accepted difference: readr's digit generator, not a wrong number.

    `readr`/vroom 1.6.5 does not always produce the shortest round-trip form --
    measured, 244 of 150,000 doubles written from R carry one extra significant
    digit (e.g. `1.6841887793779169` where the shortest form is
    `1.684188779377917`). Both parse to the *same* IEEE double, so no computed
    value differs; only the text does.

    This is deliberately **not** a numeric tolerance. The test is exact double
    equality, so any actually-wrong number is a different double and still
    fails. It is further narrowed to full-precision renderings (>= 15
    significant digits), which is the only place the quirk can occur -- so a
    rounded column printed as `0.50` against R's `0.5` is still a writer bug
    and still fails, as it should.

    Scope, re-measured on every run by `tests/test_stats_writer.py`: exactly
    one value, in `OFF_RTG`, in two files. If that count moves, this stops
    being a known quirk and the writer needs the real Grisu2 algorithm.
    """
    if not _numeric_equal(a, b):
        return False
    return _significant_digits(a) >= 15 and _significant_digits(b) >= 15


def _diff_csv(path_a: Path, path_b: Path, rel: str, max_cells: int) -> FileDiff:
    with path_a.open(newline="") as fa, path_b.open(newline="") as fb:
        rows_a = list(csv.reader(fa))
        rows_b = list(csv.reader(fb))
    head_a = rows_a[0] if rows_a else []
    head_b = rows_b[0] if rows_b else []
    d = FileDiff(
        path=rel,
        header_a=head_a,
        header_b=head_b,
        rows_a=max(len(rows_a) - 1, 0),
        rows_b=max(len(rows_b) - 1, 0),
    )
    if head_a != head_b:
        d.note = "header differs; cell comparison skipped"
        return d
    for i in range(1, min(len(rows_a), len(rows_b))):
        ra, rb = rows_a[i], rows_b[i]
        for j in range(max(len(ra), len(rb))):
            va = ra[j] if j < len(ra) else "<missing>"
            vb = rb[j] if j < len(rb) else "<missing>"
            if va == vb:
                continue
            if same_double_rendered_differently(va, vb):
                d.rendering_only += 1
                continue
            col_name = head_a[j] if j < len(head_a) else f"col{j}"
            if (rel, ra[0], col_name, va, vb) in KNOWN_TIES:
                d.known_ties += 1
                continue
            if (rel, col_name, va, vb) in KNOWN_FIXES:
                d.known_fixes += 1
                continue
            if rating_noise(col_name, va, vb):
                d.rating_noise += 1
                continue
            d.cells_total += 1
            if len(d.cells) < max_cells:
                col = head_a[j] if j < len(head_a) else f"col{j}"
                d.cells.append(CellDiff(i, col, va, vb, _numeric_equal(va, vb)))
    return d


def compare(dir_a: Path, dir_b: Path, max_cells: int = 5,
            only: Iterable[str] | None = None) -> Comparison:
    """Byte-compare two output trees; explain the CSV differences it finds.

    `only` restricts the comparison to the files the Python side has actually
    ported. Mid-port that is the whole point — without it every run reports
    80-odd missing files and the one result that matters is buried. It never
    weakens a verdict: a file in `only` is still compared byte for byte.
    """
    snap_a, snap_b = snapshot(dir_a), snapshot(dir_b)
    if only is not None:
        keep = set(only)
        snap_a = {k: v for k, v in snap_a.items() if k in keep}
        snap_b = {k: v for k, v in snap_b.items() if k in keep}
    only_a = sorted(set(snap_a) - set(snap_b))
    only_b = sorted(set(snap_b) - set(snap_a))
    identical, differing, quirk_only, fixed_files = [], [], [], []
    for rel in sorted(set(snap_a) & set(snap_b)):
        if rel in KNOWN_FIXED_FILES and snap_a[rel] != snap_b[rel]:
            fixed_files.append(rel)
        elif snap_a[rel] == snap_b[rel]:
            identical.append(rel)
        elif rel.endswith(".csv"):
            d = _diff_csv(dir_a / rel, dir_b / rel, rel, max_cells)
            if d.cells_total == 0 and (d.rendering_only or d.known_ties
                                       or d.known_fixes or d.rating_noise) and not d.note:
                quirk_only.append(d)
            else:
                differing.append(d)
        else:
            differing.append(FileDiff(path=rel, note="binary/non-CSV difference"))
    return Comparison(only_a, only_b, identical, differing, quirk_only, fixed_files)


def render(cmp_: Comparison, label_a: str = "A", label_b: str = "B") -> str:
    lines: list[str] = []
    total = (len(cmp_.identical) + len(cmp_.differing) + len(cmp_.quirk_only)
             + len(cmp_.fixed_files) + len(cmp_.only_in_a) + len(cmp_.only_in_b))
    verdict = "IDENTICAL" if cmp_.ok else "DIFFERS"
    lines.append(f"{verdict}: {len(cmp_.identical)}/{total} files byte-identical  ({label_a} vs {label_b})")
    for rel in cmp_.fixed_files:
        lines.append(f"  {rel}: differs deliberately — {KNOWN_FIXED_FILES[rel].split('.')[0]}.")
    if cmp_.quirk_only:
        parts = []
        if cmp_.rendering_only_cells:
            parts.append(f"{cmp_.rendering_only_cells} readr rendering")
        if cmp_.known_tie_cells:
            parts.append(f"{cmp_.known_tie_cells} listed .xx5 tie")
        if cmp_.known_fix_cells:
            parts.append(f"{cmp_.known_fix_cells} listed fix to an R bug")
        if cmp_.rating_noise_cells:
            parts.append(f"{cmp_.rating_noise_cells} rating within 1e-11")
        lines.append(
            f"  {len(cmp_.quirk_only)} file(s) differ only by accepted cells "
            f"({', '.join(parts)}): " + ", ".join(d.path for d in cmp_.quirk_only)
        )
    for rel in cmp_.only_in_a:
        lines.append(f"  only in {label_a}: {rel}")
    for rel in cmp_.only_in_b:
        lines.append(f"  only in {label_b}: {rel}")
    for d in cmp_.differing:
        if d.note and not d.cells_total:
            lines.append(f"  {d.path}: {d.note}")
            if d.header_a != d.header_b:
                lines.append(f"      {label_a} header: {','.join(d.header_a)}")
                lines.append(f"      {label_b} header: {','.join(d.header_b)}")
            continue
        tag = " (formatting only)" if d.formatting_only else ""
        rows = "" if d.rows_a == d.rows_b else f", rows {d.rows_a} vs {d.rows_b}"
        lines.append(f"  {d.path}: {d.cells_total} differing cell(s){rows}{tag}")
        for c in d.cells:
            mark = "~" if c.formatting_only else "!"
            lines.append(f"      {mark} row {c.row} [{c.column}]: {c.a!r} vs {c.b!r}")
        if d.cells_total > len(d.cells):
            lines.append(f"      ... {d.cells_total - len(d.cells)} more")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _default_out(name: str) -> Path:
    return SCRATCH_ROOT / name


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stats_build.harness", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    for cmd, default in (("run-r", "r"), ("run-py", "py")):
        s = sub.add_parser(cmd)
        s.add_argument("--out", type=Path, default=_default_out(default))
        s.add_argument("--season")
        s.add_argument("--through")

    s = sub.add_parser("diff")
    s.add_argument("dir_a", type=Path)
    s.add_argument("dir_b", type=Path)
    s.add_argument("--max-cells", type=int, default=5)

    for cmd in ("determinism", "port"):
        s = sub.add_parser(cmd)
        s.add_argument("--season")
        s.add_argument("--through")
        s.add_argument("--max-cells", type=int, default=5)

    a = p.parse_args(argv)

    if a.cmd in ("run-r", "run-py"):
        args = BuildArgs.resolve(a.season, a.through)
        secs = run_r(a.out, args) if a.cmd == "run-r" else run_python(a.out, args)[0]
        print(f"{a.cmd}: {len(snapshot(a.out))} files in {secs:.1f}s -> {a.out}")
        return 0

    if a.cmd == "diff":
        cmp_ = compare(a.dir_a, a.dir_b, a.max_cells)
        print(render(cmp_, a.dir_a.name, a.dir_b.name))
        return 0 if cmp_.ok else 1

    args = BuildArgs.resolve(a.season, a.through)
    print(f"season={args.season} playoffs_from={args.playoffs_from or '-'} through={args.through}")
    if a.cmd == "determinism":
        d1, d2 = _default_out("r1"), _default_out("r2")
        print(f"R run 1: {run_r(d1, args):.1f}s")
        print(f"R run 2: {run_r(d2, args):.1f}s")
        cmp_ = compare(d1, d2, a.max_cells)
        print(render(cmp_, "r1", "r2"))
    else:
        dr, dp = _default_out("r"), _default_out("py")
        print(f"R:      {run_r(dr, args):.1f}s")
        secs, written = run_python(dp, args)
        print(f"Python: {secs:.1f}s  ({len(written)} file(s) ported)")
        cmp_ = compare(dr, dp, a.max_cells, only=written)
        print(render(cmp_, "R", "python"))
        remaining = len(snapshot(dr)) - len(written)
        print(f"  {remaining} file(s) still R-only, not yet ported")
    return 0 if cmp_.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

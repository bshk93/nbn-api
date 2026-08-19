"""The port harness's comparison logic (stats-pipeline-port-spec.md, Phase 1).

Deliberately runs neither build: R takes ~24s and the Python pipeline doesn't
exist yet. What's worth pinning is the part the port's safety rests on — that
`compare` calls a difference a difference. A harness that quietly passes is
worse than no harness, because byte-identical output is the only oracle the
port has: `build/smoke_test.py` asserts required columns and row-count floors
and **no values at all**.

The load-bearing case is `formatting difference still fails`. When R writes
`0.5` and Python writes `0.50` the spec's rule is *fix the writer*, so the
harness may explain such a difference but must never forgive it — a tolerance
here would erase the only safety net the port has.
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

from stats_build import harness, pipeline

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def tree(root, files):
    for rel, text in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return Path(root)


BASE = {
    "data/owner_stats.csv": "owner,reg_w,reg_pct\nSkim,41,0.5\n",
    "players/player_seasons.csv": "PLAYER,SEASON,PTS\nCurry,25-26,2100\n",
}


def pair(files_b, files_a=None):
    """Two output trees under one temp dir, compared. Returns the Comparison."""
    tmp = tempfile.TemporaryDirectory()
    a = tree(Path(tmp.name) / "a", files_a or BASE)
    b = tree(Path(tmp.name) / "b", files_b)
    cmp_ = harness.compare(a, b)
    cmp_._tmp = tmp  # keep the directory alive as long as the result is used
    return cmp_


print("identical trees")
c = pair(BASE)
check("compares ok", c.ok)
check("both files counted identical", len(c.identical) == 2)
check("render says IDENTICAL", "IDENTICAL" in harness.render(c))

print("\na changed value")
c = pair({**BASE, "data/owner_stats.csv": "owner,reg_w,reg_pct\nSkim,42,0.5\n"})
check("fails", not c.ok)
check("one file differs", len(c.differing) == 1 and c.differing[0].path == "data/owner_stats.csv")
cell = c.differing[0].cells[0]
check("cell located by row and column name", (cell.row, cell.column) == (1, "reg_w"))
check("both values reported", (cell.a, cell.b) == ("41", "42"))
check("not classified as formatting", not cell.formatting_only and not c.differing[0].formatting_only)

print("\na formatting-only difference")
c = pair({**BASE, "data/owner_stats.csv": "owner,reg_w,reg_pct\nSkim,41,0.50\n"})
check("STILL FAILS — fix the writer, don't relax the test", not c.ok)
check("classified as formatting for the fixer's benefit", c.differing[0].formatting_only)
check("render says so", "formatting only" in harness.render(c))

print("\na changed header")
c = pair({**BASE, "data/owner_stats.csv": "owner,reg_w,REG_PCT\nSkim,41,0.5\n"})
check("fails", not c.ok)
check("cell comparison skipped", c.differing[0].cells == [])
check("note explains why", "header differs" in c.differing[0].note)
check("both headers rendered", "REG_PCT" in harness.render(c))

print("\na changed row count")
c = pair({**BASE, "players/player_seasons.csv": BASE["players/player_seasons.csv"] + "Doncic,25-26,2200\n"})
check("row counts reported", (c.differing[0].rows_a, c.differing[0].rows_b) == (1, 2))
check("render says so", "rows 1 vs 2" in harness.render(c))

print("\nmissing and extra files")
c = pair({"data/owner_stats.csv": BASE["data/owner_stats.csv"], "data/extra.csv": "A\n1\n"})
check("fails", not c.ok)
check("missing file named", c.only_in_a == ["players/player_seasons.csv"])
check("extra file named", c.only_in_b == ["data/extra.csv"])

print("\nreadr's double-rendering quirk — the one accepted difference")
RTG = {"data/atl-seasons.csv": "SEASON,OFF_RTG\n25-26,1.684188779377917\n"}
c = pair({"data/atl-seasons.csv": "SEASON,OFF_RTG\n25-26,1.6841887793779169\n"}, RTG)
check("same double printed longer is not a defect", c.ok)
check("but it IS reported, never silent", c.rendering_only_cells == 1 and len(c.quirk_only) == 1)
check("render names the file and the count", "readr's double rendering" in harness.render(c))

c = pair({"data/atl-seasons.csv": "SEASON,OFF_RTG\n25-26,1.684188779377918\n"}, RTG)
check("a DIFFERENT double at the same precision still fails", not c.ok)

c = pair({"data/x.csv": "A\n0.50\n"}, {"data/x.csv": "A\n0.5\n"})
check("0.5 vs 0.50 still fails — that is a writer bug, not the quirk", not c.ok)

c = pair({"data/x.csv": "A\n2790.0\n"}, {"data/x.csv": "A\n2790\n"})
check("2790 vs 2790.0 still fails for the same reason", not c.ok)

print("\nthe run manifest")
c = pair({**BASE, harness.MANIFEST_NAME: '{"engine": "python"}'},
         {**BASE, harness.MANIFEST_NAME: '{"engine": "R"}'})
check("excluded from the comparison — it records how a run was made", c.ok)

print("\nbuild arguments")
check("Sep 30 is still last season (build.sh's cutoff)", harness.resolve_season(date(2026, 9, 30)) == "25-26")
check("Oct 1 rolls over", harness.resolve_season(date(2026, 10, 1)) == "26-27")
check("mid-season resolves back", harness.resolve_season(date(2027, 1, 4)) == "26-27")
args = harness.BuildArgs.resolve(season="25-26", through="2026-05-10")
check("through is pinned, not left to job.R's Sys.Date()", args.through == "2026-05-10")
check("playoffs_from read from build/seasons.conf", args.playoffs_from == "2026-04-13")

print("\nthe Python side, mid-port")
with tempfile.TemporaryDirectory() as d:
    secs, written = harness.run_python(Path(d), harness.BuildArgs.resolve("25-26", "2026-05-10"))
    files = set(harness.snapshot(Path(d)))
check("run_python reports which files it ported", set(written) == files and written)
check("it writes only those — an unported file is R's, not an empty stub",
      files == set(pipeline.PORTED))

print("\ncomparing mid-port")
c = pair({**BASE, "data/extra.csv": "A\n1\n"})
check("without `only`, an unported file counts as missing", not c.ok)
tmp = tempfile.TemporaryDirectory()
a = tree(Path(tmp.name) / "a", {**BASE, "data/only-in-r.csv": "A\n1\n"})
b = tree(Path(tmp.name) / "b", BASE)
check("with `only`, the ported files still decide the verdict",
      harness.compare(a, b, only=BASE.keys()).ok)
check("and a ported file that differs still fails under `only`",
      not harness.compare(a, tree(Path(tmp.name) / "c",
                                  {**BASE, "data/owner_stats.csv": "owner,reg_w,reg_pct\nSkim,42,0.5\n"}),
                          only=BASE.keys()).ok)
tmp.cleanup()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

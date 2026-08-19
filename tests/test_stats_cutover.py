"""The production entry point, port spec Phase 3.

`python3 -m stats_build` is what `build/build.sh` runs now, in place of
`Rscript job.R`. The byte-for-byte question is settled elsewhere (the harness,
and test_stats_pipeline); what is pinned here is the wiring around it — the
things that decide *which* season gets rebuilt and *where*, and the one guard
that stops a misconfigured run from overwriting 86 good files with an empty
league.

`build.sh`'s half of the contract — that it defaults to this engine and can
still reach the dormant R one — is checked in the site repo, by
`build/test_build_sh.py` on its pre-commit hook. Asserting it from here would
put a test in one repo that fails until the *other* one deploys, which is the
cross-repo coupling this port exists to remove.
"""

import io
import pathlib
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import date

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("one season resolver, shared by the entry point and the harness")
from stats_build import buildargs, harness  # noqa: E402

check("Sep 30 is still last season (the Sep 30 cutoff)",
      buildargs.resolve_season(date(2026, 9, 30)) == "25-26")
check("Oct 1 rolls over", buildargs.resolve_season(date(2026, 10, 1)) == "26-27")
check("the harness uses that one, not a copy of it",
      harness.resolve_season is buildargs.resolve_season)
check("and the same BuildArgs", harness.BuildArgs is buildargs.BuildArgs)

print("\nthe entry point resolves its own arguments")
import stats_build.__main__ as entry  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    out = pathlib.Path(d) / "derived"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = entry.main(["--season", "25-26", "--out", str(out), "--dry-run"])
    check("--dry-run exits clean", rc == 0)
    check("--dry-run writes nothing at all", not out.exists())
    check("it says which season and where", "season=25-26" in buf.getvalue())

print("\nthe empty-league guard")
with tempfile.TemporaryDirectory() as d:
    empty, out = pathlib.Path(d) / "data", pathlib.Path(d) / "derived"
    empty.mkdir()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = entry.main(["--season", "25-26", "--data-dir", str(empty), "--out", str(out)])
    # Without this the pipeline aggregates zero rows perfectly happily and
    # writes 86 empty CSVs over the real ones. A wrong season or an unmounted
    # data directory has to fail loudly, not succeed vacuously.
    check("no raw box scores for the season is an error, not an empty build", rc == 2)
    check("and nothing is written", not out.exists())

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))
sys.exit(1 if FAILS else 0)

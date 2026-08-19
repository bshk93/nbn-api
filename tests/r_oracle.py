"""Locate R's build output — the port's oracle — now that `derived/` isn't it.

`build/build.sh` writes `$NBS_DATA_DIR/derived` with the **Python** engine since
the 2026-08-19 cutover, so that directory is no longer R's output. A test that
still reads it as "the R build" ends up comparing the port against itself: the
checks keep passing structurally while asserting nothing, and any check that
pinned an R-specific artefact (readr's float quirk, the league-history bug)
fails for a reason that has nothing to do with the code under test.

The surviving R corpus is whatever `python3 -m stats_build.harness port` last
wrote to its scratch root, which stamps `_manifest.json` with the engine and the
arguments it ran under. That is a cache, not a fixture, so it is verified rather
than trusted: this returns a path only when the manifest says the snapshot is
R's, built from the same data directory and season the caller is about to build
for, and no raw box score has landed since it was taken. Otherwise the caller
skips with the reason — a stale snapshot compares freshly aggregated rows
against an older corpus, which is a false failure, and silently tolerating one
is how a port loses its oracle without anyone noticing.
"""

import datetime
import json
import os
import pathlib

SCRATCH_ROOT = pathlib.Path(
    os.environ.get("NBN_HARNESS_DIR", pathlib.Path.home() / ".cache" / "nbn-stats-harness")
)
REFRESH = "run `python3 -m stats_build.harness port` to refresh it"


def r_snapshot(season, data_dir=pathlib.Path("/var/lib/nothing-but-stats")):
    """Return (path, None) for a usable R snapshot, else (None, reason)."""
    root = SCRATCH_ROOT / "r"
    manifest = root / "_manifest.json"
    if not manifest.exists():
        return None, f"no R snapshot at {root} — {REFRESH}"
    try:
        m = json.loads(manifest.read_text())
    except (ValueError, OSError) as exc:
        return None, f"unreadable snapshot manifest ({exc}) — {REFRESH}"

    if m.get("engine") != "R":
        return None, f"snapshot at {root} was built by {m.get('engine')!r}, not R — {REFRESH}"
    if m.get("season") != season:
        return None, (f"snapshot is season {m.get('season')!r}, test wants {season!r} — {REFRESH}")
    if m.get("data_dir") and pathlib.Path(m["data_dir"]) != pathlib.Path(data_dir):
        return None, f"snapshot came from {m['data_dir']}, not {data_dir} — {REFRESH}"
    if not (root / "data" / "h2h-alltime.csv").exists():
        return None, f"snapshot at {root} is incomplete — {REFRESH}"

    # Staleness, measured against the only inputs that can change: the raw box
    # scores. They are append-only, so "a raw file is newer than the snapshot"
    # is exactly the condition under which R's numbers no longer describe the
    # rows Python is about to aggregate.
    run_at = m.get("run_at")
    if run_at:
        try:
            taken = datetime.datetime.fromisoformat(run_at).timestamp()
        except ValueError:
            return None, f"snapshot has an unparseable run_at {run_at!r} — {REFRESH}"
        newer = [p.name for p in pathlib.Path(data_dir).glob("allstats*.csv")
                 if p.stat().st_mtime > taken]
        if newer:
            return None, (f"snapshot predates {', '.join(sorted(newer))} — {REFRESH}")

    return root, None

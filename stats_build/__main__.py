"""The stats build's entry point: `python3 -m stats_build`.

This is what runs in production after the cutover (port spec Phase 3). It is
the aggregation and nothing else -- syncing `owners.csv`, republishing the
`public/` symlink view and the smoke test all stay in `build/build.sh`, which
remains the thing the API triggers.

Full recompute, always: every run reads every raw row and rewrites all 86
outputs. That is the property that makes the build idempotent and self-healing,
and it is preserved deliberately -- see the port spec's "What must NOT change".

    python3 -m stats_build                     # current season, into NBN_OUT_DIR
    python3 -m stats_build --season 24-25
    python3 -m stats_build --out /tmp/scratch  # write somewhere else
    python3 -m stats_build --dry-run           # resolve and report, write nothing

Writes are per-file and atomic (`csvio.write_csv`), so a served CSV is never
half-written. The tree as a whole is not transactional -- a crash midway leaves
some files new and some old, exactly as the R build did, and the fix is the
same one: run it again.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from stats_build import pipeline
from stats_build.buildargs import DATA_DIR, OUT_DIR, BuildArgs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stats_build", description="Rebuild every derived stats CSV.")
    ap.add_argument("--season", help="e.g. 25-26 (default: today's, on a Sep 30 cutoff)")
    ap.add_argument("--through", help="pin the through date (nothing reads it; recorded only)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR, help=f"default {DATA_DIR}")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help=f"default {OUT_DIR}")
    ap.add_argument("--dry-run", action="store_true", help="resolve arguments and exit")
    a = ap.parse_args(argv)

    args = BuildArgs.resolve(a.season, a.through)
    print(f"season={args.season} data={a.data_dir} out={a.out}")

    # Refuse rather than write an empty league. The season's raw file is the
    # one input that cannot be rebuilt, and its absence means a wrong season
    # was resolved or the data directory is not mounted -- either way, a run
    # that "succeeds" having aggregated nothing would overwrite 86 good files
    # with empty ones.
    raw = a.data_dir / f"allstats-{args.season}.csv"
    if not raw.exists():
        print(f"ERROR: no raw box scores for {args.season} at {raw}", file=sys.stderr)
        return 2

    if a.dry_run:
        print("dry run — nothing written")
        return 0

    start = time.monotonic()
    written = pipeline.build(out_dir=a.out, data_dir=a.data_dir, args=args)
    print(f"wrote {len(written)} files in {time.monotonic() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

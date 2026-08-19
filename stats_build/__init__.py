"""Stats aggregation — the build that replaced `nbn-today/build/job.R`.

Live since the cutover on 2026-08-19 (port spec Phase 3). The entry point is
`python3 -m stats_build`; `nbn-today/build/build.sh` runs it, and R survives
only as the dormant rollback behind `NBN_STATS_ENGINE=r`.

See `nbn-today/docs/stats-pipeline-port-spec.md`. `harness.py` still runs both
engines and diffs every file byte for byte — that comparison is the only
value-level oracle these 86 files have, and it goes when R does.
"""

"""Stats aggregation — the build that replaced `nbn-today/build/job.R`.

Live since the cutover on 2026-08-19 (port spec Phase 3). The entry point is
`python3 -m stats_build`; `nbn-today/build/build.sh` runs it, and R stays as
the dormant rollback behind `NBN_STATS_ENGINE=r`.

See `nbn-today/docs/stats-pipeline-port-spec.md`. `harness.py` runs both
engines and diffs every file byte for byte. That comparison is the only
value-level oracle these 86 files have — `build/smoke_test.py` asserts schema
and no values — which is why R is kept rather than uninstalled. Run
`python3 -m stats_build.harness port` after any change here.
"""

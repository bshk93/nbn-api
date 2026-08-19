"""Stats aggregation — the pipeline that replaces the R build.

See `nbn-today/docs/stats-pipeline-port-spec.md`. Phase 1 (this) is the
run-both-and-diff harness; the pipeline itself lands in Phase 2, one
aggregation at a time, and each one flips only once its output matches the
R build byte for byte.
"""

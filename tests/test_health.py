"""`GET /api/health` — the liveness check.

Small endpoint, but it is the one thing a monitor is allowed to depend on, so
what it promises is pinned here: public (no token), never 5xx on the happy
path, and a body a machine can read without parsing prose.

The degraded case is exercised by pointing the module's DATA_DIR at a path that
does not exist, rather than by touching the real one — this suite runs against
live data like the rest of them.

    venv/bin/python -m tests.test_health
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.misc as misc  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


app = FastAPI()
app.include_router(misc.router)
client = TestClient(app)

print("healthy")
r = client.get("/api/health")
check("answers 200", r.status_code == 200)
body = r.json() if r.status_code < 500 else {}
check("status is ok", body.get("status") == "ok")
check("data_dir is true", body.get("data_dir") is True)
check("carries a timestamp", isinstance(body.get("time"), str) and body["time"].startswith("20"))

print("no auth required")
# Every other protected endpoint 401s without a token; this one must not.
check("no Authorization header still 200", client.get("/api/health").status_code == 200)

print("degraded — the data directory is gone")
real = misc.DATA_DIR
try:
    misc.DATA_DIR = Path("/var/lib/nothing-but-stats-does-not-exist")
    r = client.get("/api/health")
    check("answers 503", r.status_code == 503)
    check("status is degraded", r.json().get("status") == "degraded")
    check("data_dir is false", r.json().get("data_dir") is False)
finally:
    misc.DATA_DIR = real

print("still healthy after the swap back")
check("status is ok again", client.get("/api/health").json().get("status") == "ok")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

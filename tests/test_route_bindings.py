"""Route-topology regression test.

Every other suite here calls endpoint functions directly, bypassing FastAPI's
routing entirely (`test_validate_endpoints.py`'s docstring documents exactly
this gap for the request-shape layer). Nothing anywhere has ever asserted that
a `@router.<method>(path)` decorator actually lands on the function it's meant
to — and on 2026-09-20, one didn't: `apply_trade`, extracted as a standalone
function and placed directly under `@router.post("/api/transactions")`
(intended for `create_transaction`, defined ~60 lines later, undecorated),
silently became the real handler. `POST /api/transactions` then accepted
`apply_trade`'s raw parameters as its body schema instead of `TransactionIn`,
so no `TransactionIn`-shaped request (i.e. every real one) could succeed, and
`Depends(require_role("rosters"))` — sitting on the function that never
ran — meant the route had **no auth check** for as long as this went
unnoticed. A second, unrelated change re-triggered the identical mistake
hours later (a different extracted function landed in the same spot).

This only checks route *topology* (which function a decorator resolved to),
never calls an endpoint, and touches no data — importing `main` alone never
runs FastAPI's lifespan startup (that only fires under a real server or a
`with TestClient(app) as client:` block, neither of which happens here), so
this is safe to run against real production files.

    venv/bin/python -m tests.test_route_bindings
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def endpoint_for(path: str, method: str):
    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    return None


print("critical single-endpoint bindings")
ep = endpoint_for("/api/transactions", "POST")
check("POST /api/transactions resolves to create_transaction, not an extracted helper",
      ep is not None and ep.__name__ == "create_transaction")

print("\nno two decorators silently share one (path, method) — the general case")
# route.endpoint on a duplicate registration is whichever came first at
# runtime; the visible symptom is always on the SECOND one, so this counts
# every (path, method) pair app-wide rather than special-casing /api/transactions.
pairs = Counter(
    (r.path, m)
    for r in main.app.routes
    for m in getattr(r, "methods", set()) or set()
)
dupes = sorted(p for p, n in pairs.items() if n > 1)
check(f"no duplicate (path, method) registrations anywhere in the app (found {dupes})",
      not dupes)

print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILURES: {FAILS}")
    sys.exit(1)
print("ALL PASS")

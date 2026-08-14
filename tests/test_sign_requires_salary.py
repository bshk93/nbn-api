"""Regression test for a `sign` with an empty contract silently succeeding.

2026-08-13: an ATL `minimum` signing went through `POST /api/transactions`
with `contract.salaries == {}` — no salary year got added to the bio, and
`_check_minimum_salary`'s loop had nothing to iterate, so it reported
"Every contract year meets the § 3.12 minimum salary" for a contract with no
years at all (nbn-today/BACKLOG.md has the full incident). `offer_sheet`
already guarded this (§ 3.15's 2-year floor); plain `sign` had nothing.

Fixed with a `contract_has_salary_years` check in `_validate_sign` (so the
office's live rubric catches it before submit) and a matching hard 422 in
`_apply_sign` (since the rubric is advisory). This only exercises the
validate endpoint — never `POST /api/transactions`, which applies for real.

    venv/bin/python -m tests.test_sign_requires_salary
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.transactions as tx  # noqa: E402

FAILS = []

app = FastAPI()
app.include_router(tx.router)
client = TestClient(app)


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def rostered_subject():
    """Any player currently on a roster, plus their team."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    for slug, team in sorted(team_map.items()):
        if (bios.get(slug) or {}).get("type") in ("player", "two-way"):
            return slug, team
    raise AssertionError("no rostered player found in live data")


def main():
    print("a sign with no salary years is refused, not scored vacuously")
    slug, team = rostered_subject()

    body = {
        "player": slug, "team": team,
        "contract": {"type": "player", "salaries": {}, "cap_holds": {}},
        "signing_method": "minimum",
    }
    r = client.post("/api/validate/sign", json=body)
    check("endpoint never 5xx's", r.status_code < 500)
    data = r.json()
    checks = data.get("checks", [])
    hit = next((c for c in checks if c.get("check") == "contract_has_salary_years"), None)
    check("contract_has_salary_years check is present", hit is not None)
    check("...and fails", hit is not None and hit.get("passed") is False)
    check("overall verdict is illegal", data.get("legal") is False)

    # A real contract (at least one salary year) must not trip this check.
    body2 = dict(body)
    body2["contract"] = {"type": "player", "salaries": {"26-27": "$5,000,000"}, "cap_holds": {}}
    r2 = client.post("/api/validate/sign", json=body2)
    checks2 = r2.json().get("checks", [])
    check("a real contract doesn't trip the empty-contract check",
          not any(c.get("check") == "contract_has_salary_years" for c in checks2))


if __name__ == "__main__":
    main()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S): {FAILS}")
        sys.exit(1)
    print("\nAll checks passed.")

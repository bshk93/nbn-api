"""§ 4.2 minimum contract trade exception (added 2026-08-20).

Rulebook text (Article IV § 4.2): "A player on a minimum contract of 2
seasons or fewer does not count as incoming salary for matching purposes.
A minimum contract of more than 2 seasons does count."

Until this fix, `_trade_flows` summed every player asset's raw salary with
no such carve-out at all — the rulebook badge said "system-enforced" but
nothing enforced it. Found while entering Trade 48 (CHA/BKN/CLE, 2026-08-16):
AJ Johnson ($2,461,462, a 2024 2nd-round rookie-scale deal) failed salary
matching against $0 outgoing, which is correct — but it raised the question
of whether a *real* minimum-contract player would have been wrongly blocked
the same way. This suite pins both halves: a real minimum-contract signing
is exempt, and a real low-salary non-minimum deal (the AJ Johnson shape)
still isn't.

`_is_exempt_minimum_contract` is derived from the player's own `contracts`
ledger entry (signing_method + salaried-year count on that specific deal),
not a separate stored tag — see its docstring for why. That means the
exemption is only available for players signed *through this system*
(everything from tonight's session qualifies); a historical/backfilled deal
with no `contracts` entry is conservatively treated as non-exempt, same as
before this existed.

Subjects are derived from live data at runtime (mirrors test_validate_
endpoints.py's convention) so the suite doesn't rot the first time these
specific players are traded, re-signed, or released.

    venv/bin/python -m tests.test_minimum_contract_trade_exception
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.transactions as tx  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


app = FastAPI()
app.include_router(tx.router)
client = TestClient(app)


# ── _is_exempt_minimum_contract — pure unit tests, synthetic bios ───────────

def test_pure_function():
    print("\n_is_exempt_minimum_contract — synthetic contracts")

    bios_1yr = {"p": {"contracts": [
        {"signing_method": "minimum", "salaries": {"26-27": "$2,449,421"}},
    ]}}
    check("1-year minimum contract is exempt",
          tx._is_exempt_minimum_contract("p", "26-27", bios_1yr) is True)

    bios_2yr = {"p": {"contracts": [
        {"signing_method": "minimum", "salaries": {"26-27": "$3,066,143", "27-28": "$3,450,720"}},
    ]}}
    check("2-year minimum contract is exempt (either season)",
          tx._is_exempt_minimum_contract("p", "26-27", bios_2yr) is True)
    check("2-year minimum contract is exempt (year 2)",
          tx._is_exempt_minimum_contract("p", "27-28", bios_2yr) is True)

    bios_3yr = {"p": {"contracts": [
        {"signing_method": "minimum", "salaries": {
            "26-27": "$1", "27-28": "$2", "28-29": "$3"}},
    ]}}
    check("3-year minimum contract is NOT exempt (§ 4.2: more than 2 counts)",
          tx._is_exempt_minimum_contract("p", "26-27", bios_3yr) is False)

    bios_rookie = {"p": {"contracts": [
        {"signing_method": None, "salaries": {"26-27": "$2,461,462"}},
    ]}}
    check("non-minimum signing_method is NOT exempt even at a similar dollar amount",
          tx._is_exempt_minimum_contract("p", "26-27", bios_rookie) is False)

    bios_no_contracts = {"p": {"salaries": {"26-27": "$2,449,421"}}}
    check("no contracts ledger at all (historical backfill) is NOT exempt",
          tx._is_exempt_minimum_contract("p", "26-27", bios_no_contracts) is False)

    bios_wrong_season = {"p": {"contracts": [
        {"signing_method": "minimum", "salaries": {"25-26": "$2,000,000"}},
    ]}}
    check("contracts entry that doesn't cover the queried season is NOT exempt",
          tx._is_exempt_minimum_contract("p", "26-27", bios_wrong_season) is False)


# ── subjects, derived from live data ─────────────────────────────────────────

def minimum_exempt_subject():
    """A rostered player whose current-season salary comes from a real
    minimum contract of 2 seasons or fewer — the shape § 4.2 exempts."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    season = tx._current_league_year()
    for slug, team in sorted(team_map.items()):
        if tx._is_exempt_minimum_contract(slug, season, bios):
            return slug, team
    return None


def cheapest_non_exempt_subject(exclude_slug):
    """The lowest-salary rostered player NOT eligible for the exemption,
    among those whose salary is comfortably above the $0-outgoing tiered-
    matching floor ($250K) — the AJ Johnson shape (a low-ish salary that
    still isn't small enough to clear matching on its own, and isn't
    actually a minimum contract). Excludes anything under $500K so this
    regression can't accidentally pass for the unrelated reason that the
    tiered floor alone would have covered it."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    season = tx._current_league_year()
    best = None
    for slug, team in team_map.items():
        if slug == exclude_slug:
            continue
        bio = bios.get(slug) or {}
        sal = tx._parse_dollar((bio.get("salaries") or {}).get(season, ""))
        if sal <= 500_000 or tx._is_exempt_minimum_contract(slug, season, bios):
            continue
        if best is None or sal < best[2]:
            best = (slug, team, sal)
    return (best[0], best[1]) if best else None


def find_check(data, name):
    return next((c for c in data.get("checks", []) if c.get("check") == name), None)


def over_cap_receiving_team(exclude_team):
    """A team clearly over the cap, so § 4.2 cap-room absorption can never be
    the reason a trade passes — isolates the minimum-contract exemption as
    the only thing that could clear salary matching on $0 outgoing."""
    import json
    bios = tx.load_player_bios()
    season = tx._current_league_year()
    cap_levels = json.loads(tx.CAP_LEVELS_FILE.read_text()) if tx.CAP_LEVELS_FILE.exists() else {}
    cap = (cap_levels.get(season, {}) or {}).get("cap")
    for team in sorted(tx.VALID_TEAMS):
        if team == exclude_team:
            continue
        salary = tx._compute_team_salary_ex_holds(team, bios, season)
        if cap is not None and salary > cap:
            return team
    return next(t for t in sorted(tx.VALID_TEAMS) if t != exclude_team)


def test_exempt_player_trade():
    print("\n/api/validate/trade — real minimum-contract player is exempt")
    subj = minimum_exempt_subject()
    if subj is None:
        print("  [skip] no live player currently qualifies for the exemption")
        return
    slug, team = subj
    other = over_cap_receiving_team(team)
    body = {
        "transfers": [
            {"from_team": team, "to_team": other, "assets": [
                {"type": "player", "slug": slug},
            ]},
        ],
        "legality": "tbd",
    }
    resp = client.post("/api/validate/trade", json=body)
    check(f"{slug} ({team} -> {other}): 200 OK", resp.status_code == 200)
    data = resp.json()

    exempt_check = find_check(data, f"min_contract_exempt_{slug}")
    check(f"{slug}: min_contract_exempt check is present", exempt_check is not None)
    if exempt_check:
        check(f"{slug}: min_contract_exempt check passed", exempt_check.get("passed") is True)

    sm = find_check(data, f"salary_matching_{other.lower()}")
    check(f"{other}: salary_matching passes on $0 outgoing once {slug} is excluded",
          sm is None or sm.get("passed") is True)


def test_non_exempt_player_trade_still_fails():
    print("\n/api/validate/trade — regression: cheap non-minimum deal still isn't exempt")
    exempt_subj = minimum_exempt_subject()
    exclude = exempt_subj[0] if exempt_subj else None
    subj = cheapest_non_exempt_subject(exclude)
    if subj is None:
        print("  [skip] no live non-exempt rostered player found")
        return
    slug, team = subj
    other = over_cap_receiving_team(team)
    body = {
        "transfers": [
            {"from_team": team, "to_team": other, "assets": [
                {"type": "player", "slug": slug},
            ]},
        ],
        "legality": "tbd",
    }
    resp = client.post("/api/validate/trade", json=body)
    check(f"{slug} ({team} -> {other}): 200 OK", resp.status_code == 200)
    data = resp.json()

    exempt_check = find_check(data, f"min_contract_exempt_{slug}")
    check(f"{slug}: no min_contract_exempt check (not a minimum contract)",
          exempt_check is None)

    sm = find_check(data, f"salary_matching_{other.lower()}")
    check(f"{other}: salary_matching still fails on $0 outgoing for a real (non-exempt) salary",
          sm is not None and sm.get("passed") is False)


if __name__ == "__main__":
    test_pure_function()
    test_exempt_player_trade()
    test_non_exempt_player_trade_still_fails()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S): {FAILS}")
        sys.exit(1)
    print("\nAll checks passed.")

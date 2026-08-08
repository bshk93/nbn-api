"""Regression tests for owner self-serve roster moves — the § 3.10 renounce a
team owner can trigger from their own roster page, and the `rescind_renounce`
undo behind it.

Written 2026-08-08, when renounce became reachable by someone other than the
office. Three things carry the safety of that:

  * `auth.is_team_owner` — a *position* check on members.json tenures, not a
    role check. Every FO member of a team carries the team role (it gates the
    trading block and jersey numbers); only the owner may move real roster
    state. A GM or coach must fail this.
  * `_renounce_eligibility` — one copy of the § 3.10 test, shared by the
    validator and the apply path, so the roster page can never offer a
    renounce the apply path would reject (or hide one it would accept).
  * The renounce snapshot — a renounce erases `salaries`/`cap_holds`/
    guarantee state, so nothing in the ledger could reconstruct the player
    afterwards. The snapshot taken at the event is the only restore source
    `rescind_renounce` has.

These call the pure functions directly and never POST: `POST /api/transactions`
applies for real when checks pass, so exercising the write path against live
data would mean actually renouncing somebody.

    venv/bin/python -m tests.test_owner_self_serve
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.auth import is_team_owner  # noqa: E402
from routers.transactions import (  # noqa: E402
    _renounce_eligibility, _validate_renounce, _RENOUNCE_SNAPSHOT_FIELDS,
    RenounceDetails,
)

FAILS = []
TODAY = "2026-08-08"


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── is_team_owner ─────────────────────────────────────────────────────────────
# Patched onto the module so the test doesn't depend on who really owns what.
def with_members(members):
    import routers.auth as auth
    auth.load_members = lambda: members


print("\nis_team_owner — position, not role")

with_members({"gm_guy": {"roles": ["phx", "rosters"], "tenures": [
    {"team": "PHX", "start": "2020-07-01", "end": None, "position": "gm"}]}})
check("a GM with the team role is not an owner",
      not is_team_owner({"name": "gm_guy", "roles": ["phx", "rosters"]}, "PHX", TODAY))

with_members({"owner_guy": {"roles": ["phx"], "tenures": [
    {"team": "PHX", "start": "2020-07-01", "end": None, "position": "owner"}]}})
check("an open-ended owner tenure passes",
      is_team_owner({"name": "owner_guy", "roles": ["phx"]}, "PHX", TODAY))
check("...but only for their own team",
      not is_team_owner({"name": "owner_guy", "roles": ["phx"]}, "BOS", TODAY))

with_members({"ex_owner": {"roles": ["phx"], "tenures": [
    {"team": "PHX", "start": "2018-07-01", "end": "2024-06-30", "position": "owner"}]}})
check("a lapsed tenure does not pass",
      not is_team_owner({"name": "ex_owner", "roles": ["phx"]}, "PHX", TODAY))

with_members({"future_owner": {"roles": ["phx"], "tenures": [
    {"team": "PHX", "start": "2027-07-01", "end": None, "position": "owner"}]}})
check("a tenure that hasn't started does not pass",
      not is_team_owner({"name": "future_owner", "roles": ["phx"]}, "PHX", TODAY))

# The team is derived from the roster server-side, but role-holders of *other*
# teams must still never pass — this is the check standing between "has a token"
# and "may renounce a stranger's player".
with_members({"other_owner": {"roles": ["bos"], "tenures": [
    {"team": "BOS", "start": "2020-07-01", "end": None, "position": "owner"}]}})
check("owning one team grants nothing over another",
      not is_team_owner({"name": "other_owner", "roles": ["bos"]}, "PHX", TODAY))

check("an unknown member passes nothing",
      not is_team_owner({"name": "nobody", "roles": []}, "PHX", TODAY))
check("admin passes (consistent with every other check in auth.py)",
      is_team_owner({"name": "nobody", "roles": ["admin"]}, "PHX", TODAY))


# ── _renounce_eligibility ─────────────────────────────────────────────────────
print("\n_renounce_eligibility — § 3.10, one shared copy")

import routers.transactions as txn  # noqa: E402

SEASON = "26-27"      # so the FA window is 27-28
NEXT = "27-28"


def elig(holds, on_roster=True, team="PHX"):
    txn._build_team_map = lambda: ({"p": team} if on_roster else {})
    bios = {"p": {"name": "TEST, PLAYER", "cap_holds": holds,
                  "salaries": {NEXT: "$10,000,000"}}}
    return _renounce_eligibility("p", bios, SEASON)


check("a UFA hold for the upcoming season is renounceable",
      elig({NEXT: "UFA"})["ok"])
check("an RFA hold is too",
      elig({NEXT: "RFA"})["ok"])
check("a hold in the current season is not stale-rejected",
      elig({SEASON: "UFA"})["ok"])
check("a player under contract is not renounceable",
      not elig({"28-29": "UFA"})["ok"])
check("...and says to release instead",
      "Release" in elig({"28-29": "UFA"})["reason"])
check("an unresolved option must be declined first",
      not elig({NEXT: "PLAYER_OPT", "28-29": "UFA"})["ok"])
check("a team option likewise",
      not elig({NEXT: "TEAM_OPT", "28-29": "UFA"})["ok"])
check("no cap holds at all is not renounceable",
      not elig({})["ok"])
check("a free agent on nobody's roster is not renounceable",
      not elig({NEXT: "UFA"}, on_roster=False)["ok"])
check("an unknown slug is rejected, not scored",
      not _renounce_eligibility("ghost", {}, SEASON)["ok"])

# cutoff drives what the apply path trims — getting it wrong either strands a
# hold-season salary on the bio or eats a real prior-season earnings row.
check("cutoff is the earliest FA hold season",
      elig({NEXT: "UFA", "28-29": "UFA"})["cutoff"] == NEXT)
check("team is reported from the roster, never from input",
      elig({NEXT: "UFA"}, team="BOS")["team"] == "BOS")


# ── _validate_renounce ────────────────────────────────────────────────────────
print("\n_validate_renounce — errors block, consequences warn")

CAP_LEVELS = {SEASON: {"cap": 164961000, "min_salary_scale": {"0": 1357763}}}


def validate(holds, roster_count, bird=None, on_roster=True):
    txn._build_team_map = lambda: ({"p": "PHX"} if on_roster else {})
    txn._count_standard_roster = lambda team: roster_count
    txn._bird_tenure = lambda *a, **k: (bird or {
        "tier": None, "seasons": None, "basis": "ledger",
        "evidence": "released on 2025-01-01", "terminal_team": None})
    bios = {"p": {"name": "TEST, PLAYER", "cap_holds": holds,
                  "salaries": {NEXT: "$10,000,000"}}}
    ctx = {"bios": bios, "cur_season": SEASON, "cap_levels": CAP_LEVELS,
           "team_state": {}, "txn_date": TODAY, "trade_exceptions": {}}
    return _validate_renounce(RenounceDetails(player="p"), ctx)


def find(checks, name):
    return next((c for c in checks if c.check == name), None)


r = validate({"28-29": "UFA"}, 15)
check("an ineligible player produces an error", find(r, "renounce_eligible").level == "error")
check("...and it fails", not find(r, "renounce_eligible").passed)
# The vacuous-pass trap: reporting roster/Bird checks as "passed" on a
# transaction that can't happen would read as a mostly-clean verdict.
check("...and nothing else is scored off an unevaluatable renounce", len(r) == 1)

r = validate({NEXT: "UFA"}, 15)
check("an eligible player passes the eligibility check", find(r, "renounce_eligible").passed)
check("a roster of 15 leaves 14 and passes the minimum", find(r, "roster_minimum").passed)

r = validate({NEXT: "UFA"}, 14)
check("dropping to 13 warns about the § 2.1 minimum", not find(r, "roster_minimum").passed)
check("...as a warning, not an error", find(r, "roster_minimum").level == "warning")
check("...and names the strike consequence", "strike" in find(r, "roster_minimum").message)

r = validate({NEXT: "UFA"}, 12)
check("dropping to 11 warns about the § 2.1a charge",
      "Empty Roster Charge" in find(r, "roster_minimum").message)
check("...and prices it at the rookie minimum",
      "1,357,763" in find(r, "roster_minimum").message)

r = validate({NEXT: "UFA"}, 15, bird={
    "tier": "QVFA", "seasons": 5, "basis": "ledger",
    "evidence": "signed with PHX on 2021-07-05", "terminal_team": "PHX"})
check("forfeiting real Bird Rights warns", not find(r, "bird_rights_forfeited").passed)
check("...naming the tier", "QVFA" in find(r, "bird_rights_forfeited").message)
check("...as a warning (§ 3.10 permits it, it's just costly)",
      find(r, "bird_rights_forfeited").level == "warning")

r = validate({NEXT: "UFA"}, 15, bird={
    "tier": "QVFA", "seasons": 6, "basis": "trade_floor",
    "evidence": "acquired by trade on 2020-12-23", "terminal_team": "PHX"})
check("a trade_floor tenure is stated as a lower bound",
      "at least 6" in find(r, "bird_rights_forfeited").message)

# "unknown" is not Non-QVFA — a player with no record is typically the most
# tenured of all, so it must warn rather than quietly report nothing to lose.
r = validate({NEXT: "UFA"}, 15, bird={
    "tier": None, "seasons": None, "basis": "unknown",
    "evidence": "no signing, trade or draft record on file", "terminal_team": None})
check("unknown tenure warns rather than reading as no rights",
      not find(r, "bird_rights_forfeited").passed)

r = validate({NEXT: "UFA"}, 15)
check("a player with no rights to lose passes cleanly",
      find(r, "bird_rights_forfeited").passed)


# ── snapshot coverage ─────────────────────────────────────────────────────────
print("\nsnapshot — every field the renounce trims")

# If _apply_renounce ever trims a field the snapshot doesn't carry, a rescind
# restores a player who is quietly missing it. Pin the pairing.
import inspect  # noqa: E402
src = inspect.getsource(txn._apply_renounce)
trimmed = {f for f in ("salaries", "guaranteed", "guarantee_dates",
                       "guarantee_schedule", "cap_holds", "type")
           if f'bio["{f}"]' in src}
check("_apply_renounce writes only fields the snapshot captures",
      trimmed <= set(_RENOUNCE_SNAPSHOT_FIELDS))
check("...and the snapshot covers every one of them",
      trimmed == set(_RENOUNCE_SNAPSHOT_FIELDS))


print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

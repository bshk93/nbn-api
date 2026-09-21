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
    RenounceDetails, _validate_option, OptionDetails,
    _validate_release, ReleaseDetails,
    _self_convert_twoway_contract, _self_sign_pick_contract,
)
from fastapi import HTTPException  # noqa: E402

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


def elig(holds, on_roster=True, team="PHX", player_type=""):
    txn._build_team_map = lambda: ({"p": team} if on_roster else {})
    bios = {"p": {"name": "TEST, PLAYER", "cap_holds": holds, "type": player_type,
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
check("unsigned draft rights ARE renounceable despite no cap hold at all",
      elig({}, player_type="draft-rights")["ok"])
check("...reported with a DRAFT_RIGHTS hold_type, not a UFA/RFA one",
      elig({}, player_type="draft-rights")["hold_type"] == "DRAFT_RIGHTS")
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


# ── _validate_option ──────────────────────────────────────────────────────────
print("\n_validate_option — TEAM_OPT only, PLAYER_OPT stays a no-op")


def validate_option(holds, roster_count, option_type="TEAM_OPT", decision="decline", year=NEXT):
    txn._build_team_map = lambda: {"p": "PHX"}
    txn._count_standard_roster = lambda team: roster_count
    bios = {"p": {"name": "TEST, PLAYER", "cap_holds": holds}}
    ctx = {"bios": bios, "cur_season": SEASON, "cap_levels": CAP_LEVELS,
           "team_state": {}, "txn_date": TODAY, "trade_exceptions": {}}
    details = OptionDetails(player="p", decision=decision, option_type=option_type,
                            year=year, cap_hold_type="UFA")
    return _validate_option(details, ctx)


check("a PLAYER_OPT decision is a pure no-op, whatever the roster looks like — "
      "it's PDC's judgment call, not this validator's",
      validate_option({NEXT: "PLAYER_OPT"}, 12, option_type="PLAYER_OPT") == [])

r = validate_option({"28-29": "TEAM_OPT"}, 15, year=NEXT)  # option is for a different year
check("a TEAM_OPT for a different year than claimed is an error",
      find(r, "option_eligible").level == "error" and not find(r, "option_eligible").passed)
check("...and nothing else is scored off an unevaluatable option", len(r) == 1)

r = validate_option({NEXT: "TEAM_OPT"}, 15, decision="accept")
check("an eligible TEAM_OPT passes eligibility", find(r, "option_eligible").passed)
check("accepting never scores a roster consequence — § 1.3 already counted the "
      "salary before exercise, so nothing changes financially or on the roster",
      find(r, "roster_minimum") is None)

r = validate_option({NEXT: "TEAM_OPT"}, 15, decision="decline")
check("declining with a healthy roster passes the minimum", find(r, "roster_minimum").passed)

r = validate_option({NEXT: "TEAM_OPT"}, 14, decision="decline")
check("declining down to 13 warns about the § 2.1 minimum",
      not find(r, "roster_minimum").passed and find(r, "roster_minimum").level == "warning")

r = validate_option({NEXT: "TEAM_OPT"}, 12, decision="decline")
check("declining down to 11 warns about the § 2.1a charge",
      "Empty Roster Charge" in find(r, "roster_minimum").message)


# ── _validate_release ─────────────────────────────────────────────────────────
print("\n_validate_release — real contract only, renounce covers the rest")


def validate_release(salaries, holds, roster_count, stretch_years=None):
    txn._build_team_map = lambda: {"p": "PHX"}
    txn._count_standard_roster = lambda team: roster_count
    bios = {"p": {"name": "TEST, PLAYER", "cap_holds": holds, "salaries": salaries}}
    ctx = {"bios": bios, "cur_season": SEASON, "cap_levels": CAP_LEVELS,
           "team_state": {}, "txn_date": TODAY, "trade_exceptions": {}}
    details = ReleaseDetails(player="p", stretch_years=stretch_years)
    return _validate_release(details, ctx)


r = validate_release({}, {}, 15)
check("a player on nobody's roster is an error",
      find(r, "release_eligible").level == "error" and not find(r, "release_eligible").passed)
check("...and nothing else is scored off an unevaluatable release", len(r) == 1)

r = validate_release({NEXT: "$10,000,000"}, {NEXT: "UFA"}, 15)
check("a player with only a UFA hold left has nothing real to release",
      not find(r, "release_eligible").passed)
check("...and points at renounce instead", "renounce" in find(r, "release_eligible").message.lower())

r = validate_release({}, {}, 15)
check("no salaries at all is likewise ineligible", not find(r, "release_eligible").passed)

r = validate_release({NEXT: "$10,000,000", "28-29": "UFA"}, {"28-29": "UFA"}, 15)
check("a real contract year plus a trailing FA hold is releasable",
      find(r, "release_eligible").passed)
check("a roster of 15 leaves 14 and passes the minimum", find(r, "roster_minimum").passed)

r = validate_release({NEXT: "$10,000,000"}, {}, 14)
check("dropping to 13 warns about the § 2.1 minimum",
      not find(r, "roster_minimum").passed and find(r, "roster_minimum").level == "warning")

r = validate_release({NEXT: "$10,000,000"}, {}, 12)
check("dropping to 11 warns about the § 2.1a charge",
      "Empty Roster Charge" in find(r, "roster_minimum").message)


# ── _self_convert_twoway_contract ────────────────────────────────────────────
print("\n_self_convert_twoway_contract — server builds the deal, never checks a submitted one")

CAP_LEVELS_2YR = {
    SEASON: {"min_salary_scale": {"0": 1357763, "1": 2185116}},
    NEXT:   {"min_salary_scale": {"0": 1417307, "1": 2281457}},
}


def build_contract(contracts, draft_year=2026, cur_season=SEASON, cap_levels=None):
    bio = {"name": "TEST, PLAYER", "type": "two-way", "contracts": contracts, "draft_year": draft_year}
    return _self_convert_twoway_contract(bio, "p", cap_levels or CAP_LEVELS_2YR, cur_season)


def raises_http(name, status, fn):
    try:
        fn()
    except HTTPException as e:
        check(f"{name} → {status}", e.status_code == status)
        return
    check(f"{name} → {status}", False)


raises_http("no contract history at all is refused", 422,
           lambda: build_contract([]))

s = build_contract([{"salaries": {NEXT: "$500,000"}}])  # a 1-year prior two-way deal
check("a 1-year prior deal builds a 1-year contract, this season only", list(s) == [SEASON])
check("priced at the tier-0 minimum for a rookie (draft_year == cur_season)",
      s[SEASON] == "$1,357,763")

s2 = build_contract([{"salaries": {SEASON: "$500,000", NEXT: "$500,000"}}])  # a 2-year prior deal
check("a 2-year prior deal builds a 2-year contract, this season plus the next",
      list(s2) == [SEASON, NEXT])
check("year 1 prices at the tier for this season",
      s2[SEASON] == "$1,357,763")
check("year 2 prices at the NEXT season's tier-1 figure — the draft-year proxy "
      "climbs a season later, unlike a flat declared years_experience",
      s2[NEXT] == "$2,281,457")

raises_http("a 3-year prior deal is refused — § 2.2 caps a two-way at 2", 422,
           lambda: build_contract([{"salaries": {SEASON: "$1", NEXT: "$1", "28-29": "$1"}}]))

raises_http("no draft year on file is refused rather than guessed at", 422,
           lambda: build_contract([{"salaries": {NEXT: "$500,000"}}], draft_year=None))


# ── _self_sign_pick_contract ──────────────────────────────────────────────────
print("\n_self_sign_pick_contract — 1st round only, § 7.1's real generator")

import json as _json  # noqa: E402
import tempfile  # noqa: E402

_scale_dir = tempfile.mkdtemp()
_scale_path = Path(_scale_dir) / "rookie-scale.json"
_scale_path.write_text(_json.dumps({
    "2026": [[12000000, 12500000, 13000000, 13500000, 16000000]],  # pick 1 only
}))
txn.ROOKIE_SCALE_FILE = _scale_path


def sign_pick_contract(player_type="draft-rights", draft_round=1, draft_year=2026, draft_pick=1):
    bio = {"name": "TEST, PLAYER", "type": player_type, "draft_round": draft_round,
          "draft_year": draft_year, "draft_pick": draft_pick}
    return _self_sign_pick_contract(bio, "p")


raises_http("not holding draft rights is refused", 422, lambda: sign_pick_contract(player_type="player"))
raises_http("a 2nd-round pick is refused — no generator exists for it", 422,
           lambda: sign_pick_contract(draft_round=2))
raises_http("a draft year with no table on file is refused", 422,
           lambda: sign_pick_contract(draft_year=2099))
raises_http("a pick slot past the end of the table is refused", 422,
           lambda: sign_pick_contract(draft_pick=2))

scale = sign_pick_contract()
check("a real 1st-round pick builds the § 7.1 schedule",
      scale["salaries"][list(scale["salaries"])[0]] == "$12,000,000")
check("Years 3 and 4 are tagged TEAM_OPT, the trailing year RFA",
      list(scale["cap_holds"].values()) == ["TEAM_OPT", "TEAM_OPT", "RFA"])


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

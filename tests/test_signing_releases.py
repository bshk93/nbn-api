"""Releases named on a signing, an FA offer or a waiver claim (§ 2.1, § 5.1).

A team at the roster limit names the player it will cut on the move itself
(`SignDetails.releases`, `WaiverClaimDetails.releases`,
`OfferSheetDetails.releases`) instead of cutting first, so losing the FA
lottery or the waiver priority costs it nobody. The cut counts in the roster
check, and happens only once the signing is written.

`_signing_release_checks` judges each named player: one on the team with a
contract counts; a duplicate, the signee himself, or a player with no contract
left is an error; a player who has since left the team is a warning and isn't
counted (an FA offer is re-judged when it wins, days later).

Nothing here touches the live data dir: the roster CSV is a temp file, and
the ledger, releases and Discord are stubbed.

    venv/bin/python3 tests/test_signing_releases.py
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

import routers.transactions as t  # noqa: E402
import routers.waivers as w  # noqa: E402
import routers.free_agency as fa  # noqa: E402
from routers import season_calendar  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


SEASON = "26-27"
IN_SEASON = "2026-11-03"
CONTRACT = {"type": "player", "salaries": {"26-27": "$5,000,000", "27-28": "$5,000,000"}, "cap_holds": {}}
ROSTER = [f"lal-{i}" for i in range(1, 15)] + ["bench-guy"]          # 15 standard players
BIOS = {slug: {"name": f"{slug.upper()}, X", **CONTRACT} for slug in ROSTER}
BIOS.update({
    "hold-only": {"name": "ONLY, HOLD", "type": "player", "salaries": {"26-27": "$2,000,000"},
                  "cap_holds": {"26-27": "UFA"}},
    "elsewhere": {"name": "WHERE, ELSE", **CONTRACT},
    "signee": {"name": "SIGNEE, NEW", "type": "", "salaries": {}, "cap_holds": {}},
    "tw-1": {"name": "ONE, TW", "type": "two-way", "salaries": {"26-27": "$0"}, "cap_holds": {}},
})
TEAM_MAP = {**{s: "LAL" for s in ROSTER}, "hold-only": "LAL", "elsewhere": "BOS", "tw-1": "LAL"}

tmp = Path(tempfile.mkdtemp())
with (tmp / "lal-roster.csv").open("w", newline="") as fh:
    wr = csv.writer(fh)
    wr.writerow(["SLUG"])
    for s in ROSTER + ["hold-only", "tw-1"]:
        wr.writerow([s])
t.DATA_DIR = tmp
t.load_player_bios = lambda: BIOS
t._build_team_map = lambda: dict(TEAM_MAP)
season_calendar.is_regular_season = lambda d: d == IN_SEASON

# hold-only is a roster row with a hold — it counts as a standard body.
STD_BEFORE = t._count_standard_roster("LAL", bios=BIOS)


def verdicts(releases, signee="signee"):
    checks, valid = t._signing_release_checks("LAL", releases, signee, BIOS, SEASON)
    return {c.check: c for c in checks}, valid


print("\n-- which named players count --")
v, valid = verdicts([])
check("no releases, no checks", v == {} and valid == [])
v, valid = verdicts(["bench-guy"])
check("a player on the team with a contract counts", v["signing_release_bench-guy"].passed and valid == ["bench-guy"])
v, valid = verdicts(["bench-guy", "bench-guy"])
check("listed twice is an error, counted once",
      valid == ["bench-guy"] and any(not c.passed and c.level == "error" for c in v.values()))
v, valid = verdicts(["signee"])
check("the signee himself is an error", not v["signing_release_signee"].passed
      and v["signing_release_signee"].level == "error" and valid == [])
v, valid = verdicts(["hold-only"])
check("a hold with no contract is an error (renounce instead)",
      v["signing_release_hold-only"].level == "error" and "renounce" in v["signing_release_hold-only"].message)
v, valid = verdicts(["elsewhere"])
check("a player on another team is a warning, not counted",
      v["signing_release_elsewhere"].level == "warning" and not v["signing_release_elsewhere"].passed and valid == [])
check("pools are counted apart", t._released_counts(["bench-guy", "tw-1"], BIOS) == (1, 1))


print("\n-- the roster check counts the cut --")
check("LAL carries 16 standard bodies (a hold row counts)", STD_BEFORE == 16)


def roster_check(releases, date=IN_SEASON):
    ctx = {"bios": BIOS, "cur_season": SEASON, "team_state": {}, "cap_levels": {}, "txn_date": date}
    details = t.SignDetails(player="signee", team="LAL",
                            contract=t.ContractIn(salaries={"26-27": "$1,500,000"}), releases=releases)
    return next((c for c in t._validate_sign(details, ctx) if c.check == "roster_size"), None)


r = roster_check([])
check("in-season, no release: over the limit is an error", r is not None and r.level == "error", r)
r = roster_check(["bench-guy"])
check("one release still leaves 16: still an error", r is not None and r.level == "error", r)
r = roster_check(["bench-guy", "lal-1"])
check("two releases bring it to 15: no roster error", r is None, r)
r = roster_check(["bench-guy", "elsewhere"])
check("a player who left isn't counted", r is not None and r.level == "error", r)


print("\n-- apply_sign: cut after the signing, never on a bad release --")
calls = []
t._apply_sign = lambda details, date, info, txn_id=None: calls.append(("sign", details.player))
t._append_transaction = lambda txn: None
t.notify_transaction = lambda *a, **k: None
t.apply_release = lambda details, date, info, **k: (calls.append(("release", details.player))
                                                    or {"id": "rel-" + details.player})
t._run_validation = lambda kind, details, ctx: t._signing_release_checks(
    details.team.upper(), details.releases, details.player, BIOS, SEASON)[0]
t.load_team_state = lambda: {}
t.load_trade_exceptions = lambda: {}

txn = t.apply_sign(t.SignDetails(player="signee", team="LAL", contract=t.ContractIn(salaries={"26-27": "$1"}),
                                 releases=["bench-guy"]), IN_SEASON, {"name": "office"})
check("signs first, then releases", calls == [("sign", "signee"), ("release", "bench-guy")], calls)
check("the ledger row names its release", txn.get("release_txn_ids") == ["rel-bench-guy"], txn)

calls.clear()
try:
    t.apply_sign(t.SignDetails(player="signee", team="LAL", contract=t.ContractIn(salaries={"26-27": "$1"}),
                               releases=["hold-only"]), IN_SEASON, {"name": "office"}, force=True)
    check("a release with no contract refuses even when forced", False, "no exception")
except HTTPException as e:
    check("a release with no contract refuses even when forced",
          e.status_code == 422 and e.detail.get("can_force") is False)
check("...and writes nothing", calls == [], calls)

calls.clear()
t.apply_sign(t.SignDetails(player="signee", team="LAL", contract=t.ContractIn(salaries={"26-27": "$1"}),
                           releases=["elsewhere"]), IN_SEASON, {"name": "office"}, force_warnings_only=True)
check("a player who left is skipped, not released", calls == [("sign", "signee")], calls)


print("\n-- waiver claims --")
ok = t.CheckResult(check="roster_size", passed=True, message="")
gone = t.CheckResult(check="signing_release_x", passed=False, level="warning", message="")
other_warn = t.CheckResult(check="minimum_salary", passed=False, level="warning", message="")
check("a cut who has left doesn't sink a claim", not w._claim_fails([ok, gone]))
check("any other warning still does", w._claim_fails([ok, other_warn]))

LEDGER = [
    {"id": "r1", "type": "release", "date": IN_SEASON, "details": {"player": "waived-a", "team": "BOS"}},
    {"id": "c1", "type": "waiver_claim", "date": IN_SEASON,
     "details": {"released_txn_id": "r1", "team": "LAL", "releases": ["bench-guy"]}},
]
w._load_transactions = lambda: LEDGER
check("an open claim's cut is pending", w.pending_claim_releases("LAL") == {"bench-guy": "waived-a"})
check("...for that team only", w.pending_claim_releases("BOS") == {})

cut = []
w.release_under_lock = lambda details, date, info, **k: (cut.append(details.player) or ({"id": "x"}, None))
pairs = w._release_for_claim({"team": "LAL", "releases": ["bench-guy", "elsewhere"]}, LEDGER[0], IN_SEASON,
                             {"name": "system"}, {"bios": BIOS, "cur_season": SEASON})
check("a winning claim releases its cut, skipping one who left", cut == ["bench-guy"] and len(pairs) == 1, cut)

fa._load_offers = lambda: [{"id": "o1", "team": "LAL", "player": "fa-guy", "status": "submitted",
                            "archived_at": None, "offer": {"releases": ["lal-1"]}}]
fa.load_player_bios = lambda: BIOS
check("a claim can't reuse another claim's cut", w._release_named_elsewhere("LAL", ["bench-guy"]) is not None)
check("...or an FA offer's", w._release_named_elsewhere("LAL", ["lal-1"]) is not None)
check("a fresh cut is fine", w._release_named_elsewhere("LAL", ["lal-2"]) is None)


print("\n-- FA offers --")
mine = {"id": "o2", "team": "LAL", "player": "fa-two", "status": "draft", "archived_at": None,
        "offer": {"releases": ["lal-1"]}}
offers = fa._load_offers() + [mine]
check("an offer can't reuse another submitted offer's cut", fa._release_named_elsewhere(offers, mine) is not None)
mine["offer"]["releases"] = ["bench-guy"]
check("...or a waiver claim's", fa._release_named_elsewhere(offers, mine) is not None)
mine["offer"]["releases"] = ["lal-2"]
check("a fresh cut is fine", fa._release_named_elsewhere(offers, mine) is None)
draft_only = [{**offers[0], "status": "draft"}, mine]
mine["offer"]["releases"] = ["lal-1"]
check("a draft offer doesn't hold a cut", fa._release_named_elsewhere(draft_only, mine) is None)


print()
if FAILS:
    print(f"{len(FAILS)} failed")
    sys.exit(1)
print("all passed")

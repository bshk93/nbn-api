"""Releases carried by a trade (§ 5.1) — `TradeIn.releases`.

A team can list players it releases right after the trade: one it keeps or
one it receives here, never one it sends away. `_trade_release_checks` judges
that against the post-trade rosters; `apply_trade` refuses a bad release even
when forced, then applies each good one as its own ordinary release
(`apply_release`) after the trade. The office's own release goes through the
same `apply_release`.

Nothing here touches disk: the roster map, bios, ledger and Discord are stubbed.

    venv/bin/python3 tests/test_trade_releases.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

import routers.transactions as t  # noqa: E402
import routers.waiver_notify as wn  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


def raises(name, status, fn):
    try:
        fn()
    except HTTPException as e:
        check(name, e.status_code == status, f"got {e.status_code}: {e.detail}")
        return e
    check(name, False, "no exception")


SEASON = "26-27"
CONTRACT = {"salaries": {"26-27": "$5,000,000", "27-28": "$5,000,000"}, "cap_holds": {}}
BIOS = {
    "hayes-jaxson": {"name": "HAYES, JAXSON", **CONTRACT},
    "clayton-walter": {"name": "CLAYTON, WALTER", **CONTRACT},
    "kept-guy": {"name": "GUY, KEPT", **CONTRACT},
    "hold-only": {"name": "ONLY, HOLD", "salaries": {"26-27": "$2,000,000"}, "cap_holds": {"26-27": "UFA"}},
    "elsewhere": {"name": "WHERE, ELSE", **CONTRACT},
}
t._build_team_map = lambda: {"hayes-jaxson": "LAL", "clayton-walter": "LAL", "kept-guy": "UTA",
                             "hold-only": "UTA", "elsewhere": "BOS"}


def trade(releases):
    return t.TradeIn(transfers=[
        t.TradeTransfer(from_team="LAL", to_team="UTA", assets=[
            t.TradeAsset(type="player", slug="hayes-jaxson"), t.TradeAsset(type="player", slug="clayton-walter")]),
        t.TradeTransfer(from_team="UTA", to_team="LAL", assets=[
            t.TradeAsset(type="pick", year=2027, round=2, orig="ATL")]),
    ], releases=releases)


def verdicts(releases):
    return {c.check: (c.passed, c.message) for c in t._trade_release_checks(trade(releases), BIOS, SEASON)}


print("which releases a trade can carry")
check("no releases, no checks", t._trade_release_checks(trade({}), BIOS, SEASON) == [])
v = verdicts({"UTA": ["hayes-jaxson"]})
check("a player the team receives in the trade", v["trade_release_hayes-jaxson"][0] is True)
check("a player the team keeps", verdicts({"UTA": ["kept-guy"]})["trade_release_kept-guy"][0] is True)
v = verdicts({"LAL": ["hayes-jaxson"]})
check("not a player the team sends away", v["trade_release_hayes-jaxson"][0] is False
      and "won't be on LAL" in v["trade_release_hayes-jaxson"][1], v)
check("not a player on another team", verdicts({"UTA": ["elsewhere"]})["trade_release_elsewhere"][0] is False)
v = verdicts({"UTA": ["hold-only"]})
check("not a player with no real contract (renounce instead)", v["trade_release_hold-only"][0] is False
      and "renounce" in v["trade_release_hold-only"][1], v)
v = t._trade_release_checks(trade({"UTA": ["hayes-jaxson", "hayes-jaxson"]}), BIOS, SEASON)
check("the same player twice is refused", [c.passed for c in v] == [True, False], [c.passed for c in v])
check("team abbrs are case-blind", verdicts({"uta": ["hayes-jaxson"]})["trade_release_hayes-jaxson"][0] is True)

# ── apply_trade ────────────────────────────────────────────────────────────────

print("\napply_trade applies the releases after the trade")
EVENTS = []
t.load_player_bios = lambda: BIOS
t.load_team_state = lambda: {}
t.load_trade_exceptions = lambda: {}
t._season_for_date = lambda d: SEASON
t._apply_trade = lambda details, date, info, txn_id: (EVENTS.append(("trade", txn_id)), ["LAL", "UTA"])[1]
t._append_transaction = lambda txn: EVENTS.append(("ledger", txn["type"], txn["details"].get("player")))
t.notify_transaction = lambda txn, forced, relay_to_roster_log=False: EVENTS.append(("notify", txn["type"], relay_to_roster_log))
t._apply_release = lambda details, date, info: (EVENTS.append(("release", details.player)),
                                                 ("UTA", {"26-27": "$5,000,000"}, "$5,000,000", {"x": 1}))[1]
WAIVED = []
wn.notify_waived = lambda slug, team: WAIVED.append((slug, team))



def run_validation(kind, details, ctx):
    if kind == "trade":
        return t._trade_release_checks(details, BIOS, SEASON)
    return RELEASE_CHECKS["value"]


RELEASE_CHECKS = {"value": []}
t._run_validation = run_validation
INFO = {"name": "Pat"}

txn = t.apply_trade(trade({"UTA": ["hayes-jaxson"]}), "2026-09-29", INFO, description="TRC request #1")
kinds = [e[0] for e in EVENTS]
check("trade applied and logged, then the release applied and logged",
      kinds == ["trade", "ledger", "notify", "release", "ledger", "notify"], EVENTS)
check("the release is its own ledger entry for that player", ("ledger", "release", "hayes-jaxson") in EVENTS)
check("it opens the waiver wire like any release", WAIVED == [("hayes-jaxson", "UTA")], WAIVED)
check("the trade's return names the release entry", len(txn.get("release_txn_ids", [])) == 1)

print("\na release that can't happen blocks the trade, forced or not")
EVENTS.clear()
e = raises("refused", 422, lambda: t.apply_trade(trade({"LAL": ["hayes-jaxson"]}), "2026-09-29", INFO))
check("and says it can't be forced", e and e.detail.get("can_force") is False)
raises("refused with force=True too", 422,
       lambda: t.apply_trade(trade({"LAL": ["hayes-jaxson"]}), "2026-09-29", INFO, force=True))
check("nothing was applied", EVENTS == [], EVENTS)

print("\nthe trade's releases don't stop on a warning")
EVENTS.clear()
RELEASE_CHECKS["value"] = [t.CheckResult(check="roster_minimum", passed=False, level="warning", message="short")]
t.apply_trade(trade({"UTA": ["hayes-jaxson"]}), "2026-09-29", INFO)
check("a roster-minimum warning doesn't block the release", ("release", "hayes-jaxson") in EVENTS)

# ── the office's release ───────────────────────────────────────────────────────

print("\nthe office's release goes through apply_release")
EVENTS.clear()
raises("a warning still blocks the office unless forced", 422, lambda: t.create_transaction(
    t.TransactionIn(type="release", date="2026-09-29", details={"player": "kept-guy"}), INFO))
out = t.create_transaction(
    t.TransactionIn(type="release", date="2026-09-29", details={"player": "kept-guy"}, force=True), INFO)
check("forced, it applies", out["type"] == "release" and ("release", "kept-guy") in EVENTS)
check("with the dead cap and snapshot stored", out["details"]["dead_cap"] == {"26-27": "$5,000,000"}
      and out["details"]["_snapshot"] == {"x": 1})
check("and the forced check recorded", out["details"].get("_forced_checks") == ["roster_minimum"])
RELEASE_CHECKS["value"] = []
raises("a historical trade can't carry releases", 422, lambda: t.create_transaction(
    t.TransactionIn(type="trade", date="2026-09-29", historical=True,
                    details=trade({"UTA": ["hayes-jaxson"]}).model_dump()), INFO))

if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nall checks passed")

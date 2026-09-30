"""The rules that turn on at opening night, and the calendar they read.

Until 2026-09-30 nothing knew when the regular season started, so three rules
the rulebook marks 🔒 never switched on:

- **§ 2.1, the in-season 15-man roster.** A trade or signing that took a team
  to 16–20 players only ever warned, all year. In-season it is a hard block;
  out of season the 16–20 warning band must survive, or a June draft trade
  that lands a team at 16 for the summer is refused.
- **§ 6.3, the extension windows** that close the day before opening night.
- **§ 4.5, the 15-trade limit** (effective 2026-27), whose two exemptions —
  draft-day trades, and one trade on deadline day — need dates the system
  didn't have. The count is of league trades, not ledger entries: the office
  enters a correction as a second "Trade 42 revision" entry, and that must
  not charge the teams a second trade.

Plus § 4.5's newly-signed restriction: a free agent can't be traded until the
later of 90 days after signing and December 15. Every free-agent signing
counts (re-signs and offer sheets too, settled 2026-09-30); a sign-and-trade
doesn't.

Opening night is read off the schedule file, never stored a second time.
Everything writes to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_season_start_rules
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import season_clock  # noqa: E402
import routers.auth as auth  # noqa: E402
import routers.constants as constants  # noqa: E402
import routers.misc as misc  # noqa: E402
import routers.season_calendar as cal  # noqa: E402
import routers.transactions as T  # noqa: E402
from routers.transactions import TradeIn, TradeTransfer, TradeAsset  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-season-start-test-"))
constants.DATA_DIR = TMP
constants.LEAGUE_STATE_FILE = TMP / "league-state.json"
misc.LEAGUE_STATE_FILE = TMP / "league-state.json"
season_clock.LEAGUE_STATE_FILE = TMP / "league-state.json"
misc.log_write = lambda info, msg: None

(TMP / "schedule-26-27.json").write_text(json.dumps({"season": "26-27", "games": [
    {"id": "b", "date": "2027-04-11", "away_team": "POR", "home_team": "SAS"},
    {"id": "a", "date": "2026-10-20", "away_team": "BOS", "home_team": "DET"},
]}))

BOD_TOKEN = "b" * 64
auth.load_members = lambda: {"Boss": {"token": BOD_TOKEN, "roles": ["bod"], "tenures": []}}
app = FastAPI()
app.include_router(misc.router)
client = TestClient(app)
BOD = {"Authorization": "Bearer " + BOD_TOKEN}


def set_dates(state):
    constants.LEAGUE_STATE_FILE.write_text(json.dumps(state))


def trade(*slugs, a="AAA", b="BBB"):
    return TradeIn(transfers=[TradeTransfer(from_team=a, to_team=b,
                                            assets=[TradeAsset(type="player", slug=s) for s in slugs])])


def with_ledger(entries, run):
    orig = T._load_transactions
    T._load_transactions = lambda: entries
    T._FA_SIGN_CACHE.update({"key": None, "dates": {}})
    orig_stat = T.TRANSACTIONS_FILE
    T.TRANSACTIONS_FILE = TMP / "no-ledger-here.json"   # no stat -> no cache hit
    try:
        return run()
    finally:
        T._load_transactions = orig
        T.TRANSACTIONS_FILE = orig_stat
        T._FA_SIGN_CACHE.update({"key": None, "dates": {}})


def trade_txn(date, teams, desc="", created="", **extra):
    return {"type": "trade", "date": date, "created_at": created or date, "description": desc,
            "details": {"teams": list(teams), **extra}}


def only(checks, key):
    return next((c for c in checks if c.check == key), None)


def test_calendar():
    print("\n-- the calendar reads opening night off the schedule --")
    check("opening night is the first game", cal.opening_night("26-27") == "2026-10-20")
    check("last regular-season game is the last one",
          cal.regular_season_bounds("26-27") == ("2026-10-20", "2027-04-11"))
    check("the day before opening night", cal.day_before_opening_night("26-27") == "2026-10-19")
    check("Oct 19 is not the regular season", not cal.is_regular_season("2026-10-19"))
    check("opening night is", cal.is_regular_season("2026-10-20"))
    check("the last game is", cal.is_regular_season("2027-04-11"))
    check("the playoffs are not", not cal.is_regular_season("2027-05-01"))
    check("a season with no schedule is never the regular season", not cal.is_regular_season("2027-11-01"))
    check("...and has no opening night", cal.day_before_opening_night("27-28") is None)


def test_roster_size():
    print("\n-- § 2.1: 16 players is a block in-season, a warning out of it --")
    r = T._roster_size_check("AAA", 16, "signing", "2026-11-01")
    check("16 in-season is an error", r is not None and not r.passed and r.level == "error")
    r = T._roster_size_check("AAA", 16, "signing", "2026-08-01")
    check("16 in the offseason is a warning", r is not None and r.level == "warning")
    r = T._roster_size_check("AAA", 16, "signing", "2027-06-20")
    check("16 on draft day (after the season) is still only a warning", r is not None and r.level == "warning")
    check("15 in-season passes", T._roster_size_check("AAA", 15, "signing", "2026-11-01") is None)
    r = T._roster_size_check("AAA", 16, "signing")
    check("no date given keeps the old offseason reading", r is not None and r.level == "warning")


def test_trade_limit():
    print("\n-- § 4.5: 15 trades per team per league year --")
    set_dates({"season_dates": {"26-27": {"trade_deadline": "2027-02-11",
                                          "draft_days": ["2027-06-24", "2027-06-25"]}}})
    fourteen = [trade_txn(f"2026-08-{d:02d}", ["AAA", "CCC"], f"Trade {d}") for d in range(1, 15)]

    checks = with_ledger(fourteen, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2026-11-01"}))
    r = only(checks, "trade_limit_aaa")
    check("the 15th trade is allowed", r is not None and r.passed and "trade 15 of 15" in r.message)
    check("the other team is counted separately", only(checks, "trade_limit_bbb").passed)

    fifteen = fourteen + [trade_txn("2026-08-20", ["AAA", "CCC"], "Trade 20")]
    checks = with_ledger(fifteen, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2026-11-01"}))
    r = only(checks, "trade_limit_aaa")
    check("the 16th is refused", r is not None and not r.passed and r.level == "error")
    check("...and the message lists what was counted", "Trade 20" in r.message)

    checks = with_ledger(fifteen, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2027-02-11"}))
    check("a first deadline-day trade is exempt at the limit", only(checks, "trade_limit_aaa").passed)
    on_deadline = fifteen + [trade_txn("2027-02-11", ["AAA", "DDD"], "Trade 60")]
    checks = with_ledger(on_deadline, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2027-02-11"}))
    check("a second one on deadline day is not", not only(checks, "trade_limit_aaa").passed)

    checks = with_ledger(fifteen, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2027-06-24"}))
    check("a draft-day trade is exempt", only(checks, "trade_limit").passed)
    draft_heavy = fourteen + [trade_txn("2027-06-24", ["AAA", "EEE"], "Trade 90")]
    checks = with_ledger(draft_heavy, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2027-06-26"}))
    check("...and draft-day trades don't count later", only(checks, "trade_limit_aaa").passed)

    fixes = fourteen[:13] + [trade_txn("2026-09-01", ["AAA", "CCC"], "Trade 42: the trade"),
                        trade_txn("2026-09-01", ["AAA", "CCC"], "Trade 42 revision: fixes a pick")]
    checks = with_ledger(fixes, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2026-11-01"}))
    r = only(checks, "trade_limit_aaa")
    check("a 'Trade 42 revision' entry is the same trade, not a second one",
          r.passed and "trade 15 of 15" in r.message)

    june = [trade_txn("2026-06-26", ["AAA", "CCC"]) for _ in range(20)]
    checks = with_ledger(june, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2026-11-01"}))
    check("trades before July 1 count toward the league year before",
          only(checks, "trade_limit_aaa").passed)

    checks = with_ledger(fifteen, lambda: T._check_trade_limit(trade("p"), {"txn_date": "2026-05-01"}))
    check("no limit before 26-27", checks == [])


def test_fa_signing_freeze():
    print("\n-- § 4.5: newly signed free agents --")
    ctx = {"txn_date": "2026-11-01", "bios": {"p": {"name": "PLAYER, SOME"}}}
    july = [{"type": "sign", "date": "2026-07-06", "details": {"player": "p", "team": "AAA",
                                                                "signing_method": "mle"}}]
    r = only(with_ledger(july, lambda: T._check_fa_signing_trade_restriction(trade("p"), ctx)),
             "fa_signing_trade_freeze_p")
    check("a July signing can't be traded in November", r is not None and not r.passed)
    check("...and it names Dec 15 as the date", "2026-12-15" in r.message and "Some Player" in r.message)
    r = only(with_ledger(july, lambda: T._check_fa_signing_trade_restriction(
        trade("p"), {**ctx, "txn_date": "2026-12-15"})), "fa_signing_trade_freeze_p")
    check("...and can on Dec 15 itself", r.passed)

    late = [{"type": "sign", "date": "2026-11-01", "details": {"player": "p", "team": "AAA",
                                                                "signing_method": "minimum"}}]
    r = only(with_ledger(late, lambda: T._check_fa_signing_trade_restriction(
        trade("p"), {**ctx, "txn_date": "2026-12-20"})), "fa_signing_trade_freeze_p")
    check("a Nov 1 signing waits for day 90, past Dec 15", not r.passed and "2027-01-30" in r.message)

    rebird = [{"type": "sign", "date": "2026-07-06", "details": {"player": "p", "team": "AAA",
                                                                  "signing_method": "bird_rights"}}]
    r = only(with_ledger(rebird, lambda: T._check_fa_signing_trade_restriction(trade("p"), ctx)),
             "fa_signing_trade_freeze_p")
    check("re-signing your own free agent starts the clock", r is not None and not r.passed)

    snt = [{"type": "sign", "date": "2026-07-06", "details": {"player": "p", "team": "AAA",
                                                               "signing_method": "sign_and_trade"}}]
    checks = with_ledger(snt, lambda: T._check_fa_signing_trade_restriction(trade("p"), ctx))
    check("a sign-and-trade doesn't", checks == [])

    sheet = [{"type": "offer_sheet", "id": "o1", "date": "2026-07-10",
              "details": {"player": "p", "team": "BBB"}},
             {"type": "offer_sheet_decision", "date": "2026-07-12",
              "details": {"offer_id": "o1", "outcome": "matched"}}]
    r = only(with_ledger(sheet, lambda: T._check_fa_signing_trade_restriction(trade("p"), ctx)),
             "fa_signing_trade_freeze_p")
    check("a matched offer sheet starts the clock", r is not None and not r.passed)

    checks = with_ledger([], lambda: T._check_fa_signing_trade_restriction(trade("p"), ctx))
    check("a player with no signing on the ledger isn't checked", checks == [])


def test_dates_endpoint():
    print("\n-- PUT /api/league-year/{season}/dates --")
    set_dates({"rollovers": {}})
    r = client.put("/api/league-year/26-27/dates", headers=BOD,
                   json={"trade_deadline": "2027-02-11", "draft_days": ["2027-06-25", "2027-06-24"]})
    check("sets the deadline and draft days", r.status_code == 200 and r.json()["trade_deadline"] == "2027-02-11")
    check("draft days come back sorted", r.json()["draft_days"] == ["2027-06-24", "2027-06-25"])
    check("opening night rides along from the schedule", r.json()["opening_night"] == "2026-10-20")
    check("the rollovers are untouched", json.loads(constants.LEAGUE_STATE_FILE.read_text())["rollovers"] == {})
    r = client.put("/api/league-year/27-28/dates", headers=BOD, json={"draft_days": ["2027-06-24"]})
    check("a June draft can't be filed under the next season", r.status_code == 422)
    r = client.put("/api/league-year/26-27/dates", headers=BOD, json={"draft_days": []})
    check("a left-out field is left alone",
          r.status_code == 200 and r.json()["trade_deadline"] == "2027-02-11" and r.json()["draft_days"] == [])
    r = client.put("/api/league-year/26-27/dates", json={"trade_deadline": "2027-02-11"})
    check("needs a bod token", r.status_code in (401, 403))
    r = client.get("/api/league-year")
    check("GET /api/league-year shows the calendar", "calendar" in r.json())


if __name__ == "__main__":
    test_calendar()
    test_roster_size()
    test_trade_limit()
    test_fa_signing_freeze()
    test_dates_endpoint()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nall season-start checks passed")

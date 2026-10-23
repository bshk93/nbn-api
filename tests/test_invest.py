"""Regression tests for routers/invest.py — NBN Wall Street.

What must keep being true:

  * every raw box score file on disk counts, so a new season moves prices
    from its first game;
  * a price is the full-precision product of `1 + diff_diff / MOVE_DIVISOR`,
    and nothing a member does moves it;
  * a playoff game moves by its margin alone, doubled, so it is zero-sum;
  * a team can't be traded while a played game of its has no box score, and
    an index can't while any of its teams can't; today's games only count as
    played once Pick'em says the day has started;
  * buying while short (and shorting while long) is refused before any NB¥
    moves — the old buy took the money and then refused;
  * the per-ticker and total limits are counted at cost and refuse before any
    NB¥ moves;
  * the fee is charged on top of a buy and out of a sale, and stays out of
    positions;
  * P&L and the portfolio ignore trades from before the NB¥ reset.

    venv/bin/python -m tests.test_invest
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.invest as inv  # noqa: E402
import routers.league_time as league_time  # noqa: E402
import routers.schedule as schedule  # noqa: E402
import routers.pickem as pickem  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# Never the live data dir, the real ledger, or the real channel.
D = Path(tempfile.mkdtemp())
inv.DATA_DIR = D
inv.INVEST_HOLDINGS_FILE = D / "invest-holdings.json"
inv.INVEST_TRADES_FILE = D / "invest-trades.json"
inv.INVEST_MARKET_FILE = D / "invest-market.json"
inv._discord_trade = lambda *a, **k: None

BAL: dict[str, float] = {}
LEDGER: list[tuple] = []


def _post(member, delta, reason):
    if BAL.get(member, 0.0) + delta < 0:
        raise HTTPException(status_code=402, detail="Not enough NB¥")
    BAL[member] = round(BAL.get(member, 0.0) + delta, 2)
    LEDGER.append((member, delta, reason))
    return BAL[member]


inv.wallet = SimpleNamespace(
    enabled=lambda kind: True, require_enabled=lambda kind: None,
    balances=lambda: dict(BAL), balance=lambda m: BAL.get(m, 0.0),
    debit=lambda m, amt, kind, reason, ref=None: _post(m, -amt, reason),
    credit=lambda m, amt, kind, reason, ref=None: _post(m, amt, reason),
)

TODAY = "2026-11-05"
league_time.league_today_str = lambda: TODAY
SCHEDULE: list[dict] = []
schedule._load = lambda season: {"games": SCHEDULE}
DAY_STARTED = {"v": False}
pickem.lock_state = lambda date, rows: {"why": "stream"} if DAY_STARTED["v"] else None

HEADER = ["DATE", "TEAM", "OPP", "OPP_RAW", "TEAM_PTS", "OPP_TEAM_PTS", "gametype"]


def write_games(name: str, games: list[tuple]):
    """games: (date, home, away, home_pts, away_pts) — one row per team."""
    with open(D / name, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for date, a, b, pa, pb in games:
            w.writerow([date, a, b, b, pa, pb, "REG"])
            w.writerow([date, b, f"@{a}", a, pb, pa, "REG"])
    inv._price_cache["prices"] = None


def reset_market():
    for f in (inv.INVEST_HOLDINGS_FILE, inv.INVEST_TRADES_FILE, inv.INVEST_MARKET_FILE):
        f.unlink(missing_ok=True)
    BAL.clear()
    LEDGER.clear()


def body(**kw):
    return SimpleNamespace(**kw)


ME = {"name": "alice"}

# Last season, plus a new season the old hardcoded list didn't know about.
write_games("allstats-25-26.csv", [
    ("2026-01-01", "BOS", "NYK", 110, 100),
    ("2026-01-03", "NYK", "BOS", 105, 100),
])
print("\nSeasons and prices")
write_games("allstats-26-27.csv", [
    ("2026-11-01", "BOS", "NYK", 120, 100),
    ("2026-11-03", "NYK", "BOS", 90, 100),
])
prices, current = inv._get_prices()
check("a 26-27 game moves the price", any(h["date"] == "2026-11-01" for h in prices["BOS"]))

# BOS: game 1 of each season has no opponent average yet, so no move.
# 25-26 g2: BOS lost by 5, NYK averaged -10 before it → dd = -5 + -10 = -15.
# 26-27 g2: BOS won by 10, NYK averaged -20 before it → dd = 10 + -20 = -10.
want = 100 * (1 - 15 / inv.MOVE_DIVISOR) * (1 - 10 / inv.MOVE_DIVISOR)
check("price is the unrounded product of the game factors", abs(current["BOS"] - want) < 1e-9)
# BOS and NYK are both Atlantic and every move is one beating the other, so the
# index's daily average of their factors is exactly 1.
check("an index is the daily average of its teams' moves", abs(current["NATL"] - 100) < 1e-9)

print("\nPlayoffs")
# Two good teams (both +20 a game) meet in the playoffs. The old formula
# credited both sides for the strong opponent; now a game is zero-sum.
write_games("allstats-playoffs-27.csv", [("2027-04-20", "BOS", "NYK", 105, 100)])
(D / "allstats-playoffs-27.csv").write_text(
    (D / "allstats-playoffs-27.csv").read_text().replace(",REG", ",PLAYOFF"))
write_games("allstats-26-27.csv", [
    ("2026-11-01", "BOS", "ATL", 120, 100), ("2026-11-01", "NYK", "CHI", 120, 100),
    ("2026-11-03", "BOS", "CHI", 120, 100), ("2026-11-03", "NYK", "ATL", 120, 100),
])
prices, _ = inv._get_prices()
bos = next(h for h in prices["BOS"] if h["date"] == "2027-04-20")
nyk = next(h for h in prices["NYK"] if h["date"] == "2027-04-20")
check("a playoff game moves by the margin, doubled", bos["diff_diff"] == 10 and nyk["diff_diff"] == -10)
check("a playoff game between two teams in one index leaves it flat",
      abs(next(h for h in prices["NATL"] if h["date"] == "2027-04-20")["price"]
          - next(h for h in prices["NATL"] if h["date"] == "2026-11-03")["price"]) < 1e-3)
(D / "allstats-playoffs-27.csv").unlink()
write_games("allstats-26-27.csv", [
    ("2026-11-01", "BOS", "NYK", 120, 100),
    ("2026-11-03", "NYK", "BOS", 90, 100),
])

print("\nNo member moves a price")
reset_market()
BAL["alice"] = 5000
before = inv._get_prices()[1]["BOS"]
inv.buy_shares(body(team="BOS", nbyen=2000), ME)
check("buying leaves the price where it was", inv._get_prices()[1]["BOS"] == before)

print("\nFee")
check("a 2,000 buy costs 2,020", BAL["alice"] == 5000 - 2020)
h = inv._load_holdings()["alice"]["BOS"]["long"]
check("the position is 2,000 at cost, without the fee",
      abs(h["shares"] * h["avg_buy_price"] - 2000) < 0.01)
shares = h["shares"]
inv.sell_shares(body(team="BOS", shares=shares), ME)
check("selling at the same price returns 2,000 less 1%", BAL["alice"] == 5000 - 2020 + 1980)
check("the trade log records the fee", inv._load_trades()[-1]["fee"] == 20)

print("\nRefusals keep the money")
reset_market()
BAL["alice"] = 20000
inv.short_shares(body(team="NYK", nbyen=500), ME)
bal = BAL["alice"]
try:
    inv.buy_shares(body(team="NYK", nbyen=500), ME)
    check("buying while short is refused", False)
except HTTPException as e:
    check("buying while short is refused", e.status_code == 400)
check("... and takes no NB¥", BAL["alice"] == bal)

inv.buy_shares(body(team="BOS", nbyen=2000), ME)
try:
    inv.short_shares(body(team="BOS", nbyen=100), ME)
    check("shorting while long is refused", False)
except HTTPException as e:
    check("shorting while long is refused", e.status_code == 400)

print("\nLimits")
bal = BAL["alice"]
try:
    inv.buy_shares(body(team="BOS", nbyen=600), ME)
    check("over 2,500 in one ticker is refused", False)
except HTTPException as e:
    check("over 2,500 in one ticker is refused", e.status_code == 400 and "2,500" in e.detail)
check("... and takes no NB¥", BAL["alice"] == bal)
inv.buy_shares(body(team="BOS", nbyen=500), ME)
check("exactly 2,500 is allowed", abs(inv._deployed(inv._load_holdings()["alice"])["BOS"] - 2500) < 0.01)
check("shorts count toward the limits", abs(inv._deployed(inv._load_holdings()["alice"])["NYK"] - 500) < 0.01)
write_games("allstats-26-27.csv", [
    ("2026-11-01", "BOS", "NYK", 120, 100), ("2026-11-03", "NYK", "BOS", 90, 100),
    ("2026-11-01", "ATL", "CHI", 100, 90), ("2026-11-01", "MIA", "DAL", 100, 90),
])
inv.buy_shares(body(team="ATL", nbyen=2500), ME)
inv.buy_shares(body(team="CHI", nbyen=2500), ME)
# 2,500 + 500 + 2,500 + 2,500 = 8,000 in; 2,000 left of the 10,000.
try:
    inv.buy_shares(body(team="MIA", nbyen=2100), ME)
    check("over 10,000 in total is refused", False)
except HTTPException as e:
    check("over 10,000 in total is refused", e.status_code == 400 and "10,000" in e.detail)
inv.buy_shares(body(team="MIA", nbyen=2000), ME)
check("up to 10,000 in total is allowed", abs(sum(inv._deployed(inv._load_holdings()["alice"]).values()) - 10000) < 0.01)

print("\nPending games")
reset_market()
BAL["alice"] = 5000
SCHEDULE[:] = [
    {"date": "2026-11-03", "away_team": "BOS", "home_team": "NYK"},   # box score in
    {"date": "2026-11-04", "away_team": "LAL", "home_team": "GSW"},   # played, no box score
    {"date": TODAY,        "away_team": "PHX", "home_team": "SAC"},   # today
    {"date": "2026-11-09", "away_team": "BOS", "home_team": "MIA"},   # future
]
write_games("allstats-26-27.csv", [
    ("2026-11-01", "BOS", "NYK", 120, 100), ("2026-11-03", "NYK", "BOS", 90, 100),
    ("2026-11-01", "LAL", "GSW", 100, 90), ("2026-11-01", "PHX", "SAC", 100, 90),
])
DAY_STARTED["v"] = False
m = {t["team"]: t for t in inv.get_market()["teams"]}
idx = {i["ticker"]: i for i in inv.get_market()["indexes"]}
check("a played game with no box score closes both teams",
      m["LAL"]["pending"] and m["GSW"]["pending"]["date"] == "2026-11-04")
check("a team whose box score is in stays open", m["BOS"]["pending"] is None)
check("an index closes when one of its teams does", idx["PAC"]["pending"] is not None)
check("today's game doesn't close a team before the day starts", m["PHX"]["pending"] is None)
try:
    inv.buy_shares(body(team="LAL", nbyen=100), ME)
    check("trading a closed team is refused", False)
except HTTPException as e:
    check("trading a closed team is refused", e.status_code == 423 and "LAL @ GSW" in e.detail)
check("... and takes no NB¥", BAL["alice"] == 5000)
DAY_STARTED["v"] = True
m = {t["team"]: t for t in inv.get_market()["teams"]}
check("once the day starts, today's teams close", m["PHX"]["pending"]["date"] == TODAY)
write_games("allstats-26-27.csv", [
    ("2026-11-01", "BOS", "NYK", 120, 100), ("2026-11-03", "NYK", "BOS", 90, 100),
    ("2026-11-01", "LAL", "GSW", 100, 90), ("2026-11-01", "PHX", "SAC", 100, 90),
    ("2026-11-04", "GSW", "LAL", 100, 95),
])
m = {t["team"]: t for t in inv.get_market()["teams"]}
check("the box score coming in reopens them", m["LAL"]["pending"] is None and m["GSW"]["pending"] is None)
SCHEDULE.clear()

print("\nShort payoff")
reset_market()
BAL["alice"] = 1000
inv.short_shares(body(team="BOS", nbyen=500), ME)
pos = inv._load_holdings()["alice"]["BOS"]["short"]
check("a short can't be worth less than zero", inv._short_equity(pos["shares"], pos["avg_open_price"], pos["avg_open_price"] * 3) == 0)

print("\nOld trades")
reset_market()
inv._save_trades([{"ts": "2026-09-01T00:00:00+00:00", "member": "alice", "team": "BOS",
                   "action": "buy", "shares": 10, "price": 50, "nbyen": 500}])
check("a pre-reset buy shows no portfolio", inv.get_portfolio(ME)["summary"] is None)
check("... and no P&L", inv.compute_member_pnl("alice")["positions"] == [])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all passed")

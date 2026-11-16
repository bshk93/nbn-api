"""§ 4.5's re-acquisition restriction, on the signing side.

A team may not re-acquire a player it traded away until the following free
agency period. `_check_reacquisition` reads that as "not in the league year of
the trade". Found 2026-11-16: HOU traded Deandre Ayton to NOP on 2026-07-02,
NOP released him on 2026-11-15, and `POST /api/validate/sign` raised nothing
about HOU signing him back (only the hard cap stopped it).

    venv/bin/python -m tests.test_reacquisition
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import season_clock  # noqa: E402
from routers import transactions as T  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def trade(date, legs, historical=False):
    return {"type": "trade", "date": date, "details": {
        "historical": historical,
        "transfers": [{"from_team": f, "to_team": t,
                       "assets": [{"type": "player", "slug": s} for s in slugs]}
                      for f, t, slugs in legs]}}


LEDGER = [
    # 25-26: HOU acquires Ayton (a backfilled trade still counts)
    trade("2026-02-03", [("PHX", "HOU", ["ayton"]), ("HOU", "PHX", ["brooks"])], historical=True),
    # 26-27: HOU sends him on
    trade("2026-07-02", [("HOU", "NOP", ["ayton"]), ("NOP", "HOU", ["maxey"])]),
    {"type": "release", "date": "2026-11-15", "details": {"player": "ayton", "team": "NOP"}},
]
BIOS = {"ayton": {"name": "AYTON, DEANDRE"}, "brooks": {"name": "BROOKS, DILLON"},
        "maxey": {"name": "MAXEY, TYRESE"}}


def main():
    print("re-acquisition restriction (§ 4.5)")
    orig = (T._load_transactions, T.TRANSACTIONS_FILE, T._build_team_map, T._league_rollovers,
            T._season_for_date)
    T._load_transactions = lambda: LEDGER
    T.TRANSACTIONS_FILE = Path("/nonexistent/transactions.json")  # no cache key: rebuild each call
    T._league_rollovers = lambda: {}
    T._season_for_date = lambda d: season_clock.season_for_date(d, {})
    roster = {"maxey": "HOU"}
    T._build_team_map = lambda: roster
    try:
        r = T._check_reacquisition("ayton", "HOU", "26-27", BIOS)
        check("the team that traded him away this league year is blocked",
              r is not None and r.level == "error" and r.check == "reacquisition_restriction")
        check("...naming the trade date and when it lifts",
              r is not None and "2026-07-02" in r.message and "2027-07-01" in r.message)
        check("lowercase team abbr is blocked the same way",
              T._check_reacquisition("ayton", "hou", "26-27", BIOS) is not None)

        check("next league year he is fair game again",
              T._check_reacquisition("ayton", "HOU", "27-28", BIOS) is None)
        check("PHX traded him away in 25-26, so it can sign him in 26-27",
              T._check_reacquisition("ayton", "PHX", "26-27", BIOS) is None)
        check("...but would have been blocked in 25-26",
              T._check_reacquisition("ayton", "PHX", "25-26", BIOS) is not None)
        check("NOP, which received him and released him, isn't covered by § 4.5",
              T._check_reacquisition("ayton", "NOP", "26-27", BIOS) is None)
        check("an uninvolved team can sign him",
              T._check_reacquisition("ayton", "BOS", "26-27", BIOS) is None)
        check("a player with no trade history is unaffected",
              T._check_reacquisition("nobody", "HOU", "26-27", BIOS) is None)

        roster["ayton"] = "HOU"
        check("a team that already rosters him is re-signing its own player",
              T._check_reacquisition("ayton", "HOU", "26-27", BIOS) is None)
    finally:
        (T._load_transactions, T.TRANSACTIONS_FILE, T._build_team_map, T._league_rollovers,
         T._season_for_date) = orig
        T._TRADED_AWAY_CACHE.update({"key": None, "index": {}})

    print("FAIL" if FAILS else "all ok")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

"""`apply_trade`'s `force_warnings_only`: warnings pass, errors still block.

TRC's finalize calls a trade legal when no *error* fails, then hands it to
`apply_trade`. That used to block on any failed check, warnings included, so a
trade the dashboard showed as ready 422'd on finalize — a UTA roster going to
16 in the offseason was enough (2026-10-05). test_trade_requests stubs
apply_trade out, which is why it never saw this; this runs the real one with
only its I/O stubbed.

    venv/bin/python -m tests.test_apply_trade_warnings
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

from routers import transactions as tx  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


CHECKS = {"value": []}
APPENDED = []

tx.load_player_bios = lambda: {}
tx.load_team_state = lambda: {}
tx.load_trade_exceptions = lambda: {}
tx.CAP_LEVELS_FILE = Path("/nonexistent/cap-levels.json")
tx._run_validation = lambda kind, details, ctx: CHECKS["value"]
tx._apply_trade = lambda details, date, info, txn_id: {}
tx._append_transaction = lambda txn: APPENDED.append(txn)
tx.notify_transaction = lambda *a, **k: None

TRADE = tx.TradeIn(transfers=[])
INFO = {"name": "head"}
WARN = tx.CheckResult(check="roster_size_uta", passed=False, level="warning", message="16 players")
ERR = tx.CheckResult(check="salary_matching", passed=False, level="error", message="no")


def status_of(fn):
    try:
        fn()
        return 200
    except HTTPException as exc:
        return exc.status_code


print("a warning alone")
CHECKS["value"] = [WARN]
check("blocks a plain apply, as the office's submit path expects",
      status_of(lambda: tx.apply_trade(TRADE, "2026-10-05", INFO)) == 422)
check("passes with force_warnings_only",
      status_of(lambda: tx.apply_trade(TRADE, "2026-10-05", INFO, force_warnings_only=True)) == 200)
check("and the ledger entry records the warning it went past",
      APPENDED[-1]["details"].get("_forced_checks") == ["roster_size_uta"])

print("an error")
CHECKS["value"] = [WARN, ERR]
n = len(APPENDED)
check("still blocks with force_warnings_only",
      status_of(lambda: tx.apply_trade(TRADE, "2026-10-05", INFO, force_warnings_only=True)) == 422)
check("and nothing was written", len(APPENDED) == n)

print("nothing failed")
CHECKS["value"] = []
tx.apply_trade(TRADE, "2026-10-05", INFO)
check("no _forced_checks stamp", "_forced_checks" not in APPENDED[-1]["details"])

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

"""NB¥ — the one place a balance changes.

Every credit and debit in the API goes through `post()`. Before the 2026-09
reset, nine routers each loaded `member-balances.json`, changed it, saved it,
and then appended to the ledger as a separate step. A failure between the two
left a balance change with no ledger line, and every reason was free text, so
reading the economy back needed a parser. This module replaces all of that.

**The ledger is the record; the balances file is a cache of it.**
`nbyen-ledger.jsonl` is append-only, one JSON line per movement, each carrying
a `kind` from `KINDS`. `member-balances.json` is rewritten after every append
so the many read paths (pages, the Discord bot) keep working unchanged, and
`rebuild_balances()` regenerates it from the ledger if the two ever disagree.

**The on/off switch is `KINDS`.** At the reset the economy was cut back to
what traces to a real dollar, plus betting: starting grants, donations, Twitch
subs, wagers and payouts, and buying a stream. Everything else is listed but
paused, so a paused feature can keep running (Perry still ranks, Poeltl still
counts streaks) without moving money. Turning one back on is flipping its entry
here after its price is decided — `docs/nbyen-economy.md` has the order.

**Nobody gets NB¥ silently.** There is no default balance for an unknown
member. A starting grant is a real `start` line, written by the reset script or
when a member is added.

The old `bets-ledger.json` is left untouched as the record of the pre-reset
economy; nothing writes to it any more.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import datetime, timezone

from fastapi import HTTPException

from .constants import DATA_DIR
from .storage import _load_json, _save_json

LEDGER_FILE   = DATA_DIR / "nbyen-ledger.jsonl"
BALANCES_FILE = DATA_DIR / "member-balances.json"

# The peg. One stream game is $10, and a stream game is the starting grant.
NBY_PER_DOLLAR = 100
STREAM_PRICE   = 1_000
START_GRANT    = 1_000

# Twitch subs, per month. Prime reads as tier 1 in the API (tested 2026-09-08),
# so it pays the tier 1 rate. The rates are the proposal's (docs/nbyen-economy.md)
# at the 100-per-dollar scale.
SUB_RATES = {1: 300, 2: 700, 3: 1_700}

# kind -> enabled. A kind not in this table is a programming error.
KINDS: dict[str, bool] = {
    # Money in, traced to a real dollar or a stated grant
    "start":           True,
    "donation":        True,
    "twitch_sub":      True,
    # Betting
    "wager":           True,
    "payout":          True,
    "bet_refund":      True,
    # Futures markets (routers/markets.py)
    "market_buy":      True,
    "market_sell":     True,
    "market_payout":   True,
    "market_refund":   True,
    # Buying a stream
    "stream_purchase": True,
    "stream_refund":   True,
    # The commissioner's correction, always with a reason
    "admin":           True,
    # Paused at the reset — see the module docstring
    "tip":             False,
    "achievement":     False,
    "boxscore":        False,
    "bio":             False,
    "poeltl":          False,
    "perry":           False,
    "trivia":          False,
    "invest":          False,
    "theme":           False,
    "cosmetic":        False,
    "avatar":          False,
}

# Human names for the 423 a paused feature returns.
_KIND_LABEL = {
    "tip": "Tipping", "achievement": "Achievement NB¥", "boxscore": "Box score NB¥",
    "bio": "Bio NB¥", "poeltl": "Poeltl NB¥", "perry": "Perry NB¥", "trivia": "Trivia NB¥",
    "invest": "The market", "theme": "Buying themes", "cosmetic": "Buying cosmetics",
    "avatar": "Buying an avatar",
}


class _WalletLock:
    """Re-entrant within a thread, so a router can hold it across its own
    check-then-post; and exclusive across processes via flock, because the
    monthly Twitch job and the reset script write the ledger from outside the
    API process."""

    def __init__(self):
        self._thread = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._thread.acquire()
        if self._depth == 0:
            self._fd = open(LEDGER_FILE.with_suffix(".lock"), "a")
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
        self._thread.release()
        return False


lock = _WalletLock()


def enabled(kind: str) -> bool:
    if kind not in KINDS:
        raise ValueError(f"unknown NB¥ kind {kind!r}")
    return KINDS[kind]


def require_enabled(kind: str) -> None:
    """423 if `kind` is paused. Call before doing any work a paused feature
    would otherwise do and then fail to pay for."""
    if not enabled(kind):
        label = _KIND_LABEL.get(kind, kind)
        raise HTTPException(status_code=423, detail=f"{label} is paused while NB¥ restarts.")


def balances() -> dict[str, float]:
    return _load_json(BALANCES_FILE, {})


def balance(member: str) -> float:
    return float(balances().get(member, 0.0))


def post(lines: list[dict], *, allow_negative: bool = False) -> dict[str, float]:
    """Apply several movements atomically. Each line is
    `{member, delta, kind, reason, ref?}`. Either every line lands or none does.

    Refuses (423) if any kind is paused, and (402) if any member would end below
    zero — unless `allow_negative`, which exists for reversing a donation that was
    entered wrong after the member already spent it.

    Returns the new balance of each member touched.
    """
    for ln in lines:
        require_enabled(ln["kind"])
        if not isinstance(ln["delta"], (int, float)) or ln["delta"] == 0:
            raise ValueError(f"bad delta in {ln!r}")
    ts = datetime.now(timezone.utc).isoformat()
    with lock:
        bal = balances()
        out = []
        for ln in lines:
            m = ln["member"]
            new = round(bal.get(m, 0.0) + ln["delta"], 2)
            if new < 0 and ln["delta"] < 0 and not allow_negative:
                raise HTTPException(
                    status_code=402,
                    detail=f"Not enough NB¥ — {m} has {bal.get(m, 0.0):,.0f}, "
                           f"this needs {-ln['delta']:,.0f}")
            bal[m] = new
            row = {"ts": ts, "member": m, "delta": round(ln["delta"], 2), "balance": new,
                   "kind": ln["kind"], "reason": ln["reason"]}
            if ln.get("ref"):
                row["ref"] = ln["ref"]
            out.append(row)
        _append(out)
        _save_json(BALANCES_FILE, bal)
    return {r["member"]: r["balance"] for r in out}


def credit(member: str, amount: float, kind: str, reason: str, ref: str | None = None) -> float:
    return post([{"member": member, "delta": amount, "kind": kind, "reason": reason, "ref": ref}])[member]


def debit(member: str, amount: float, kind: str, reason: str, ref: str | None = None) -> float:
    return post([{"member": member, "delta": -amount, "kind": kind, "reason": reason, "ref": ref}])[member]


def try_credit(member: str, amount: float, kind: str, reason: str,
               ref: str | None = None) -> tuple[float, float]:
    """For a reward attached to some other action (a box score, a puzzle): pay it
    if the kind is on, pay nothing if paused, and never fail the action itself.
    Returns (amount paid, balance)."""
    if not enabled(kind):
        return 0.0, balance(member)
    return amount, credit(member, amount, kind, reason, ref)


def _append(rows: list[dict]) -> None:
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    with open(LEDGER_FILE, "a") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def read_ledger() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    with open(LEDGER_FILE) as f:
        return [json.loads(line) for line in f if line.strip()]


def has_ref(ref: str) -> bool:
    """Whether any ledger line carries `ref` — how a job that might run twice
    (the monthly Twitch check) avoids paying twice."""
    return any(r.get("ref") == ref for r in read_ledger())


def rebuild_balances() -> dict[str, float]:
    """Recompute every balance from the ledger and rewrite the cache."""
    with lock:
        bal: dict[str, float] = {}
        for r in read_ledger():
            bal[r["member"]] = round(bal.get(r["member"], 0.0) + r["delta"], 2)
        _save_json(BALANCES_FILE, bal)
    return bal

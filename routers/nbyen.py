"""Public read of NB¥ — every balance and every movement, for the /nbyen page.

Read-only. All writes are routers/wallet.py's. The history is public because
nearly everything in it already is: bets are public on /bet, donations on
/donations, balances on /api/bets/balances. What this adds is Twitch sub tiers.
"""
from fastapi import APIRouter, HTTPException, Query

from .auth import load_members
from . import markets, wallet

router = APIRouter()


@router.get("/api/nbyen/summary")
def nbyen_summary():
    """Every member's balance, the total in circulation, and the total moved by
    each kind since the reset. `paused` lists the kinds switched off.

    `futures` is what a member holds in unsettled markets at current prices.
    That NB¥ has left the wallet but isn't spent, so `net_worth` adds it back.
    Pricing is markets.py's; nothing here does its own."""
    members = load_members()
    bal = wallet.balances()
    held = markets.open_holdings()
    by_kind: dict[str, float] = {}
    for r in wallet.read_ledger():
        by_kind[r["kind"]] = round(by_kind.get(r["kind"], 0.0) + r["delta"], 2)
    rows = []
    for n in sorted(set(members) | set(bal) | set(held)):
        b, f = round(bal.get(n, 0.0), 2), held.get(n, 0.0)
        rows.append({"name": n, "balance": b, "futures": f,
                     "net_worth": round(b + f, 2), "member": n in members})
    rows.sort(key=lambda r: (-r["net_worth"], r["name"].lower()))
    return {
        "balances": rows,
        "total": round(sum(bal.values()), 2),
        "futures_total": round(sum(held.values()), 2),
        "by_kind": by_kind,
        "paused": sorted(k for k, on in wallet.KINDS.items() if not on),
    }


@router.get("/api/nbyen/ledger")
def nbyen_ledger(member: str | None = None, kind: str | None = None,
                 limit: int = Query(200, ge=1, le=5000)):
    """Ledger lines, newest first. Filter by member and/or kind."""
    if kind is not None and kind not in wallet.KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown kind {kind!r}")
    rows = wallet.read_ledger()
    if member:
        rows = [r for r in rows if r["member"] == member]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    total = len(rows)
    return {"total": total, "entries": list(reversed(rows))[:limit]}

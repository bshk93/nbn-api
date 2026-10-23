"""Public read of NB¥ — every balance and every movement, for the /nbyen page.

Read-only. All writes are routers/wallet.py's. The history is public because
nearly everything in it already is: bets are public on /bet, donations on
/donations, balances on /api/bets/balances. What this adds is Twitch sub tiers.
"""
from fastapi import APIRouter, HTTPException, Query

from .auth import load_members
from . import bets, invest, markets, wallet

router = APIRouter()

# The ledger kinds that are each game's money in and out. A game's P&L is what
# its kinds moved plus what is still held in it at today's prices — so fees,
# refunds and payouts are all in, and nothing has to replay a trade log.
GAMES: dict[str, set[str]] = {
    "stocks":  {"invest"},
    "futures": {"market_buy", "market_sell", "market_payout", "market_refund"},
    "bets":    {"wager", "payout", "bet_refund"},
}


def net_worth_rows() -> list[dict]:
    """Every member's money, the one definition every page and command uses:
    wallet + stocks + futures + open bets, where each holding is what its own
    module says it is worth now (stocks and futures at current prices, open
    bets at their stake). `pnl[game]` is None for a game they never played."""
    members = load_members()
    bal = wallet.balances()
    held = {"stocks": invest.open_holdings(), "futures": markets.open_holdings(),
            "bets": bets.open_stakes()}
    flow: dict[str, dict[str, float]] = {g: {} for g in GAMES}
    for r in wallet.read_ledger():
        for g, kinds in GAMES.items():
            if r["kind"] in kinds:
                flow[g][r["member"]] = flow[g].get(r["member"], 0.0) + r["delta"]
    names = set(members) | set(bal)
    for d in (*held.values(), *flow.values()):
        names |= set(d)
    rows = []
    for n in names:
        h = {g: round(held[g].get(n, 0.0), 2) for g in GAMES}
        b = round(bal.get(n, 0.0), 2)
        rows.append({
            "name": n, "balance": b, **h,
            "net_worth": round(b + sum(h.values()), 2),
            "pnl": {g: (round(flow[g].get(n, 0.0) + h[g], 2)
                        if n in flow[g] or h[g] else None) for g in GAMES},
            "member": n in members,
        })
    rows.sort(key=lambda r: (-r["net_worth"], r["name"].lower()))
    return rows


@router.get("/api/nbyen/summary")
def nbyen_summary():
    """Every member's money (`net_worth_rows`), the total in circulation, and
    the total moved by each kind since the reset. `paused` lists the kinds
    switched off."""
    rows = net_worth_rows()
    bal = wallet.balances()
    by_kind: dict[str, float] = {}
    for r in wallet.read_ledger():
        by_kind[r["kind"]] = round(by_kind.get(r["kind"], 0.0) + r["delta"], 2)
    return {
        "balances": rows,
        "total": round(sum(bal.values()), 2),
        **{f"{g}_total": round(sum(r[g] for r in rows), 2) for g in GAMES},
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

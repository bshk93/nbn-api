"""Futures markets — contracts on a league outcome that members buy and sell.

A market has outcomes (usually the 30 teams). A share of an outcome pays
`PAYOUT` (100 NB¥) if that outcome happens and nothing if it doesn't, so its
price reads as a probability: 23 NB¥ a share is the market saying 23%.

**Nobody sets the price.** It comes from a logarithmic market scoring rule
(LMSR): the house quotes every outcome, and each buy pushes that outcome's
price up and every other one down. Prices always sum to 100. A bookie only
opens a market, closes it, and names the winner. That's the point of it: a
trade in the league moves the price as soon as a member acts on it, with no
committee keeping lines current.

**What it does to the money supply is bounded and known up front.** Buying and
selling back is path-independent — only the final state matters. At settlement
the house's result is

    NB¥ created = b × ln(winner's closing price ÷ winner's opening price)

so it creates NB¥ when the crowd was right and destroys it when the crowd was
wrong, and the most it can ever create is `b × ln(1 ÷ lowest opening price)`.
A floor on opening prices (`MIN_OPEN_PRICE`) keeps that finite when a bookie
seeds uneven odds. `docs/nbyen-economy.md` § "Futures markets" has the numbers.

Two things keep fast money on stale news from being free: a trading fee on
every buy and sell, which is burned rather than paid to anyone, and a cap on
what one member can have in one market (`max_stake`, net of what they've sold).

There's no shorting. Selling means selling shares you hold.

Balances move only through `wallet.post`, under the `market_*` kinds.
"""
from __future__ import annotations

import math
import secrets
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import DATA_DIR, VALID_TEAMS
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, require_role
from . import wallet
from .bets import _discord_post_bet

router = APIRouter()

MARKETS_FILE = DATA_DIR / "markets.json"

PAYOUT          = 100.0     # NB¥ a winning share pays
DEFAULT_B       = 1_500.0   # liquidity; the max NB¥ a flat 30-way market can create is b × ln 30 ≈ 5,100
DEFAULT_FEE     = 0.02      # on every buy and sell, burned
DEFAULT_STAKE   = 500.0     # per member per market, net of sales
MIN_OPEN_PRICE  = 0.01      # no outcome opens below 1%, so the worst case is b × ln 100
MIN_TRADE       = 1.0       # NB¥

_lock = threading.Lock()


# ── LMSR ─────────────────────────────────────────────────────────────────────
# q is shares outstanding per outcome, including the house's opening shares.
# With k = PAYOUT / b, the cost function is C(q) = b · ln Σ exp(k·q_i) and the
# price of outcome i is PAYOUT · softmax(k·q)_i.

def _probs(q: list[float], b: float) -> list[float]:
    k = PAYOUT / b
    m = max(k * x for x in q)
    e = [math.exp(k * x - m) for x in q]
    s = sum(e)
    return [x / s for x in e]


def _buy_shares_for(q: list[float], b: float, i: int, spend: float) -> float:
    """Shares of outcome i that `spend` NB¥ buys (before the fee).
    Cost of Δ shares is b·ln(1 − p + p·e^{kΔ}); solved for Δ."""
    p = _probs(q, b)[i]
    k = PAYOUT / b
    return math.log((math.exp(spend / b) - (1 - p)) / p) / k


def _sell_proceeds(q: list[float], b: float, i: int, shares: float) -> float:
    """NB¥ the house pays for `shares` of outcome i (before the fee)."""
    p = _probs(q, b)[i]
    k = PAYOUT / b
    return -b * math.log(1 - p + p * math.exp(-k * shares))


def _opening_q(probs: list[float], b: float) -> list[float]:
    k = PAYOUT / b
    return [math.log(p) / k for p in probs]


def _normalize_open(raw: list[float | None], floor: float = MIN_OPEN_PRICE) -> list[float]:
    """Opening probabilities: uniform when none given, else normalized with
    every outcome at least `floor`."""
    n = len(raw)
    if all(r is None for r in raw):
        return [1 / n] * n
    if any(r is None or r <= 0 for r in raw):
        raise HTTPException(status_code=422, detail="give an opening price for every outcome, or for none")
    if floor * n > 1:
        raise HTTPException(status_code=422, detail="too many outcomes for the opening-price floor")
    total = sum(raw)
    p = [r / total for r in raw]
    # Pin anything under the floor to it and rescale the rest to fill what's
    # left. Rescaling can push another outcome under, so repeat until stable.
    pinned: set[int] = set()
    while True:
        free = [i for i in range(n) if i not in pinned]
        room = 1 - floor * len(pinned)
        s = sum(p[i] for i in free)
        q = [floor if i in pinned else p[i] * room / s for i in range(n)]
        low = {i for i in free if q[i] < floor}
        if not low:
            return q
        pinned |= low


# ── Storage and shape ────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict]:
    return _load_json(MARKETS_FILE, [])


def _save(ms: list[dict]) -> None:
    _save_json(MARKETS_FILE, ms)


def _find(ms: list[dict], mid: str) -> dict:
    m = next((x for x in ms if x["id"] == mid), None)
    if m is None:
        raise HTTPException(status_code=404, detail="Market not found")
    return m


def _is_trading(m: dict) -> bool:
    if m["status"] != "open":
        return False
    return not (m.get("closes_at") and _now() >= m["closes_at"])


def _house(m: dict) -> dict:
    """Cash in and out of this market. `net` is the house's result: positive
    means NB¥ left circulation, negative means it was created."""
    cin = sum(t["cash"] for t in m["trades"] if t["side"] == "buy")
    cout = sum(t["cash"] for t in m["trades"] if t["side"] == "sell")
    paid = sum((m.get("resolution") or {}).get("payouts", {}).values())
    refunded = sum((m.get("resolution") or {}).get("refunds", {}).values())
    fees = sum(t["fee"] for t in m["trades"])
    return {"cash_in": round(cin, 2), "cash_out": round(cout, 2), "paid_out": round(paid, 2),
            "refunded": round(refunded, 2), "fees_burned": round(fees, 2),
            "net": round(cin - cout - paid - refunded, 2)}


def _view(m: dict) -> dict:
    p = _probs(m["q"], m["b"])
    outs = []
    for o, pi, q, q0 in zip(m["outcomes"], p, m["q"], m["q0"]):
        outs.append({**o, "price": round(pi * PAYOUT, 2), "shares_out": round(q - q0, 4)})
    return {
        "id": m["id"], "title": m["title"], "description": m["description"],
        "status": m["status"], "trading": _is_trading(m),
        "b": m["b"], "fee": m["fee"], "max_stake": m["max_stake"], "payout": PAYOUT,
        "created_by": m["created_by"], "created_at": m["created_at"],
        "closes_at": m.get("closes_at"), "locked_at": m.get("locked_at"),
        "settled_at": m.get("settled_at"), "winner": m.get("winner"),
        "outcomes": outs,
        "open_prices": [round(x * PAYOUT, 2) for x in _probs(m["q0"], m["b"])],
        "max_mint": round(m["b"] * math.log(1 / min(_probs(m["q0"], m["b"]))), 2),
        "positions": m["positions"], "accounts": m["accounts"],
        "house": _house(m),
        "trade_count": len(m["trades"]),
    }


def _account(m: dict, member: str) -> dict:
    return m["accounts"].setdefault(member, {"spent": 0.0, "received": 0.0})


def _outcome_index(m: dict, oid: str) -> int:
    for i, o in enumerate(m["outcomes"]):
        if o["id"] == oid:
            return i
    raise HTTPException(status_code=422, detail="Invalid outcome_id")


# ── Quotes ───────────────────────────────────────────────────────────────────
# Buys round cost up and sells round proceeds down, to the cent, so rounding
# never favours the trader.

def _quote_buy(m: dict, i: int, spend: float) -> dict:
    if spend < MIN_TRADE:
        raise HTTPException(status_code=422, detail=f"spend at least NB¥{MIN_TRADE:.0f}")
    spend = math.floor(spend * 100) / 100
    shares = _buy_shares_for(m["q"], m["b"], i, spend)
    fee = math.ceil(spend * m["fee"] * 100) / 100
    before = _probs(m["q"], m["b"])[i] * PAYOUT
    q2 = list(m["q"]); q2[i] += shares
    after = _probs(q2, m["b"])[i] * PAYOUT
    return {"side": "buy", "shares": round(shares, 4), "cost": spend, "fee": fee,
            "total": round(spend + fee, 2), "avg_price": round(spend / shares, 2),
            "price_before": round(before, 2), "price_after": round(after, 2),
            "pays_if_wins": round(shares * PAYOUT, 2)}


def _quote_sell(m: dict, i: int, shares: float) -> dict:
    if shares <= 0:
        raise HTTPException(status_code=422, detail="shares must be positive")
    gross = math.floor(_sell_proceeds(m["q"], m["b"], i, shares) * 100) / 100
    fee = math.ceil(gross * m["fee"] * 100) / 100
    before = _probs(m["q"], m["b"])[i] * PAYOUT
    q2 = list(m["q"]); q2[i] -= shares
    after = _probs(q2, m["b"])[i] * PAYOUT
    return {"side": "sell", "shares": round(shares, 4), "proceeds": gross, "fee": fee,
            "total": round(gross - fee, 2), "avg_price": round(gross / shares, 2),
            "price_before": round(before, 2), "price_after": round(after, 2)}


# ── Models ───────────────────────────────────────────────────────────────────

class OutcomeIn(BaseModel):
    label: str
    team: str | None = None
    open_price: float | None = None   # any positive weight; normalized


class MarketCreate(BaseModel):
    title: str
    description: str = ""
    outcomes: list[OutcomeIn]
    b: float = DEFAULT_B
    fee: float = DEFAULT_FEE
    max_stake: float = DEFAULT_STAKE
    closes_at: str | None = None


class QuoteIn(BaseModel):
    outcome_id: str
    side: str                 # "buy" | "sell"
    spend: float | None = None
    shares: float | None = None


class BuyIn(BaseModel):
    outcome_id: str
    spend: float
    min_shares: float | None = None    # refuse if the price moved against you


class SellIn(BaseModel):
    outcome_id: str
    shares: float
    min_proceeds: float | None = None


class SettleIn(BaseModel):
    winner: str


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/api/markets")
def list_markets():
    return [_view(m) for m in reversed(_load())]


@router.get("/api/markets/house")
def markets_house():
    """Every market's result summed. Same sign as `GET /api/bets/house`."""
    tot = {"cash_in": 0.0, "cash_out": 0.0, "paid_out": 0.0, "refunded": 0.0,
           "fees_burned": 0.0, "net": 0.0}
    for m in _load():
        for k, v in _house(m).items():
            tot[k] = round(tot[k] + v, 2)
    return tot


@router.get("/api/markets/{mid}")
def get_market(mid: str):
    return _view(_find(_load(), mid))


@router.get("/api/markets/{mid}/history")
def market_history(mid: str):
    """Price after every trade, and the trades themselves. Both public."""
    m = _find(_load(), mid)
    return {"outcomes": [o["id"] for o in m["outcomes"]], "prices": m["history"],
            "trades": m["trades"]}


@router.post("/api/markets")
def create_market(body: MarketCreate, info: dict = Depends(require_role("bookie"))):
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title is required")
    outs = [o for o in body.outcomes if o.label.strip()]
    if len(outs) < 2:
        raise HTTPException(status_code=422, detail="at least 2 outcomes")
    if len({o.label.strip().lower() for o in outs}) != len(outs):
        raise HTTPException(status_code=422, detail="outcome labels must be distinct")
    if not (100 <= body.b <= 20_000):
        raise HTTPException(status_code=422, detail="b must be between 100 and 20,000")
    if not (0 <= body.fee <= 0.1):
        raise HTTPException(status_code=422, detail="fee must be between 0 and 10%")
    if body.max_stake < MIN_TRADE:
        raise HTTPException(status_code=422, detail="max_stake too small")
    closes_at = None
    if body.closes_at:
        try:
            dt = datetime.fromisoformat(body.closes_at)
        except ValueError:
            raise HTTPException(status_code=422, detail="closes_at must be an ISO date-time")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        closes_at = dt.astimezone(timezone.utc).isoformat()
        if closes_at <= _now():
            raise HTTPException(status_code=422, detail="closes_at is in the past")
    for o in outs:
        if o.team and o.team.upper() not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"unknown team {o.team!r}")

    probs = _normalize_open([o.open_price for o in outs])
    q0 = _opening_q(probs, body.b)
    now = _now()
    m = {
        "id": secrets.token_hex(6),
        "title": body.title.strip(),
        "description": body.description.strip(),
        "status": "open",
        "b": body.b, "fee": body.fee, "max_stake": body.max_stake,
        "outcomes": [{"id": secrets.token_hex(3), "label": o.label.strip(),
                      "team": o.team.upper() if o.team else None} for o in outs],
        "q0": q0, "q": list(q0),
        "positions": {}, "accounts": {}, "trades": [],
        "history": [{"ts": now, "p": [round(x * PAYOUT, 2) for x in probs]}],
        "created_by": info["name"], "created_at": now,
        "closes_at": closes_at, "locked_at": None, "settled_at": None,
        "winner": None, "resolution": None,
    }
    with _lock:
        ms = _load()
        ms.append(m)
        _save(ms)
    log_write(info, f"POST markets — {m['title']!r} ({len(outs)} outcomes, b={body.b})")
    _discord_post_bet({
        "title": f"📈 New market: {m['title']}",
        "description": (m["description"] + "\n\n" if m["description"] else "")
                       + f"{len(outs)} outcomes. Buy and sell at [nbn.today/bet](https://nbn.today/bet/#futures).",
        "color": 0x3B82F6,
    })
    return _view(m)


@router.post("/api/markets/{mid}/quote")
def quote(mid: str, body: QuoteIn):
    """What a buy or sell would do right now. Read-only; the page calls it as
    the member types."""
    m = _find(_load(), mid)
    i = _outcome_index(m, body.outcome_id)
    if body.side == "buy":
        if body.spend is None:
            raise HTTPException(status_code=422, detail="spend is required for a buy")
        return _quote_buy(m, i, body.spend)
    if body.side == "sell":
        if body.shares is None:
            raise HTTPException(status_code=422, detail="shares is required for a sell")
        return _quote_sell(m, i, body.shares)
    raise HTTPException(status_code=422, detail="side must be buy or sell")


def _record(m: dict, member: str, i: int, side: str, shares: float, cash: float, fee: float) -> None:
    oid = m["outcomes"][i]["id"]
    m["q"][i] += shares if side == "buy" else -shares
    pos = m["positions"].setdefault(member, {})
    pos[oid] = round(pos.get(oid, 0.0) + (shares if side == "buy" else -shares), 4)
    if pos[oid] <= 1e-4:
        del pos[oid]
    if not pos:
        del m["positions"][member]
    ts = _now()
    m["trades"].append({"ts": ts, "member": member, "outcome_id": oid, "side": side,
                        "shares": round(shares, 4), "cash": cash, "fee": fee})
    m["history"].append({"ts": ts, "p": [round(x * PAYOUT, 2) for x in _probs(m["q"], m["b"])]})


@router.post("/api/markets/{mid}/buy")
def buy(mid: str, body: BuyIn, info: dict = Depends(get_token_info)):
    who = info["name"]
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if not _is_trading(m):
            raise HTTPException(status_code=409, detail="This market isn't trading")
        i = _outcome_index(m, body.outcome_id)
        qt = _quote_buy(m, i, body.spend)
        if body.min_shares is not None and qt["shares"] < body.min_shares - 1e-6:
            raise HTTPException(status_code=409, detail="The price moved — check the new quote")
        acct = _account(m, who)
        net = acct["spent"] - acct["received"]
        if net + qt["total"] > m["max_stake"] + 1e-6:
            room = max(0.0, m["max_stake"] - net)
            raise HTTPException(status_code=422,
                                detail=f"The most you can have in this market is NB¥{m['max_stake']:,.0f}, "
                                       f"net of what you've sold. You have NB¥{room:,.2f} of room, fee included.")
        label = m["outcomes"][i]["label"]
        wallet.post([{"member": who, "delta": -qt["total"], "kind": "market_buy",
                      "reason": f"Bought {qt['shares']:.2f} {label} in \"{m['title']}\" "
                                f"(NB¥{qt['cost']:,.2f} + NB¥{qt['fee']:,.2f} fee)",
                      "ref": f"market:{mid}"}])
        acct["spent"] = round(acct["spent"] + qt["total"], 2)
        _record(m, who, i, "buy", qt["shares"], qt["total"], qt["fee"])
        _save(ms)
    log_write(info, f"POST markets/{mid}/buy — {qt['shares']} {label} for NB¥{qt['total']}")
    return {"trade": qt, "market": _view(m), "balance": wallet.balance(who)}


@router.post("/api/markets/{mid}/sell")
def sell(mid: str, body: SellIn, info: dict = Depends(get_token_info)):
    who = info["name"]
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if not _is_trading(m):
            raise HTTPException(status_code=409, detail="This market isn't trading")
        i = _outcome_index(m, body.outcome_id)
        held = m["positions"].get(who, {}).get(body.outcome_id, 0.0)
        # "Sell all" sends what the page showed, which is rounded; accept that.
        shares = held if abs(body.shares - held) < 1e-3 else body.shares
        if shares > held + 1e-9:
            raise HTTPException(status_code=422, detail=f"You hold {held:.2f} shares of that outcome")
        qt = _quote_sell(m, i, shares)
        if body.min_proceeds is not None and qt["total"] < body.min_proceeds - 1e-6:
            raise HTTPException(status_code=409, detail="The price moved — check the new quote")
        label = m["outcomes"][i]["label"]
        if qt["total"] > 0:
            wallet.post([{"member": who, "delta": qt["total"], "kind": "market_sell",
                          "reason": f"Sold {shares:.2f} {label} in \"{m['title']}\" "
                                    f"(NB¥{qt['proceeds']:,.2f} − NB¥{qt['fee']:,.2f} fee)",
                          "ref": f"market:{mid}"}])
        acct = _account(m, who)
        acct["received"] = round(acct["received"] + qt["total"], 2)
        _record(m, who, i, "sell", shares, qt["total"], qt["fee"])
        _save(ms)
    log_write(info, f"POST markets/{mid}/sell — {shares} {label} for NB¥{qt['total']}")
    return {"trade": qt, "market": _view(m), "balance": wallet.balance(who)}


@router.post("/api/markets/{mid}/lock")
def lock_market(mid: str, info: dict = Depends(require_role("bookie"))):
    """Stop trading ahead of settlement. Reopening is `unlock`."""
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if m["status"] != "open":
            raise HTTPException(status_code=409, detail="Market isn't open")
        m["status"] = "locked"
        m["locked_at"] = _now()
        _save(ms)
    log_write(info, f"POST markets/{mid}/lock")
    return _view(m)


@router.post("/api/markets/{mid}/unlock")
def unlock_market(mid: str, info: dict = Depends(require_role("bookie"))):
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if m["status"] != "locked":
            raise HTTPException(status_code=409, detail="Market isn't locked")
        m["status"] = "open"
        m["locked_at"] = None
        _save(ms)
    log_write(info, f"POST markets/{mid}/unlock")
    return _view(m)


@router.post("/api/markets/{mid}/settle")
def settle(mid: str, body: SettleIn, info: dict = Depends(require_role("bookie"))):
    """Pay every share of the winner PAYOUT NB¥. Everything else is worth 0."""
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if m["status"] not in ("open", "locked"):
            raise HTTPException(status_code=409, detail="Market is already settled")
        i = _outcome_index(m, body.winner)
        label = m["outcomes"][i]["label"]
        payouts = {}
        for member, pos in m["positions"].items():
            sh = pos.get(body.winner, 0.0)
            if sh > 0:
                payouts[member] = math.floor(sh * PAYOUT * 100) / 100
        lines = [{"member": mem, "delta": amt, "kind": "market_payout",
                  "reason": f"{label} won \"{m['title']}\"", "ref": f"market:{mid}"}
                 for mem, amt in payouts.items() if amt > 0]
        if lines:
            wallet.post(lines)
        m.update(status="settled", settled_at=_now(), winner=body.winner,
                 resolution={"payouts": payouts, "refunds": {}, "voided": False,
                             "closing_price": round(_probs(m["q"], m["b"])[i] * PAYOUT, 2)})
        _save(ms)
    h = _house(m)
    log_write(info, f"POST markets/{mid}/settle — {label}; paid NB¥{h['paid_out']}, house {h['net']:+.2f}")
    _discord_post_bet({
        "title": f"🏁 Market settled: {m['title']}",
        "description": f"**{label}** won. Paid NB¥{h['paid_out']:,.0f} to {len(payouts)} holder(s).",
        "color": 0xF59E0B,
    })
    return _view(m)


@router.post("/api/markets/{mid}/void")
def void_market(mid: str, info: dict = Depends(require_role("bookie"))):
    """Call the market off. Everyone gets back what they put in, net of what
    they already took out by selling; nobody is charged for a sale that came
    out ahead. Fees are refunded with the rest."""
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if m["status"] not in ("open", "locked"):
            raise HTTPException(status_code=409, detail="Market is already settled")
        refunds = {}
        for member, a in m["accounts"].items():
            net = round(a["spent"] - a["received"], 2)
            if net > 0:
                refunds[member] = net
        if refunds:
            wallet.post([{"member": mem, "delta": amt, "kind": "market_refund",
                          "reason": f"Refund (voided): \"{m['title']}\"", "ref": f"market:{mid}"}
                         for mem, amt in refunds.items()])
        m.update(status="voided", settled_at=_now(),
                 resolution={"payouts": {}, "refunds": refunds, "voided": True})
        _save(ms)
    log_write(info, f"POST markets/{mid}/void — refunded {len(refunds)}")
    return _view(m)

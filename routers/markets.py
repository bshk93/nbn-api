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

**Every outcome has a Yes and a No.** A No share on Boston pays 100 if Boston
doesn't win. To the market maker it is one share of every other outcome, so it
needs no math of its own, and it pushes Boston's price down exactly as a Yes
pushes it up. That's what makes a price someone pumped too high cheap to
correct, and it's why there's no per-member cap by default: a big bet can't
make the house lose more than its bound, and a price pushed the wrong way is
money for everyone who pushes it back. A bookie can still set `max_stake` on
one market.

**Nobody bets against their own team** — a member with a team role or a
current tenure on an outcome's team. A position like that pays more the worse
the team does, and it's the one bet its holder can influence. A direct No on
your team is refused, and so is anything that would let your bets gain more
than OWN_TEAM_ALLOWANCE if your team threw its season — which is what catches
Yes on 28 of the other 29 teams on a team with a real chance (`_check_own_team`).
Knowing about your own trade early is not blocked; see
`docs/nbyen-economy.md` § 4a.

**Every market has a close time.** One left trading after its result is known
sells the winner below 100 to whoever notices first.

A trading fee on every buy and sell is burned rather than paid to anyone. It
makes flipping stale news cost something.

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
from .auth import get_token_info, require_role, load_members
from . import wallet
from .bets import _discord_post_bet
from .news import load_articles

router = APIRouter()

MARKETS_FILE = DATA_DIR / "markets.json"

PAYOUT          = 100.0     # NB¥ a winning share pays
DEFAULT_B       = 1_500.0   # liquidity; the max NB¥ a flat 30-way market can create is b × ln 30 ≈ 5,100
DEFAULT_FEE     = 0.02      # on every buy and sell, burned
DEFAULT_STAKE   = None      # no per-member cap unless a bookie sets one
MIN_OPEN_PRICE  = 0.01      # no outcome opens below 1%, so the worst case is b × ln 100
MIN_TRADE       = 1.0       # NB¥
# Seeding a title market from power rankings: a team's weight falls by a factor
# of e for every SEED_SPREAD places of average rank. At 3, the 2026 preseason
# edition opens its top team near 21%, its top five near 70%, and everyone
# ranked below about 12th at the 1% floor.
SEED_SPREAD     = 3.0
# The most a member's bets may stand to gain, at market odds, if their own
# team threw its season (`_tank_gain`).
OWN_TEAM_ALLOWANCE = 100.0

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


# A Yes share of outcome i adds 1 to q_i. A No share adds 1 to every other
# outcome. Both are a bundle whose price is p (Yes: p_i; No: 1 − p_i), and the
# cost of Δ of a bundle priced p is b·ln(1 − p + p·e^{kΔ}), so one formula
# serves both.

def _side_price(q: list[float], b: float, i: int, contract: str) -> float:
    p = _probs(q, b)[i]
    return p if contract == "yes" else 1 - p


def _apply(q: list[float], i: int, contract: str, shares: float) -> list[float]:
    """q after adding `shares` (negative to remove) of a Yes or No on i."""
    if contract == "yes":
        return [x + shares if j == i else x for j, x in enumerate(q)]
    return [x if j == i else x + shares for j, x in enumerate(q)]


def _buy_shares_for(q: list[float], b: float, i: int, spend: float, contract: str = "yes") -> float:
    """Shares that `spend` NB¥ buys (before the fee); the cost formula solved for Δ."""
    p = _side_price(q, b, i, contract)
    k = PAYOUT / b
    return math.log((math.exp(spend / b) - (1 - p)) / p) / k


def _sell_proceeds(q: list[float], b: float, i: int, shares: float, contract: str = "yes") -> float:
    """NB¥ the house pays for `shares` (before the fee)."""
    p = _side_price(q, b, i, contract)
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
        raise HTTPException(status_code=422, detail="Give every outcome a weight, or leave them all blank.")
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
    held = {o["id"]: {"yes": 0.0, "no": 0.0} for o in m["outcomes"]}
    for pos in m["positions"].values():
        for oid, h in pos.items():
            for c in ("yes", "no"):
                held[oid][c] += h.get(c, 0.0)
    for o, pi in zip(m["outcomes"], p):
        outs.append({**o, "price": round(pi * PAYOUT, 2),
                     "yes_out": round(held[o["id"]]["yes"], 4), "no_out": round(held[o["id"]]["no"], 4)})
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


def _contract(c: str) -> str:
    if c not in ("yes", "no"):
        raise HTTPException(status_code=422, detail="contract must be yes or no")
    return c


def _payoffs(m: dict, pos: dict) -> list[float]:
    """What a position pays under each outcome."""
    ids = [o["id"] for o in m["outcomes"]]
    no_total = sum(h.get("no", 0.0) for h in pos.values())
    return [PAYOUT * (pos.get(w, {}).get("yes", 0.0) + no_total - pos.get(w, {}).get("no", 0.0))
            for w in ids]


def _member_teams(info: dict) -> set[str]:
    """Teams a member works for: a team role, or a current tenure in any
    position. Admin's implied roles don't count — admin isn't every team."""
    teams = {r.upper() for r in info.get("roles", []) if r.upper() in VALID_TEAMS}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for t in (load_members().get(info.get("name", "")) or {}).get("tenures", []):
        if (t.get("start") or "") <= today and (not t.get("end") or t["end"] >= today) and t.get("team"):
            teams.add(t["team"].upper())
    return teams


def _tank_gain(m: dict, pos: dict, q: list[float], t: int) -> float:
    """What this position stands to gain, at the market's odds in `q`, if
    outcome t's chance fell to zero — i.e. if the team threw its season.

    That's t's price times the gap between the expected payout if t loses
    (over every other outcome, weighted by price) and the payout if t wins.
    Weighting by t's own price is the point: a 1% team has almost nothing to
    throw away, so its GM can trade other teams freely, while a contender's GM
    can't stack much against their own team."""
    pay = _payoffs(m, pos)
    p = _probs(q, m["b"])
    rest = 1 - p[t]
    if rest <= 0:
        return 0.0
    lose = sum(p[j] * pay[j] for j in range(len(pay)) if j != t) / rest
    return p[t] * (lose - pay[t])


def _check_own_team(m: dict, info: dict, i: int, contract: str, side: str, delta: float) -> None:
    """Refuse a trade that leaves a member better off if their own team loses.

    Two parts. A direct No on your own team is always refused. And after any
    trade, what your whole position would gain if your team threw its season
    (`_tank_gain`, at the odds after the trade) may be at most
    OWN_TEAM_ALLOWANCE. The second part is what catches a No in disguise (Yes
    on 28 of the other 29) on a team with something to throw away.

    A trade that doesn't raise that gain is always allowed, so someone who
    joins a team holding a position against it can still sell out of it."""
    mine = _member_teams(info)
    if not mine:
        return
    who = info["name"]
    oid = m["outcomes"][i]["id"]
    before = m["positions"].get(who, {})
    after = _position_after(m, who, oid, contract, delta)
    q_after = _apply(m["q"], i, contract, delta)
    for t, o in enumerate(m["outcomes"]):
        if o.get("team") not in mine:
            continue
        if t == i and contract == "no" and side == "buy":
            raise HTTPException(status_code=422, detail=f"You can't bet against your own team ({o['label']}).")
        gain_after = _tank_gain(m, after, q_after, t)
        gain_before = _tank_gain(m, before, m["q"], t)
        if gain_after > OWN_TEAM_ALLOWANCE and gain_after > gain_before + 0.01:
            raise HTTPException(
                status_code=422,
                detail=f"You can't bet against your own team. At the current odds, after this trade your bets "
                       f"would gain about NB¥{gain_after:,.0f} if {o['label']} threw their season "
                       f"(the most allowed is NB¥{OWN_TEAM_ALLOWANCE:,.0f}). Backing {o['label']} too would balance it.")


# ── Quotes ───────────────────────────────────────────────────────────────────
# Buys round cost up and sells round proceeds down, to the cent, so rounding
# never favours the trader.

# `price_before`/`price_after` are the outcome's own price (its odds of
# happening) whichever side is traded, since that's the number on the board.

def _quote_buy(m: dict, i: int, spend: float, contract: str = "yes") -> dict:
    if spend < MIN_TRADE:
        raise HTTPException(status_code=422, detail=f"spend at least NB¥{MIN_TRADE:.0f}")
    spend = math.floor(spend * 100) / 100
    # Round shares down, so rounding never hands the buyer a fraction of a cent.
    shares = math.floor(_buy_shares_for(m["q"], m["b"], i, spend, contract) * 1e4) / 1e4
    fee = math.ceil(spend * m["fee"] * 100) / 100
    before = _probs(m["q"], m["b"])[i] * PAYOUT
    after = _probs(_apply(m["q"], i, contract, shares), m["b"])[i] * PAYOUT
    return {"side": "buy", "contract": contract, "shares": round(shares, 4), "cost": spend, "fee": fee,
            "total": round(spend + fee, 2), "avg_price": round(spend / shares, 2),
            "price_before": round(before, 2), "price_after": round(after, 2),
            "pays_if_right": round(shares * PAYOUT, 2)}


def _quote_sell(m: dict, i: int, shares: float, contract: str = "yes") -> dict:
    if shares <= 0:
        raise HTTPException(status_code=422, detail="shares must be positive")
    gross = math.floor(_sell_proceeds(m["q"], m["b"], i, shares, contract) * 100) / 100
    fee = math.ceil(gross * m["fee"] * 100) / 100
    before = _probs(m["q"], m["b"])[i] * PAYOUT
    after = _probs(_apply(m["q"], i, contract, -shares), m["b"])[i] * PAYOUT
    return {"side": "sell", "contract": contract, "shares": round(shares, 4), "proceeds": gross, "fee": fee,
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
    max_stake: float | None = DEFAULT_STAKE
    closes_at: str             # required: a market left open past its result pays whoever notices first


class QuoteIn(BaseModel):
    outcome_id: str
    side: str                 # "buy" | "sell"
    contract: str = "yes"     # "yes" | "no"
    spend: float | None = None
    shares: float | None = None


class BuyIn(BaseModel):
    outcome_id: str
    contract: str = "yes"
    spend: float
    min_shares: float | None = None    # refuse if the price moved against you


class SellIn(BaseModel):
    outcome_id: str
    contract: str = "yes"
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


class PreviewIn(BaseModel):
    open_prices: list[float | None]
    b: float = DEFAULT_B


@router.post("/api/markets/preview")
def preview_market(body: PreviewIn):
    """The opening prices a market would get from these weights, after
    normalizing and the 1% floor, and the most it could create. Read-only;
    the new-market form calls it as the bookie types, so the form shows what
    create will do without copying the math."""
    if len(body.open_prices) < 2:
        raise HTTPException(status_code=422, detail="at least 2 outcomes")
    if body.b <= 0:
        raise HTTPException(status_code=422, detail="b must be positive")
    p = _normalize_open(body.open_prices)
    return {"prices": [round(x * PAYOUT, 2) for x in p],
            "max_mint": round(body.b * math.log(1 / min(p)), 2),
            "move_10_to_20": round(body.b * math.log(0.9 / 0.8), 2)}


def power_ranking_seed(articles: list[dict]) -> dict | None:
    """Opening weights for the 30 teams from the latest published power
    rankings. Uses each team's average ballot rank, not its rank, so a clear
    gap between two teams shows up as a gap in the odds. Weights are returned
    as percentages to one decimal (at least 0.1) so the form shows numbers a
    bookie can read and edit; the 1% floor is applied when the market opens."""
    eds = [a for a in articles
           if a.get("type") == "power_rankings" and a.get("status") == "published" and a.get("final")]
    if not eds:
        return None
    ed = max(eds, key=lambda a: a.get("published_at") or "")
    raw = {r["team"]: math.exp(-(r["avg"] - 1) / SEED_SPREAD) for r in ed["final"]}
    total = sum(raw.values())
    return {
        "source": {"id": ed["id"], "title": ed.get("title", ""), "published_at": ed.get("published_at")},
        "teams": {r["team"]: {"rank": r["rank"], "avg": r["avg"],
                              "weight": max(0.1, round(raw[r["team"]] / total * 100, 1))}
                  for r in ed["final"]},
    }


@router.get("/api/markets/seeds/power-rankings")
def seed_from_power_rankings():
    seed = power_ranking_seed(load_articles())
    if seed is None:
        raise HTTPException(status_code=404, detail="No published power rankings yet")
    return seed


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
    if body.max_stake is not None and body.max_stake < MIN_TRADE:
        raise HTTPException(status_code=422, detail="max_stake too small")
    if not body.closes_at:
        raise HTTPException(status_code=422, detail="A market needs a close time, before the result can be known.")
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
    the member types. It doesn't apply the own-team rule, which needs to know
    who's asking; the trade does."""
    m = _find(_load(), mid)
    i = _outcome_index(m, body.outcome_id)
    c = _contract(body.contract)
    if body.side == "buy":
        if body.spend is None:
            raise HTTPException(status_code=422, detail="spend is required for a buy")
        return _quote_buy(m, i, body.spend, c)
    if body.side == "sell":
        if body.shares is None:
            raise HTTPException(status_code=422, detail="shares is required for a sell")
        return _quote_sell(m, i, body.shares, c)
    raise HTTPException(status_code=422, detail="side must be buy or sell")


def _position_after(m: dict, member: str, oid: str, contract: str, delta: float) -> dict:
    """A copy of the member's position with `delta` shares added (or removed)."""
    pos = {k: dict(v) for k, v in m["positions"].get(member, {}).items()}
    h = pos.setdefault(oid, {"yes": 0.0, "no": 0.0})
    h[contract] = round(h.get(contract, 0.0) + delta, 4)
    return pos


def _record(m: dict, member: str, i: int, contract: str, side: str, shares: float,
            cash: float, fee: float) -> None:
    oid = m["outcomes"][i]["id"]
    delta = shares if side == "buy" else -shares
    m["q"] = _apply(m["q"], i, contract, delta)
    pos = _position_after(m, member, oid, contract, delta)
    for k in list(pos):
        pos[k] = {c: v for c, v in pos[k].items() if v > 1e-4}
        if not pos[k]:
            del pos[k]
    if pos:
        m["positions"][member] = pos
    else:
        m["positions"].pop(member, None)
    ts = _now()
    m["trades"].append({"ts": ts, "member": member, "outcome_id": oid, "contract": contract,
                        "side": side, "shares": round(shares, 4), "cash": cash, "fee": fee})
    m["history"].append({"ts": ts, "p": [round(x * PAYOUT, 2) for x in _probs(m["q"], m["b"])]})


def _what(label: str, contract: str) -> str:
    return label if contract == "yes" else f"No on {label}"


@router.post("/api/markets/{mid}/buy")
def buy(mid: str, body: BuyIn, info: dict = Depends(get_token_info)):
    who = info["name"]
    c = _contract(body.contract)
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if not _is_trading(m):
            raise HTTPException(status_code=409, detail="This market isn't trading")
        i = _outcome_index(m, body.outcome_id)
        qt = _quote_buy(m, i, body.spend, c)
        if body.min_shares is not None and qt["shares"] < body.min_shares - 1e-6:
            raise HTTPException(status_code=409, detail="The price moved — check the new quote")
        _check_own_team(m, info, i, c, "buy", qt["shares"])
        acct = _account(m, who)
        net = acct["spent"] - acct["received"]
        if m.get("max_stake") is not None and net + qt["total"] > m["max_stake"] + 1e-6:
            room = max(0.0, m["max_stake"] - net)
            raise HTTPException(status_code=422,
                                detail=f"The most you can have in this market is NB¥{m['max_stake']:,.0f}, "
                                       f"net of what you've sold. You have NB¥{room:,.2f} of room, fee included.")
        what = _what(m["outcomes"][i]["label"], c)
        wallet.post([{"member": who, "delta": -qt["total"], "kind": "market_buy",
                      "reason": f"Bought {qt['shares']:.2f} {what} in \"{m['title']}\" "
                                f"(NB¥{qt['cost']:,.2f} + NB¥{qt['fee']:,.2f} fee)",
                      "ref": f"market:{mid}"}])
        acct["spent"] = round(acct["spent"] + qt["total"], 2)
        _record(m, who, i, c, "buy", qt["shares"], qt["total"], qt["fee"])
        _save(ms)
    log_write(info, f"POST markets/{mid}/buy — {qt['shares']} {what} for NB¥{qt['total']}")
    return {"trade": qt, "market": _view(m), "balance": wallet.balance(who)}


@router.post("/api/markets/{mid}/sell")
def sell(mid: str, body: SellIn, info: dict = Depends(get_token_info)):
    who = info["name"]
    c = _contract(body.contract)
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if not _is_trading(m):
            raise HTTPException(status_code=409, detail="This market isn't trading")
        i = _outcome_index(m, body.outcome_id)
        held = m["positions"].get(who, {}).get(body.outcome_id, {}).get(c, 0.0)
        # "Sell all" sends what the page showed, which is rounded; accept that.
        shares = held if abs(body.shares - held) < 1e-3 else body.shares
        if shares > held + 1e-9:
            raise HTTPException(status_code=422, detail=f"You hold {held:.2f} of those shares")
        qt = _quote_sell(m, i, shares, c)
        if body.min_proceeds is not None and qt["total"] < body.min_proceeds - 1e-6:
            raise HTTPException(status_code=409, detail="The price moved — check the new quote")
        _check_own_team(m, info, i, c, "sell", -shares)
        what = _what(m["outcomes"][i]["label"], c)
        if qt["total"] > 0:
            wallet.post([{"member": who, "delta": qt["total"], "kind": "market_sell",
                          "reason": f"Sold {shares:.2f} {what} in \"{m['title']}\" "
                                    f"(NB¥{qt['proceeds']:,.2f} − NB¥{qt['fee']:,.2f} fee)",
                          "ref": f"market:{mid}"}])
        acct = _account(m, who)
        acct["received"] = round(acct["received"] + qt["total"], 2)
        _record(m, who, i, c, "sell", shares, qt["total"], qt["fee"])
        _save(ms)
    log_write(info, f"POST markets/{mid}/sell — {shares} {what} for NB¥{qt['total']}")
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
    """Pay PAYOUT NB¥ for every Yes on the winner and every No on anything
    else. Everything else is worth 0."""
    with _lock:
        ms = _load()
        m = _find(ms, mid)
        if m["status"] not in ("open", "locked"):
            raise HTTPException(status_code=409, detail="Market is already settled")
        i = _outcome_index(m, body.winner)
        label = m["outcomes"][i]["label"]
        payouts = {}
        for member, pos in m["positions"].items():
            amt = math.floor(_payoffs(m, pos)[i] * 100) / 100
            if amt > 0:
                payouts[member] = amt
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

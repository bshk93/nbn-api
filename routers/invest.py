"""NBN Wall Street (`/invest`) — shares in teams, priced by game results.

A league-subsidised game, not a market (`docs/nbyen-economy.md` § 3). There is
no counterparty: profit is minted and losses are burned. The rules:

  * **Price.** Every ticker starts at 100 in 20-21 and moves after each game by
    `1 + diff_diff / MOVE_DIVISOR`, where `diff_diff` is the margin plus the
    opponent's average margin so far that season. A playoff game counts the
    margin alone, doubled: the opponent credit doesn't cancel out when every
    team is a good one, so it inflated every postseason.
    Returns are a percentage, so the price level never matters. Nothing a
    member does moves a price: the old sentiment overlay let one member pump
    the stock he was trading, and was deleted. The price is compounded at full
    precision; rounding it every game froze low-priced teams.
  * **Seasons** are every raw box score file on disk (`game_highs.iter_raw_files`,
    the build's own list), so a new season counts from its first game.
  * **Pending games.** A team can't be traded while one of its games has been
    played but its box score isn't in yet, or anyone who watched the stream
    could trade on a result the price hasn't seen. A game counts as played
    once its date is past, or on the day itself once Pick'em's lock says the
    day's games have started. An index is closed while any of its teams is.
  * **Limits.** `TICKER_CAP` NB¥ per ticker and `TOTAL_CAP` across all of them,
    counted at cost (what was put in, not what it is worth now). Shorts count
    against the same caps. A short can lose no more than its collateral.
  * **Fee.** `FEE_RATE` on every trade, charged on top of a buy or short and
    taken out of a sale or cover. It is burned. Positions and P&L are kept
    without it.
  * **Old trades.** Holdings were emptied at the NB¥ reset (`RESET_AT`). Trades
    before it stay in the log as history, but P&L and portfolio only replay
    trades since.

    venv/bin/python -m tests.test_invest
"""
import csv
import io
import json
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR,
    INVEST_HOLDINGS_FILE, INVEST_TRADES_FILE, INVEST_MARKET_FILE,
    _invest_lock, _market_lock,
    VALID_TEAMS, logger,
)
from .storage import _load_json, _save_json
from .auth import get_token_info, has_role, load_members

router = APIRouter()

NBY_START = 0.0   # no default balance since the 2026-09 reset; P&L is against zero

# Set 2026-10-23. /3000 puts the best team of a season at about +55% and a
# "buy the five hottest teams after 20 games" strategy at about +24%, which
# keeps the league's payout near the § 3 budget. /1000 was about four times that.
MOVE_DIVISOR = 3000.0
TICKER_CAP   = 2500.0
TOTAL_CAP    = 10000.0
FEE_RATE     = 0.01

# When nbyen_reset.py emptied the holdings. Trades before it were made with
# pre-reset NB¥ and close no position that exists now.
RESET_AT = "2026-09-25T17:59:20+00:00"

# ── Index definitions ─────────────────────────────────────────────────────────

INDEXES: dict[str, list[str]] = {
    "EAST": ["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NYK", "ORL", "PHI", "TOR", "WAS"],
    "WEST": ["DAL", "DEN", "GSW", "HOU", "LAC", "LAL", "MEM", "MIN", "NOP", "OKC", "PHX", "POR", "SAC", "SAS", "UTA"],
    "ATLC": ["BOS", "BKN", "NYK", "PHI", "TOR"],
    "CTRL": ["CHI", "CLE", "DET", "IND", "MIL"],
    "SE":   ["ATL", "CHA", "MIA", "ORL", "WAS"],
    "NW":   ["DEN", "MIN", "OKC", "POR", "UTA"],
    "PAC":  ["GSW", "LAC", "LAL", "PHX", "SAC"],
    "SW":   ["DAL", "HOU", "MEM", "NOP", "SAS"],
}
INDEX_META: dict[str, dict] = {
    "EAST": {"name": "Eastern Conference", "conf": "East"},
    "WEST": {"name": "Western Conference", "conf": "West"},
    "ATLC": {"name": "Atlantic Division",  "conf": "East", "div": "Atlantic"},
    "CTRL": {"name": "Central Division",   "conf": "East", "div": "Central"},
    "SE":   {"name": "Southeast Division", "conf": "East", "div": "Southeast"},
    "NW":   {"name": "Northwest Division", "conf": "West", "div": "Northwest"},
    "PAC":  {"name": "Pacific Division",   "conf": "West", "div": "Pacific"},
    "SW":   {"name": "Southwest Division", "conf": "West", "div": "Southwest"},
}

from .bets import DISCORD_BETS_WEBHOOK
from . import wallet

import httpx

from .game_highs import iter_raw_files


def _raw_files() -> list[tuple[str, bool, "Path"]]:
    """(season, is_playoff, path) for every raw box score file on disk."""
    return list(iter_raw_files(DATA_DIR))


# ── Discord ──────────────────────────────────────────────────────────────────

def _discord_trade(
    action: str,        # "buy" | "sell" | "short" | "cover"
    member: str,
    team: str,
    shares: float,
    price: float,
    nbyen: float,
    new_balance: float,
    holdings: dict,     # full holdings dict (already saved)
    current: dict,      # current prices
) -> None:
    action_labels = {
        "buy":   ("📈 Bought",  0x22c55e),
        "sell":  ("📉 Sold",    0xef4444),
        "short": ("🔻 Shorted",0xa855f7),
        "cover": ("✅ Covered", 0x8b5cf6),
    }
    label, color = action_labels.get(action, (action.title(), 0x6b7280))

    # Member's updated position in this team
    mh    = holdings.get(member, {})
    entry = mh.get(team, {})
    lp    = entry.get("long",  {})
    sp    = entry.get("short", {})
    lsh   = lp.get("shares", 0.0)
    ssh   = sp.get("shares", 0.0)
    cur   = current.get(team, price)
    l_eq  = round(lsh * cur, 2)
    s_eq  = round(max(0.0, ssh * (2 * sp.get("avg_open_price", 0) - cur)), 2) if ssh > 0 else 0.0

    pos_lines = []
    if lsh > 0:
        pos_lines.append(f"Long  {lsh:.4f} sh · ¥{l_eq:,.2f}")
    if ssh > 0:
        pos_lines.append(f"Short {ssh:.4f} sh · ¥{s_eq:,.2f}")
    if not pos_lines:
        pos_lines.append("No position")
    pos_str = "\n".join(pos_lines)

    # Market-wide totals for this team
    total_long = total_short = 0.0
    for mh2 in holdings.values():
        e2 = mh2.get(team, {})
        total_long  += e2.get("long",  {}).get("shares", 0.0)
        total_short += e2.get("short", {}).get("shares", 0.0)
    tl_val = round(total_long  * cur, 2)
    ts_val = round(total_short * cur, 2)

    description = (
        f"**{label} {shares:.4f} shares of {team}** @ ¥{price:,.2f}  ·  ¥{nbyen:,.2f}\n\n"
        f"**{member}'s position:**\n{pos_str}\n\n"
        f"**Market-wide {team}:**\n"
        f"Long  {total_long:.4f} sh · ¥{tl_val:,.2f}\n"
        f"Short {total_short:.4f} sh · ¥{ts_val:,.2f}"
    )

    try:
        httpx.post(
            DISCORD_BETS_WEBHOOK,
            json={"embeds": [{"description": description, "color": color}]},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Discord invest webhook failed: %s", exc)


# ── Price engine ─────────────────────────────────────────────────────────────

_price_cache:    dict = {"prices": None, "current": None, "mtime": -1.0}
_price_cache_v2: dict = {"prices": None, "current": None, "mtime": -1.0}
_price_cache_lock = threading.Lock()


def _allstats_max_mtime() -> float:
    return max((p.stat().st_mtime for _, _, p in _raw_files()), default=0.0)


def _load_game_data() -> list[dict]:
    """Load all allstats CSVs; return one deduplicated record per (team, date)."""
    games: list[dict] = []
    seen: set = set()

    def _process(path, season_year: str, is_playoff: bool):
        if not path.exists():
            return
        text = path.read_text()
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            team = row.get("TEAM", "").strip()
            date = row.get("DATE", "").strip()
            if not team or not date or team == "TEAM":
                continue
            key = (team, date)
            if key in seen:
                continue
            seen.add(key)
            opp_raw = row.get("OPP_RAW", "").strip()
            if not opp_raw or opp_raw.lower() == "none":
                opp_raw = row.get("OPP", "").strip().lstrip("@")
            try:
                team_pts = float(row["TEAM_PTS"])
                opp_pts  = float(row["OPP_TEAM_PTS"])
            except (KeyError, ValueError, TypeError):
                continue
            gametype = row.get("gametype", "REG").strip().upper()
            games.append({
                "team":        team,
                "date":        date,
                "opp_raw":     opp_raw,
                "diff":        team_pts - opp_pts,
                "season_year": season_year,
                "is_playoff":  is_playoff or gametype == "PLAYOFF",
            })

    for season, is_playoff, path in _raw_files():
        _process(path, season, is_playoff)

    return games


def _compute_prices(own_avg: bool = False) -> tuple[dict, dict]:
    """Every ticker's price history, and its current price.

    `own_avg` is the preview algorithm (`/invest/preview`): it also subtracts
    the team's own average margin, so a routine win by a good team moves it
    less than an upset does.
    """
    from collections import defaultdict
    games = _load_game_data()
    if not games:
        return {}, {}

    games.sort(key=lambda g: g["date"])

    team_season: dict = defaultdict(list)
    for g in games:
        team_season[(g["team"], g["season_year"])].append(g)

    game_meta: dict = {}
    for (team, _sy), sg in team_season.items():
        sg.sort(key=lambda x: x["date"])
        cum = 0.0
        for i, g in enumerate(sg):
            game_meta[(team, g["date"])] = {"lag_cum": cum, "g": i + 1}
            cum += g["diff"]

    def _avg(meta):
        return meta["lag_cum"] / (meta["g"] - 1) if meta and meta["g"] > 1 else None

    for g in games:
        opp_avg = _avg(game_meta.get((g["opp_raw"], g["date"])))
        own     = _avg(game_meta.get((g["team"],    g["date"]))) if own_avg else 0.0
        if g["is_playoff"]:
            # Margin only, doubled. Every playoff team is a good team, so the
            # opponent credit never cancels out there: both sides of every
            # game gained, and the indexes rose 5-29% every postseason.
            dd = 2.0 * g["diff"]
        elif opp_avg is None or own is None:
            g["diff_diff"] = None
            g["pct_chg"]   = 1.0
            continue
        else:
            dd = g["diff"] + opp_avg - own
        g["diff_diff"] = dd
        g["pct_chg"]   = 1.0 + dd / MOVE_DIVISOR

    team_games: dict = defaultdict(list)
    for g in games:
        team_games[g["team"]].append(g)

    prices: dict  = {}
    current: dict = {}
    for team, tg in team_games.items():
        tg.sort(key=lambda x: x["date"])
        price = 100.0
        history = []
        for i, g in enumerate(tg):
            price *= g["pct_chg"]
            history.append({
                "date":       g["date"],
                "price":      round(price, 4),
                "game_num":   i + 1,
                "is_playoff": g["is_playoff"],
                "diff":       g["diff"],
                "diff_diff":  round(g["diff_diff"], 2) if g["diff_diff"] is not None else None,
            })
        prices[team]  = history
        current[team] = price

    _inject_index_prices(team_games, prices, current)
    return prices, current


def _inject_index_prices(team_games: dict, prices: dict, current: dict) -> None:
    """Compute index price histories from constituent team game data and inject into prices/current."""
    from collections import defaultdict
    for idx_ticker, idx_teams in INDEXES.items():
        date_chgs:    dict[str, list[float]] = defaultdict(list)
        date_playoff: dict[str, bool]         = {}
        for team in idx_teams:
            for g in team_games.get(team, []):
                date_chgs[g["date"]].append(g["pct_chg"])
                if g["is_playoff"]:
                    date_playoff[g["date"]] = True
        if not date_chgs:
            continue
        idx_price = 100.0
        idx_history: list[dict] = []
        for date in sorted(date_chgs):
            chgs = date_chgs[date]
            idx_price *= sum(chgs) / len(chgs)
            idx_history.append({
                "date":       date,
                "price":      round(idx_price, 4),
                "game_num":   len(idx_history) + 1,
                "is_playoff": date_playoff.get(date, False),
                "diff":       None,
                "diff_diff":  None,
            })
        prices[idx_ticker]  = idx_history
        current[idx_ticker] = idx_price


def _get_prices(algo: str = "current") -> tuple[dict, dict]:
    with _price_cache_lock:
        mt    = _allstats_max_mtime()
        cache = _price_cache_v2 if algo == "preview" else _price_cache
        if cache["prices"] is None or mt != cache["mtime"]:
            label = "v2/preview" if algo == "preview" else "current"
            logger.info("[invest] recomputing prices (%s, mtime changed)", label)
            cache["prices"], cache["current"] = (
                _compute_prices(own_avg=(algo == "preview"))
            )
            cache["mtime"] = mt
        return cache["prices"], cache["current"]


# ── Balance helpers ───────────────────────────────────────────────────────────

def _load_balances() -> dict:
    return wallet.balances()


# ── Holdings helpers ──────────────────────────────────────────────────────────
#
# Holdings structure:
#   { member: { team: { "long":  {"shares": f, "avg_buy_price":  f},
#                        "short": {"shares": f, "avg_open_price": f} } } }
# Either side may be absent if there is no position.
# Legacy flat format { team: {"shares":f, "avg_buy_price":f} } is migrated on read.

def _load_holdings() -> dict:
    raw = _load_json(INVEST_HOLDINGS_FILE, {})
    # Migrate any legacy flat-format entries
    for member, mh in raw.items():
        for team, entry in mh.items():
            if "shares" in entry and "long" not in entry and "short" not in entry:
                mh[team] = {"long": {"shares": entry["shares"],
                                     "avg_buy_price": entry.get("avg_buy_price", 0.0)}}
    return raw


def _save_holdings(h: dict):
    _save_json(INVEST_HOLDINGS_FILE, h)


def _load_trades() -> list[dict]:
    return _load_json(INVEST_TRADES_FILE, [])


def _save_trades(t: list[dict]):
    _save_json(INVEST_TRADES_FILE, t)


def _load_market() -> dict:
    return _load_json(INVEST_MARKET_FILE, {"locked": False, "locked_reason": "", "locked_until": None})


def _save_market(m: dict):
    _save_json(INVEST_MARKET_FILE, m)


def _check_market_open():
    wallet.require_enabled("invest")
    m = _load_market()
    if m.get("locked"):
        raise HTTPException(status_code=423, detail=m.get("locked_reason") or "Market is locked")


def _short_equity(shares: float, avg_open: float, current_price: float) -> float:
    """Current redemption value of a short position (floored at 0)."""
    return round(max(0.0, shares * (2 * avg_open - current_price)), 2)


def _live_trades(all_trades: list) -> list:
    """Trades since the NB¥ reset — the only ones that built today's positions."""
    return [t for t in all_trades if t.get("ts", "") >= RESET_AT]


# ── Pending games ─────────────────────────────────────────────────────────────

def _pending_games(prices_hist: dict) -> dict[str, dict]:
    """Team → its earliest scheduled game that has been played but has no box
    score yet. A game is played once its date is past, or today once Pick'em's
    lock says the day's games have started (the stream went live, a box score
    or the VOD came in)."""
    from .league_time import league_today_str
    from .schedule import _load as load_schedule
    import season_clock

    today  = league_today_str()
    games  = load_schedule(season_clock.season_for_date(today)).get("games", [])
    played = {(t, h["date"]) for t, hist in prices_hist.items() for h in hist}
    started: Optional[bool] = None
    out: dict[str, dict] = {}
    for g in sorted(games, key=lambda g: g.get("date") or ""):
        d = g.get("date") or ""
        if not d or d > today:
            continue
        missing = [t for t in (g.get("away_team"), g.get("home_team")) if t and (t, d) not in played]
        if not missing:
            continue
        if d == today:
            if started is None:
                from .pickem import lock_state
                rows = [{"DATE": today}] if any(day == today for _, day in played) else []
                started = lock_state(today, rows) is not None
            if not started:
                continue
        for t in missing:
            out.setdefault(t, {k: g.get(k) for k in ("date", "away_team", "home_team")})
    return out


def _ticker_pending(ticker: str, pending: dict) -> Optional[dict]:
    for team in INDEXES.get(ticker, [ticker]):
        if team in pending:
            return pending[team]
    return None


def _check_ticker_open(ticker: str, prices_hist: dict) -> None:
    g = _ticker_pending(ticker, _pending_games(prices_hist))
    if g:
        raise HTTPException(status_code=423, detail=(
            f"{ticker} can't be traded until the box score for "
            f"{g['away_team']} @ {g['home_team']} on {g['date']} is in."))


# ── Limits and fee ────────────────────────────────────────────────────────────

def _deployed(mh: dict) -> dict[str, float]:
    """Ticker → NB¥ at cost in one member's open positions, long and short."""
    out = {}
    for ticker, entry in mh.items():
        lp, sp = entry.get("long", {}), entry.get("short", {})
        cost = (lp.get("shares", 0.0) * lp.get("avg_buy_price", 0.0)
                + sp.get("shares", 0.0) * sp.get("avg_open_price", 0.0))
        if cost > 0:
            out[ticker] = cost
    return out


def _check_caps(mh: dict, ticker: str, amount: float) -> None:
    dep   = _deployed(mh)
    here  = dep.get(ticker, 0.0)
    total = sum(dep.values())
    room  = max(0.0, min(TICKER_CAP - here, TOTAL_CAP - total))
    if here + amount > TICKER_CAP + 0.005:
        raise HTTPException(status_code=400, detail=(
            f"The limit is ¥{TICKER_CAP:,.0f} per ticker and you have ¥{here:,.2f} in {ticker}. "
            f"You can add up to ¥{room:,.2f}."))
    if total + amount > TOTAL_CAP + 0.005:
        raise HTTPException(status_code=400, detail=(
            f"The limit is ¥{TOTAL_CAP:,.0f} across all tickers and you have ¥{total:,.2f} in. "
            f"You can add up to ¥{room:,.2f}."))


def _fee(amount: float) -> float:
    return round(amount * FEE_RATE, 2)


# ── Pydantic models ───────────────────────────────────────────────────────────

class BuyIn(BaseModel):
    team: str
    nbyen: float


class SellIn(BaseModel):
    team: str
    shares: float


class ShortIn(BaseModel):
    team: str
    nbyen: float   # collateral


class CoverIn(BaseModel):
    team: str
    shares: float  # short shares to cover


class LockIn(BaseModel):
    locked: bool
    reason: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/invest/market")
def get_market(algo: str = "current"):
    """Current market status + latest price for every ticker."""
    prices_hist, current = _get_prices(algo)
    market   = _load_market()
    holdings = _load_holdings()
    pending  = _pending_games(prices_hist) if algo == "current" else {}

    team_long:  dict[str, float] = {}
    team_short: dict[str, float] = {}
    for mh in holdings.values():
        for team, entry in mh.items():
            lp = entry.get("long",  {})
            sp = entry.get("short", {})
            if lp.get("shares", 0) > 0:
                team_long[team]  = round(team_long.get(team, 0.0)  + lp["shares"], 6)
            if sp.get("shares", 0) > 0:
                team_short[team] = round(team_short.get(team, 0.0) + sp["shares"], 6)

    def _row(ticker: str) -> dict:
        p  = current[ticker]
        ls = round(team_long.get(ticker, 0.0), 4)
        ss = round(team_short.get(ticker, 0.0), 4)
        return {
            "price":             round(p, 4),
            "change":            round(p - 100.0, 2),
            "change_pct":        round(p - 100.0, 2),
            "total_shares":      ls,
            "total_value":       round(ls * p, 2),
            "total_short":       ss,
            "total_short_value": round(ss * p, 2),
            "pending":           _ticker_pending(ticker, pending),
        }

    teams = [{"team": t, **_row(t)} for t in sorted(VALID_TEAMS) if t in current]
    indexes = []
    for idx_ticker, meta in INDEX_META.items():
        if idx_ticker not in current:
            continue
        indexes.append({"ticker": idx_ticker, "name": meta["name"],
                        "conf": meta.get("conf"), "div": meta.get("div"), **_row(idx_ticker)})

    return {
        "locked":        market.get("locked", False) or not wallet.enabled("invest"),
        "locked_reason": market.get("locked_reason", "") or
                         ("" if wallet.enabled("invest") else "The market is paused while NB¥ restarts."),
        "locked_until":  market.get("locked_until"),
        "rules":         {"ticker_cap": TICKER_CAP, "total_cap": TOTAL_CAP, "fee_rate": FEE_RATE},
        "teams":         teams,
        "indexes":       indexes,
    }


@router.get("/api/invest/prices/{team}")
def get_team_prices(team: str, algo: str = "current"):
    team = team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=404, detail="Unknown team or index")
    prices_hist, _ = _get_prices(algo)
    return {"team": team, "history": prices_hist.get(team, [])}


@router.get("/api/invest/holdings")
def get_my_holdings(info: dict = Depends(get_token_info)):
    _, current = _get_prices()
    holdings   = _load_holdings()
    member     = info["name"]
    result     = []

    for team, entry in holdings.get(member, {}).items():
        price = round(current.get(team, 0.0), 4)
        lp    = entry.get("long",  {})
        sp    = entry.get("short", {})

        if lp.get("shares", 0.0) > 0:
            sh  = lp["shares"]
            avg = lp["avg_buy_price"]
            eq  = round(sh * price, 2)
            cost = round(sh * avg, 2)
            result.append({
                "team": team, "side": "long",
                "shares": round(sh, 6), "avg_price": round(avg, 4),
                "current_price": price, "equity": eq, "cost": cost,
                "pl":     round(eq - cost, 2),
                "pl_pct": round((eq - cost) / cost * 100, 2) if cost else 0.0,
            })

        if sp.get("shares", 0.0) > 0:
            sh       = sp["shares"]
            avg_open = sp["avg_open_price"]
            collat   = round(sh * avg_open, 2)
            eq       = _short_equity(sh, avg_open, price)
            result.append({
                "team": team, "side": "short",
                "shares": round(sh, 6), "avg_price": round(avg_open, 4),
                "current_price": price, "equity": eq, "cost": collat,
                "pl":     round(eq - collat, 2),
                "pl_pct": round((eq - collat) / collat * 100, 2) if collat else 0.0,
            })

    result.sort(key=lambda x: (x["side"], -x["equity"]))
    return result


def _compute_realized_pnl_all(all_trades: list) -> dict[str, float]:
    realized: dict[str, float] = {}
    long_state:  dict[str, dict[str, dict]] = {}
    short_state: dict[str, dict[str, dict]] = {}

    for t in sorted(all_trades, key=lambda x: x["ts"]):
        member = t["member"]
        team   = t["team"]
        action = t["action"]
        shares = t["shares"]
        price  = t["price"]
        nbyen  = t["nbyen"]

        if member not in realized:
            realized[member] = 0.0

        ls = long_state.setdefault(member, {}).setdefault(team, {"shares": 0.0, "avg_price": 0.0})
        ss = short_state.setdefault(member, {}).setdefault(team, {"shares": 0.0, "avg_open": 0.0})

        if action == "buy":
            total = ls["shares"] + shares
            ls["avg_price"] = (ls["shares"] * ls["avg_price"] + shares * price) / total if total else price
            ls["shares"] = round(total, 6)
        elif action == "sell":
            profit = round(nbyen - shares * ls["avg_price"], 2)
            realized[member] = round(realized[member] + profit, 2)
            new_shares = round(ls["shares"] - shares, 6)
            ls["shares"] = new_shares if new_shares >= 1e-9 else 0.0
        elif action == "short":
            total = ss["shares"] + shares
            ss["avg_open"] = (ss["shares"] * ss["avg_open"] + shares * price) / total if total else price
            ss["shares"] = round(total, 6)
        elif action == "cover":
            collateral = round(shares * ss["avg_open"], 2)
            profit = round(nbyen - collateral, 2)
            realized[member] = round(realized[member] + profit, 2)
            new_shares = round(ss["shares"] - shares, 6)
            ss["shares"] = new_shares if new_shares >= 1e-9 else 0.0

    return {m: round(v, 2) for m, v in realized.items()}


@router.get("/api/invest/holdings/all")
def get_all_holdings():
    _, current = _get_prices()
    holdings   = _load_holdings()
    balances   = _load_balances()
    members    = set(list(holdings.keys()) + list(balances.keys()))
    realized_map = _compute_realized_pnl_all(_live_trades(_load_trades()))

    rows = []
    for member in members:
        liquid = balances.get(member, NBY_START)
        equity = 0.0
        for team, entry in holdings.get(member, {}).items():
            price = current.get(team, 0.0)
            lp = entry.get("long",  {})
            sp = entry.get("short", {})
            if lp.get("shares", 0) > 0:
                equity += lp["shares"] * price
            if sp.get("shares", 0) > 0:
                equity += _short_equity(sp["shares"], sp["avg_open_price"], price)
        equity = round(equity, 2)
        rows.append({
            "member":       member,
            "equity":       equity,
            "liquid":       round(liquid, 2),
            "net_worth":    round(equity + liquid, 2),
            "pl":           round(equity + liquid - NBY_START, 2),
            "realized_pnl": realized_map.get(member, 0.0),
        })
    rows.sort(key=lambda x: x["net_worth"], reverse=True)
    return rows


def compute_member_pnl(member: str) -> dict:
    """Per-team realized + unrealized P&L for one member. Realized is replayed
    from the member's trade history (same math as _compute_realized_pnl_all);
    unrealized marks open positions to the current market price (same valuation
    as get_all_holdings). Returns {positions: [...], total_realized,
    total_unrealized}, positions sorted by total P&L descending."""
    _, cur = _get_prices()
    all_trades = _live_trades(_load_trades())
    holdings   = _load_holdings().get(member, {})
    member_trades = sorted([t for t in all_trades if t["member"] == member],
                           key=lambda t: t["ts"])

    realized: dict[str, float] = {}
    ls_state:  dict[str, dict] = {}
    ss_state:  dict[str, dict] = {}
    for t in member_trades:
        team, action = t["team"], t["action"]
        shares, price, nbyen = t["shares"], t["price"], t["nbyen"]
        ls = ls_state.setdefault(team, {"shares": 0.0, "avg": 0.0})
        ss = ss_state.setdefault(team, {"shares": 0.0, "avg": 0.0})
        realized.setdefault(team, 0.0)
        if action == "buy":
            total = ls["shares"] + shares
            ls["avg"] = (ls["shares"] * ls["avg"] + shares * price) / total if total else price
            ls["shares"] = round(total, 6)
        elif action == "sell":
            realized[team] = round(realized[team] + (nbyen - shares * ls["avg"]), 2)
            ls["shares"] = round(ls["shares"] - shares, 6)
        elif action == "short":
            total = ss["shares"] + shares
            ss["avg"] = (ss["shares"] * ss["avg"] + shares * price) / total if total else price
            ss["shares"] = round(total, 6)
        elif action == "cover":
            realized[team] = round(realized[team] + (nbyen - shares * ss["avg"]), 2)
            ss["shares"] = round(ss["shares"] - shares, 6)

    positions = []
    for team in set(realized) | set(holdings):
        entry = holdings.get(team, {})
        lp, sp = entry.get("long", {}), entry.get("short", {})
        open_pos = lp.get("shares", 0) > 0 or sp.get("shares", 0) > 0
        unreal = 0.0
        if open_pos:
            price = cur.get(team, 0.0)
            if lp.get("shares", 0) > 0:
                unreal += lp["shares"] * (price - lp.get("avg_buy_price", 0.0))
            if sp.get("shares", 0) > 0:
                unreal += sp["shares"] * (sp.get("avg_open_price", 0.0) - price)
        positions.append({
            "team":       team,
            "realized":   round(realized.get(team, 0.0), 2),
            "unrealized": round(unreal, 2),
            "open":       open_pos,
        })

    positions = [p for p in positions
                 if abs(p["realized"]) >= 0.005 or abs(p["unrealized"]) >= 0.005 or p["open"]]
    positions.sort(key=lambda p: -(p["realized"] + p["unrealized"]))
    return {
        "positions":        positions,
        "total_realized":   round(sum(p["realized"] for p in positions), 2),
        "total_unrealized": round(sum(p["unrealized"] for p in positions), 2),
    }


def _compute_invest_stats(member: str, all_trades: list) -> dict:
    member_trades = sorted(
        [t for t in all_trades if t["member"] == member],
        key=lambda t: t["ts"],
    )

    trade_count = len(member_trades)
    realized_pnl = 0.0
    best_single_profit = 0.0
    ever_shorted_profitably = False

    long_state: dict[str, dict] = {}   # team -> {shares, avg_price}
    short_state: dict[str, dict] = {}  # team -> {shares, avg_open}

    for t in member_trades:
        team   = t["team"]
        action = t["action"]
        shares = t["shares"]
        price  = t["price"]
        nbyen  = t["nbyen"]

        if action == "buy":
            ls = long_state.get(team, {"shares": 0.0, "avg_price": 0.0})
            total = ls["shares"] + shares
            ls["avg_price"] = (ls["shares"] * ls["avg_price"] + shares * price) / total
            ls["shares"] = round(total, 6)
            long_state[team] = ls

        elif action == "sell":
            ls = long_state.get(team, {"shares": 0.0, "avg_price": 0.0})
            profit = round(nbyen - shares * ls.get("avg_price", 0.0), 2)
            realized_pnl = round(realized_pnl + profit, 2)
            best_single_profit = max(best_single_profit, profit)
            new_shares = round(ls["shares"] - shares, 6)
            if new_shares < 1e-9:
                long_state.pop(team, None)
            else:
                ls["shares"] = new_shares
                long_state[team] = ls

        elif action == "short":
            ss = short_state.get(team, {"shares": 0.0, "avg_open": 0.0})
            total = ss["shares"] + shares
            ss["avg_open"] = (ss["shares"] * ss["avg_open"] + shares * price) / total
            ss["shares"] = round(total, 6)
            short_state[team] = ss

        elif action == "cover":
            ss = short_state.get(team, {"shares": 0.0, "avg_open": 0.0})
            collateral = round(shares * ss.get("avg_open", 0.0), 2)
            profit = round(nbyen - collateral, 2)
            realized_pnl = round(realized_pnl + profit, 2)
            best_single_profit = max(best_single_profit, profit)
            if profit > 0:
                ever_shorted_profitably = True
            new_shares = round(ss["shares"] - shares, 6)
            if new_shares < 1e-9:
                short_state.pop(team, None)
            else:
                ss["shares"] = new_shares
                short_state[team] = ss

    return {
        "trade_count": trade_count,
        "realized_pnl": round(realized_pnl, 2),
        "best_single_trade": round(best_single_profit, 2),
        "ever_shorted_profitably": ever_shorted_profitably,
    }


@router.get("/api/invest/stats")
def get_all_invest_stats():
    """Investment stats for every member in one pass (used by the members list)."""
    from .auth import load_members
    all_members = load_members()
    all_trades = _load_trades()
    return {m: _compute_invest_stats(m, all_trades) for m in all_members}


@router.get("/api/invest/stats/{member}")
def get_member_invest_stats(member: str):
    from .auth import load_members
    all_members = load_members()
    if member not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{member}' not found")
    return _compute_invest_stats(member, _load_trades())


@router.get("/api/invest/portfolio")
def get_portfolio(info: dict = Depends(get_token_info)):
    member = info["name"]
    trades = [t for t in _live_trades(_load_trades()) if t["member"] == member]
    if not trades:
        return {"equity_chart": [], "summary": None}

    prices, current = _get_prices()
    held_teams = {t["team"] for t in trades}
    team_price_at: dict[str, dict[str, float]] = {
        team: {p["date"]: p["price"] for p in prices.get(team, [])}
        for team in held_teams
    }
    all_dates = sorted({d for tp in team_price_at.values() for d in tp})
    trades.sort(key=lambda t: t["ts"])

    chart = []
    for date in all_dates:
        long_snap:  dict[str, float] = {}
        short_snap: dict[str, dict]  = {}  # team → {shares, collateral}

        for t in trades:
            if t["ts"][:10] > date:
                break
            team = t["team"]
            act  = t["action"]
            if act == "buy":
                long_snap[team] = long_snap.get(team, 0.0) + t["shares"]
            elif act == "sell":
                long_snap[team] = max(0.0, long_snap.get(team, 0.0) - t["shares"])
            elif act == "short":
                s = short_snap.get(team, {"shares": 0.0, "collateral": 0.0})
                s["collateral"] += t["shares"] * t["price"]
                s["shares"]     += t["shares"]
                short_snap[team] = s
            elif act == "cover":
                s = short_snap.get(team, {"shares": 0.0, "collateral": 0.0})
                if s["shares"] > 0:
                    ratio = min(1.0, t["shares"] / s["shares"])
                    s["collateral"] = s["collateral"] * (1 - ratio)
                    s["shares"]     = max(0.0, s["shares"] - t["shares"])
                short_snap[team] = s

        p_at = lambda team: team_price_at[team].get(date, 0.0)
        long_eq  = sum(sh * p_at(team) for team, sh in long_snap.items() if sh > 0)
        short_eq = sum(
            max(0.0, s["collateral"] * 2 - s["shares"] * p_at(team))
            for team, s in short_snap.items() if s["shares"] > 0
        )
        chart.append({"date": date, "equity": round(long_eq + short_eq, 2)})

    balances     = _load_balances()
    liquid       = balances.get(member, NBY_START)
    total_equity = chart[-1]["equity"] if chart else 0.0

    return {
        "equity_chart": chart,
        "summary": {
            "equity":    round(total_equity, 2),
            "liquid":    round(liquid, 2),
            "net_worth": round(total_equity + liquid, 2),
            "pl":        round(total_equity + liquid - NBY_START, 2),
        },
    }


@router.get("/api/invest/trades")
def get_my_trades(info: dict = Depends(get_token_info)):
    member = info["name"]
    trades = [t for t in _load_trades() if t["member"] == member]
    trades.sort(key=lambda t: t["ts"], reverse=True)
    return trades


def _record(trades_entry: dict) -> None:
    trades = _load_trades()
    trades.append(trades_entry)
    _save_trades(trades)


def _open_position(body, info: dict, side: str) -> dict:
    """Buy (side "long") or short. `body.nbyen` goes into the position; the
    fee is charged on top. Every refusal comes before any NB¥ moves."""
    team = body.team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=400, detail="Unknown team or index")
    if body.nbyen <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    _check_market_open()

    prices_hist, current = _get_prices()
    price = current.get(team)
    if price is None:
        raise HTTPException(status_code=400, detail="No price data for team")
    _check_ticker_open(team, prices_hist)

    amount = round(body.nbyen, 2)
    fee    = _fee(amount)
    shares = amount / price
    member = info["name"]
    other  = "short" if side == "long" else "long"
    action = "buy" if side == "long" else "short"
    avg_k  = "avg_buy_price" if side == "long" else "avg_open_price"

    with _invest_lock:
        holdings = _load_holdings()
        mh = holdings.get(member, {})
        if mh.get(team, {}).get(other, {}).get("shares", 0.0) > 0:
            raise HTTPException(status_code=400, detail=(
                f"Cover your short position in {team} before going long" if side == "long"
                else f"Sell your long position in {team} before shorting"))
        _check_caps(mh, team, amount)

        what = "Share purchase" if side == "long" else "Short position"
        new_balance = wallet.debit(member, round(amount + fee, 2), "invest",
                                   f"{what}: {team} (incl. ¥{fee:,.2f} fee)")

        entry = holdings.setdefault(member, {}).setdefault(team, {})
        pos   = entry.get(side, {"shares": 0.0, avg_k: 0.0})
        total = pos["shares"] + shares
        entry[side] = {
            "shares": round(total, 6),
            avg_k:    round((pos["shares"] * pos[avg_k] + shares * price) / total, 6),
        }
        _save_holdings(holdings)
        _record({"ts": datetime.now(timezone.utc).isoformat(), "member": member, "team": team,
                 "action": action, "shares": round(shares, 6), "price": round(price, 4),
                 "nbyen": amount, "fee": fee, "balance_after": new_balance,
                 "game_count": len(prices_hist.get(team, []))})

    _discord_trade(action, member, team, shares, price, amount,
                   new_balance, _load_holdings(), current)
    return {"team": team, "shares": round(shares, 6), "price": round(price, 4),
            "nbyen": amount, "fee": fee, "balance_after": new_balance}


def _close_position(body, info: dict, side: str) -> dict:
    """Sell (side "long") or cover. The fee comes out of what is paid back."""
    team = body.team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=400, detail="Unknown team or index")
    if body.shares <= 0:
        raise HTTPException(status_code=400, detail="Shares must be positive")
    _check_market_open()

    prices_hist, current = _get_prices()
    price = current.get(team)
    if price is None:
        raise HTTPException(status_code=400, detail="No price data for team")
    _check_ticker_open(team, prices_hist)

    member = info["name"]
    action = "sell" if side == "long" else "cover"

    with _invest_lock:
        holdings = _load_holdings()
        mh   = holdings.get(member, {})
        pos  = mh.get(team, {}).get(side, {})
        held = pos.get("shares", 0.0)
        if held < body.shares - 1e-9:
            raise HTTPException(status_code=400, detail=f"Only hold {held:.6f} {side} shares of {team}")

        if side == "long":
            nbyen = round(body.shares * price, 2)
        else:
            nbyen = _short_equity(body.shares, pos["avg_open_price"], price)
        fee = _fee(nbyen)
        net = round(nbyen - fee, 2)

        what = "Share sale" if side == "long" else "Cover short"
        new_balance = (wallet.credit(member, net, "invest", f"{what}: {team} (after ¥{fee:,.2f} fee)")
                       if net > 0 else wallet.balance(member))

        new_sh = round(held - body.shares, 6)
        if new_sh < 1e-9:
            mh[team].pop(side, None)
        else:
            mh[team][side]["shares"] = new_sh
        if team in mh and not mh[team]:
            del mh[team]
        if member in holdings and not holdings[member]:
            del holdings[member]
        _save_holdings(holdings)
        _record({"ts": datetime.now(timezone.utc).isoformat(), "member": member, "team": team,
                 "action": action, "shares": round(body.shares, 6), "price": round(price, 4),
                 "nbyen": nbyen, "fee": fee, "balance_after": new_balance,
                 "game_count": len(prices_hist.get(team, []))})

    _discord_trade(action, member, team, body.shares, price, nbyen,
                   new_balance, _load_holdings(), current)
    return {"team": team, "shares": round(body.shares, 6), "price": round(price, 4),
            "nbyen": nbyen, "fee": fee, "balance_after": new_balance}


@router.post("/api/invest/buy")
def buy_shares(body: BuyIn, info: dict = Depends(get_token_info)):
    return _open_position(body, info, "long")


@router.post("/api/invest/sell")
def sell_shares(body: SellIn, info: dict = Depends(get_token_info)):
    return _close_position(body, info, "long")


@router.post("/api/invest/short")
def short_shares(body: ShortIn, info: dict = Depends(get_token_info)):
    """Open a short position: pay collateral, profit if price falls.
    Max loss = collateral (if price doubles). Cover to close."""
    return _open_position(body, info, "short")


@router.post("/api/invest/cover")
def cover_shares(body: CoverIn, info: dict = Depends(get_token_info)):
    """Close a short position. Receive collateral ± P/L (floored at 0)."""
    return _close_position(body, info, "short")


@router.post("/api/invest/lock")
def toggle_lock(body: LockIn, info: dict = Depends(get_token_info)):
    if not (has_role(info, "bookie") or has_role(info, "admin")):
        raise HTTPException(status_code=403, detail="bookie or admin role required")
    with _market_lock:
        market = _load_market()
        market["locked"]        = body.locked
        market["locked_reason"] = body.reason if body.locked else ""
        market["locked_until"]  = None
        _save_market(market)
    return market

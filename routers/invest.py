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
    BALANCES_FILE, LEDGER_FILE,
    _invest_lock, _market_lock,
    VALID_TEAMS, logger,
)
from .storage import _load_json, _save_json
from .auth import get_token_info, has_role, load_members

router = APIRouter()

NBY_START = 1000.0

# ── Index definitions ─────────────────────────────────────────────────────────

INDEXES: dict[str, list[str]] = {
    "EAST": ["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NYK", "ORL", "PHI", "TOR", "WAS"],
    "WEST": ["DAL", "DEN", "GSW", "HOU", "LAC", "LAL", "MEM", "MIN", "NOP", "OKC", "PHX", "POR", "SAC", "SAS", "UTA"],
    "NATL": ["BOS", "BKN", "NYK", "PHI", "TOR"],
    "CTRL": ["CHI", "CLE", "DET", "IND", "MIL"],
    "SE":   ["ATL", "CHA", "MIA", "ORL", "WAS"],
    "NW":   ["DEN", "MIN", "OKC", "POR", "UTA"],
    "PAC":  ["GSW", "LAC", "LAL", "PHX", "SAC"],
    "SW":   ["DAL", "HOU", "MEM", "NOP", "SAS"],
}
INDEX_META: dict[str, dict] = {
    "EAST": {"name": "Eastern Conference", "conf": "East"},
    "WEST": {"name": "Western Conference", "conf": "West"},
    "NATL": {"name": "Atlantic Division",  "conf": "East", "div": "Atlantic"},
    "CTRL": {"name": "Central Division",   "conf": "East", "div": "Central"},
    "SE":   {"name": "Southeast Division", "conf": "East", "div": "Southeast"},
    "NW":   {"name": "Northwest Division", "conf": "West", "div": "Northwest"},
    "PAC":  {"name": "Pacific Division",   "conf": "West", "div": "Pacific"},
    "SW":   {"name": "Southwest Division", "conf": "West", "div": "Southwest"},
}

# Shared balance/ledger locks — same objects used by bets.py so all writers
# contend on the same lock even when imported across modules.
from .bets import _balances_lock, _ledger_lock, DISCORD_BETS_WEBHOOK

import httpx

# ── Allstats file manifest ────────────────────────────────────────────────────

_REG_SEASONS   = ["20-21", "21-22", "22-23", "23-24", "24-25", "25-26"]
_PLAYOFF_YEARS = ["21",    "22",    "23",    "24",    "25",    "26"]

_PLAYOFF_SEASON = {y: f"{int(y)-1:02d}-{y}" for y in _PLAYOFF_YEARS}


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
    mt = 0.0
    for sy in _REG_SEASONS:
        p = DATA_DIR / f"allstats-{sy}.csv"
        if p.exists():
            mt = max(mt, p.stat().st_mtime)
    for py in _PLAYOFF_YEARS:
        p = DATA_DIR / f"allstats-playoffs-{py}.csv"
        if p.exists():
            mt = max(mt, p.stat().st_mtime)
    return mt


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

    for sy in _REG_SEASONS:
        _process(DATA_DIR / f"allstats-{sy}.csv", sy, False)
    for py in _PLAYOFF_YEARS:
        _process(DATA_DIR / f"allstats-playoffs-{py}.csv", _PLAYOFF_SEASON[py], True)

    return games


def _compute_prices() -> tuple[dict, dict]:
    games = _load_game_data()
    if not games:
        return {}, {}

    games.sort(key=lambda g: g["date"])

    from collections import defaultdict
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

    for g in games:
        opp_meta = game_meta.get((g["opp_raw"], g["date"]))
        if opp_meta and opp_meta["g"] > 1:
            opp_avg = opp_meta["lag_cum"] / (opp_meta["g"] - 1)
        else:
            opp_avg = None

        if opp_avg is None:
            g["diff_diff"] = None
            g["pct_chg"]   = 1.0
        else:
            dd = g["diff"] + opp_avg
            if g["is_playoff"]:
                dd *= 2.0
            g["diff_diff"] = dd
            g["pct_chg"]   = 1.0 + dd / 1000.0

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
            price = round(price * g["pct_chg"], 2)
            history.append({
                "date":       g["date"],
                "price":      price,
                "game_num":   i + 1,
                "is_playoff": g["is_playoff"],
                "diff":       g["diff"],
                "diff_diff":  round(g["diff_diff"], 2) if g["diff_diff"] is not None else None,
            })
        prices[team]  = history
        current[team] = price

    _inject_index_prices(team_games, prices, current)
    return prices, current


def _compute_prices_v2() -> tuple[dict, dict]:
    """Preview algorithm: DIFF_DIFF = DIFF + OPP_AVG_DIFF - OWN_AVG_DIFF.
    Measures performance vs. expectation — upsets are amplified, routine
    wins/losses are dampened."""
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

    for g in games:
        opp_meta = game_meta.get((g["opp_raw"], g["date"]))
        own_meta = game_meta.get((g["team"],    g["date"]))

        opp_avg = opp_meta["lag_cum"] / (opp_meta["g"] - 1) if opp_meta and opp_meta["g"] > 1 else None
        own_avg = own_meta["lag_cum"] / (own_meta["g"] - 1) if own_meta and own_meta["g"] > 1 else None

        if opp_avg is None or own_avg is None:
            g["diff_diff"] = None
            g["pct_chg"]   = 1.0
        else:
            dd = g["diff"] + opp_avg - own_avg
            if g["is_playoff"]:
                dd *= 2.0
            g["diff_diff"] = dd
            g["pct_chg"]   = 1.0 + dd / 1000.0

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
            price = round(price * g["pct_chg"], 2)
            history.append({
                "date":       g["date"],
                "price":      price,
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
            idx_price = round(idx_price * (sum(chgs) / len(chgs)), 2)
            idx_history.append({
                "date":       date,
                "price":      idx_price,
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
                _compute_prices_v2() if algo == "preview" else _compute_prices()
            )
            cache["mtime"] = mt
        return cache["prices"], cache["current"]


# ── Balance helpers ───────────────────────────────────────────────────────────

def _load_balances() -> dict:
    return _load_json(BALANCES_FILE, {})


def _save_balances(bal: dict):
    _save_json(BALANCES_FILE, bal)


def _init_bal(bal: dict, name: str) -> float:
    if name not in bal:
        bal[name] = NBY_START
    return bal[name]


def _append_ledger(entries: list[dict]):
    with _ledger_lock:
        ledger = json.loads(LEDGER_FILE.read_text()) if LEDGER_FILE.exists() else []
        ledger.extend(entries)
        LEDGER_FILE.write_text(json.dumps(ledger))


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
    m = _load_market()
    if m.get("locked"):
        raise HTTPException(status_code=423, detail=m.get("locked_reason") or "Market is locked")


def _short_equity(shares: float, avg_open: float, current_price: float) -> float:
    """Current redemption value of a short position (floored at 0)."""
    return round(max(0.0, shares * (2 * avg_open - current_price)), 2)


# ── Sentiment ─────────────────────────────────────────────────────────────────
#
# Each trade records `game_count` (all-time games played by the team at trade
# time) and `sentiment_delta` (±nbyen / SENTIMENT_DIVISOR).  Sentiment decays
# linearly to zero over 82 games.  The market price = algo_price × (1 + s).

SENTIMENT_DIVISOR = 50_000.0
SENTIMENT_CAP     = 0.50   # ±50% max


def _sentiment_at(N: int, team_trades: list) -> float:
    """Sentiment for a team at all-time game N, derived from its trade records."""
    s = 0.0
    for t in team_trades:
        delta = t.get("sentiment_delta", 0.0)
        gc    = t.get("game_count", 0)
        if not delta or gc > N:
            continue
        elapsed = N - gc
        if elapsed >= 82:
            continue
        s += delta * (1.0 - elapsed / 82.0)
    return max(-SENTIMENT_CAP, min(SENTIMENT_CAP, s))


def _sentiment_series(team_history: list, team_trades: list) -> list[float]:
    """One sentiment value per game in team_history."""
    return [_sentiment_at(i + 1, team_trades) for i in range(len(team_history))]


def _current_sentiment(team: str, prices_hist: dict, all_trades: list) -> float:
    team_trades = [t for t in all_trades if t.get("team") == team]
    N = len(prices_hist.get(team, []))
    return _sentiment_at(N, team_trades)


def _market_price(algo_price: float, sentiment: float) -> float:
    return round(algo_price * (1.0 + sentiment), 2)


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
    """Current market status + latest price for all 30 teams."""
    prices_hist, algo_current = _get_prices(algo)
    market     = _load_market()
    holdings   = _load_holdings()
    all_trades = _load_trades()

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

    teams = []
    for team in sorted(VALID_TEAMS):
        ap = algo_current.get(team)
        if ap is None:
            continue
        s  = _current_sentiment(team, prices_hist, all_trades)
        mp = _market_price(ap, s)
        ls = round(team_long.get(team, 0.0), 4)
        ss = round(team_short.get(team, 0.0), 4)
        teams.append({
            "team":              team,
            "price":             mp,
            "algo_price":        ap,
            "sentiment":         round(s, 4),
            "change":            round(mp - 100.0, 2),
            "change_pct":        round((mp - 100.0) / 100.0 * 100, 2),
            "total_shares":      ls,
            "total_value":       round(ls * mp, 2),
            "total_short":       ss,
            "total_short_value": round(ss * mp, 2),
        })
    indexes = []
    for idx_ticker in INDEXES:
        ap = algo_current.get(idx_ticker)
        if ap is None:
            continue
        s  = _current_sentiment(idx_ticker, prices_hist, all_trades)
        mp = _market_price(ap, s)
        ls = round(team_long.get(idx_ticker, 0.0), 4)
        ss = round(team_short.get(idx_ticker, 0.0), 4)
        meta = INDEX_META[idx_ticker]
        indexes.append({
            "ticker":       idx_ticker,
            "name":         meta["name"],
            "conf":         meta.get("conf"),
            "div":          meta.get("div"),
            "price":        mp,
            "algo_price":   ap,
            "sentiment":    round(s, 4),
            "change":       round(mp - 100.0, 2),
            "change_pct":   round((mp - 100.0) / 100.0 * 100, 2),
            "total_shares": ls,
            "total_short":  ss,
        })

    return {
        "locked":        market.get("locked", False),
        "locked_reason": market.get("locked_reason", ""),
        "locked_until":  market.get("locked_until"),
        "teams":         teams,
        "indexes":       indexes,
    }


@router.get("/api/invest/prices/{team}")
def get_team_prices(team: str, algo: str = "current"):
    team = team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=404, detail="Unknown team or index")
    prices_hist, _ = _get_prices(algo)
    history        = prices_hist.get(team, [])
    all_trades     = _load_trades()
    team_trades    = [t for t in all_trades if t.get("team") == team]
    sentiments     = _sentiment_series(history, team_trades)
    enriched = [
        {**g, "sentiment": round(s, 4), "market_price": _market_price(g["price"], s)}
        for g, s in zip(history, sentiments)
    ]
    return {"team": team, "history": enriched}


@router.get("/api/invest/holdings")
def get_my_holdings(info: dict = Depends(get_token_info)):
    prices_hist, algo_cur = _get_prices()
    holdings   = _load_holdings()
    all_trades = _load_trades()
    member     = info["name"]
    result     = []

    for team, entry in holdings.get(member, {}).items():
        s     = _current_sentiment(team, prices_hist, all_trades)
        price = _market_price(algo_cur.get(team, 0.0), s)
        lp    = entry.get("long",  {})
        sp    = entry.get("short", {})

        if lp.get("shares", 0.0) > 0:
            sh  = lp["shares"]
            avg = lp["avg_buy_price"]
            eq  = round(sh * price, 2)
            cost = round(sh * avg, 2)
            result.append({
                "team": team, "side": "long",
                "shares": round(sh, 6), "avg_price": round(avg, 2),
                "current_price": price, "equity": eq,
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
                "shares": round(sh, 6), "avg_price": round(avg_open, 2),
                "current_price": price, "equity": eq,
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
    prices_hist, algo_cur = _get_prices()
    holdings   = _load_holdings()
    balances   = _load_balances()
    all_trades = _load_trades()
    members    = set(list(holdings.keys()) + list(balances.keys()))
    realized_map = _compute_realized_pnl_all(all_trades)

    rows = []
    for member in members:
        liquid = balances.get(member, NBY_START)
        equity = 0.0
        for team, entry in holdings.get(member, {}).items():
            s     = _current_sentiment(team, prices_hist, all_trades)
            price = _market_price(algo_cur.get(team, 0.0), s)
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
    prices_hist, cur = _get_prices()
    all_trades = _load_trades()
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
            price = _market_price(cur.get(team, 0.0),
                                  _current_sentiment(team, prices_hist, all_trades))
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
    trades = [t for t in _load_trades() if t["member"] == member]
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


@router.post("/api/invest/buy")
def buy_shares(body: BuyIn, info: dict = Depends(get_token_info)):
    team = body.team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=400, detail="Unknown team or index")
    if body.nbyen <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    _check_market_open()

    prices_hist, algo_cur = _get_prices()
    ap = algo_cur.get(team)
    if ap is None:
        raise HTTPException(status_code=400, detail="No price data for team")

    all_trades = _load_trades()
    s          = _current_sentiment(team, prices_hist, all_trades)
    price      = _market_price(ap, s)
    game_count = len(prices_hist.get(team, []))
    s_delta    = round(body.nbyen / SENTIMENT_DIVISOR, 8)

    shares = body.nbyen / price
    member = info["name"]
    ts     = datetime.now(timezone.utc).isoformat()

    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, member)
        if balances[member] < body.nbyen:
            raise HTTPException(status_code=400, detail="Insufficient NB¥ balance")
        balances[member] = round(balances[member] - body.nbyen, 2)
        new_balance = balances[member]
        _save_balances(balances)

    _append_ledger([{"ts": ts, "member": member, "delta": -round(body.nbyen, 2),
                     "balance": new_balance, "reason": f"Share purchase: {team}"}])

    with _invest_lock:
        holdings = _load_holdings()
        if holdings.get(member, {}).get(team, {}).get("short", {}).get("shares", 0.0) > 0:
            raise HTTPException(status_code=400, detail=f"Cover your short position in {team} before going long")
        entry = holdings.setdefault(member, {}).setdefault(team, {})
        lp    = entry.get("long", {"shares": 0.0, "avg_buy_price": 0.0})
        total = lp["shares"] + shares
        entry["long"] = {
            "shares":        round(total, 6),
            "avg_buy_price": round((lp["shares"] * lp["avg_buy_price"] + shares * price) / total, 6),
        }
        trades = _load_trades()
        trades.append({"ts": ts, "member": member, "team": team, "action": "buy",
                       "shares": round(shares, 6), "price": price,
                       "nbyen": round(body.nbyen, 2), "balance_after": new_balance,
                       "game_count": game_count, "sentiment_delta": s_delta})
        _save_holdings(holdings)
        _save_trades(trades)

    _discord_trade("buy", member, team, shares, price, round(body.nbyen, 2),
                   new_balance, _load_holdings(), algo_cur)
    return {"team": team, "shares": round(shares, 6), "price": price,
            "algo_price": ap, "sentiment": round(s, 4),
            "nbyen": round(body.nbyen, 2), "balance_after": new_balance}


@router.post("/api/invest/sell")
def sell_shares(body: SellIn, info: dict = Depends(get_token_info)):
    team = body.team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=400, detail="Unknown team or index")
    if body.shares <= 0:
        raise HTTPException(status_code=400, detail="Shares must be positive")
    _check_market_open()

    prices_hist, algo_cur = _get_prices()
    ap = algo_cur.get(team)
    if ap is None:
        raise HTTPException(status_code=400, detail="No price data for team")

    all_trades = _load_trades()
    s          = _current_sentiment(team, prices_hist, all_trades)
    price      = _market_price(ap, s)
    game_count = len(prices_hist.get(team, []))

    member = info["name"]
    ts     = datetime.now(timezone.utc).isoformat()
    nbyen  = round(body.shares * price, 2)
    s_delta = -round(nbyen / SENTIMENT_DIVISOR, 8)

    with _invest_lock:
        holdings = _load_holdings()
        mh       = holdings.get(member, {})
        lp       = mh.get(team, {}).get("long", {})
        held     = lp.get("shares", 0.0)
        if held < body.shares - 1e-9:
            raise HTTPException(status_code=400, detail=f"Only hold {held:.6f} long shares of {team}")
        new_sh = round(held - body.shares, 6)
        if new_sh < 1e-9:
            mh.get(team, {}).pop("long", None)
        else:
            mh[team]["long"]["shares"] = new_sh
        if team in mh and not mh[team]:
            del mh[team]
        if member in holdings and not holdings[member]:
            del holdings[member]
        trades = _load_trades()
        trades.append({"ts": ts, "member": member, "team": team, "action": "sell",
                       "shares": round(body.shares, 6), "price": price,
                       "nbyen": nbyen, "balance_after": None,
                       "game_count": game_count, "sentiment_delta": s_delta})
        _save_holdings(holdings)

    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, member)
        balances[member] = round(balances[member] + nbyen, 2)
        new_balance = balances[member]
        _save_balances(balances)

    trades[-1]["balance_after"] = new_balance
    _save_trades(trades)
    _append_ledger([{"ts": ts, "member": member, "delta": nbyen,
                     "balance": new_balance, "reason": f"Share sale: {team}"}])
    _discord_trade("sell", member, team, body.shares, price, nbyen,
                   new_balance, _load_holdings(), algo_cur)
    return {"team": team, "shares": round(body.shares, 6), "price": price,
            "algo_price": ap, "sentiment": round(s, 4),
            "nbyen": nbyen, "balance_after": new_balance}


@router.post("/api/invest/short")
def short_shares(body: ShortIn, info: dict = Depends(get_token_info)):
    """Open a short position: pay collateral, profit if price falls.
    Max loss = collateral (if price doubles). Cover to close."""
    team = body.team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=400, detail="Unknown team or index")
    if body.nbyen <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    _check_market_open()

    prices_hist, algo_cur = _get_prices()
    ap = algo_cur.get(team)
    if ap is None:
        raise HTTPException(status_code=400, detail="No price data for team")

    all_trades = _load_trades()
    s          = _current_sentiment(team, prices_hist, all_trades)
    price      = _market_price(ap, s)
    game_count = len(prices_hist.get(team, []))
    s_delta    = -round(body.nbyen / SENTIMENT_DIVISOR, 8)

    shares = body.nbyen / price
    member = info["name"]
    ts     = datetime.now(timezone.utc).isoformat()

    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, member)
        if balances[member] < body.nbyen:
            raise HTTPException(status_code=400, detail="Insufficient NB¥ balance")
        balances[member] = round(balances[member] - body.nbyen, 2)
        new_balance = balances[member]
        _save_balances(balances)

    _append_ledger([{"ts": ts, "member": member, "delta": -round(body.nbyen, 2),
                     "balance": new_balance, "reason": f"Short position: {team}"}])

    with _invest_lock:
        holdings = _load_holdings()
        if holdings.get(member, {}).get(team, {}).get("long", {}).get("shares", 0.0) > 0:
            raise HTTPException(status_code=400, detail=f"Sell your long position in {team} before shorting")
        entry = holdings.setdefault(member, {}).setdefault(team, {})
        sp    = entry.get("short", {"shares": 0.0, "avg_open_price": 0.0})
        total = sp["shares"] + shares
        entry["short"] = {
            "shares":          round(total, 6),
            "avg_open_price":  round(
                (sp["shares"] * sp["avg_open_price"] + shares * price) / total, 6
            ),
        }
        trades = _load_trades()
        trades.append({"ts": ts, "member": member, "team": team, "action": "short",
                       "shares": round(shares, 6), "price": price,
                       "nbyen": round(body.nbyen, 2), "balance_after": new_balance,
                       "game_count": game_count, "sentiment_delta": s_delta})
        _save_holdings(holdings)
        _save_trades(trades)

    _discord_trade("short", member, team, shares, price, round(body.nbyen, 2),
                   new_balance, _load_holdings(), algo_cur)
    return {"team": team, "shares": round(shares, 6), "price": price,
            "algo_price": ap, "sentiment": round(s, 4),
            "nbyen": round(body.nbyen, 2), "balance_after": new_balance}


@router.post("/api/invest/cover")
def cover_shares(body: CoverIn, info: dict = Depends(get_token_info)):
    """Close a short position. Receive collateral ± P/L (floored at 0)."""
    team = body.team.upper()
    if team not in VALID_TEAMS and team not in INDEXES:
        raise HTTPException(status_code=400, detail="Unknown team or index")
    if body.shares <= 0:
        raise HTTPException(status_code=400, detail="Shares must be positive")
    _check_market_open()

    prices_hist, algo_cur = _get_prices()
    ap = algo_cur.get(team)
    if ap is None:
        raise HTTPException(status_code=400, detail="No price data for team")

    all_trades = _load_trades()
    s          = _current_sentiment(team, prices_hist, all_trades)
    price      = _market_price(ap, s)
    game_count = len(prices_hist.get(team, []))

    member = info["name"]
    ts     = datetime.now(timezone.utc).isoformat()

    with _invest_lock:
        holdings = _load_holdings()
        mh       = holdings.get(member, {})
        sp       = mh.get(team, {}).get("short", {})
        held     = sp.get("shares", 0.0)
        if held < body.shares - 1e-9:
            raise HTTPException(status_code=400, detail=f"Only hold {held:.6f} short shares of {team}")

        avg_open = sp["avg_open_price"]
        nbyen    = _short_equity(body.shares, avg_open, price)
        s_delta  = round(nbyen / SENTIMENT_DIVISOR, 8)

        new_sh = round(held - body.shares, 6)
        if new_sh < 1e-9:
            mh.get(team, {}).pop("short", None)
        else:
            mh[team]["short"]["shares"] = new_sh
        if team in mh and not mh[team]:
            del mh[team]
        if member in holdings and not holdings[member]:
            del holdings[member]

        trades = _load_trades()
        trades.append({"ts": ts, "member": member, "team": team, "action": "cover",
                       "shares": round(body.shares, 6), "price": price,
                       "nbyen": nbyen, "balance_after": None,
                       "game_count": game_count, "sentiment_delta": s_delta})
        _save_holdings(holdings)

    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, member)
        balances[member] = round(balances[member] + nbyen, 2)
        new_balance = balances[member]
        _save_balances(balances)

    trades[-1]["balance_after"] = new_balance
    _save_trades(trades)
    _append_ledger([{"ts": ts, "member": member, "delta": nbyen,
                     "balance": new_balance, "reason": f"Cover short: {team}"}])
    _discord_trade("cover", member, team, body.shares, price, nbyen,
                   new_balance, _load_holdings(), algo_cur)
    return {"team": team, "shares": round(body.shares, 6), "price": price,
            "algo_price": ap, "sentiment": round(s, 4),
            "avg_open": round(avg_open, 2), "nbyen": nbyen, "balance_after": new_balance}


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

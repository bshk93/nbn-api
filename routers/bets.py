import json
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .constants import (
    DATA_DIR, BETS_FILE, BALANCES_FILE, LEDGER_FILE, TIPS_FILE,
    logger,
)
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, has_role, require_role, require_admin, load_members

router = APIRouter()

NBY_START            = 1000.0
NBY_BOXSCORE_REWARD  = 200.0
NBY_BIO_REWARD       = 10.0
NBY_MAX_WAGER        = 300.0

DISCORD_BETS_WEBHOOK = os.environ.get("DISCORD_BETS_WEBHOOK", "")

_bets_lock     = threading.Lock()
_balances_lock = threading.Lock()
_ledger_lock   = threading.Lock()


# ── Discord helpers ───────────────────────────────────────────────────────────

def _discord_post_bet(embed: dict) -> None:
    try:
        httpx.post(DISCORD_BETS_WEBHOOK, json={"embeds": [embed]}, timeout=5)
    except Exception as exc:
        logger.warning("Discord webhook failed: %s", exc)


def _fmt_nby(amount: float) -> str:
    return f"NB¥{amount:,.2f}"


def _prob_to_american(p: float) -> str:
    if p >= 0.5:
        odds = round(-(p / (1 - p)) * 100)
        return str(odds)
    else:
        odds = round(((1 - p) / p) * 100)
        return f"+{odds}"


def _discord_bet_created(bet: dict) -> None:
    bet_type = bet.get("bet_type", "pool")
    if bet_type == "fixed_odds":
        options_lines = "\n".join(
            f"• {o['label']}  ({_prob_to_american(o['probability'])} / {round(1 / o['probability'], 2)}× payout)"
            for o in bet["options"]
        )
        type_label = "Fixed Odds"
    else:
        options_lines = "\n".join(f"• {o['label']}" for o in bet["options"])
        type_label = "Pool Bet"

    description = bet.get("description", "").strip()
    body = f"**{bet['title']}**"
    if description:
        body += f"\n{description}"
    body += f"\n\n**Type:** {type_label}\n**Options:**\n{options_lines}"
    body += "\n\nPlace your wagers at **[nbn.today/bet](https://nbn.today/bet)**"

    _discord_post_bet({
        "title": "🎲  New Bet Opened",
        "description": body,
        "color": 0x2ecc71,
    })


def _discord_bet_locked(bet: dict) -> None:
    wagers = bet.get("wagers", {})
    option_totals: dict[str, float] = {o["id"]: 0.0 for o in bet["options"]}
    bettor_counts: dict[str, int]   = {o["id"]: 0   for o in bet["options"]}
    for w in wagers.values():
        oid = w["option_id"]
        if oid in option_totals:
            option_totals[oid] = round(option_totals[oid] + w["amount"], 2)
            bettor_counts[oid] += 1
    total_pool = round(sum(option_totals.values()), 2)

    opt_lines = []
    for o in bet["options"]:
        oid   = o["id"]
        amt   = option_totals[oid]
        cnt   = bettor_counts[oid]
        pct   = round(amt / total_pool * 100, 1) if total_pool else 0.0
        bettors_str = f"{cnt} bettor{'s' if cnt != 1 else ''}"
        opt_lines.append(f"• **{o['label']}** — {_fmt_nby(amt)}  ({bettors_str}, {pct}%)")

    description = (
        f"**{bet['title']}**\n\n"
        f"**Total Pool:** {_fmt_nby(total_pool)}\n"
        + "\n".join(opt_lines)
    )

    _discord_post_bet({
        "title": "🔒  Betting Closed — Awaiting Outcome",
        "description": description,
        "color": 0xe67e22,
    })


def _discord_bet_settled(bet: dict) -> None:
    resolution = bet.get("resolution", {})
    voided     = resolution.get("voided", False)
    wagers     = bet.get("wagers", {})
    payouts    = resolution.get("payouts", {})

    win_ids = bet.get("winning_option_ids") or ([bet["winning_option_id"]] if bet.get("winning_option_id") else [])
    win_id_set = set(win_ids)
    winning_label = ", ".join(o["label"] for o in bet["options"] if o["id"] in win_id_set) or "?"

    if voided:
        total_refunded = resolution.get("total_pool", 0.0)
        description = (
            f"**{bet['title']}**\n\n"
            f"No bettors picked the winning option. All wagers have been refunded.\n"
            f"**Total Refunded:** {_fmt_nby(total_refunded)}"
        )
        _discord_post_bet({
            "title": "⚠️  Bet Voided — Full Refunds Issued",
            "description": description,
            "color": 0xe74c3c,
        })
        return

    winner_lines = []
    for member, payout in sorted(payouts.items(), key=lambda x: -x[1]):
        wagered = wagers.get(member, {}).get("amount", 0.0)
        net     = round(payout - wagered, 2)
        net_str = f"+{_fmt_nby(net)}" if net >= 0 else f"-{_fmt_nby(abs(net))}"
        winner_lines.append(f"• **{member}** — received {_fmt_nby(payout)}  ({net_str})")

    if len(winner_lines) > 8:
        winner_lines = winner_lines[:8] + [f"• *(+{len(winner_lines) - 8} more)*"]

    bet_type = bet.get("bet_type", "pool")
    if bet_type == "fixed_odds":
        total_label = "Total Stakes"
        total_amt   = resolution.get("total_pool", 0.0)
    else:
        total_label = "Total Pool"
        total_amt   = resolution.get("total_pool", 0.0)

    description = (
        f"**{bet['title']}**\n\n"
        f"**Result:** ✅ {winning_label}\n"
        f"**{total_label}:** {_fmt_nby(total_amt)}\n\n"
        "**Winners:**\n" + "\n".join(winner_lines)
    )

    _discord_post_bet({
        "title": "🏆  Bet Settled",
        "description": description,
        "color": 0xf1c40f,
    })


# ── Balance/ledger helpers ────────────────────────────────────────────────────

def _load_bets() -> list[dict]:
    return _load_json(BETS_FILE, [])


def _save_bets(bets: list[dict]):
    _save_json(BETS_FILE, bets)


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


def _award_submission_reward(name: str) -> tuple[float, float]:
    """Award NBY_BOXSCORE_REWARD to *name* for submitting a box score.
    Returns (reward, new_balance)."""
    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, name)
        balances[name] = round(balances[name] + NBY_BOXSCORE_REWARD, 2)
        _save_balances(balances)
    ts = datetime.now(timezone.utc).isoformat()
    _append_ledger([{"ts": ts, "member": name, "delta": NBY_BOXSCORE_REWARD,
                     "balance": balances[name], "reason": "Box score submission reward"}])
    return NBY_BOXSCORE_REWARD, balances[name]


def _award_bio_reward(name: str, amount: float) -> tuple[float, float]:
    """Award *amount* to *name* for filling in player bio fields.
    Returns (amount, new_balance)."""
    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, name)
        balances[name] = round(balances[name] + amount, 2)
        _save_balances(balances)
    ts = datetime.now(timezone.utc).isoformat()
    _append_ledger([{"ts": ts, "member": name, "delta": amount,
                     "balance": balances[name], "reason": f"Bio field reward"}])
    return amount, balances[name]


def _award_cleanup_reward(name: str, amount: float, reason: str) -> tuple[float, float]:
    """Award *amount* to *name* for an approved Clean Up the Poo Poo submission.
    Deliberately separate from _award_bio_reward: that one fires off the direct
    curator/rosters edit path (players.py), this one off admin approval of a
    member's submission — crediting the submitter, never whoever's token made
    the approval call. Returns (amount, new_balance)."""
    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, name)
        balances[name] = round(balances[name] + amount, 2)
        _save_balances(balances)
    ts = datetime.now(timezone.utc).isoformat()
    _append_ledger([{"ts": ts, "member": name, "delta": amount,
                     "balance": balances[name], "reason": reason}])
    return amount, balances[name]


def _bet_summary(bet: dict) -> dict:
    wagers = bet.get("wagers", {})
    option_totals: dict[str, float] = {opt["id"]: 0.0 for opt in bet["options"]}
    for w in wagers.values():
        oid = w["option_id"]
        if oid in option_totals:
            option_totals[oid] = round(option_totals[oid] + w["amount"], 2)
    total_pool = round(sum(option_totals.values()), 2)
    result = {**bet, "option_totals": option_totals, "total_pool": total_pool}
    if bet.get("bet_type") == "fixed_odds":
        option_exposure: dict[str, float] = {opt["id"]: 0.0 for opt in bet["options"]}
        for w in wagers.values():
            oid = w["option_id"]
            if oid in option_exposure:
                option_exposure[oid] = round(
                    option_exposure[oid] + w.get("potential_payout", w["amount"]), 2
                )
        result["option_exposure"] = option_exposure
    return result


# ── Pydantic models ───────────────────────────────────────────────────────────

class BetOptionSpec(BaseModel):
    label: str
    probability: float | None = None


class BetCreate(BaseModel):
    title: str
    description: str = ""
    options: list[BetOptionSpec]
    bet_type: str = "pool"


class WagerIn(BaseModel):
    option_id: str
    amount: float


class CloseBetIn(BaseModel):
    winning_option_id: str | None = None
    winning_option_ids: list[str] | None = None


class BalanceAdjustIn(BaseModel):
    member: str
    delta: float
    reason: str = ""


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/bets")
def list_bets():
    return [_bet_summary(b) for b in _load_bets()]


@router.post("/api/bets")
def create_bet(body: BetCreate, info: dict = Depends(require_role("bookie"))):
    if body.bet_type not in ("pool", "fixed_odds"):
        raise HTTPException(status_code=422, detail="bet_type must be 'pool' or 'fixed_odds'")
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title is required")

    opts = []
    for o in body.options:
        if not o.label.strip():
            continue
        opt: dict = {"id": secrets.token_hex(4), "label": o.label.strip()}
        if body.bet_type == "fixed_odds":
            if o.probability is None:
                raise HTTPException(status_code=422, detail=f"probability required for all options in a fixed_odds bet")
            if not (0.01 <= o.probability <= 0.99):
                raise HTTPException(status_code=422, detail="each probability must be between 0.01 and 0.99")
            opt["probability"] = round(o.probability, 6)
        opts.append(opt)

    if len(opts) < 2:
        raise HTTPException(status_code=422, detail="At least 2 non-empty options required")

    with _bets_lock:
        bets = _load_bets()
        bet: dict = {
            "id": secrets.token_hex(8),
            "title": body.title.strip(),
            "description": body.description.strip(),
            "bet_type": body.bet_type,
            "options": opts,
            "status": "open",
            "created_by": info["name"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "winning_option_id": None,
            "wagers": {},
            "resolution": None,
        }
        bets.append(bet)
        _save_bets(bets)
    log_write(info, f"POST bets ({body.bet_type}) — {body.title!r} ({len(opts)} options)")
    _discord_bet_created(bet)
    return _bet_summary(bet)


@router.post("/api/bets/{bet_id}/wager")
def place_wager(bet_id: str, body: WagerIn, info: dict = Depends(get_token_info)):
    if body.amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    with _bets_lock, _balances_lock:
        bets     = _load_bets()
        bet      = next((b for b in bets if b["id"] == bet_id), None)
        if bet is None:
            raise HTTPException(status_code=404, detail="Bet not found")
        if bet["status"] != "open":
            raise HTTPException(status_code=409, detail="Bet is not open for wagering")
        opt_ids = {o["id"] for o in bet["options"]}
        if body.option_id not in opt_ids:
            raise HTTPException(status_code=422, detail="Invalid option_id")
        existing = bet["wagers"].get(info["name"])
        if existing and existing["option_id"] != body.option_id:
            raise HTTPException(status_code=409, detail="You have already backed a different option in this bet")
        already_staked = existing["amount"] if existing else 0.0
        if round(already_staked + body.amount, 2) > NBY_MAX_WAGER:
            remaining = round(NBY_MAX_WAGER - already_staked, 2)
            raise HTTPException(
                status_code=422,
                detail=f"Maximum stake per bet is NB¥{NBY_MAX_WAGER:.0f} — you have NB¥{already_staked:.2f} on this bet, so you can add at most NB¥{remaining:.2f} more",
            )
        balances = _load_balances()
        _init_bal(balances, info["name"])
        if balances[info["name"]] < body.amount:
            raise HTTPException(
                status_code=422,
                detail=f"Insufficient balance — NB¥{balances[info['name']]:.2f} available",
            )
        balances[info["name"]] = round(balances[info["name"]] - body.amount, 2)
        _save_balances(balances)
        ts = datetime.now(timezone.utc).isoformat()
        _append_ledger([{"ts": ts, "member": info["name"], "delta": -body.amount,
                         "balance": balances[info["name"]],
                         "reason": f"Wager on \"{bet['title']}\""}])
        if bet.get("bet_type") == "fixed_odds":
            opt    = next(o for o in bet["options"] if o["id"] == body.option_id)
            prob   = opt["probability"]
            new_pp = round(body.amount / prob, 2)
            if existing:
                bet["wagers"][info["name"]]["amount"]           = round(existing["amount"] + body.amount, 2)
                bet["wagers"][info["name"]]["potential_payout"] = round(
                    existing.get("potential_payout", 0.0) + new_pp, 2
                )
            else:
                bet["wagers"][info["name"]] = {
                    "option_id":        body.option_id,
                    "amount":           body.amount,
                    "potential_payout": new_pp,
                    "placed_at":        datetime.now(timezone.utc).isoformat(),
                }
        else:
            if existing:
                bet["wagers"][info["name"]]["amount"] = round(existing["amount"] + body.amount, 2)
            else:
                bet["wagers"][info["name"]] = {
                    "option_id": body.option_id,
                    "amount": body.amount,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                }
        _save_bets(bets)
    log_write(info, f"POST bets/{bet_id}/wager — NB¥{body.amount} on {body.option_id}")
    return _bet_summary(bet)


@router.post("/api/bets/{bet_id}/lock")
def lock_bet(bet_id: str, info: dict = Depends(require_role("bookie"))):
    with _bets_lock:
        bets = _load_bets()
        bet  = next((b for b in bets if b["id"] == bet_id), None)
        if bet is None:
            raise HTTPException(status_code=404, detail="Bet not found")
        if bet["status"] != "open":
            raise HTTPException(status_code=409, detail="Bet is not open")
        bet["status"]    = "locked"
        bet["locked_at"] = datetime.now(timezone.utc).isoformat()
        _save_bets(bets)
    log_write(info, f"POST bets/{bet_id}/lock")
    _discord_bet_locked(bet)
    return _bet_summary(bet)


@router.post("/api/bets/{bet_id}/close")
def close_bet(bet_id: str, body: CloseBetIn, info: dict = Depends(require_role("bookie"))):
    with _bets_lock, _balances_lock:
        bets = _load_bets()
        bet  = next((b for b in bets if b["id"] == bet_id), None)
        if bet is None:
            raise HTTPException(status_code=404, detail="Bet not found")
        if bet["status"] not in ("open", "locked"):
            raise HTTPException(status_code=409, detail="Bet is already closed")

        # Normalize to a list of winning IDs
        if body.winning_option_ids:
            win_ids = list(dict.fromkeys(body.winning_option_ids))
        elif body.winning_option_id:
            win_ids = [body.winning_option_id]
        else:
            raise HTTPException(status_code=422, detail="winning_option_id or winning_option_ids is required")
        opt_id_set = {o["id"] for o in bet["options"]}
        for wid in win_ids:
            if wid not in opt_id_set:
                raise HTTPException(status_code=422, detail=f"Invalid winning option id: {wid}")
        win_ids_set = set(win_ids)

        wagers   = bet["wagers"]
        balances = _load_balances()
        payouts: dict[str, float] = {}

        if bet.get("bet_type") == "fixed_odds":
            total_stakes   = round(sum(w["amount"] for w in wagers.values()), 2)
            winners_stakes = round(
                sum(w["amount"] for w in wagers.values() if w["option_id"] in win_ids_set),
                2,
            )
            total_paid_out = 0.0

            ledger_entries = []
            ts = datetime.now(timezone.utc).isoformat()
            for member, w in wagers.items():
                if w["option_id"] in win_ids_set:
                    payout = w.get("potential_payout", w["amount"])
                    _init_bal(balances, member)
                    balances[member] = round(balances[member] + payout, 2)
                    payouts[member]  = payout
                    total_paid_out   = round(total_paid_out + payout, 2)
                    ledger_entries.append({"ts": ts, "member": member, "delta": payout,
                                           "balance": balances[member],
                                           "reason": f"Payout (fixed odds): \"{bet['title']}\"" })

            net_shortfall = round(total_paid_out - total_stakes, 2)
            _save_balances(balances)
            if ledger_entries:
                _append_ledger(ledger_entries)
            bet.update(
                status="closed",
                closed_at=datetime.now(timezone.utc).isoformat(),
                winning_option_id=win_ids[0],
                winning_option_ids=win_ids,
                resolution={
                    "total_pool":     total_stakes,
                    "winners_pool":   winners_stakes,
                    "total_paid_out": total_paid_out,
                    "net_shortfall":  net_shortfall,
                    "voided":         False,
                    "payouts":        payouts,
                },
            )
            _save_bets(bets)
            log_write(info, f"POST bets/{bet_id}/close (fixed_odds) — winners={win_ids}, stakes=NB¥{total_stakes}, paid=NB¥{total_paid_out:.2f}, shortfall=NB¥{net_shortfall:.2f}")
            _discord_bet_settled(bet)
            return _bet_summary(bet)

        # Pool bet
        total_pool   = round(sum(w["amount"] for w in wagers.values()), 2)
        winners_pool = round(
            sum(w["amount"] for w in wagers.values() if w["option_id"] in win_ids_set),
            2,
        )
        voided = winners_pool == 0

        ledger_entries = []
        ts = datetime.now(timezone.utc).isoformat()
        if voided:
            for member, w in wagers.items():
                _init_bal(balances, member)
                balances[member] = round(balances[member] + w["amount"], 2)
                payouts[member]  = w["amount"]
                ledger_entries.append({"ts": ts, "member": member, "delta": w["amount"],
                                       "balance": balances[member],
                                       "reason": f"Refund (voided): \"{bet['title']}\"" })
        else:
            for member, w in wagers.items():
                if w["option_id"] in win_ids_set:
                    payout = round((w["amount"] / winners_pool) * total_pool, 2)
                    _init_bal(balances, member)
                    balances[member] = round(balances[member] + payout, 2)
                    payouts[member]  = payout
                    ledger_entries.append({"ts": ts, "member": member, "delta": payout,
                                           "balance": balances[member],
                                           "reason": f"Payout (pool): \"{bet['title']}\"" })

        _save_balances(balances)
        if ledger_entries:
            _append_ledger(ledger_entries)
        bet.update(
            status="closed",
            closed_at=datetime.now(timezone.utc).isoformat(),
            winning_option_id=win_ids[0],
            winning_option_ids=win_ids,
            resolution={
                "total_pool":   total_pool,
                "winners_pool": winners_pool,
                "voided":       voided,
                "payouts":      payouts,
            },
        )
        _save_bets(bets)

    log_write(info, f"POST bets/{bet_id}/close — winners={win_ids}, pool=NB¥{total_pool}")
    _discord_bet_settled(bet)
    return _bet_summary(bet)


@router.delete("/api/bets/{bet_id}")
def delete_bet(bet_id: str, info: dict = Depends(require_role("bookie"))):
    with _bets_lock, _balances_lock:
        bets = _load_bets()
        bet  = next((b for b in bets if b["id"] == bet_id), None)
        if bet is None:
            raise HTTPException(status_code=404, detail="Bet not found")
        if bet["status"] not in ("open", "locked"):
            raise HTTPException(status_code=409, detail="Cannot delete a closed bet")
        if bet["wagers"] and not has_role(info, "admin"):
            raise HTTPException(status_code=409, detail="Cannot delete a bet that has wagers — close it instead")
        if bet["wagers"]:
            balances = _load_balances()
            ledger_entries = []
            ts = datetime.now(timezone.utc).isoformat()
            for member, w in bet["wagers"].items():
                _init_bal(balances, member)
                balances[member] = round(balances[member] + w["amount"], 2)
                ledger_entries.append({"ts": ts, "member": member, "delta": w["amount"],
                                       "balance": balances[member],
                                       "reason": f"Refund (deleted): \"{bet['title']}\"" })
            _save_balances(balances)
            _append_ledger(ledger_entries)
        _save_bets([b for b in bets if b["id"] != bet_id])
    log_write(info, f"DELETE bets/{bet_id}")
    return {"ok": True}


@router.get("/api/bets/balance/{member}")
def get_member_balance(member: str):
    all_members = load_members()
    if member not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{member}' not found")
    balances = _load_balances()
    _init_bal(balances, member)
    return {"name": member, "balance": balances[member]}


def _compute_bet_stats(member: str, bets: list, tips: list) -> dict:
    wager_count = 0
    win_count = 0
    net_pnl = 0.0
    max_odds_win = 0.0
    bet_results: list[dict] = []  # {ts, won} for streak calc

    for bet in bets:
        if bet["status"] != "closed":
            continue
        wager = bet.get("wagers", {}).get(member)
        if not wager:
            continue
        resolution = bet.get("resolution", {})
        if resolution.get("voided", False):
            continue

        win_ids = set(bet.get("winning_option_ids") or
                      ([bet["winning_option_id"]] if bet.get("winning_option_id") else []))
        won = wager["option_id"] in win_ids
        payout = resolution.get("payouts", {}).get(member, 0.0)

        wager_count += 1
        net_pnl = round(net_pnl + payout - wager["amount"], 2)
        bet_results.append({"ts": bet.get("closed_at", ""), "won": won})

        if won:
            win_count += 1
            if bet.get("bet_type") == "fixed_odds" and wager["amount"] > 0:
                pp = wager.get("potential_payout", 0.0)
                max_odds_win = max(max_odds_win, round(pp / wager["amount"], 2))
            elif bet.get("bet_type") != "fixed_odds" and wager["amount"] > 0:
                max_odds_win = max(max_odds_win, round(payout / wager["amount"], 2))

    bet_results.sort(key=lambda x: x["ts"])
    best_streak = 0
    cur_streak = 0
    for r in bet_results:
        if r["won"]:
            cur_streak += 1
            best_streak = max(best_streak, cur_streak)
        else:
            cur_streak = 0

    tips_sent = round(sum(t["amount"] for t in tips if t.get("from") == member), 2)

    return {
        "wager_count": wager_count,
        "win_count": win_count,
        "net_pnl": net_pnl,
        "best_streak": best_streak,
        "max_odds_win": round(max_odds_win, 2),
        "tips_sent": tips_sent,
    }


@router.get("/api/bets/stats")
def get_all_bet_stats():
    """Bet stats for every member in one pass (used by the members list)."""
    all_members = load_members()
    bets = _load_bets()
    tips = _load_json(TIPS_FILE, [])
    return {m: _compute_bet_stats(m, bets, tips) for m in all_members}


@router.get("/api/bets/stats/{member}")
def get_member_bet_stats(member: str):
    all_members = load_members()
    if member not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{member}' not found")
    bets = _load_bets()
    tips = _load_json(TIPS_FILE, [])
    return _compute_bet_stats(member, bets, tips)


@router.get("/api/bets/balances")
def get_bets_balances():
    all_members = load_members()
    balances    = _load_balances()
    for name in all_members:
        _init_bal(balances, name)

    breakdown: dict[str, dict] = {}
    def _bd(name: str) -> dict:
        if name not in breakdown:
            breakdown[name] = {"bet_won": 0.0, "bet_lost": 0.0, "bet_placed": 0.0,
                               "stats_earned": 0.0, "trivia_earned": 0.0, "bio_earned": 0.0,
                               "bet_pnl": 0.0}
        return breakdown[name]

    if LEDGER_FILE.exists():
        ledger = json.loads(LEDGER_FILE.read_text())
        for entry in ledger:
            name   = entry.get("member", "")
            delta  = entry.get("delta", 0.0)
            reason = entry.get("reason", "")
            bd = _bd(name)
            if reason.startswith("Payout"):
                bd["bet_won"] += delta
            elif "Box score" in reason:
                bd["stats_earned"] += delta
            elif reason.startswith("Trivia"):
                bd["trivia_earned"] += delta
            elif "Bio field" in reason:
                bd["bio_earned"] += delta

    for bet in _load_bets():
        wagers = bet.get("wagers", {})
        if bet["status"] in ("open", "locked"):
            for member, w in wagers.items():
                _bd(member)["bet_placed"] = round(_bd(member)["bet_placed"] + w["amount"], 2)
        elif bet["status"] == "closed":
            resolution = bet.get("resolution", {})
            if not resolution.get("voided", False):
                payouts = resolution.get("payouts", {})
                for member, w in wagers.items():
                    stake = w["amount"]
                    if member not in payouts:
                        _bd(member)["bet_lost"] = round(_bd(member)["bet_lost"] + stake, 2)
                        _bd(member)["bet_pnl"]  = round(_bd(member)["bet_pnl"]  - stake, 2)
                    else:
                        payout = payouts[member]
                        _bd(member)["bet_pnl"] = round(_bd(member)["bet_pnl"] + payout - stake, 2)

    result = []
    for n, b in balances.items():
        bd = _bd(n)
        result.append({
            "name":          n,
            "balance":       round(b, 2),
            "bet_won":       round(bd["bet_won"], 2),
            "bet_lost":      round(bd["bet_lost"], 2),
            "bet_placed":    round(bd["bet_placed"], 2),
            "stats_earned":  round(bd["stats_earned"], 2),
            "trivia_earned": round(bd["trivia_earned"], 2),
            "bio_earned":    round(bd["bio_earned"], 2),
            "bet_pnl":       round(bd["bet_pnl"], 2),
        })
    return sorted(result, key=lambda x: (-(x["balance"] + x["bet_placed"]), x["name"]))


@router.get("/api/bets/ledger")
def get_bets_ledger(info: dict = Depends(require_admin)):
    if not LEDGER_FILE.exists():
        return []
    ledger = json.loads(LEDGER_FILE.read_text())
    return list(reversed(ledger))


@router.post("/api/bets/admin/adjust")
def admin_adjust_balance(body: BalanceAdjustIn, info: dict = Depends(require_admin)):
    if body.delta == 0:
        raise HTTPException(status_code=422, detail="delta cannot be zero")
    all_members = load_members()
    if body.member not in all_members:
        raise HTTPException(status_code=404, detail=f"Member '{body.member}' not found")
    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, body.member)
        old_bal = balances[body.member]
        new_bal = round(old_bal + body.delta, 2)
        if new_bal < 0:
            raise HTTPException(
                status_code=422,
                detail=f"Would result in negative balance (NB¥{new_bal:.2f}); current balance is NB¥{old_bal:.2f}",
            )
        balances[body.member] = new_bal
        _save_balances(balances)
    ts = datetime.now(timezone.utc).isoformat()
    reason_str = f"Admin adjustment: {body.reason}" if body.reason else "Admin adjustment"
    _append_ledger([{"ts": ts, "member": body.member, "delta": body.delta,
                     "balance": new_bal, "reason": reason_str}])
    verb = "gave" if body.delta > 0 else "took"
    log_write(info, f"POST bets/admin/adjust — {verb} NB¥{abs(body.delta)} {'to' if body.delta > 0 else 'from'} {body.member}" + (f" ({body.reason})" if body.reason else ""))
    return {"member": body.member, "old_balance": old_bal, "new_balance": new_bal, "delta": body.delta, "reason": body.reason}

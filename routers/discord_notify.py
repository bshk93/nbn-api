"""Post every live transaction to Discord as it happens.

Wired into the two *live* submit paths in `transactions.py` — `POST
/api/transactions` and `POST /api/self/renounce` — and deliberately **not** into
`_append_transaction` itself. That function is also the append path for
`_append_historical`, and 1,935 of the ledger's 2,241 entries are backfill: a
re-run of the Discord import would fire ~2,000 messages and rate-limit the bot.
Notification is opt-in per call site so a future append path has to ask for it
rather than inheriting it by accident.

Uses the same `DISCORD_BOT_TOKEN` and channel-post endpoint as
`misc._notify_join_discord`. Set `DISCORD_TXN_CHANNEL` to the target channel id;
with it unset the whole module is a no-op, so this is safe to deploy before the
channel exists.

Delivery — pacing, retry, the queue-depth backstop and the burst cap — lives in
`discord_transport`, shared with `fa_notify` so the two feeds can't each pace
themselves correctly and still collectively exceed Discord's rate limit. What
stays here is policy: what a transaction announcement says, and how big a burst
this particular channel should tolerate.

Everything here is best-effort and off the request thread. A transaction is a
real roster write that has already been committed by the time we post — Discord
being down, slow, or misconfigured must never fail it, delay it, or roll it back.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from . import discord_transport as transport
from .players import load_player_bios, _display_name

logger = logging.getLogger(__name__)

DISCORD_TXN_CHANNEL = os.environ.get("DISCORD_TXN_CHANNEL", "").strip()

SITE = "https://nbn.today"

TYPE_LABELS = {
    "sign": "Signing", "offer_sheet": "Offer Sheet", "pick": "Draft Pick",
    "offer_sheet_decision": "Offer Sheet Decision",
    "sign_pick": "Pick Signing", "option": "Option", "guarantee": "Guarantee",
    "release": "Release", "renounce": "Renounce",
    "rescind_renounce": "Renounce Rescinded", "trade": "Trade",
    "convert_twoway": "Two-Way Conversion", "void_player": "Void Player",
    "set_hard_cap_level": "Hard Cap",
}

# Mirrors the badge colours on /transactions so the channel reads the same way
# the log does.
TYPE_COLORS = {
    "sign": 0x22C55E, "pick": 0x60A5FA, "sign_pick": 0x60A5FA,
    "option": 0xFB923C, "guarantee": 0x2DD4BF, "release": 0xEF4444,
    "renounce": 0xFCD34D, "rescind_renounce": 0xFCD34D, "trade": 0xC084FC,
    "convert_twoway": 0xA8A29E, "void_player": 0x9CA3AF,
    "set_hard_cap_level": 0xD4AF37, "offer_sheet": 0xA5B4FC,
    "offer_sheet_decision": 0xA5B4FC,
}

METHOD_LABELS = {
    "bird_rights": "Bird Rights", "mle": "MLE", "ntmle": "NTMLE", "tmle": "TMLE",
    "minimum": "Minimum", "room_exception": "Room Exc.", "bae": "BAE",
    "sign_and_trade": "S&T",
}

LEVEL_LABELS = {
    "first_apron": "First Apron", "second_apron": "Second Apron",
    "default": "Default (cleared)",
}


def _dollars(v) -> int:
    return int("".join(c for c in str(v) if c.isdigit()) or 0)


# Mirrors teams/team.js `CONTRACT_TAGS` / `summarizeContract` so a deal reads the
# same in Discord as it does on the roster page. Divergent shorthand for the same
# contract is worse than no shorthand.
CONTRACT_TAGS = {"PLAYER_OPT": "PO", "TEAM_OPT": "TO", "NON_GTD": "NG"}
TAG_WORDS     = {"PLAYER_OPT": "player option", "TEAM_OPT": "team option",
                 "NON_GTD": "non-guaranteed"}
_FA_HOLDS     = ("UFA", "RFA")
_YEAR_RE      = re.compile(r"^\d{2}-\d{2}$")


def _contract_parts(contract: dict) -> tuple[list[tuple[str, str, Optional[str]]],
                                             Optional[tuple[str, str, str]]]:
    """Split a contract into ``(deal_years, trailing_hold)``.

    A UFA/RFA line is a cap hold, not a contract year — it's what the deal rolls
    *into*, so it ends the deal rather than counting as part of it (same rule
    `summarizeContract` applies on the roster page).
    """
    sals  = (contract or {}).get("salaries") or {}
    holds = (contract or {}).get("cap_holds") or {}
    deal: list[tuple[str, str, Optional[str]]] = []
    for y in sorted(y for y in sals if _YEAR_RE.match(y)):
        if holds.get(y) in _FA_HOLDS:
            break
        deal.append((y, sals[y], holds.get(y)))
    trailing = None
    for y in sorted(y for y in holds if _YEAR_RE.match(y)):
        if holds[y] in _FA_HOLDS and (not deal or y > deal[-1][0]):
            trailing = (y, holds[y], sals.get(y, ""))
            break
    return deal, trailing


def _contract_str(contract: dict) -> str:
    """Headline shape: guaranteed years, then option/non-guaranteed runs, then
    total — "2+1 PO · $25.0M"."""
    deal, trailing = _contract_parts(contract)
    if not deal:
        if (contract or {}).get("type") == "two-way":
            return "Two-Way"
        if trailing:
            # Nothing but a cap hold. A signing always has real years so this is
            # rare here, but the roster page's summarizeContract renders it and
            # a blank line reads as "no contract found" rather than "hold only".
            _y, kind, amt = trailing
            val = _dollars(amt)
            # Placeholder holds carry a nominal $1 — worth hiding, not showing.
            return f"{kind} hold · ${val / 1e6:.1f}M" if val >= 1000 else f"{kind} hold"
        return ""

    base = 0
    while base < len(deal) and not deal[base][2]:
        base += 1
    extras, i = [], base
    while i < len(deal):
        tag, n = deal[i][2], 0
        while i < len(deal) and deal[i][2] == tag:
            n, i = n + 1, i + 1
        extras.append(f"{n} {CONTRACT_TAGS[tag]}" if tag else str(n))

    if not extras:
        yrs = f"{base} yr" if base == 1 else f"{base} yrs"
    elif base == 0:
        yrs = "+".join(extras)
    else:
        yrs = f"{base}+" + "+".join(extras)

    total = sum(_dollars(a) for _, a, _ in deal)
    return f"{yrs} · ${total / 1e6:.1f}M" if total else yrs


def _contract_breakdown(contract: dict) -> str:
    """Year-by-year table, as a Discord code block so the columns actually line
    up. The headline summary collapses a deal to its shape; this is where you
    check the individual figures and which years carry an option."""
    deal, trailing = _contract_parts(contract)
    if not deal and not trailing:
        return ""
    rows = [(y, f"${_dollars(a):,}", TAG_WORDS.get(tag, "")) for y, a, tag in deal]
    if trailing:
        y, kind, amt = trailing
        val = _dollars(amt)
        # Placeholder holds carry a nominal $1 — a figure worth hiding, not showing.
        rows.append((y, f"${val:,}" if val >= 1000 else "—", f"{kind} hold"))
    w = max(len(r[1]) for r in rows)
    body = "\n".join(
        f"{y}  {amt:>{w}}" + (f"   {lbl}" if lbl else "") for y, amt, lbl in rows
    )
    return f"```\n{body}\n```"


def _method_str(details: dict) -> str:
    method = details.get("signing_method")
    if not method or method == "cap_space":
        return ""
    s = " · " + METHOD_LABELS.get(method, method)
    if details.get("bird_rights_type"):
        s += f" ({details['bird_rights_type']})"
    return s


def _player_name(slug: str, bios: dict) -> str:
    if not slug:
        return ""
    return _display_name((bios.get(slug) or {}).get("name", "")) or slug


def _offer_sheet_sides(d: dict) -> tuple[str, str]:
    """``(offering_team, retaining_team)``. The stored `teams` list is in that
    order; `offering_team` is the authoritative field when present."""
    teams = list(d.get("teams") or [])
    offering = (d.get("offering_team") or (teams[0] if teams else "")).upper()
    retaining = next((t for t in teams if t.upper() != offering), "")
    return offering, retaining.upper()


def _teams(txn: dict) -> list[str]:
    d = txn.get("details") or {}
    if txn.get("type") in ("trade", "offer_sheet", "offer_sheet_decision"):
        return list(d.get("teams") or [])
    return [d["team"]] if d.get("team") else []


def _headline_team(txn: dict) -> str:
    """The team the title leads with. For an offer sheet that's whoever actually
    ends up with the player, so the header never implies the wrong destination."""
    d = txn.get("details") or {}
    if txn.get("type") in ("offer_sheet", "offer_sheet_decision"):
        offering, retaining = _offer_sheet_sides(d)
        outcome = d.get("outcome")
        if not outcome:
            # Still pending — nobody has "got" the player yet. Lead with the
            # incumbent, whose decision the league is waiting on.
            return retaining or offering or ""
        return (retaining if outcome == "matched" else offering) or ""
    return ""


def _describe(txn: dict, bios: dict) -> str:
    """One-line summary of what the transaction did — the same content
    `renderDetailsCell` shows on /transactions, so the channel and the log never
    tell different stories about the same move."""
    t, d = txn.get("type"), (txn.get("details") or {})

    if t in ("sign", "pick"):
        contract = _contract_str(d.get("contract") or {})
        if t == "pick" and d.get("pick"):
            p = d["pick"]
            num = f" #{p['pick_number']}" if p.get("pick_number") else ""
            pick_str = f"{p.get('year')} R{p.get('round')} {p.get('orig')}{num}"
            return pick_str + (f" · {contract}" if contract else "")
        return contract + _method_str(d)

    if t in ("convert_twoway", "sign_pick"):
        s = _contract_str(d.get("contract") or {})
        if t == "convert_twoway":
            s += _method_str(d)
        return s

    if t == "option":
        label = "Player Opt" if d.get("option_type") == "PLAYER_OPT" else "Team Opt"
        if d.get("decision") == "accept":
            return f"{d.get('year', '')} {label} — accepted"
        return f"{d.get('year', '')} {label} — declined → {d.get('cap_hold_type') or 'UFA'} hold"

    if t == "guarantee":
        return f"{d.get('year', '')} fully guaranteed"

    if t == "release":
        dead = d.get("dead_cap") or {}
        if not dead:
            return "No dead cap"
        total = sum(_dollars(v) for v in dead.values())
        stretch = f" · stretched over {d['stretch_years']}yr" if d.get("stretch_years") else ""
        return f"Dead cap: ${total / 1e6:.1f}M ({', '.join(dead)}){stretch}"

    if t == "renounce":
        return "Renounced — free agent, no dead cap"

    if t == "rescind_renounce":
        return f"Restored to {d.get('team', '')} — cap hold and contract terms reinstated"

    if t in ("offer_sheet", "offer_sheet_decision"):
        # `teams` is stored [offering, retaining]. Joining them with an arrow
        # said the opposite of what happened on a non-match: the player leaves
        # for the *offering* team, not the incumbent. State the outcome in words
        # and name the destination explicitly instead.
        offering, retaining = _offer_sheet_sides(d)
        contract = _contract_str(d.get("contract") or {})
        head = f"{contract}{_method_str(d)}"
        outcome = d.get("outcome")

        if not outcome:
            # An offer that's been extended but not answered. Say what happens
            # next, because a pending offer is the one thing in this feed that
            # is waiting on somebody.
            line = (f"Offered by **{offering}** — {retaining} has 48 hours to match"
                    + (f" (by {d['deadline']})" if d.get("deadline") else ""))
        elif outcome == "matched":
            line = f"**{retaining} matched** {offering}'s offer — stays with {retaining}"
        else:
            line = f"**Not matched** by {retaining} — signs with {offering}"
        return f"{head}\n{line}" if head else line

    if t == "void_player":
        return f"Voided — {d['reason']}" if d.get("reason") else "Voided — no cap hit"

    if t == "set_hard_cap_level":
        level = LEVEL_LABELS.get(d.get("level"), d.get("level", ""))
        return f"{level} — {d['reason']}" if d.get("reason") else level

    if t == "trade":
        # Grouped by team rather than by leg, so a team appearing in several legs
        # of a 3-way shows one consolidated block — same as the log.
        moves: dict[str, list[str]] = {}
        for leg in d.get("transfers") or []:
            frm, to = leg.get("from_team"), leg.get("to_team")
            for a in leg.get("assets") or []:
                if a.get("type") == "player":
                    label = _player_name(a.get("slug"), bios)
                elif a.get("type") == "pick":
                    label = f"{a.get('year')} R{a.get('round')} {a.get('orig')}"
                    mods = []
                    if a.get("protection"):
                        mods.append(f"top-{a['protection']} prot.")
                    if a.get("swap_with"):
                        mods.append(f"swap w/ {a['swap_with']}")
                    if mods:
                        label += f" ({', '.join(mods)})"
                else:
                    continue
                if not label:
                    continue
                moves.setdefault(to, []).append(f"← {label} (from {frm})")
        return "\n".join(
            f"**{tm}** receives:\n" + "\n".join(f"　{ln}" for ln in lines)
            for tm, lines in sorted(moves.items())
        ) or "No assets recorded"

    return ""


def build_embed(txn: dict, forced_checks: Optional[list[str]] = None) -> dict:
    bios = load_player_bios()
    t = txn.get("type", "")
    d = txn.get("details") or {}
    teams = _teams(txn)

    title = TYPE_LABELS.get(t, t)
    lead = _headline_team(txn) or (teams[0] if len(teams) == 1 else "")
    if lead:
        title = f"{lead} — {title}"
    elif teams:
        title = f"{' · '.join(teams)} — {title}"

    fields = []
    if t != "trade" and d.get("player"):
        name = _player_name(d["player"], bios)
        fields.append({
            "name": "Player",
            "value": f"[{name}]({SITE}/players/?p={d['player']})",
            "inline": True,
        })
    if teams:
        if t in ("offer_sheet", "offer_sheet_decision"):
            offering, retaining = _offer_sheet_sides(d)
            value = (f"[{offering}]({SITE}/teams/{offering}) offering · "
                     f"[{retaining}]({SITE}/teams/{retaining}) incumbent")
        else:
            value = " · ".join(f"[{tm}]({SITE}/teams/{tm})" for tm in teams)
        fields.append({
            "name": "Team" if len(teams) == 1 else "Teams",
            "value": value,
            "inline": True,
        })

    # Year-by-year figures for anything carrying a contract. The headline gives
    # the shape ("2+1 PO · $25.0M"); this is where the actual numbers and which
    # years are optional get checked.
    if t in ("sign", "offer_sheet", "offer_sheet_decision", "sign_pick", "convert_twoway", "pick"):
        breakdown = _contract_breakdown(d.get("contract") or {})
        if breakdown:
            fields.append({"name": "Year by year", "value": breakdown, "inline": False})

    description = _describe(txn, bios)
    if txn.get("description"):
        description += f"\n\n*{txn['description']}*"

    # A forced transaction overrode a rulebook check that failed. That is already
    # in the ledger as _forced_checks and visible on /transactions; saying it here
    # keeps the channel a faithful record rather than a flattering one.
    if forced_checks:
        fields.append({
            "name": "⚠️ Overridden checks",
            "value": ", ".join(f"`{c}`" for c in forced_checks),
            "inline": False,
        })

    who = txn.get("created_by") or "unknown"
    if d.get("_source") == "owner_self_serve":
        who += " (team owner)"
    footer = f"{who} · {txn.get('date', '')}"

    return {
        "title": title,
        "description": description or None,
        "color": 0xF59E0B if forced_checks else TYPE_COLORS.get(t, 0x64748B),
        "fields": fields,
        "url": f"{SITE}/transactions",
        "footer": {"text": footer},
    }


# ── Anti-flood guards ─────────────────────────────────────────────────────────
# Three independent gates, because "don't dump the backlog into the channel" is a
# hard requirement and one convention-based check is not a guarantee. Any single
# gate alone would prevent a mass post; all three have to be defeated at once.
#
#   1. Call-site opt-in — only the two live submit paths call this at all. The
#      historical/backfill append path doesn't, and there is no startup, replay,
#      migration or scheduled hook that iterates the ledger and notifies.
#   2. Freshness — a transaction whose `created_at` is older than
#      MAX_AGE_SECONDS is never announced. This is what makes replaying old
#      entries structurally silent, whatever the caller intended.
#   3. Burst cap — at most MAX_BURST messages per BURST_WINDOW seconds on this
#      channel. A runaway loop posts MAX_BURST times and then goes quiet with one
#      log line, instead of emptying 2,000 rows into the channel. Enforced by
#      `discord_transport`, sized here.
#
# Sizing gate 3 is a real trade-off, so it's set against measured activity rather
# than a guess. Ledger history: busiest single day 52 live transactions
# (2026-06-21), tightest actual 10-minute burst 19. Draft day is expected to beat
# both — ~30 pick signings plus trades, 50+ total. The cap therefore has to clear
# 50-in-a-sitting comfortably while still stopping a 2,000-row loop dead. An
# earlier value of 20/5min would have clipped that real 19-transaction burst.
MAX_AGE_SECONDS = 300          # 5 min; a live submit posts within milliseconds
MAX_BURST       = 250          # ~5x the busiest real day, ~13x the tightest real burst
BURST_WINDOW    = 900          # seconds


def _is_fresh(txn: dict) -> bool:
    """Only announce something that just happened. A ledger entry being handed to
    us long after it was created means a replay, not a submission."""
    stamp = txn.get("created_at")
    if not stamp:
        return True   # live paths always set it; absence isn't evidence of a replay
    try:
        created = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - created).total_seconds() <= MAX_AGE_SECONDS


def notify_transaction(txn: dict, forced_checks: Optional[list[str]] = None) -> None:
    """Announce one just-submitted transaction. Fire-and-forget: returns
    immediately, the POST runs on a daemon thread.

    Never raises — the caller has already written the roster and appended the
    ledger entry, and a notification problem must not surface as a failed
    transaction.
    """
    if not transport.configured(DISCORD_TXN_CHANNEL):
        return
    if (txn.get("details") or {}).get("historical"):
        return
    if not _is_fresh(txn):
        logger.info("Discord notify skipped for %s — not a fresh submission", txn.get("id"))
        return
    # Built lazily, so a suppressed burst doesn't reload every player bio per
    # refused message.
    transport.send(DISCORD_TXN_CHANNEL,
                   lambda: {"embeds": [build_embed(txn, forced_checks)]},
                   max_burst=MAX_BURST, burst_window=BURST_WINDOW)

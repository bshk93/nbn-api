"""Discord alerts for the TRC trade pipeline — the `trc-alerts` channel.

Private, committee-only, the TRC analogue of `pdc-alerts`
(`DISCORD_TRC_CHANNEL`). Two posts, both points where the committee has
something to do (decided 2026-09-29): every team has agreed, so it is ready
for a vote; and the third approval is in, so it is ready to finalize. Nothing
on proposal, individual votes, or the endings. A finalized trade posts to
#transactions through `apply_trade`, as before.

`post_trade` is separate: the finalized trade, as plain text in the league's
own format, to the public #transactions channel (`DISCORD_TRADES_CHANNEL`).
That channel is the league's record of trades. The #roster-log relay copies it
from there.

Same rules as fa_notify/poext_notify: a no-op while the env var is unset,
delivered through discord_transport's paced queue, and never raises — the
write it describes has already happened.
"""
from __future__ import annotations

import logging
import os

from . import discord_transport as transport
from .discord_notify import SITE, _player_name
from .players import load_player_bios

logger = logging.getLogger(__name__)

DISCORD_TRC_CHANNEL = os.environ.get("DISCORD_TRC_CHANNEL", "").strip()
DISCORD_TRADES_CHANNEL = os.environ.get("DISCORD_TRADES_CHANNEL", "").strip()

# Trade requests are low-volume (a handful a week). This only stops a runaway
# loop from flooding the channel.
TRC_MAX_BURST = 60
TRC_BURST_WINDOW = 900

DASHBOARD = f"{SITE}/committees/trc/"

COLOR_ACTION = 0xFBBF24

# The 👍/👎 buttons on a "ready for a vote" post. A click comes back through
# /api/discord/interactions to routers/trc_discord.py, which casts the vote.
VOTE_PREFIX = "trc_vote"


def vote_buttons(request_id: str) -> list[dict]:
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "emoji": {"name": "👍"}, "custom_id": f"{VOTE_PREFIX}:approve:{request_id}"},
        {"type": 2, "style": 4, "emoji": {"name": "👎"}, "custom_id": f"{VOTE_PREFIX}:reject:{request_id}"},
    ]}]


def _alert(embed_fn) -> bool:
    try:
        return transport.send(DISCORD_TRC_CHANNEL, embed_fn,
                              max_burst=TRC_MAX_BURST, burst_window=TRC_BURST_WINDOW)
    except Exception as exc:
        logger.warning("TRC alert failed: %s", exc)
        return False


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _asset(a: dict, bios: dict) -> str:
    if a.get("type") == "player":
        return _player_name(a["slug"], bios) or a["slug"]
    label = f"{a.get('year')} R{a.get('round')} ({a.get('orig')})"
    if a.get("protection"):
        label += " protected"
    if a.get("swap_with"):
        label += " swap"
    return label


def _legs(item: dict) -> str:
    try:
        bios = load_player_bios()
    except Exception:
        bios = {}
    lines = []
    for tr in (item.get("trade") or {}).get("transfers", []):
        assets = ", ".join(_asset(a, bios) for a in tr.get("assets", []))
        lines.append(f"**{tr['from_team']} → {tr['to_team']}**: {assets}")
    for team, slugs in _releases(item).items():
        lines.append(f"**{team} releases**: {', '.join(_player_name(s, bios) or s for s in slugs)}")
    return _truncate("\n".join(lines), 1000) or "—"


def _releases(item: dict) -> dict:
    return {t: s for t, s in ((item.get("trade") or {}).get("releases") or {}).items() if s}


def _title(item: dict, what: str) -> str:
    return f"Trade #{item['number']} ({' ⇄ '.join(item['parties'])}) — {what}"


def _legality(item: dict) -> str:
    v = item.get("validation") or {}
    if v.get("legal", True):
        return "✓ Passes every check"
    failed = [c for c in v.get("checks", []) if not c.get("passed") and c.get("level", "error") == "error"]
    problems = len(failed) + len(v.get("ownership_problems", []))
    return f"✗ Fails {problems} check{'s' if problems != 1 else ''} right now"


def notify_ready_for_vote(item: dict, needed: int) -> None:
    def build():
        return {"embeds": [{
            "title": _title(item, "ready for a vote"),
            "description": f"Every team has consented. It needs {needed} approvals.",
            "color": COLOR_ACTION,
            "fields": [{"name": "Trade", "value": _legs(item), "inline": False},
                       {"name": "Legality", "value": _legality(item), "inline": False}],
            "url": DASHBOARD,
        }], "components": vote_buttons(item["id"])}
    _alert(build)


def notify_ready_to_finalize(item: dict, needed: int) -> None:
    def build():
        return {"embeds": [{
            "title": _title(item, "ready to finalize"),
            "description": f"{needed} approvals in. A TRC head can finalize or reject it.",
            "color": COLOR_ACTION,
            "fields": [{"name": "Legality", "value": _legality(item), "inline": False}],
            "url": DASHBOARD,
        }]}
    _alert(build)


def _ordinal(n) -> str:
    return {1: "1st", 2: "2nd"}.get(n, f"{n}th")


def _trade_asset(a: dict, bios: dict) -> str:
    """One asset the way the league writes it in #transactions: a player's
    name, or a pick as "ATL 2027 2nd"."""
    if a.get("type") == "player":
        return _player_name(a["slug"], bios) or a["slug"]
    label = f"{a.get('orig')} {a.get('year')} {_ordinal(a.get('round'))}"
    if a.get("protection"):
        label += f" (top-{a['protection']} protected)"
    if a.get("swap_with"):
        label += f" (swap with {a['swap_with']})"
    return label


def trade_post_text(item: dict, number: int) -> str:
    try:
        bios = load_player_bios()
    except Exception:
        bios = {}
    received: dict[str, list[str]] = {}
    for tr in (item.get("trade") or {}).get("transfers", []):
        received.setdefault(tr["to_team"], []).extend(_trade_asset(a, bios) for a in tr.get("assets", []))
    blocks = [f"{team} receives: {', '.join(assets)}" for team, assets in received.items()]
    blocks += [f"{team} releases: {', '.join(_player_name(s, bios) or s for s in slugs)}"
               for team, slugs in _releases(item).items()]
    return f"Trade {number}:\n" + "\n\n".join(blocks)


def post_trade(item: dict, number: int) -> bool:
    """Post a finalized trade to #transactions. Never raises."""
    try:
        return transport.send(DISCORD_TRADES_CHANNEL, lambda: {"content": trade_post_text(item, number)},
                              max_burst=TRC_MAX_BURST, burst_window=TRC_BURST_WINDOW)
    except Exception as exc:
        logger.warning("#transactions trade post failed: %s", exc)
        return False

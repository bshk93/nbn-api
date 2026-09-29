"""Discord alerts for the TRC trade pipeline — the `trc-alerts` channel.

Private, committee-only, the TRC analogue of `pdc-alerts`
(`DISCORD_TRC_CHANNEL`). It gets every step of a trade request's life:
submitted, ready for ballots, each ballot, ready to finalize, and the three
endings (rejected, withdrawn, finalized). A finalized trade also posts to
#transactions through `apply_trade`, same as before; the post here just closes
the thread for the committee.

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

# Trade requests are low-volume (a handful a week). This only stops a runaway
# loop from flooding the channel.
TRC_MAX_BURST = 60
TRC_BURST_WINDOW = 900

DASHBOARD = f"{SITE}/committees/trc/"

COLOR_SUBMIT = 0x60A5FA
COLOR_ACTION = 0xFBBF24
COLOR_BALLOT = 0x94A3B8
COLOR_DONE = 0x22C55E
COLOR_CLOSED = 0xEF4444


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
    return _truncate("\n".join(lines), 1000) or "—"


def _title(item: dict, what: str) -> str:
    return f"Trade #{item['number']} ({' ⇄ '.join(item['parties'])}) — {what}"


def _approvals(item: dict) -> int:
    return sum(1 for b in (item.get("ballots") or {}).values() if b.get("decision") == "approve")


def _legality(item: dict) -> str:
    v = item.get("validation") or {}
    if v.get("legal", True):
        return "✓ Passes every check"
    failed = [c for c in v.get("checks", []) if not c.get("passed") and c.get("level", "error") == "error"]
    problems = len(failed) + len(v.get("ownership_problems", []))
    return f"✗ Fails {problems} check{'s' if problems != 1 else ''} right now"


def notify_submitted(item: dict) -> None:
    def build():
        return {"embeds": [{
            "title": _title(item, "proposed"),
            "description": f"Proposed by **{item['created_by']}**. Waiting on every team's consent "
                           f"before it opens for ballots.",
            "color": COLOR_SUBMIT,
            "fields": [{"name": "Trade", "value": _legs(item), "inline": False},
                       {"name": "Legality", "value": _legality(item), "inline": False}],
            "url": DASHBOARD,
        }]}
    _alert(build)


def notify_ready_for_ballots(item: dict, needed: int) -> None:
    def build():
        return {"embeds": [{
            "title": _title(item, "ready for ballots"),
            "description": f"Every team has consented. It needs {needed} approvals.",
            "color": COLOR_ACTION,
            "fields": [{"name": "Trade", "value": _legs(item), "inline": False},
                       {"name": "Legality", "value": _legality(item), "inline": False}],
            "url": DASHBOARD,
        }]}
    _alert(build)


def notify_ballot(item: dict, member: str, decision: str, note: str, needed: int) -> None:
    def build():
        verb = "approved" if decision == "approve" else "voted to reject"
        return {"embeds": [{
            "title": _title(item, f"{member} {verb}"),
            "description": _truncate(note, 1500),
            "color": COLOR_BALLOT,
            "fields": [{"name": "Approvals", "value": f"{_approvals(item)}/{needed}", "inline": True}],
            "url": DASHBOARD,
        }]}
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


def notify_closed(item: dict) -> None:
    """Rejected, withdrawn or finalized — whichever the item now is."""
    status = item.get("status")
    end = item.get(status) or {}
    if status == "finalized":
        what, color = "finalized", COLOR_DONE
        desc = f"Finalized by **{end.get('by')}**. The trade is applied and posted to #transactions."
    elif status == "rejected":
        what, color = "rejected", COLOR_CLOSED
        desc = f"Rejected by **{end.get('by')}**: {_truncate(end.get('reason'), 1500)}"
    elif status == "withdrawn":
        what, color = "withdrawn", COLOR_CLOSED
        reason = _truncate(end.get("reason"), 1500)
        desc = f"Withdrawn by **{end.get('by')}**" + (f": {reason}" if reason else ".")
    else:
        return

    def build():
        return {"embeds": [{"title": _title(item, what), "description": desc,
                            "color": color, "url": DASHBOARD}]}
    _alert(build)

"""Discord announcements for the PDC extension pipeline (§ 6.2/6.3).

Reuses the **same** private channel free agency already posts to
(`DISCORD_PDC_CHANNEL`, `pdc-alerts`) rather than adding a second env var —
per nbn-today/docs/poext-extension-pipeline.md D12, that channel is the
shared committee alert feed, extensions included, not an FA-only one. No
public channel: unlike free agency's `fa-news`, an extension proposal names
a team and a dollar figure on every event including the ones a rival never
gets to see in FA (there's only one proposer here in the first place), so
there is nothing in this pipeline that belongs in front of the public. D9's
"public gets accept only" is deferred — see the module docstring's final
paragraph.

Same no-op-without-config rule as fa_notify/discord_notify: with
DISCORD_PDC_CHANNEL unset this module is inert. Delivery is
discord_transport's shared paced queue — one burst budget for the whole
pdc-alerts channel, split between this module and fa_notify's own PDC_MAX_BURST
so the two can't collectively exceed what one channel can take (see
POEXT_MAX_BURST below).

Every function here is best-effort and never raises: the proposal, remand or
finalize it describes has already been written, and Discord must not be able
to fail it.

Not yet built: a public announcement on `agreed` (D9's "public gets accept
only"). Free agency's public channel exists because free agency has a public
half to announce (who signed). An accepted extension is exactly that same
kind of public news, but there's no public feed for it today, and this
module deliberately doesn't invent one on its own — that's a call for
whoever wires DISCORD_FA_NEWS_CHANNEL-equivalent scope here, not something to
default into being active. File as a follow-up if wanted.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from . import discord_transport as transport
from .discord_notify import SITE, _contract_breakdown, _contract_str, _player_name
from .players import load_player_bios

logger = logging.getLogger(__name__)

DISCORD_PDC_CHANNEL = os.environ.get("DISCORD_PDC_CHANNEL", "").strip()

PDC_SITE = "https://pdc.nbn.today"

# Half of fa_notify's PDC_MAX_BURST (120/900s) — the two modules share one
# channel and must not collectively exceed what it can take. Extensions are
# far lower-volume than free agency (~32 real eligible players vs hundreds of
# free agents), so 60 clears any plausible extension-specific burst with room
# to spare; if that ever proves wrong, tests/test_poext_notify.py is the
# place to size it against real measured volume, the way fa_notify's own
# figure was.
POEXT_MAX_BURST = 60
POEXT_BURST_WINDOW = 900

COLOR_SUBMIT = 0x60A5FA
COLOR_REMAND = 0xFB923C
COLOR_VOID = 0xEF4444
COLOR_RESTORE = 0x60A5FA
COLOR_AGREED = 0x22C55E
COLOR_REJECTED = 0xEF4444

KIND_LABELS = {"veteran": "Veteran", "rookie_scale": "Rookie scale", "extend_and_trade": "Extend-and-trade"}


def _name(slug: str) -> str:
    try:
        return _player_name(slug, load_player_bios()) or slug
    except Exception:
        return slug


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _link(slug: str) -> str:
    return f"{PDC_SITE}/#/p/{slug}"


def _alert(embed_fn) -> bool:
    """Post to the shared private committee channel. Never raises."""
    try:
        return transport.send(DISCORD_PDC_CHANNEL, embed_fn,
                              max_burst=POEXT_MAX_BURST, burst_window=POEXT_BURST_WINDOW)
    except Exception as exc:
        logger.warning("PO-EXT alert failed: %s", exc)
        return False


def _proposal_fields(p: dict) -> list[dict]:
    slug, team = p["player"], p["team"]
    contract = p.get("contract") or {}
    fields = [
        {"name": "Player", "value": f"[{_name(slug)}]({SITE}/players/?p={slug})", "inline": True},
        {"name": "Team", "value": f"[{team}]({SITE}/teams/{team})", "inline": True},
        {"name": "Proposal", "value": f"#{p['number']} · v{p.get('version', 1)} · {KIND_LABELS.get(p.get('kind'), p.get('kind') or '—')}",
         "inline": True},
    ]
    breakdown = _contract_breakdown(contract)
    if breakdown:
        fields.append({"name": "Year by year", "value": breakdown, "inline": False})
    return fields


def notify_proposal_submitted(p: dict) -> None:
    """A proposal reached the queue — the first submission or a resubmission
    after a remand (one endpoint, per D4, so one announcement path)."""
    revision = p.get("version", 1) > 1

    def build():
        return {"embeds": [{
            "title": f"{p['team']} — extension proposal for {_name(p['player'])}"
                    + (f" (v{p['version']})" if revision else ""),
            "description": f"{_contract_str(p.get('contract') or {})}"
                          + (" — resubmitted after a remand." if revision else " — submitted, unclaimed."),
            "color": COLOR_SUBMIT,
            "fields": _proposal_fields(p),
            "url": _link(p["player"]),
        }]}
    _alert(build)


def notify_proposal_remanded(p: dict, remand: dict) -> None:
    def build():
        return {"embeds": [{
            "title": f"{_name(p['player'])} — extension proposal sent back",
            "description": f"**{remand['by']}**: {_truncate(remand.get('note'), 1500)}",
            "color": COLOR_REMAND,
            "fields": [{"name": "Proposal", "value": f"#{p['number']} · v{remand.get('from_version', p.get('version', 1))}", "inline": True},
                      {"name": "Team", "value": p["team"], "inline": True}],
            "url": _link(p["player"]),
        }]}
    _alert(build)


def notify_proposal_voided(p: dict) -> None:
    void = p.get("void") or {}
    def build():
        return {"embeds": [{
            "title": f"{p['team']} — extension proposal for {_name(p['player'])} voided",
            "description": _truncate(void.get("reason"), 1500) or "—",
            "color": COLOR_VOID,
            "fields": [{"name": "Proposal", "value": f"#{p['number']}", "inline": True},
                      {"name": "By", "value": void.get("by") or "—", "inline": True}],
            "url": _link(p["player"]),
        }]}
    _alert(build)


def notify_proposal_restored(p: dict) -> None:
    def build():
        return {"embeds": [{
            "title": f"{p['team']} — extension proposal for {_name(p['player'])} restored",
            "description": f"Back to **{p['status']}** — live again.",
            "color": COLOR_RESTORE,
            "fields": [{"name": "Proposal", "value": f"#{p['number']}", "inline": True}],
            "url": _link(p["player"]),
        }]}
    _alert(build)


def notify_player_finalized(slug: str, team: str, final: dict) -> None:
    """The committee's decision. Neutral title either way — `outcome` and the
    tally are what carry the news, not a winner/loser framing."""
    outcome = final["outcome"]
    color = COLOR_AGREED if outcome == "agreed" else COLOR_REJECTED
    exhausted_note = ""
    if outcome == "rejected" and final.get("exhausted"):
        exhausted_note = "\n\n§ 6.3: three proposals now rejected — no further extension opportunities arise for this player unless PO-EXT unlocks it."

    def build():
        embed = {
            "title": f"{_name(slug)} — extension {outcome}",
            "description": f"{final['accept']}–{final['reject']} · locked by {final['locked_by']}" + exhausted_note,
            "color": color,
            "fields": [{"name": "Team", "value": team, "inline": True},
                      {"name": "Rejections on record", "value": str(final.get("rejections_total", 0)) + "/3", "inline": True}],
            "url": _link(slug),
        }
        if outcome == "agreed":
            embed["footer"] = {"text": "Not applied automatically — enter the extension on /transactions by hand."}
        return {"embeds": [embed]}
    _alert(build)

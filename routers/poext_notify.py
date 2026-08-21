"""Discord announcements for the PDC extension pipeline (§ 6.2/6.3).

Three channels, two different appetites:

* **`pdc-alerts`** (`DISCORD_PDC_CHANNEL`) — private, committee-only, full
  detail. Reuses the **same** channel free agency already posts to rather
  than adding a second env var — per
  nbn-today/docs/poext-extension-pipeline.md D12, that channel is the shared
  committee alert feed, extensions included, not an FA-only one. Gets
  submitted/remanded/voided/restored/finalized — everything.
* **`fa-news`** (`DISCORD_FA_NEWS_CHANNEL`) and **`#roster-log`**
  (`DISCORD_ROSTER_LOG_CHANNEL`) — public, and **`agreed`-only** (D9: "public
  gets accept only"). A submitted, remanded or rejected proposal never
  reaches either — only the committee's final yes. `#roster-log` gets the
  full picture (team, contract shorthand — that channel already carries this
  much detail for every real transaction); `fa-news` gets the same
  name-only, no-team, no-dollar treatment `fa_notify._news()` already
  enforces for free agency, via its own choke point below (`_news()`) rather
  than importing that module's — the "one function can reach the public
  channel" discipline has to be a property of *this* module too, not
  borrowed from a different one that happens to write to the same host.

Same no-op-without-config rule as fa_notify/discord_notify: each channel is
independently inert without its own env var. Delivery is discord_transport's
shared paced queue — pdc-alerts' burst budget is split with fa_notify's own
PDC_MAX_BURST so the two can't collectively exceed what one channel can take
(see POEXT_MAX_BURST below); fa-news is its own budget, split the same way
against fa_notify's NEWS_MAX_BURST.

Every function here is best-effort and never raises: the proposal, remand or
finalize it describes has already been written, and Discord must not be able
to fail it.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from . import discord_transport as transport
from . import roster_log_relay
from .discord_notify import SITE, _contract_breakdown, _contract_str, _player_name
from .players import load_player_bios

logger = logging.getLogger(__name__)

DISCORD_PDC_CHANNEL = os.environ.get("DISCORD_PDC_CHANNEL", "").strip()
# Deliberately the same env var fa_notify.py reads — one public news feed,
# not a second channel for extensions to announce into.
DISCORD_FA_NEWS_CHANNEL = os.environ.get("DISCORD_FA_NEWS_CHANNEL", "").strip()

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

# fa-news half: same reasoning as POEXT_MAX_BURST above, against fa_notify's
# NEWS_MAX_BURST (60/900s) rather than PDC_MAX_BURST — this only ever posts
# on `agreed`, so real volume is a fraction of the ~32-player eligible pool,
# not every proposal event.
NEWS_MAX_BURST = 30
NEWS_BURST_WINDOW = 900

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


_TEAM_ABBRS = ["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
              "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
              "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"]
_ABBR_RE = re.compile(r"\b(" + "|".join(_TEAM_ABBRS) + r")\b")


def _news(slug: str, text: str) -> bool:
    """Post to the **public** `fa-news` channel.

    This is the only function in this module that can reach it, and it takes
    a player and a finished string — never a proposal, never a team, never a
    figure — the same signature discipline `fa_notify._news` uses for the
    same reason (§ 9.2 there; D9 here). Belt-and-braces: also asserted at
    the call site by construction (nothing built into `text` ever includes
    team/dollar data) and by tests/test_poext_notify.py scanning rendered
    output, but the signature is what makes a future caller unable to hand
    this a team or a contract even by accident.
    """
    if not transport.configured(DISCORD_FA_NEWS_CHANNEL):
        return False
    try:
        return transport.send(DISCORD_FA_NEWS_CHANNEL, {"content": text},
                              max_burst=NEWS_MAX_BURST, burst_window=NEWS_BURST_WINDOW)
    except Exception as exc:
        logger.warning("PO-EXT news post failed for %s: %s", slug, exc)
        return False


def _roster_log(text: str) -> bool:
    """Post to `#roster-log`, reusing `roster_log_relay._send` — an agreed
    extension is exactly the same shape of entry the relay itself produces
    (a bare description-only card, same color, same mention suppression),
    not a new format, so this calls straight into it rather than keeping a
    second copy of that embed shape. Unlike everything the relay itself
    posts, this isn't relaying an existing Discord message — PO-EXT
    finalizing a proposal is a new event this module is the source of, so it
    calls `_send` directly rather than going through the poll cycle."""
    try:
        return roster_log_relay._send(text)
    except Exception as exc:
        logger.warning("PO-EXT roster-log post failed: %s", exc)
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


def notify_player_finalized(slug: str, proposal: dict, final: dict) -> None:
    """The committee's decision — always to `pdc-alerts`; on `agreed` only,
    also to the two public channels (D9: "public gets accept only"). A
    submitted, remanded or rejected proposal never reaches either public
    channel — only the yes.

    `proposal` (not just `team`) so the public `#roster-log` post can carry
    the contract shorthand, the same level of detail that channel already
    carries for every real transaction — a rejection or an in-progress
    negotiation never gets that treatment, only the decided deal.
    """
    team = proposal["team"]
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

    if outcome != "agreed":
        return

    # #roster-log: full detail, same as any other entry there. Worded as a
    # committee decision pending entry, not as an applied contract change —
    # the actual transaction is still typed into /transactions by hand
    # afterward (same manual hand-off as an accepted FA offer), and
    # #roster-log otherwise only ever carries transactions that already
    # happened.
    shorthand = _contract_str(proposal.get("contract") or {})
    _roster_log(
        f"**{team}** — PO-EXT approved an extension for **{_name(slug)}**"
        + (f" ({shorthand})" if shorthand else "") + " — pending entry on /transactions."
    )

    # fa-news: same no-team-no-dollar discipline fa_notify._news() enforces,
    # via this module's own choke point.
    _news(slug, f"{_name(slug)} has agreed to a contract extension.")

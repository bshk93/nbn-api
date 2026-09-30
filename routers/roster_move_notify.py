"""The public announcement of a roster move made on the site.

An owner used to make a move and then post it in Discord by hand, and that
post was the league's news. Once the owner makes the move on the site, nobody
posts it, so the site has to. Each move goes to the channel the humans used:

* **`#fa-news`** (`DISCORD_FA_NEWS_CHANNEL`) — renounce, team option,
  two-way conversion, rookie-scale signing, stash. The player ends up a free
  agent or signed, which is free-agency news.
* **`#waivers`** (`DISCORD_WAIVERS_CHANNEL`) — void (decided 2026-09-30). A
  release also goes there, but through `waiver_notify.notify_waived`, since it
  opens a claim window and says so.

One line per move, in the league's own wording: "The Milwaukee Bucks stash
Vsevolod Ishchenko." No dollars — the contract is in the transaction embed in
`#roster-log-nbn-today`.

**How it reaches #roster-log.** Not from here. `#fa-news` bot posts are never
relayed (`roster_log_relay`'s `humans_only`), so the caller sends the embed
with `relay_to_roster_log=True` instead, and #roster-log gets the embed's
fuller text, contract year by year. `#waivers` *is* relayed, so a void's
embed stays unrelayed and its line here is what #roster-log gets. Either way
the move lands in #roster-log once.

Team names are fine here, unlike `fa_notify._news` and `poext_notify._news`:
those keep sealed free-agency bids out of a public channel, and a roster move
is public the moment it is made.

Inert without its channel env var. Never raises — the move is already written.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from . import discord_transport as transport
from .constants import TEAM_NAMES
from .discord_notify import _player_name
from .players import load_player_bios

logger = logging.getLogger("nbn-api")

DISCORD_FA_NEWS_CHANNEL = os.environ.get("DISCORD_FA_NEWS_CHANNEL", "").strip()
DISCORD_WAIVERS_CHANNEL = os.environ.get("DISCORD_WAIVERS_CHANNEL", "").strip()

# A move is one owner clicking one button. The busiest real run is a team
# renouncing four holds in a minute (OKC, 2026-08-09); 60 per 15 minutes is
# the whole league doing that at once, and still stops a loop.
MAX_BURST = 60
BURST_WINDOW = 900


def _team(abbr: str) -> str:
    return TEAM_NAMES.get((abbr or "").upper(), abbr or "")


def _name(slug: str) -> str:
    try:
        return _player_name(slug, load_player_bios()) or slug
    except Exception:
        return slug


def announcement(txn: dict) -> Optional[tuple[str, str]]:
    """`(channel, text)` for a transaction, or None for a type that isn't
    announced here. Pure apart from the player-name lookup."""
    d = txn.get("details") or {}
    kind = txn.get("type")
    team = f"The {_team(d.get('team'))}"
    player = _name(d.get("player") or "")

    if kind == "renounce":
        return DISCORD_FA_NEWS_CHANNEL, f"{team} renounce the rights to {player}."
    if kind == "option":
        year = f"{d['year']} " if d.get("year") else ""
        if d.get("decision") == "accept":
            return DISCORD_FA_NEWS_CHANNEL, f"{team} exercise {player}'s {year}team option."
        fa = "a restricted" if d.get("cap_hold_type") == "RFA" else "an unrestricted"
        return (DISCORD_FA_NEWS_CHANNEL,
                f"{team} decline {player}'s {year}team option. {player} becomes {fa} free agent.")
    if kind == "convert_twoway":
        return DISCORD_FA_NEWS_CHANNEL, f"{team} convert {player} from a two-way to a standard contract."
    if kind == "sign_pick":
        return DISCORD_FA_NEWS_CHANNEL, f"{team} sign {player} to a rookie-scale contract."
    if kind == "stash":
        return DISCORD_FA_NEWS_CHANNEL, f"{team} stash the draft rights to {player}."
    if kind == "void_player":
        return DISCORD_WAIVERS_CHANNEL, f"{team} void {player}'s contract."
    return None


def announce(txn: dict) -> bool:
    """Post the move's line to its channel. False if it wasn't sent."""
    try:
        found = announcement(txn)
        if not found:
            return False
        channel, text = found
        if not transport.configured(channel):
            return False
        return transport.send(channel, {"content": text},
                              max_burst=MAX_BURST, burst_window=BURST_WINDOW)
    except Exception as exc:
        logger.warning("roster move announcement failed for %s: %s", txn.get("id"), exc)
        return False

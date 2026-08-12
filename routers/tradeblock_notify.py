"""Optional per-edit Discord post for manual tradeblock changes.

`PUT /api/trading-block/{team}` is also how a player quietly falls off the
block for reasons that have nothing to do with anyone editing it — most
notably `_apply_trade`, which drops a dealt player from their old team's
block as part of applying the trade. Silently reposting a message every time
would announce noise, not moves, so this module is never called from a
transaction apply path. It is opt-in, per save, from the /tradeblock editor
itself: the caller (`roster_picks.put_trading_block`) computes what actually
changed and hands over the diff only when the team checked the box.

Uses the same shared queue/pacing as `discord_notify` and `fa_notify`
(`discord_transport`), sized for how this channel actually gets used — a team
editing its own block by hand, not an automated feed. Set
`DISCORD_TRADEBLOCK_CHANNEL` to the target channel id; unset, this module is a
complete no-op.
"""
from __future__ import annotations

import os

from . import discord_transport as transport

DISCORD_TRADEBLOCK_CHANNEL = os.environ.get("DISCORD_TRADEBLOCK_CHANNEL", "").strip()

# A manual edit, not an automated feed — a handful of saves per team per day at
# most. Sized well above any plausible real burst while still catching a loop.
MAX_BURST = 30
BURST_WINDOW = 900   # seconds


def _pick_label(team: str, year, round_: str, orig: str) -> str:
    orig = (orig or "Own").strip()
    if orig == "Own" or orig.upper() == team.upper():
        return f"{year} {round_}"
    return f"{year} {round_} ({orig})"


def notify_tradeblock_change(
    member: str,
    team: str,
    added_players: list[str],
    removed_players: list[str],
    added_picks: list[dict],
    removed_picks: list[dict],
) -> None:
    """Announce one team's manual tradeblock edit. Fire-and-forget — never
    raises, and the save it describes has already been written by the time
    this is called.
    """
    if not transport.configured(DISCORD_TRADEBLOCK_CHANNEL):
        return

    added = list(added_players) + [
        _pick_label(team, p["year"], p["round"], p.get("team", "")) for p in added_picks
    ]
    removed = list(removed_players) + [
        _pick_label(team, p["year"], p["round"], p.get("team", "")) for p in removed_picks
    ]
    if not added and not removed:
        return

    if added and removed:
        body = f"added {', '.join(added)} to the tradeblock and removed {', '.join(removed)}"
    elif added:
        body = f"added {', '.join(added)} to the tradeblock"
    else:
        body = f"removed {', '.join(removed)} from the tradeblock"

    content = f"**{member or 'Someone'}** ({team}) {body}."
    transport.send(DISCORD_TRADEBLOCK_CHANNEL, {"content": content},
                    max_burst=MAX_BURST, burst_window=BURST_WINDOW)

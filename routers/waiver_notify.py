"""§ 5.1 waiver wire Discord feeds — design record: nbn-today/docs/waiver-wire-spec.md § 6.

Each event posts to exactly one channel — a transaction never sends more than
a single Discord message:

* **`#waivers`** (own channel, `DISCORD_WAIVERS_CHANNEL`) — a release opening
  a 48-hour claim window. Reuses a channel that already exists and is already
  relayed into `#roster-log` by `roster_log_relay.py`'s `"waivers"` source
  (`humans_only: False`) — that machinery predates this module, built only
  because a human used to type these announcements in by hand. No relay
  changes needed; posting here programmatically *is* the whole "propagate in
  the discord" requirement. (Previously *also* posted to `fa-news` for the
  same event — a second, near-duplicate message — removed 2026-08-12.)
* **`fa-news`** (public, shared with `fa_notify.py`) — window closes only.
  Reuses `fa_notify._news`, whose signature (a slug + a finished string, never
  a team, never an offer object) is what stops a team name or a dollar figure
  reaching the public channel; nothing here tries to hand it either.
* **`pdc-alerts`** (private committee channel, shared with `fa_notify.py`) —
  every claim submitted, and the § 5 step 5 manual-tie flag. Claims are sealed
  everywhere else (spec § 2) — this is the only place any claim becomes
  visible before resolution, including to the committee.

Inert without its own env var, like every other Discord module here. Never
raises — a Discord outage must not fail or delay a real transaction, the same
guarantee `discord_notify`/`fa_notify` already give.
"""
import logging
import os
from typing import Optional

from . import discord_transport as transport
from . import fa_notify

logger = logging.getLogger("nbn-api")

DISCORD_WAIVERS_CHANNEL = os.environ.get("DISCORD_WAIVERS_CHANNEL", "").strip()

# Sized like fa_notify's own pdc-alerts feed (offer submissions) — a waiver
# claim is that order of event frequency, not the high-volume #transactions
# firehose discord_notify.py paces separately.
WAIVERS_MAX_BURST = 60
WAIVERS_BURST_WINDOW = 900


def _waivers_post(text: str) -> bool:
    if not transport.configured(DISCORD_WAIVERS_CHANNEL):
        return False
    try:
        return transport.send(DISCORD_WAIVERS_CHANNEL, {"content": text},
                              max_burst=WAIVERS_MAX_BURST, burst_window=WAIVERS_BURST_WINDOW)
    except Exception as exc:
        logger.warning("waivers channel post failed: %s", exc)
        return False


def notify_waived(player_slug: str, team: str) -> None:
    """#waivers only (roster-log-relayed). A transaction posts to exactly one
    Discord channel; fa-news previously got a second, near-duplicate post for
    the same event, which is what this was fixed away from 2026-08-12."""
    name = fa_notify._name(player_slug)
    _waivers_post(f"**{name}** has been waived by **{team}**. Waiver claims are open for 48 hours.")


def notify_window_closed(player_slug: str, claimed: bool) -> None:
    """fa-news, posted either way (spec § 6) — `_news`'s signature can't carry
    who claimed the player even if the caller wanted it to."""
    name = fa_notify._name(player_slug)
    if claimed:
        fa_notify._news(player_slug, f"Waiver claims on **{name}** have closed — the player was claimed.")
    else:
        fa_notify._news(player_slug, f"Waiver claims on **{name}** have closed.")


def notify_claim_submitted(player_slug: str, team: str, signing_method: Optional[str]) -> None:
    """pdc-alerts. The only place any claim is visible before resolution."""
    name = fa_notify._name(player_slug)
    method = f" (funded via {signing_method})" if signing_method else ""
    fa_notify._alert(lambda: {
        "content": f"**{team}** filed a waiver claim on **{name}**{method}.",
    })


def notify_manual_tie(player_slug: str, tied_teams: list[str], h2h: Optional[dict], season: Optional[str]) -> None:
    """pdc-alerts. Spec § 5 step 5 — a head-to-head tie that's itself tied (or
    a 3+-way tie) doesn't auto-resolve; this is the flag that tells PDC it's
    waiting on `POST /api/waivers/{txn_id}/resolve`."""
    name = fa_notify._name(player_slug)
    teams_str = " vs. ".join(tied_teams)
    h2h_str = ", ".join(f"{t} {w}" for t, w in h2h.items()) if h2h else "no head-to-head meetings this season"
    fa_notify._alert(lambda: {
        "content": (f"**{name}**'s waiver priority is tied between {teams_str} ({season}: {h2h_str}) "
                    f"and needs a PDC ruling — resolve it from the waivers panel."),
    })

"""The weekly "still on the block?" nudge.

A player sitting on a team's trading block for a week or more gets that team
an inbox message naming him, at most once a week per team, with two buttons
per player: **Remove** (takes him off the block) and **Still available**
(sets `snoozed_until` two weeks out, after which he's back in the nudge). Both
are in `roster_picks.py`. There is deliberately no permanent "keep": the
point is a block that stays current, and a mark that silenced a player for
good would become the default answer. The snooze is on the block entry, so
it's team-wide, and it goes away with the listing — a trade or release
already scrubs the player off (`players._scrub_trading_block`), and a player
taken off and put back is a new listing with a new `added_at`.

Run daily by `nbn-tradeblock-nudge.timer` (`sweep_tradeblock_nudges.py`); the
per-team week is kept in `tradeblock-nudges.json`, so a missed or repeated
run neither skips a team nor nudges it twice. Players only — picks on the
block carry no date and are never nudged.
"""
from __future__ import annotations

from datetime import date

from .constants import DATA_DIR
from .league_time import league_today
from .storage import _load_json, _save_json
from . import inbox
from . import roster_picks as rp

TRADEBLOCK_NUDGES_FILE = DATA_DIR / "tradeblock-nudges.json"

NUDGE_AFTER_DAYS = 7   # how long a player sits before his team hears about it
NUDGE_EVERY_DAYS = 7   # how often a team hears about it


def _age(days: int) -> str:
    weeks = days // 7
    if weeks >= 1:
        return f"{weeks} week{'s' if weeks != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''}"


def stale_entries(block: dict, today: date) -> list[tuple[str, int]]:
    """(player, days on the block) for every entry old enough to nudge about
    and not snoozed past today, longest-listed first."""
    out = []
    for e in block.get("players", []):
        if not e.get("added_at") or (e.get("snoozed_until") or "") > today.isoformat():
            continue
        try:
            days = (today - date.fromisoformat(e["added_at"])).days
        except ValueError:
            continue
        if days >= NUDGE_AFTER_DAYS:
            out.append((e["player"], days))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def sweep_tradeblock_nudges(today: date | None = None, dry_run: bool = False) -> dict:
    """One pass over every team's block. Returns what it did, for the log.

    Also stamps `added_at` on any entry that has none — the ones listed before
    the field existed — so their week starts on the first run, not at once."""
    today = today or league_today()
    data = rp.load_trading_block()
    sent = _load_json(TRADEBLOCK_NUDGES_FILE, {})
    done = {"nudged": [], "stamped": 0}

    for team, block in data.items():
        for e in block.get("players", []):
            if not e.get("added_at"):
                e["added_at"] = today.isoformat()
                done["stamped"] += 1

    for team in sorted(data):
        stale = stale_entries(data[team], today)
        if not stale:
            continue
        last = sent.get(team)
        if last and (today - date.fromisoformat(last)).days < NUDGE_EVERY_DAYS:
            continue
        done["nudged"].append({"team": team, "players": [p for p, _ in stale]})
        if dry_run:
            continue
        names = ", ".join(f"{p} ({_age(d)})" for p, d in stale)
        text = (f"Still shopping {names}? "
                f"{'They have' if len(stale) > 1 else 'He has'} been on {team}'s trading block a while. "
                f"Take {'them' if len(stale) > 1 else 'him'} off, or say still available "
                f"and we'll check again in {rp.SNOOZE_DAYS // 7} weeks.")
        actions = []
        for p, _ in stale:
            # `short` is the button text when the page lays a group out as one
            # row under the player's name; `label` stands alone.
            actions.append({"kind": "tradeblock_remove", "label": f"Remove {p}", "short": "Remove",
                            "group": p, "team": team, "player": p})
            actions.append({"kind": "tradeblock_snooze", "label": f"{p} still available",
                            "short": "Still available", "group": p, "team": team, "player": p})
        inbox.notify_team(team, text, link="/fo/tradeblock/", actions=actions)
        sent[team] = today.isoformat()

    if not dry_run:
        if done["stamped"]:
            rp.save_trading_block(data)
        if done["nudged"]:
            _save_json(TRADEBLOCK_NUDGES_FILE, sent)
    return done

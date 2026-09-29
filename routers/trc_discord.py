"""TRC votes from Discord — the 👍 / 👎 buttons on a trc-alerts
"ready for a vote" post (`trc_notify.vote_buttons`).

A click arrives at `/api/discord/interactions` (routers/discord.py) as a
message-component interaction, and the click is the whole vote (decided
2026-09-29). The clicker is matched to a member by their linked Discord account
(`/link`) and must hold `trc`, the same as on the site. The vote goes through
`ballot_trade_request` itself, so a Discord vote and a site vote are the same
record. The site requires a note; a Discord vote carries `DISCORD_NOTE`. Every
reply is ephemeral — only the clicker sees it.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from .auth import load_members
from .trc_notify import VOTE_PREFIX

logger = logging.getLogger(__name__)

DISCORD_NOTE = "Voted from Discord"
CHANNEL_MESSAGE = 4
EPHEMERAL = 1 << 6

_DECISIONS = ("approve", "reject")


def _reply(text: str) -> dict:
    return {"type": CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL}}


def _member(payload: dict) -> tuple[str | None, dict]:
    """The clicker as `(name, info)` — `info` in the `{name, roles}` shape
    every permission check takes. `(None, {})` if their Discord isn't linked."""
    user = (payload.get("member") or {}).get("user") or payload.get("user") or {}
    did = str(user.get("id") or "")
    if not did:
        return None, {}
    for name, m in load_members().items():
        if str(m.get("discord_id") or "") == did:
            return name, {"name": name, "roles": m.get("roles") or []}
    return None, {}


def _parse(custom_id: str, prefix: str) -> tuple[str, str] | None:
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[0] != prefix or parts[1] not in _DECISIONS:
        return None
    return parts[1], parts[2]


def _gate(payload: dict) -> tuple[dict | None, dict | None]:
    """(info, None) when the clicker may vote, else (None, the reply saying why)."""
    name, info = _member(payload)
    if not name:
        return None, _reply("Your Discord account isn't linked to an NBN member yet — run `/link` first.")
    if "trc" not in info["roles"]:
        return None, _reply("Only TRC members can vote on trades.")
    return info, None


def handle_component(payload: dict) -> dict:
    """A 👍/👎 click: cast the vote, or say why it can't be."""
    parsed = _parse((payload.get("data") or {}).get("custom_id", ""), VOTE_PREFIX)
    if not parsed:
        return _reply("Unsupported button.")
    decision, request_id = parsed
    info, refusal = _gate(payload)
    if refusal:
        return refusal

    # Imported here: trade_requests pulls in the whole transactions module,
    # which the slash-command handlers never need.
    from . import trade_requests as tr
    try:
        item = tr.ballot_trade_request(request_id, tr.BallotBody(decision=decision, note=DISCORD_NOTE), info)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "That vote couldn't be recorded."
        return _reply(f"Vote not recorded — {detail}")
    except Exception:
        logger.exception("TRC vote from Discord failed")
        return _reply("Something went wrong recording that vote. Try the TRC page instead.")

    approvals = tr._approve_count(item)
    verb = "Approved" if decision == "approve" else "Voted to reject"
    tail = " It's ready to finalize." if item["status"] == "ready_to_finalize" else ""
    return _reply(f"{verb} trade #{item['number']} — {approvals}/{tr.APPROVALS_NEEDED} approvals.{tail}")

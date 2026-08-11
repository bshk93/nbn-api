"""Discord announcements for the PDC free-agency pipeline (§ 9, Phase 6).

Two channels with deliberately different appetites, and the difference is the
whole design:

* **`pdc-alerts`** (`DISCORD_PDC_CHANNEL`) — private, committee-only. Full
  detail: the contract year by year, the funding method, the promises, the
  pitch, the legality verdict, and on a resubmission a **diff of what actually
  changed**. Reviewing a revision without seeing what moved is the same work
  twice (§ 4.3a).
* **`fa-news`** (`DISCORD_FA_NEWS_CHANNEL`) — public, league-wide, and
  **FFA-mode only**. Exactly two posts per player: the clock starting, and the
  window closing. Each names the length off the clock's own stamp, since the
  head can change how long a window runs (§ 4.1). No team, no dollars, no offer count, ever — that a
  team is bidding is committee information (§ 9.2).

The second bullet is a security property, not a stylistic one, so it is enforced
structurally rather than by care: `_news()` is the only function that touches the
public channel, it takes a player slug and a deadline and nothing else, and
`tests/test_fa_notify.py` asserts every rendered public payload contains no team
abbreviation and no `$`. There is no path by which an offer object reaches it.

Same no-op-without-config rule as `discord_notify`: with a channel env var unset
that channel is inert, so this ships before either channel exists — which is
exactly how Phase 6 rolls out (module first, then `DISCORD_PDC_CHANNEL`, then
`DISCORD_FA_NEWS_CHANNEL` last).

Delivery is `discord_transport`'s shared paced queue. Every function here is
best-effort and never raises: the offer, remand or finalize it describes has
already been written, and Discord must not be able to fail it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from . import discord_transport as transport
from .discord_notify import (SITE, _contract_breakdown, _contract_str, _dollars,
                             _method_str, _player_name)
from .players import load_player_bios

logger = logging.getLogger(__name__)

DISCORD_PDC_CHANNEL     = os.environ.get("DISCORD_PDC_CHANNEL", "").strip()
DISCORD_FA_NEWS_CHANNEL = os.environ.get("DISCORD_FA_NEWS_CHANNEL", "").strip()

PDC_SITE = "https://pdc.nbn.today"

# Burst sizing. Unlike `discord_notify`'s, these can't be measured against
# history — nothing has run yet — so they're sized against the structural worst
# case of one round instead, which is a better argument than a round number:
#
#  * `pdc-alerts`: 30 teams could in principle each submit on each open player,
#    but offers arrive over days of a round, not in a burst. The real burst is
#    the head working through a session — opening a round, then finalizing every
#    open player one click at a time. 120 clears ~4x the largest plausible
#    sitting and still stops a loop dead.
#  * `fa-news`: two posts per player per FFA window, and the one genuine burst
#    source is the lazy expiry sweep (§ 4.1) — if nobody loads the dashboard for
#    a day, the next request observes every expired window at once. 60 clears an
#    entire FFA slate closing in one sweep.
PDC_MAX_BURST      = 120
PDC_BURST_WINDOW   = 900
NEWS_MAX_BURST     = 60
NEWS_BURST_WINDOW  = 900

# An absolute lateness bound on a closure post, independent of how long the
# window itself ran: a day late isn't news, it's a replay. The case that matters
# is deploying this module while clocks that expired weeks ago are still
# unflagged in `fa-state.json`. The sweep still stamps them (so they never post
# later either); it just doesn't announce them.
MAX_CLOSE_AGE_HOURS = 24

COLOR_SUBMIT   = 0x22C55E   # matches the `sign` badge — an offer is a signing proposal
COLOR_REVISION = 0x60A5FA
COLOR_REMAND   = 0xFB923C
COLOR_VOID     = 0xEF4444   # a remand is orange because it comes back; this doesn't
COLOR_BOARD    = 0xA78BFA   # mode / round / clock — the head moving the board
COLOR_FINAL    = 0xD4AF37

PROMISE_ROLE_LABELS = {"face": "franchise face", "starter": "starter",
                       "role_player": "role player", "veteran": "veteran presence",
                       "none": "no role promised"}


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _window_label(ffa: dict) -> str:
    """"24-hour ", for the window this player's clock actually got — the head can
    change the length (§ 4.1) and a post must describe the clock it announces,
    not the setting current when it was read. Empty (so the sentence reads "the
    window") when the length is unrecoverable. Imported at call time because
    `free_agency` imports this module."""
    from .free_agency import ffa_window_label
    label = ffa_window_label(ffa)
    return f"{label} " if label else ""


def _stamp(ts: Optional[str], style: str = "f") -> str:
    """A Discord dynamic timestamp, which every reader sees in their own
    timezone. A league spread across timezones acting on a 24-hour deadline is
    exactly the case a fixed "5:00 PM" gets wrong."""
    at = _parse_ts(ts)
    return f"<t:{int(at.timestamp())}:{style}>" if at else "—"


def _name(slug: str) -> str:
    try:
        return _player_name(slug, load_player_bios()) or slug
    except Exception:
        return slug


def _money(v) -> str:
    n = _dollars(v)
    return f"${n:,}" if n else "—"


def _promises_str(p: Optional[dict]) -> str:
    p = p or {}
    bits = []
    if p.get("mpg"):
        bits.append(f"{p['mpg']} mpg")
    bits.append(PROMISE_ROLE_LABELS.get(p.get("role"), p.get("role") or "—"))
    if p.get("playoffs"):
        bits.append("playoff contention")
    return " · ".join(bits)


# ── the diff a resubmission is reviewed against (§ 4.3a) ──────────────────────

def _map_diff(old: dict, new: dict, fmt) -> list[str]:
    return [f"{k}  {fmt(old.get(k))} → {fmt(new.get(k))}"
            for k in sorted(set(old or {}) | set(new or {}))
            if fmt(old.get(k)) != fmt(new.get(k))]


def _label(v) -> str:
    return str(v) if v else "—"


def _diff_lines(prev: dict, offer: dict) -> list[str]:
    """What moved between the frozen prior version and the one just resubmitted.

    `prev` is an entry from `offer["versions"]` — frozen at the *remand*, not at
    the resubmission, which is what makes this a comparison against the terms
    the committee actually objected to rather than against themselves.
    """
    po, no = prev.get("offer") or {}, offer.get("offer") or {}
    pc, nc = po.get("contract") or {}, no.get("contract") or {}
    lines: list[str] = []
    lines += _map_diff(pc.get("salaries") or {}, nc.get("salaries") or {}, _money)
    lines += _map_diff(pc.get("guaranteed") or {}, nc.get("guaranteed") or {}, _money)
    lines += _map_diff(pc.get("cap_holds") or {}, nc.get("cap_holds") or {}, _label)
    lines += _map_diff(pc.get("guarantee_dates") or {}, nc.get("guarantee_dates") or {}, _label)
    for key, lbl in (("signing_method", "method"), ("bird_rights_type", "bird type"),
                     ("eaps_assumption", "EAPS")):
        if po.get(key) != no.get(key):
            lines.append(f"{lbl}  {_label(po.get(key))} → {_label(no.get(key))}")
    if (prev.get("promises") or {}) != (offer.get("promises") or {}):
        lines.append(f"promises  {_promises_str(prev.get('promises'))} → "
                     f"{_promises_str(offer.get('promises'))}")
    if (prev.get("pitch") or "") != (offer.get("pitch") or ""):
        lines.append("pitch  rewritten")
    return lines


def _legality_value(validation: Optional[dict]) -> str:
    """The submit-time verdict. Warnings are the interesting half — the submit
    path refuses an illegal offer outright (there is no `force` on it), so a
    posted offer is legal by construction and what the committee needs to see is
    what passed *conditionally*."""
    if not validation:
        return "not validated"
    checks = validation.get("checks") or []
    failed = [c for c in checks if not c.get("passed") and c.get("level") == "error"]
    warned = [c for c in checks if not c.get("passed") and c.get("level") != "error"]
    head = "✅ Legal at submission" if validation.get("legal") and not failed \
        else "❌ " + ", ".join(f"`{c['check']}`" for c in failed)
    if warned:
        head += "\n⚠️ " + "\n⚠️ ".join(
            f"{c.get('message') or c['check']}" for c in warned[:4])
    return head


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _offer_link(offer: dict) -> str:
    return f"{PDC_SITE}/#/p/{offer['player']}"


# ── payload builders ──────────────────────────────────────────────────────────

def _offer_embed(offer: dict, prev: Optional[dict]) -> dict:
    slug = offer["player"]
    team = offer["team"]
    body = offer.get("offer") or {}
    contract = body.get("contract") or {}
    revision = bool(prev)

    fields = [
        {"name": "Player",
         "value": f"[{_name(slug)}]({SITE}/players/?p={slug})", "inline": True},
        {"name": "Team", "value": f"[{team}]({SITE}/teams/{team})", "inline": True},
        {"name": "Offer", "value": f"#{offer['number']} · v{offer.get('version', 1)}",
         "inline": True},
    ]
    breakdown = _contract_breakdown(contract)
    if breakdown:
        fields.append({"name": "Year by year", "value": breakdown, "inline": False})
    if revision:
        lines = _diff_lines(prev, offer)
        fields.append({
            "name": f"Changed since v{prev.get('version', 1)}",
            # Named rather than implied: a resubmission that changed nothing is
            # itself worth seeing, since it means the remand went unanswered.
            "value": "```\n" + "\n".join(lines) + "\n```" if lines
                     else "_Nothing changed — the terms were resubmitted as they were._",
            "inline": False,
        })
        notes = [r for r in (offer.get("remands") or [])
                 if r.get("from_version") == prev.get("version")]
        if notes:
            fields.append({
                "name": "Answering",
                "value": "\n".join(f"**{r['by']}:** {_truncate(r.get('note'), 300)}"
                                   for r in notes[:4]),
                "inline": False,
            })
    fields.append({"name": "Promises", "value": _promises_str(offer.get("promises")),
                   "inline": False})
    if offer.get("pitch"):
        fields.append({"name": "Pitch", "value": _truncate(offer["pitch"], 1000),
                       "inline": False})
    fields.append({"name": "Legality", "value": _legality_value(offer.get("validation")),
                   "inline": False})

    verb = "revised offer" if revision else "offer"
    return {
        "title": f"{team} — {verb} to {_name(slug)}",
        "description": (_contract_str(contract) + _method_str(body)) or None,
        "color": COLOR_REVISION if revision else COLOR_SUBMIT,
        "fields": fields,
        "url": _offer_link(offer),
        "footer": {"text": f"submitted by {offer.get('submitted_by') or '?'}"
                           + (f" · drafted by {offer['created_by']}"
                              if offer.get("created_by") != offer.get("submitted_by") else "")},
    }


def _remand_embed(offer: dict, remand: dict) -> dict:
    slug = offer["player"]
    fields = [
        {"name": "Player", "value": f"[{_name(slug)}]({SITE}/players/?p={slug})", "inline": True},
        {"name": "Offer", "value": f"#{offer['number']} · v{remand.get('from_version', 1)}",
         "inline": True},
        {"name": "Outstanding notes",
         "value": "\n".join(
             f"**{r['by']}:** {_truncate(r.get('note'), 300)}"
             for r in (offer.get("remands") or [])
             if r.get("from_version") == offer.get("version"))[:1000] or "—",
         "inline": False},
    ]
    if remand.get("conflict"):
        # "Send that rival's offer back" is where the incentive bites hardest
        # (§ 4.6), so the flag travels with the announcement, not just the record.
        fields.append({
            "name": "⚠️ Conflict of interest",
            "value": f"{remand['by']}'s own team ({remand['conflict']}) has a live "
                     f"offer on this player. Warned, not blocked.",
            "inline": False,
        })
    return {
        "title": f"{offer['team']} — offer to {_name(slug)} sent back",
        "description": f"**{remand['by']}** asked for a revision. The team may resubmit; "
                       f"nothing else about the offer changed.",
        "color": COLOR_REMAND,
        "fields": fields,
        "url": _offer_link(offer),
        "footer": {"text": f"remanded by {remand['by']}"},
    }


def _void_embed(offer: dict, void: dict) -> dict:
    """§ 4.3b. Carries the terms that were voided, not just the fact of it — the
    bid is leaving the board, so the announcement is the last place it appears in
    full for anyone not reading the review page."""
    slug = offer["player"]
    contract = (offer.get("offer") or {}).get("contract") or {}
    fields = [
        {"name": "Player", "value": f"[{_name(slug)}]({SITE}/players/?p={slug})", "inline": True},
        {"name": "Team", "value": f"[{offer['team']}]({SITE}/teams/{offer['team']})", "inline": True},
        {"name": "Offer", "value": f"#{offer['number']} · v{offer.get('version', 1)}", "inline": True},
        {"name": "Reason", "value": _truncate(void.get("reason"), 1000) or "—", "inline": False},
    ]
    breakdown = _contract_breakdown(contract)
    if breakdown:
        fields.append({"name": "Voided terms", "value": breakdown, "inline": False})
    return {
        "title": f"{offer['team']} — offer to {_name(slug)} voided",
        "description": "Out of play as if it had never been submitted: off the ballot, out of "
                       f"{offer['team']}'s exposure, and they may bid again. The record is kept.",
        "color": COLOR_VOID,
        "fields": fields,
        "url": _offer_link(offer),
        "footer": {"text": f"voided by {void.get('by') or '?'}"},
    }


# ── the two channels ──────────────────────────────────────────────────────────

def _alert(embed_fn) -> bool:
    """Post to the private committee channel. Never raises."""
    try:
        return transport.send(DISCORD_PDC_CHANNEL, embed_fn,
                              max_burst=PDC_MAX_BURST, burst_window=PDC_BURST_WINDOW)
    except Exception as exc:
        logger.warning("PDC alert failed: %s", exc)
        return False


def _news(slug: str, text: str) -> bool:
    """Post to the **public** channel.

    This is the only function in the module that can reach `fa-news`, and it
    takes a player and a finished string — never an offer, never a team, never a
    figure. The § 9.2 promise that no contract data can leak here is that
    signature, not a convention about what callers pass.
    """
    if not transport.configured(DISCORD_FA_NEWS_CHANNEL):
        return False
    try:
        return transport.send(DISCORD_FA_NEWS_CHANNEL, {"content": text},
                              max_burst=NEWS_MAX_BURST, burst_window=NEWS_BURST_WINDOW)
    except Exception as exc:
        logger.warning("FA news post failed for %s: %s", slug, exc)
        return False


# ── events ────────────────────────────────────────────────────────────────────

def notify_offer_submitted(offer: dict, prev: Optional[dict] = None) -> None:
    """An offer reached the committee — the first submission or a resubmission
    after a remand, which is one endpoint and therefore one announcement path
    (§ 4.3a). `prev` is the frozen version being revised, if any."""
    _alert(lambda: {"embeds": [_offer_embed(offer, prev)]})


def notify_offer_remanded(offer: dict, remand: dict) -> None:
    _alert(lambda: {"embeds": [_remand_embed(offer, remand)]})


def notify_offer_voided(offer: dict) -> None:
    """Private channel only — like every other offer event. A void names a team
    and prices its bid, so it is committee information by § 9.2, and `_news`
    could not carry it even if a caller tried."""
    void = offer.get("void") or {}
    _alert(lambda: {"embeds": [_void_embed(offer, void)]})


def notify_offer_restored(offer: dict, void: dict) -> None:
    """The undo. Announced for the same reason `void` is: the committee saw the
    bid leave the board, so it has to see it come back."""
    _alert(lambda: {"embeds": [{
        "title": f"{offer['team']} — offer to {_name(offer['player'])} restored",
        "description": f"The void is undone; the offer is **{offer['status']}** again, "
                       f"on the ballot and back in {offer['team']}'s exposure.",
        "color": COLOR_REVISION,
        "fields": [
            {"name": "Offer", "value": f"#{offer['number']} · v{offer.get('version', 1)}",
             "inline": True},
            {"name": "Had been voided",
             "value": f"{_stamp(void.get('at'))} by {void.get('by') or '?'}", "inline": True},
            {"name": "Stated reason", "value": _truncate(void.get("reason"), 1000) or "—",
             "inline": False},
        ],
        "url": _offer_link(offer),
    }]})


def notify_mode_change(previous: str, mode: str, actor: str) -> None:
    """Private only. The league learns free agency is live from the clock posts
    (§ 9.2), which are the thing it can act on; the mode flip itself is board
    mechanics."""
    _alert(lambda: {"embeds": [{
        "title": f"Free agency mode — {previous} → {mode}",
        "color": COLOR_BOARD,
        "url": PDC_SITE,
        "footer": {"text": f"set by {actor}"},
    }]})


def notify_ffa_window_change(previous: float, hours: float, actor: str,
                             running: int = 0) -> None:
    """Private only, like the mode flip it sits beside — board mechanics. The
    league reads the length off each clock post, which carries the real deadline.

    `running` is stated because it is the question a committee member will ask
    on seeing this, and the answer is always "none of them": clocks keep the
    deadline they were stamped with (§ 4.1)."""
    def num(h):
        return int(h) if float(h).is_integer() else round(float(h), 1)
    _alert(lambda: {"embeds": [{
        "title": f"FFA window length — {num(previous)}h → {num(hours)}h",
        "description": (f"Applies to clocks started from now on. "
                        + (f"**{running}** clock{'s' if running != 1 else ''} already running "
                           f"keep{'' if running != 1 else 's'} the deadline "
                           f"{'they were' if running != 1 else 'it was'} stamped with."
                           if running else "No clocks are currently running.")),
        "color": COLOR_BOARD,
        "url": PDC_SITE,
        "footer": {"text": f"set by {actor}"},
    }]})


def notify_round_opened(rnd: dict, actor: str) -> None:
    closes = rnd.get("closes_at")
    _alert(lambda: {"embeds": [{
        "title": f"{rnd.get('name') or rnd.get('id')} opened",
        "description": (f"Advisory close: {_stamp(closes)} — a display label only; "
                        f"players are closed by hand." if closes else
                        "No advisory close set. Players are opened and closed by hand."),
        "color": COLOR_BOARD,
        "url": PDC_SITE,
        "footer": {"text": f"opened by {actor}"},
    }]})


def notify_ffa_started(slug: str, ffa: dict) -> None:
    """The first *submitted* offer started a player's FFA window (§ 4.1).

    Both channels fire, with different content: the committee gets who started
    it, the league gets the deadline and nothing else.
    """
    deadline = ffa.get("deadline")
    _alert(lambda: {"embeds": [{
        "title": f"FFA clock started — {_name(slug)}",
        "description": f"Offers close {_stamp(deadline)} ({_stamp(deadline, 'R')}).",
        "color": COLOR_BOARD,
        "url": f"{PDC_SITE}/#/p/{slug}",
        "fields": [{"name": "Started by offer", "value": f"`{ffa.get('started_by_offer', '?')}`",
                    "inline": True}],
        "footer": {"text": f"submitted by {ffa.get('started_by') or '?'}"},
    }]})
    _news(slug, f"🕐 **{_name(slug)}** has received an FFA offer. A {_window_label(ffa)}clock "
                f"is now running — other teams have until {_stamp(deadline)} "
                f"({_stamp(deadline, 'R')}) to submit offers.")


def notify_ffa_extended(slug: str, ffa: dict, added_hours: float,
                        reason: str, actor: str, reopened: bool) -> None:
    """The head pushed a window's deadline out (§ 4.1).

    Both channels, for the same reason the clock start fires on both: a team
    deciding whether to bid needs the new deadline, and it is the one FFA fact
    that is not committee-private. The public line carries the player, the new
    deadline and the head's reason — **never a team and never a dollar figure**,
    which `_news` enforces by only accepting a slug and a string.

    `reopened` distinguishes the two cases in the copy: extending a live window
    is more time, while extending an expired one puts a player back on the
    board, and a team that had stopped watching needs to be told which.
    """
    deadline = ffa.get("deadline")
    _alert(lambda: {"embeds": [{
        "title": (f"FFA window {'reopened' if reopened else 'extended'} — {_name(slug)}"),
        "description": (f"Offers now close {_stamp(deadline)} ({_stamp(deadline, 'R')}). "
                        f"Existing offers stand and ballots are unaffected."),
        "color": COLOR_BOARD,
        "url": f"{PDC_SITE}/#/p/{slug}",
        "fields": [
            {"name": "Added", "value": f"{_hours(added_hours)}h", "inline": True},
            {"name": "Window now", "value": _window_label(ffa) or "—", "inline": True},
            {"name": "Reason", "value": reason or "—", "inline": False},
        ],
        "footer": {"text": f"{'reopened' if reopened else 'extended'} by {actor}"},
    }]})
    verb = ("has been **reopened**" if reopened else "has been **extended**")
    _news(slug, f"🕐 The window on **{_name(slug)}** {verb} by {_hours(added_hours)} hours — "
                f"offers now close {_stamp(deadline)} ({_stamp(deadline, 'R')}). "
                f"Reason: {reason}")


def _hours(h: float) -> str:
    return str(int(h)) if float(h).is_integer() else str(round(float(h), 1))


def notify_ffa_closed(slug: str, ffa: dict) -> None:
    """The window expired. Emitted by whichever request first observed it (§ 4.1),
    guarded upstream by a flag on the player's `ffa` object so it fires once.

    **Nothing has been decided here** — expiry closes a submission window and
    hands the player to the committee. The wording says exactly that, because a
    post that reads like an outcome is worse than no post.
    """
    deadline = _parse_ts(ffa.get("deadline"))
    stale = deadline and (datetime.now(timezone.utc) - deadline).total_seconds() \
        > MAX_CLOSE_AGE_HOURS * 3600
    if stale:
        # A closure older than the window it closes is a replay, not news — the
        # case this catches is deploying with long-expired clocks still
        # unflagged in fa-state.json.
        logger.info("FA news skipped for %s — window closed %s, too old to announce", slug, deadline)
        return
    _alert(lambda: {"embeds": [{
        "title": f"FFA window closed — {_name(slug)}",
        "description": "No further offers are accepted. Ready for sub-committee ballots — "
                       "nothing has been decided.",
        "color": COLOR_BOARD,
        "url": f"{PDC_SITE}/#/p/{slug}",
        "footer": {"text": "closed on the clock"},
    }]})
    _news(slug, f"🔒 The {_window_label(ffa)}window on **{_name(slug)}** has closed. No further "
                f"offers are being accepted; the FAC will review.")


def notify_player_finalized(slug: str, final: dict, offers: list[dict]) -> None:
    """Private only — the allocation totals are the committee's record, and
    nothing here signs anybody (§ 11.1). The site never resolves an outcome; the
    FAC enters the signing on /transactions by hand."""
    def build():
        by_id = {o["id"]: o for o in offers}
        totals = final.get("totals") or {}
        rows = []
        for key, n in sorted(totals.items(), key=lambda kv: -kv[1]):
            if key == "NO_SIGNING":
                label = "No signing"
            elif key == "QO":
                label = "Qualifying offer"
            else:
                o = by_id.get(key)
                label = f"{o['team']} · offer #{o['number']}" if o else key
            rows.append(f"{n:>5}  {label}")
        fields = [{"name": "Allocation", "value": "```\n" + ("\n".join(rows) or "no ballots cast") + "\n```",
                   "inline": False},
                  {"name": "Voted", "value": ", ".join(final.get("voters") or []) or "—",
                   "inline": True}]
        if final.get("abstained"):
            fields.append({"name": "Abstained", "value": ", ".join(final["abstained"]),
                           "inline": True})
        if final.get("outstanding_remands"):
            # Warns, never blocks (D15) — the head locked knowing these were
            # unanswered, and the channel should record that they were.
            fields.append({
                "name": "⚠️ Locked with unanswered remands",
                "value": ", ".join(f"{r['team']} #{r['number']}"
                                   for r in final["outstanding_remands"]),
                "inline": False,
            })
        return {"embeds": [{
            "title": f"Finalized — {_name(slug)}",
            "description": "Ballots are locked. **No signing has been made** — the FAC "
                           "enters the transaction by hand.",
            "color": COLOR_FINAL,
            "fields": fields,
            "url": f"{PDC_SITE}/#/p/{slug}",
            "footer": {"text": f"locked by {final.get('locked_by') or '?'}"},
        }]}
    _alert(build)

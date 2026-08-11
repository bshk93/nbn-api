"""Mirror the league's transaction-bearing Discord channels into **#roster-log**.

`#roster-log` is where the committee reads off the changes that still have to be
entered into the sheets and the site. Until now a human copy-pasted each message
into it by hand, from four channels, which is exactly the kind of job that gets
skipped on a busy day. This module does the copying.

The contract is deliberately narrow — **it relays, it does not interpret**:

* The relayed message is the source message's own text, verbatim. No summary, no
  re-parse, no attempt to decide whether a message "is" a transaction. A human
  reads `#roster-log` and enters what it says; a wrong paraphrase there is worse
  than a message that turned out not to need entering.
* Only **parent** messages: `type` 0 and 19 (a plain message and an inline
  reply). Thread messages never appear in a channel's message list, and system
  messages (joins, pins, "started a thread") are not league news.
* Whether a source's bot posts are relayed is **per source**, because the answer
  genuinely differs. `#fa-news` carries our own FFA clock posts (a window
  opening is not a sheet change) so only humans are relayed there; the humans in
  that channel post the signings, renounces, team options and guarantees, and
  all of those are relayed without looking at the words. `#roster-log-nbn-today`
  is *entirely* our own transaction embeds and every one of them is a real
  applied transaction, so all of it is relayed.

Duplicates are expected and intended: a renounce shows up both as a human post
in `#fa-news` and as our embed in `#roster-log-nbn-today`, and both are relayed.
Matching them would need a fuzzy content match on free text, and a missed entry
costs more than a doubled line.

## Reading, in a service that has only ever written

The API has no Discord gateway connection and doesn't want one (`discord.py`
handles slash commands over HTTP for the same reason). This polls
`GET /channels/{id}/messages?after=<cursor>` on a timer; REST reads return
message content without the privileged Message Content intent. Sends go through
`discord_transport`'s shared paced queue, the same one every other feed uses, so
this can't collectively out-run the rate limit alongside them.

## Never dump the channel

Same hard requirement as `discord_notify`, and a relay is *more* exposed to it:
the source channels hold thousands of old messages, and anything that resets the
cursor could try to replay them. Four gates:

1. **Seeding is silent.** A source with no stored cursor is seeded to the
   newest message id and relays nothing. First run posts nothing at all.
2. **Age gate.** A message older than `MAX_AGE_HOURS` is skipped and the cursor
   moves past it. A lost or truncated state file therefore costs at most the
   last day of traffic, never the channel's history — and neither does a
   deploy after a long outage.
3. **Burst cap** on the destination, via the transport (`MAX_BURST` per
   `BURST_WINDOW`).
4. **Page cap** — `MAX_PER_POLL` messages examined per source per cycle.

Gate 3 differs from `discord_notify`'s in one important way: a refusal here is a
**throttle, not a drop**. The cursor is only advanced past a message that was
actually enqueued, so a clipped burst is relayed on the next cycle instead of
being lost. A log that silently skips entries is not a log.

Sizing is measured, from the four sources' real traffic (2026-03-01 → 2026-08-11,
226 relayable messages): busiest day **16**, tightest 15-minute burst **7**. Those
figures predate `#roster-log-nbn-today` carrying a full day's transactions,
though — `discord_notify`'s own measurements (busiest day 52, tightest 10-minute
burst 19, draft day expected 50+) are the real ceiling, since every one of those
posts is relayed. 150/900s clears a draft day with headroom and still stops a
runaway dead. `tests/test_roster_log_relay.py` pins the figures.

Inert without `DISCORD_ROSTER_LOG_CHANNEL`, like every other Discord module here.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import auth
from . import discord_transport as transport
from .constants import DATA_DIR
from .storage import _load_json, _save_json

logger = logging.getLogger("nbn-api")
router = APIRouter()

# Polling four channels a minute is ~5,700 httpx INFO lines a day in the journal,
# which buries everything else the service says. Nothing is lost by quieting it:
# a failed read is logged by `_get` below, and a failed send by the transport.
logging.getLogger("httpx").setLevel(logging.WARNING)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_ROSTER_LOG_CHANNEL = os.environ.get("DISCORD_ROSTER_LOG_CHANNEL", "").strip()

STATE_FILE = DATA_DIR / "roster-log-relay.json"

# The sources, and the one policy decision each carries. Adding a channel is a
# line here; `humans_only` is True only where a bot posts things that are not
# sheet changes (see the module docstring).
SOURCES: list[dict] = [
    {"name": "fa-news",              "id": "1517304922847055994", "humans_only": True},
    {"name": "transactions",         "id": "1517538131950440469", "humans_only": False},
    {"name": "waivers",              "id": "1116542114382217316", "humans_only": False},
    {"name": "roster-log-nbn-today", "id": "1535455964256276490", "humans_only": False},
]

POLL_INTERVAL = 60          # seconds; the league is not in a hurry
MAX_PER_POLL  = 200         # messages examined per source per cycle
MAX_AGE_HOURS = 24          # older than this is history, not news — see gate 2
MAX_BURST     = 150         # per BURST_WINDOW, on the destination channel
BURST_WINDOW  = 900
MAX_MANUAL    = 25          # ids per manual relay call — a pass, not a backfill

DISCORD_LIMIT = 2000        # Discord's own message length ceiling

# Relayed text must never ping. The mention still *renders* as the role or user
# name, so the message reads exactly as it did at the source; it just doesn't
# notify a second time. Nearly every fa-news signing carries a team role ping.
NO_MENTIONS = {"parse": []}

# Each entry goes out as a bare embed: the same text, in a box with a coloured
# spine. Plain text ran the entries together — a multi-line trade and the three
# one-liners after it read as one blob, and #roster-log is read as a checklist of
# separate things to enter. The card carries **no content of its own** (no title,
# no source label, no author, no timestamp); it is a boundary, not a header. One
# neutral colour for the same reason: a per-source palette would be a legend to
# learn, and nobody asked for one.
CARD_COLOR = 0x64748B

_thread_started = False
_thread_lock = threading.Lock()
_state_lock = threading.Lock()


def configured() -> bool:
    return bool(DISCORD_ROSTER_LOG_CHANNEL and DISCORD_BOT_TOKEN)


def _source(name_or_id: str) -> Optional[dict]:
    for s in SOURCES:
        if name_or_id in (s["name"], s["id"]):
            return s
    return None


# ── state ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    st = _load_json(STATE_FILE, {})
    st.setdefault("channels", {})
    return st


def _save_state(st: dict) -> None:
    _save_json(STATE_FILE, st)


# ── Discord reads ─────────────────────────────────────────────────────────────

def _get(path: str, params: Optional[dict] = None):
    """One authenticated GET against the Discord API. Returns the decoded body,
    or None on any failure — a relay that raises would kill its own thread."""
    try:
        r = httpx.get(
            f"https://discord.com/api/v10{path}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            params=params or {},
            timeout=15,
        )
    except Exception as exc:
        logger.warning("roster-log relay: GET %s errored: %s", path, exc)
        return None
    if r.status_code == 429:
        try:
            time.sleep(min(float(r.json().get("retry_after", 1.0)), 30) + 0.25)
        except Exception:
            time.sleep(1.0)
        return None
    if r.status_code >= 300:
        logger.warning("roster-log relay: GET %s failed: %s %s",
                       path, r.status_code, r.text[:200])
        return None
    return r.json()


def _fetch_after(channel_id: str, after: str, limit: int = 100) -> list[dict]:
    """Messages after `after`, oldest first. Discord's ordering within a page is
    not something to rely on, so sort by id — which is a snowflake, so id order
    is time order."""
    msgs = _get(f"/channels/{channel_id}/messages", {"after": after, "limit": limit})
    if not isinstance(msgs, list):
        return []
    return sorted(msgs, key=lambda m: int(m["id"]))


def _fetch_latest_id(channel_id: str) -> Optional[str]:
    msgs = _get(f"/channels/{channel_id}/messages", {"limit": 1})
    if not isinstance(msgs, list):
        return None
    return msgs[0]["id"] if msgs else "0"


# ── what gets relayed, and how it reads ───────────────────────────────────────

def _parts(msg: dict) -> list[dict]:
    """The message, plus anything forwarded inside it.

    A Discord **forward** carries no content of its own — the text lives in
    `message_snapshots`, and the outer message is empty. Forwarding is how some
    of the league actually posts (the #roster-log copies were part typed, part
    forwarded from #fa-news), so treating one as an empty message would drop a
    real signing without a trace. The forwarder is the author, which is what the
    per-source bot policy should judge: a human forwarding something is a human
    posting it.
    """
    return [msg] + [s.get("message") or {} for s in (msg.get("message_snapshots") or [])]


def is_relayable(msg: dict, humans_only: bool) -> bool:
    if msg.get("type") not in (0, 19):      # plain message or inline reply
        return False
    if humans_only and (msg.get("author", {}).get("bot") or msg.get("webhook_id")):
        return False
    return any((p.get("content") or "").strip() or p.get("embeds") or p.get("attachments")
               for p in _parts(msg))


def _delink(value: str) -> str:
    """`[Monte Morris](https://…)` → `Monte Morris`."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value or "").strip()


def _embed_text(embed: dict) -> str:
    """Collapse one embed to plain text.

    Our transaction embeds put the headline in `title`, the substance in
    `description` (multi-line for a trade — those lines are the trade, so they
    are kept) and the player/team in `fields`. The player is only worth adding
    when the description doesn't already name them, which for a release it
    doesn't and for a renounce it does.

    The year-by-year block comes across as-is, code fences and all. The headline
    gives a deal's shape (`2+1 PO · $30.0M`); the per-season figures and which
    years carry an option are what actually gets typed into the sheet, so a log
    that dropped them would send the reader back to the source message.
    """
    title = (embed.get("title") or "").strip()
    desc = (embed.get("description") or "").strip()
    lines = []

    head = title
    player = ""
    for f in embed.get("fields") or []:
        if f.get("name") == "Player":
            player = _delink(f.get("value", ""))
            break
    if player and player.lower() not in f"{title}\n{desc}".lower():
        head = f"{head} · {player}" if head else player
    if head:
        lines.append(head)
    if desc:
        lines.append(desc)

    # The contract table, unchanged — it is already a code block, which is what
    # keeps its columns lined up. No label: under a signing headline, a column of
    # seasons and dollars needs no introduction.
    for f in embed.get("fields") or []:
        if f.get("name") == "Year by year" and (f.get("value") or "").strip():
            lines.append(f["value"].strip())

    # Anything that isn't the player, the teams or the year-by-year block — in
    # practice the overridden-checks warning on a forced transaction, which is
    # precisely the thing a log should not swallow.
    for f in embed.get("fields") or []:
        if f.get("name") in ("Player", "Team", "Teams", "Year by year"):
            continue
        val = _delink(f.get("value", ""))
        if val:
            lines.append(f"{f.get('name', '')}: {val}".strip(": "))

    return "\n".join(lines).strip()


def render(msg: dict) -> str:
    """The text this message becomes in #roster-log. Content is verbatim, and a
    forwarded message contributes the text it forwarded."""
    out = []
    for part in _parts(msg):
        content = (part.get("content") or "").strip()
        if content:
            out.append(content)
        for embed in part.get("embeds") or []:
            text = _embed_text(embed)
            if text:
                out.append(text)
        for att in part.get("attachments") or []:
            if att.get("url"):
                out.append(att["url"])
    return "\n".join(out).strip()


def _chunks(text: str) -> list[str]:
    """Split at Discord's length ceiling, on line boundaries where possible. A
    league message never gets near 2000 characters, but truncating a log entry
    would be a silent edit, so it splits rather than cuts."""
    out: list[str] = []
    for para in text.split("\n"):
        while len(para) > DISCORD_LIMIT:
            out.append(para[:DISCORD_LIMIT])
            para = para[DISCORD_LIMIT:]
        if not out or len(out[-1]) + 1 + len(para) > DISCORD_LIMIT:
            out.append(para)
        else:
            out[-1] = f"{out[-1]}\n{para}"
    return [c for c in out if c.strip()]


def _image_url(msg: dict) -> Optional[str]:
    """The first image attached to the message, if any. Inside an embed a bare
    URL is only a link, so an image-only post (a screenshot of a sheet) would
    lose its preview unless it's hung off the card as `image`."""
    for part in _parts(msg):
        for att in part.get("attachments") or []:
            url = att.get("url") or ""
            if (att.get("content_type") or "").startswith("image/") or \
                    re.search(r"\.(png|jpe?g|gif|webp)(\?|$)", url, re.I):
                return url
    return None


def _send(text: str, image: Optional[str] = None) -> bool:
    """Queue one relayed message. False means nothing was queued — refused by the
    burst cap or the backlog ceiling — which the caller must treat as "try again
    later", not as "done".

    A message that split into chunks and was refused *part way* counts as sent:
    retrying it would repost the chunks that did go through. That trade only
    exists above 2000 characters, where a duplicate is the worse of the two
    failures, and the transport has already logged the refusal.
    """
    sent_any = False
    for i, chunk in enumerate(_chunks(text)):
        card = {"description": chunk, "color": CARD_COLOR}
        if image and i == 0:
            card["image"] = {"url": image}
        ok = transport.send(
            DISCORD_ROSTER_LOG_CHANNEL,
            {"embeds": [card], "allowed_mentions": NO_MENTIONS},
            max_burst=MAX_BURST, burst_window=BURST_WINDOW,
        )
        if ok:
            sent_any = True
        elif sent_any:
            logger.warning("roster-log relay: dropped chunk %d of a split message", i + 1)
    return sent_any


# ── the poll cycle ────────────────────────────────────────────────────────────

def _too_old(msg: dict) -> bool:
    ts = msg.get("timestamp") or ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - when > timedelta(hours=MAX_AGE_HOURS)


def relay_once() -> dict:
    """One pass over every source. Returns {source: relayed_count}."""
    if not configured():
        return {}
    counts: dict[str, int] = {}
    with _state_lock:
        state = _load_state()
        for src in SOURCES:
            cid, name = src["id"], src["name"]
            entry = state["channels"].setdefault(cid, {"name": name})
            if not entry.get("last_id"):
                latest = _fetch_latest_id(cid)
                if latest is None:
                    continue
                entry["last_id"] = latest
                entry["seeded_at"] = datetime.now(timezone.utc).isoformat()
                logger.info("roster-log relay: seeded #%s at %s (nothing relayed)", name, latest)
                continue
            counts[name] = _drain(cid, src, entry)
        _save_state(state)
    return {k: v for k, v in counts.items() if v}


def _drain(cid: str, src: dict, entry: dict) -> int:
    """Relay everything new on one source, mutating `entry["last_id"]` as it
    goes. Stops early — without advancing — on a refused send."""
    relayed = examined = 0
    while examined < MAX_PER_POLL:
        batch = _fetch_after(cid, entry["last_id"])
        if not batch:
            break
        for msg in batch:
            examined += 1
            if _too_old(msg):
                entry["last_id"] = msg["id"]
                continue
            if not is_relayable(msg, src["humans_only"]):
                entry["last_id"] = msg["id"]
                continue
            text = render(msg)
            if not text:
                entry["last_id"] = msg["id"]
                continue
            if not _send(text, _image_url(msg)):
                # Refused, not dropped: leave the cursor behind this message so
                # the next cycle picks it up again.
                logger.warning("roster-log relay: throttled on #%s at %s", src["name"], msg["id"])
                return relayed
            entry["last_id"] = msg["id"]
            entry["last_relayed_at"] = datetime.now(timezone.utc).isoformat()
            relayed += 1
        if len(batch) < 100:
            break
    if relayed:
        logger.info("roster-log relay: %d message(s) from #%s", relayed, src["name"])
    return relayed


def _loop() -> None:
    while True:
        try:
            relay_once()
        except Exception:
            logger.exception("roster-log relay: poll failed")
        time.sleep(POLL_INTERVAL)


def start_roster_log_relay() -> None:
    """Called from the app lifespan. A no-op without the destination channel."""
    global _thread_started
    if not configured():
        logger.info("roster-log relay: inert (DISCORD_ROSTER_LOG_CHANNEL unset)")
        return
    with _thread_lock:
        if _thread_started:
            return
        threading.Thread(target=_loop, daemon=True, name="roster-log-relay").start()
        _thread_started = True
    logger.info("roster-log relay: polling %d source(s) every %ds",
                len(SOURCES), POLL_INTERVAL)


# ── admin: status, and the selective pass over history ────────────────────────
# Seeding deliberately skips everything already in the channels (gate 1), so the
# only way to carry an older message across is to name it. These two endpoints
# are that: list what's there, then relay the ids worth relaying.

class ManualRelay(BaseModel):
    source: str
    message_ids: list[str]


@router.get("/api/roster-log/status")
def relay_status(_: dict = Depends(auth.require_admin)):
    state = _load_state()
    return {
        "configured": configured(),
        "destination": DISCORD_ROSTER_LOG_CHANNEL or None,
        "poll_interval": POLL_INTERVAL,
        "sources": [
            {**src, "cursor": state["channels"].get(src["id"], {})}
            for src in SOURCES
        ],
    }


@router.get("/api/roster-log/candidates")
def relay_candidates(source: str = Query(...),
                     before: Optional[str] = None,
                     after: Optional[str] = None,
                     limit: int = Query(50, ge=1, le=100),
                     _: dict = Depends(auth.require_admin)):
    """What a manual pass has to choose from: the source's messages, each with
    the exact text it would become and whether the filter would take it."""
    src = _source(source)
    if not src:
        raise HTTPException(400, f"unknown source: {source}")
    params: dict = {"limit": limit}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    msgs = _get(f"/channels/{src['id']}/messages", params)
    if not isinstance(msgs, list):
        raise HTTPException(502, "could not read the source channel")
    return {
        "source": src["name"],
        "messages": [
            {
                "id": m["id"],
                "timestamp": m.get("timestamp"),
                "author": m.get("author", {}).get("username"),
                "bot": bool(m.get("author", {}).get("bot")),
                "relayable": is_relayable(m, src["humans_only"]),
                "text": render(m),
            }
            for m in sorted(msgs, key=lambda m: int(m["id"]))
        ],
    }


@router.post("/api/roster-log/relay")
def relay_manual(body: ManualRelay, _: dict = Depends(auth.require_admin)):
    """Relay named messages regardless of age or cursor. Capped, because this is
    for picking a handful out of history — a bulk pass is what the gates exist
    to prevent."""
    if not configured():
        raise HTTPException(503, "roster-log relay is not configured")
    src = _source(body.source)
    if not src:
        raise HTTPException(400, f"unknown source: {body.source}")
    if not body.message_ids:
        raise HTTPException(400, "no message_ids given")
    if len(body.message_ids) > MAX_MANUAL:
        raise HTTPException(400, f"at most {MAX_MANUAL} messages per call")

    results = []
    for mid in body.message_ids:
        msg = _get(f"/channels/{src['id']}/messages/{mid}")
        if not isinstance(msg, dict) or "id" not in msg:
            results.append({"id": mid, "relayed": False, "reason": "not found"})
            continue
        text = render(msg)
        if not text:
            results.append({"id": mid, "relayed": False, "reason": "nothing to relay"})
            continue
        sent = _send(text, _image_url(msg))
        results.append({"id": mid, "relayed": sent,
                        "reason": None if sent else "refused by the burst cap"})
    return {"results": results}

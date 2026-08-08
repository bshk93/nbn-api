"""Shared Discord delivery — one paced queue for every notification module.

Lifted out of `discord_notify.py` when `fa_notify.py` (PDC Phase 6) needed the
same transport. Two modules posting to Discord with two workers of their own
would each pace themselves correctly and still collectively blow past the rate
limit, so there is exactly **one** worker and one queue process-wide.

The split is between delivery and policy:

* **Here:** pacing, retry, the queue-depth backstop, and the per-channel burst
  cap — everything about getting a message onto Discord without being
  rate-limited or dumping a backlog.
* **In each caller:** whether an event is worth announcing at all, what the
  message says, and how big a burst is plausible on *its* channel. Those
  numbers are measured per feed (see `discord_notify`'s sizing note) and one
  module's figures say nothing about the other's.

The burst cap is therefore keyed by channel: an FA offer storm must not suppress
a trade announcement, and a runaway loop on one feed must not silence the other.

Everything here is best-effort and off the request thread. Callers have already
committed a real write by the time they post — Discord being down, slow, or
misconfigured must never fail that write, delay it, or roll it back.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time

import httpx

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# One background worker draining a queue, rather than a thread per message.
# Discord rate-limits channel messages at roughly 5 per 5 seconds; firing a draft
# day's 30 pick signings concurrently would 429 most of them, and a dropped
# announcement is worse than a late one. The worker paces sends and honours the
# `retry_after` Discord hands back, so a burst is *delayed*, never lost.
SEND_INTERVAL = 1.25   # seconds between sends — ~4 per 5s, inside the limit
MAX_RETRIES   = 5      # per message, for 429s and transient 5xx
MAX_QUEUE     = 400    # pending backlog that means something is dumping

_queue: "queue.Queue[dict]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def configured(channel: str) -> bool:
    """A channel id *and* a bot token. Either missing makes the caller inert,
    which is what lets a notification module ship before its channel exists."""
    return bool(channel and DISCORD_BOT_TOKEN)


def _post(msg: dict) -> bool:
    """POST one message. Returns True when delivered. Retries a rate-limit or a
    transient server error rather than dropping the message."""
    channel, payload = msg["channel"], msg["payload"]
    for attempt in range(MAX_RETRIES):
        try:
            r = httpx.post(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                json=payload,
                timeout=10,
            )
            if r.status_code < 300:
                return True
            if r.status_code == 429:
                # Discord tells us exactly how long to wait; respect it.
                try:
                    wait = float(r.json().get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                logger.info("Discord rate-limited, retrying in %.2fs", wait)
                time.sleep(min(wait, 30) + 0.25)
                continue
            if 500 <= r.status_code < 600:
                time.sleep(1.5 * (attempt + 1))
                continue
            # 4xx that isn't a rate limit is a real misconfiguration (bad channel
            # id, bot not in the guild, missing Send Messages) — retrying won't
            # help and would just repeat the log line.
            logger.warning("Discord post to %s failed: %s %s", channel, r.status_code, r.text[:300])
            return False
        except Exception as exc:
            logger.warning("Discord post to %s errored (attempt %d): %s", channel, attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    logger.warning("Discord post to %s gave up after %d attempts", channel, MAX_RETRIES)
    return False


def _worker() -> None:
    last = 0.0
    while True:
        msg = _queue.get()
        try:
            gap = time.time() - last
            if gap < SEND_INTERVAL:
                time.sleep(SEND_INTERVAL - gap)
            _post(msg)
            last = time.time()
        except Exception as exc:      # a worker death would silently end all notifications
            logger.warning("Discord notify worker error: %s", exc)
        finally:
            _queue.task_done()


def _enqueue(msg: dict) -> None:
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="discord-notify").start()
            _worker_started = True
    _queue.put(msg)


# ── Burst cap, per channel ────────────────────────────────────────────────────
# "Don't dump a backlog into the channel" is a hard requirement, and convention
# alone is not a guarantee — this is the gate that holds whatever a caller
# intended. A runaway loop posts `max_burst` times and then goes quiet with one
# log line, instead of emptying thousands of rows into Discord.
#
# Sizing is the caller's, because it is a real trade-off measured against that
# feed's actual activity: too low clips a genuinely busy day, too high isn't a
# cap. See each module's constants for the numbers and where they came from.

_burst_lock = threading.Lock()
_recent_sends: dict[str, list[float]] = {}
_suppressed: dict[str, int] = {}


def _burst_ok(channel: str, max_burst: int, burst_window: int) -> bool:
    now = time.time()
    with _burst_lock:
        recent = [t for t in _recent_sends.get(channel, []) if now - t < burst_window]
        _recent_sends[channel] = recent
        if len(recent) >= max_burst:
            n = _suppressed[channel] = _suppressed.get(channel, 0) + 1
            if n == 1 or n % 50 == 0:
                logger.warning(
                    "Discord notify suppressed on channel %s: %d messages in %ds exceeds the "
                    "burst cap of %d (%d suppressed so far). This usually means something is "
                    "bulk-calling a notify function; it should not.",
                    channel, len(recent), burst_window, max_burst, n,
                )
            return False
        prior = _suppressed.pop(channel, 0)
        if prior:
            logger.info("Discord notify resumed on channel %s after %d suppressed", channel, prior)
        recent.append(now)
        return True


def send(channel: str, payload, *, max_burst: int, burst_window: int) -> bool:
    """Queue one message body (`{"embeds": [...]}` or `{"content": "..."}`).

    `payload` may be a dict or a zero-argument callable returning one. The
    callable form is built **after** the gates pass, which is the point: a
    suppressed message must not cost what its message costs to assemble, and an
    embed can be expensive (transaction embeds load every player bio). A runaway
    loop should be cheap to refuse.

    Returns False when the message was refused — unconfigured channel, backlog
    past `MAX_QUEUE`, the burst cap, or a payload that failed to build. Never
    raises: the caller's write is already committed and a notification problem
    must not surface as a failure.
    """
    if not configured(channel):
        return False
    if _queue.qsize() >= MAX_QUEUE:
        logger.warning(
            "Discord notify queue at %d pending — dropping. Something is bulk-calling "
            "a notify function; it should not.", _queue.qsize(),
        )
        return False
    if not _burst_ok(channel, max_burst, burst_window):
        return False
    if callable(payload):
        try:
            payload = payload()
        except Exception as exc:
            logger.warning("Discord payload for channel %s failed to build: %s", channel, exc)
            return False
    _enqueue({"channel": channel, "payload": payload})
    return True

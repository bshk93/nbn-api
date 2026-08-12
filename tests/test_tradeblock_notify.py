"""Regression tests for routers.tradeblock_notify — the optional per-save
Discord post for manual /tradeblock edits.

Two things matter here:

  * **It only ever fires when the team asked for it.** The trading-block PUT
    endpoint is also how a player silently falls off the block when they're
    traded away (`_scrub_trading_block`, called from `transactions.py`); this
    module has no caller on that path at all — the diff/notify call lives
    only in `roster_picks.put_trading_block`, gated on `body.notify_discord`.
    That's exercised by inspection of the diff logic here, not re-tested
    against the live endpoint (no HTTP harness in this suite for that router).
  * **The message reflects only what actually changed**, not the new state of
    the whole block — added and removed are computed from a real diff, so a
    save that re-sends the same list unchanged says nothing.

Nothing here touches the network — the transport's enqueue is a list append.

    venv/bin/python -m tests.test_tradeblock_notify
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_transport as tp  # noqa: E402
import routers.tradeblock_notify as tn  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"
tn.DISCORD_TRADEBLOCK_CHANNEL = "tb-chan"


def reset():
    SENT.clear()
    tp._recent_sends.clear()
    tp._suppressed.clear()


# ── Unconfigured channel is a no-op ─────────────────────────────────────────
print("\nunconfigured channel")

reset()
tn.DISCORD_TRADEBLOCK_CHANNEL = ""
tn.notify_tradeblock_change("Alice", "PHX", ["Devin Booker"], [], [], [])
check("no channel id sends nothing", len(SENT) == 0, f"{len(SENT)} sent")
tn.DISCORD_TRADEBLOCK_CHANNEL = "tb-chan"

# ── No real change sends nothing ────────────────────────────────────────────
print("\nempty diff")

reset()
tn.notify_tradeblock_change("Alice", "PHX", [], [], [], [])
check("an empty diff sends nothing", len(SENT) == 0, f"{len(SENT)} sent")

# ── Players only ─────────────────────────────────────────────────────────────
print("\nplayers-only diffs")

reset()
tn.notify_tradeblock_change("Alice", "PHX", ["Devin Booker", "Bradley Beal"], [], [], [])
check("additions-only sent one message", len(SENT) == 1)
content = SENT[0][1]["content"]
check("names both additions", "Devin Booker" in content and "Bradley Beal" in content, content)
check("names the editor and team", "Alice" in content and "PHX" in content, content)
check("says added, not removed", "added" in content and "removed" not in content, content)

reset()
tn.notify_tradeblock_change("Bob", "GSW", [], ["Klay Thompson"], [], [])
check("removals-only sent one message", len(SENT) == 1)
content = SENT[0][1]["content"]
check("says removed, not added", "removed" in content and "added" not in content, content)

reset()
tn.notify_tradeblock_change("Bob", "GSW", ["Jonathan Kuminga"], ["Klay Thompson"], [], [])
check("mixed add+remove sent one message", len(SENT) == 1)
content = SENT[0][1]["content"]
check("mentions both directions", "added" in content and "removed" in content, content)

# ── Picks are labelled, own vs. acquired ────────────────────────────────────
print("\npick labels")

reset()
tn.notify_tradeblock_change("Cara", "BOS", [], [], [
    {"year": 2027, "round": "1st", "team": "Own"},
    {"year": 2028, "round": "2nd", "team": "NYK"},
], [])
content = SENT[0][1]["content"]
check("own pick has no origin suffix", "2027 1st" in content and "2027 1st (" not in content, content)
check("acquired pick names its origin", "2028 2nd (NYK)" in content, content)

# ── Channel isolation from the other feeds ──────────────────────────────────
print("\nchannel sizing")

check("tradeblock burst cap is generous for a handful of manual saves/day",
      tn.MAX_BURST >= 10, tn.MAX_BURST)

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

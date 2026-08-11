"""Regression tests for routers.roster_log_relay — the #roster-log mirror.

The load-bearing properties are about *not* posting, and about not editing:

  * **A first run posts nothing.** Seeding is silent, so deploying against four
    channels holding thousands of messages relays zero of them.
  * **Old messages are never relayed**, whatever the cursor says. A lost state
    file, or a deploy after a week down, costs the last day at most — it can't
    replay the channel's history.
  * **A refused send is a throttle, not a drop.** The cursor stays behind the
    refused message and the next cycle relays it. This is the one place this
    module deliberately differs from `discord_notify`, where a suppressed
    announcement is simply gone: a log that silently skips entries is not a log.
  * **The text is the source's text.** Content is passed through byte for byte,
    including the role pings — which must render and must not notify, so the
    payload carries `allowed_mentions: {"parse": []}` rather than stripping
    anything out of the message.
  * **Per-source bot policy.** Our FFA clock posts in #fa-news are not sheet
    changes and are skipped; our transaction embeds in #roster-log-nbn-today are
    every one of them a real applied transaction and are relayed.

Nothing here touches the network: `_get` is replaced by a fake channel store and
the transport's enqueue by a list append.

    venv/bin/python -m tests.test_roster_log_relay
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_transport as tp  # noqa: E402
import routers.roster_log_relay as rl  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── Harness ───────────────────────────────────────────────────────────────────

SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"
rl.DISCORD_BOT_TOKEN = "test-token"
rl.DISCORD_ROSTER_LOG_CHANNEL = "dest-chan"
rl.STATE_FILE = Path(tempfile.mkdtemp()) / "roster-log-relay.json"

FA_NEWS = "1517304922847055994"
TXNS    = "1517538131950440469"
WAIVERS = "1116542114382217316"
BOT_LOG = "1535455964256276490"

STORE: dict[str, list[dict]] = {}
_next_id = [100000000000000000]


def iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def msg(channel: str, content="", *, bot=False, mtype=0, hours_ago=0.1,
        embeds=None, attachments=None, author="kjurgs") -> dict:
    _next_id[0] += 1
    m = {"id": str(_next_id[0]), "type": mtype, "timestamp": iso(hours_ago),
         "content": content, "author": {"username": author, "bot": bot},
         "embeds": embeds or [], "attachments": attachments or []}
    STORE.setdefault(channel, []).append(m)
    return m


def fake_get(path: str, params=None):
    params = params or {}
    parts = path.strip("/").split("/")
    msgs = STORE.get(parts[1], [])
    if len(parts) == 4:                                    # /channels/{cid}/messages/{mid}
        return next((m for m in msgs if m["id"] == parts[3]), {})
    msgs = sorted(msgs, key=lambda m: int(m["id"]))
    if params.get("after"):
        msgs = [m for m in msgs if int(m["id"]) > int(params["after"])]
    if params.get("before"):
        msgs = [m for m in msgs if int(m["id"]) < int(params["before"])]
    limit = int(params.get("limit", 50))
    return list(reversed(msgs[-limit:] if not params.get("after") else msgs[:limit]))


rl._get = fake_get


def reset(seed=True):
    SENT.clear()
    STORE.clear()
    tp._recent_sends.clear()
    tp._suppressed.clear()
    if rl.STATE_FILE.exists():
        rl.STATE_FILE.unlink()
    if seed:
        for cid in (FA_NEWS, TXNS, WAIVERS, BOT_LOG):
            msg(cid, "pre-existing history", hours_ago=0.2)
        rl.relay_once()          # seeds every cursor, posts nothing
        SENT.clear()


def cards() -> list[dict]:
    return [p["embeds"][0] for c, p in SENT if c == "dest-chan"]


def texts() -> list[str]:
    return [card["description"] for card in cards()]


def cursor(cid: str) -> str:
    return json.loads(rl.STATE_FILE.read_text())["channels"][cid].get("last_id", "")


# ── 1. Seeding is silent ──────────────────────────────────────────────────────

print("\n== seeding")
reset(seed=False)
for cid in (FA_NEWS, TXNS, WAIVERS, BOT_LOG):
    for i in range(50):
        msg(cid, f"old message {i}", hours_ago=0.2)
relayed = rl.relay_once()
check("a first run over 200 existing messages relays none", texts() == [], texts()[:2])
check("...and returns nothing relayed", relayed == {}, relayed)
check("...but the cursors are set", all(cursor(c) for c in (FA_NEWS, TXNS, WAIVERS, BOT_LOG)))

msg(TXNS, "Trade 45: | ATL receives: X | BOS receives: Y")
rl.relay_once()
check("the next message after seeding is relayed",
      texts() == ["Trade 45: | ATL receives: X | BOS receives: Y"], texts())


# ── 2. Per-source bot policy ──────────────────────────────────────────────────

print("\n== who gets relayed")
reset()
msg(FA_NEWS, "🕐 **Aj Green** has received an FFA offer. A 48-hour clock is now running",
    bot=True, author="NBN API bot")
msg(FA_NEWS, "🔒 The 24-hour window on **Sam Merrill** has closed.", bot=True, author="NBN API bot")
msg(FA_NEWS, "The Washington Wizards renounce their rights to Nick Smith Jr")
rl.relay_once()
check("#fa-news relays the human, not our FFA clock posts",
      texts() == ["The Washington Wizards renounce their rights to Nick Smith Jr"], texts())

reset()
msg(BOT_LOG, "", bot=True, author="NBN API bot", embeds=[{
    "title": "BOS — Release",
    "description": "Dead cap: $2.2M (26-27)",
    "fields": [{"name": "Player", "value": "[Hunter Sallis](https://nbn.today/players/?p=sallis-hunter)"},
               {"name": "Team", "value": "[BOS](https://nbn.today/teams/BOS)"}],
    "footer": {"text": "bryn · 2026-08-09"}}])
rl.relay_once()
check("#roster-log-nbn-today relays our own bot", len(texts()) == 1, texts())

reset()
msg(FA_NEWS, "The Phoenix Suns sign Kris Dunn", mtype=19)          # inline reply
msg(FA_NEWS, "started a thread", mtype=18)                          # system
msg(FA_NEWS, "")                                                    # nothing to say
rl.relay_once()
check("an inline reply is a parent message and is relayed",
      texts() == ["The Phoenix Suns sign Kris Dunn"], texts())


# ── 3. The text is the source's text ──────────────────────────────────────────

print("\n== rendering")
reset()
VERBATIM = ("Goga Bitadze has signed a 2+1 PO $30,000,000 contract with the "
            "Los Angeles Clippers <@&663947145610657802>")
msg(FA_NEWS, VERBATIM)
rl.relay_once()
check("content is passed through byte for byte", texts() == [VERBATIM], texts())
payload = [p for c, p in SENT if c == "dest-chan"][0]
check("the role ping is kept in the text but suppressed",
      payload["allowed_mentions"] == {"parse": []}
      and "<@&663947145610657802>" in payload["embeds"][0]["description"])

# The card is a boundary, not a header: entries ran together as plain text, and
# the fix is the box, not a label on it. Anything added here is content the
# source message didn't have.
check("each entry is its own card", len(payload["embeds"]) == 1 and "content" not in payload)
check("...carrying the text and nothing else",
      set(payload["embeds"][0]) == {"description", "color"}, payload["embeds"][0])

reset()
msg(BOT_LOG, "", bot=True, embeds=[{
    "title": "GSW — Renounce",
    "description": "Renounced — free agent, no dead cap\n\n*GSW renounce Monte Morris*",
    "fields": [{"name": "Player", "value": "[Monte Morris](https://nbn.today/players/?p=morris-monte)"},
               {"name": "Team", "value": "[GSW](https://nbn.today/teams/GSW)"}]}])
rl.relay_once()
out = texts()[0]
check("an embed becomes plain text", out.startswith("GSW — Renounce"), out)
check("...naming the player once, not twice", out.count("Monte Morris") == 1, out)
check("...with the links flattened", "http" not in out, out)

reset()
msg(BOT_LOG, "", bot=True, embeds=[{
    "title": "BOS — Release",
    "description": "Dead cap: $2.2M (26-27)",
    "fields": [{"name": "Player", "value": "[Hunter Sallis](https://nbn.today/players/?p=sallis-hunter)"}]}])
rl.relay_once()
check("...and the player added when the description doesn't name them",
      "Hunter Sallis" in texts()[0], texts())

reset()
msg(BOT_LOG, "", bot=True, embeds=[{
    "title": "PHX — Signing",
    "description": "2+1 PO · $30.0M · Bird Rights (QVFA)",
    "fields": [{"name": "Player", "value": "[Kris Dunn](https://nbn.today/players/?p=dunn-kris)"},
               {"name": "Team", "value": "[PHX](https://nbn.today/teams/PHX)"},
               {"name": "Year by year",
                "value": "```\n26-27  $14,500,000\n27-28  $15,500,000   Player Option\n28-29            —   UFA hold\n```"}]}])
rl.relay_once()
out = texts()[0]
check("the year-by-year figures come across", "$14,500,000" in out and "$15,500,000" in out, out)
check("...with the option year labelled", "Player Option" in out and "UFA hold" in out, out)
check("...still inside its code fences, so the columns line up",
      out.count("```") == 2 and "\n```\n" in out, repr(out))
check("...and the headline still leads", out.startswith("PHX — Signing · Kris Dunn"), out)
check("...naming the player once", out.count("Kris Dunn") == 1, out)

reset()
msg(BOT_LOG, "", bot=True, embeds=[{
    "title": "ATL · MIA — Trade",
    "description": "**ATL** receives:\n　← Dean Wade\n**MIA** receives:\n　← 2028 R2 LAC",
    "fields": [{"name": "Teams", "value": "[ATL](x) · [MIA](x)"},
               {"name": "⚠️ Overridden checks", "value": "`hard_cap`"}]}])
rl.relay_once()
out = texts()[0]
check("a trade keeps its per-team lines", out.count("receives") == 2, out)
check("an overridden check survives the collapse", "hard_cap" in out, out)

reset()
# A Discord forward: the outer message is empty and the text is in the snapshot.
# This is how part of #roster-log was actually filled in by hand, so a relay that
# read it as an empty message would drop a real signing without a trace.
forward = msg(FA_NEWS, "", author="avatar118")
forward["message_snapshots"] = [{"message": {
    "type": 0, "content": "The Minnesota Timberwolves sign Jakob Poeltl to a 1 year, $9,366,000 contract.",
    "embeds": [], "attachments": []}}]
rl.relay_once()
check("a forwarded message relays what it forwarded",
      texts() == ["The Minnesota Timberwolves sign Jakob Poeltl to a 1 year, $9,366,000 contract."],
      texts())

reset()
fwd_bot = msg(FA_NEWS, "", bot=True, author="NBN API bot")
fwd_bot["message_snapshots"] = [{"message": {"type": 0, "content": "forwarded by a bot",
                                             "embeds": [], "attachments": []}}]
rl.relay_once()
check("...and the forwarder is who the bot policy judges", texts() == [], texts())

reset()
msg(WAIVERS, "", attachments=[{"url": "https://cdn.discordapp.com/attachments/1/2/sheet.png"}])
rl.relay_once()
check("an image-only message relays its attachment",
      texts() == ["https://cdn.discordapp.com/attachments/1/2/sheet.png"], texts())
check("...and hangs it off the card, since a URL inside an embed is only a link",
      cards()[0].get("image", {}).get("url", "").endswith("sheet.png"), cards())

reset()
long_msg = "\n".join(f"line {i} " + "x" * 90 for i in range(40))     # > 2000 chars
msg(TXNS, long_msg)
rl.relay_once()
check("an over-long message splits rather than truncates",
      len(texts()) > 1 and all(len(t) <= rl.DISCORD_LIMIT for t in texts())
      and "".join(texts()).replace("\n", "") == long_msg.replace("\n", ""),
      [len(t) for t in texts()])


# ── 4. Never dump: the age gate ───────────────────────────────────────────────

print("\n== anti-dump")
reset()
for i in range(30):
    msg(TXNS, f"ancient trade {i}", hours_ago=rl.MAX_AGE_HOURS + 5)
msg(TXNS, "today's trade", hours_ago=0.1)
rl.relay_once()
check("messages older than the age gate are never relayed",
      texts() == ["today's trade"], texts())
check("...and the cursor still moved past them", cursor(TXNS) == STORE[TXNS][-1]["id"])

reset()
rl.STATE_FILE.unlink()                       # the state file is lost entirely
for i in range(500):
    msg(TXNS, f"history {i}", hours_ago=100)
rl.relay_once()
check("losing the state file re-seeds rather than replaying 500 messages", texts() == [], texts())


# ── 5. A refused send is a throttle, not a drop ───────────────────────────────

print("\n== throttling")
reset()
msg(TXNS, "trade A")
msg(TXNS, "trade B")
msg(TXNS, "trade C")
real_send = tp.send
calls = [0]


def refusing_send(channel, payload, **kw):
    calls[0] += 1
    return False if calls[0] == 2 else real_send(channel, payload, **kw)


tp.send = refusing_send
rl.relay_once()
check("a refused send stops the pass", texts() == ["trade A"], texts())
tp.send = real_send
rl.relay_once()
check("...and the next cycle relays what was refused, in order",
      texts() == ["trade A", "trade B", "trade C"], texts())

reset()
msg(TXNS, "trade D")
rl.relay_once()
rl.relay_once()
rl.relay_once()
check("a message is relayed exactly once across repeated polls", texts() == ["trade D"], texts())


# ── 6. Burst sizing is measured, not guessed ──────────────────────────────────
# The four sources measured 16 on their busiest day and 7 in their tightest
# 15-minute burst (2026-03-01 → 2026-08-11). But #roster-log-nbn-today carries
# every live transaction, so `discord_notify`'s own measurements are the real
# ceiling: busiest day 52, tightest 10-minute burst 19, draft day expected 50+.
# If league activity outgrows these, this fails rather than messages quietly
# going missing.

print("\n== sizing")
BUSIEST_DAY_TXNS = 52
DRAFT_DAY_EXPECTED = 60
check("the burst cap clears a draft day with headroom",
      rl.MAX_BURST >= 2 * DRAFT_DAY_EXPECTED, rl.MAX_BURST)
check("...and the busiest measured transaction day",
      rl.MAX_BURST >= 2 * BUSIEST_DAY_TXNS, rl.MAX_BURST)
check("the burst window is a real window, not a rate", rl.BURST_WINDOW >= 600)
check("a poll can't examine an unbounded backlog", rl.MAX_PER_POLL <= 500)
check("the age gate is a day, not a week", rl.MAX_AGE_HOURS <= 48)

reset()
for i in range(rl.MAX_BURST + 20):
    msg(TXNS, f"runaway {i}")
rl.relay_once()
check("a runaway posts the cap and then goes quiet", len(texts()) == rl.MAX_BURST, len(texts()))


# ── 7. Inert without configuration ────────────────────────────────────────────

print("\n== configuration")
reset()
msg(TXNS, "a real trade")
rl.DISCORD_ROSTER_LOG_CHANNEL = ""
try:
    check("unconfigured, a poll relays nothing", rl.relay_once() == {} and texts() == [])
    rl.start_roster_log_relay()
    check("...and the poll thread never starts", not rl._thread_started)
finally:
    rl.DISCORD_ROSTER_LOG_CHANNEL = "dest-chan"


# ── 8. Nothing raises into the poll loop ──────────────────────────────────────

print("\n== robustness")
reset()
rl._get = lambda path, params=None: None          # Discord unreachable
try:
    rl.relay_once()
    check("a dead Discord doesn't raise", True)
except Exception as exc:
    check("a dead Discord doesn't raise", False, exc)
rl._get = fake_get

reset()
msg(TXNS, "trade E", hours_ago=0)
STORE[TXNS][-1]["timestamp"] = "not-a-timestamp"
try:
    rl.relay_once()
    check("a junk timestamp doesn't raise", True)
except Exception as exc:
    check("a junk timestamp doesn't raise", False, exc)

reset()
msg(BOT_LOG, "", bot=True, embeds=[{}])           # an embed with nothing in it
try:
    rl.relay_once()
    check("an empty embed relays nothing and doesn't raise", texts() == [], texts())
except Exception as exc:
    check("an empty embed relays nothing and doesn't raise", False, exc)


print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

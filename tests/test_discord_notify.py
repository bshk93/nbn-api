"""Regression tests for routers.discord_notify — Discord announcements for live
transactions.

The load-bearing requirement is **negative**: the channel must never receive a
dump. The ledger holds 2,241 entries of which 1,935 are backfill, so any code
path that iterates it and notifies would fire ~2,000 messages and rate-limit the
bot. Convention alone doesn't guarantee that, so three independent gates do, and
this module exists to prove each one holds on its own:

  1. call-site opt-in (historical entries never notify)
  2. freshness (a replayed old entry never notifies)
  3. burst cap (a runaway loop is clipped, not unbounded)

Nothing here touches the network — `_post` is replaced with a counter.

    venv/bin/python -m tests.test_discord_notify
"""
from __future__ import annotations

import sys
import time as time_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_notify as dn  # noqa: E402
import routers.discord_transport as tp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── Harness ───────────────────────────────────────────────────────────────────
# Capture at the enqueue boundary — in `discord_transport`, which is where
# delivery moved when `fa_notify` came to share it. Every gate runs before it,
# and the worker that drains the queue is exercised separately below.
SENT = []
tp._enqueue = lambda msg: SENT.append(msg["payload"]["embeds"][0])
tp.DISCORD_BOT_TOKEN = "test-token"
dn.DISCORD_TXN_CHANNEL = "123456789"

BIOS = {
    "curry-stephen": {"name": "CURRY, STEPHEN"},
    "james-lebron":  {"name": "JAMES, LEBRON"},
}
dn.load_player_bios = lambda: BIOS


def now_stamp(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def txn(**kw):
    base = {
        "id": "abc123", "type": "sign", "date": "2026-08-08",
        "created_by": "tester", "created_at": now_stamp(),
        "description": "", "details": {"player": "curry-stephen", "team": "GSW"},
    }
    base.update(kw)
    return base


def reset():
    SENT.clear()
    tp._recent_sends.clear()
    tp._suppressed.clear()


# ── Gate 1: backfill never notifies ───────────────────────────────────────────
print("\ngate 1 — historical/backfill entries are silent")

reset()
dn.notify_transaction(txn(details={"player": "curry-stephen", "team": "GSW", "historical": True}))
check("a historical entry sends nothing", len(SENT) == 0, f"{len(SENT)} sent")

reset()
for i in range(500):
    dn.notify_transaction(txn(id=f"h{i}", details={"player": "curry-stephen", "historical": True}))
check("500 backfill entries send nothing", len(SENT) == 0, f"{len(SENT)} sent")

reset()
dn.notify_transaction(txn())
check("a live entry does send", len(SENT) == 1, f"{len(SENT)} sent")


# ── Gate 2: freshness ─────────────────────────────────────────────────────────
print("\ngate 2 — replaying old ledger entries is silent")

reset()
dn.notify_transaction(txn(created_at=now_stamp(-dn.MAX_AGE_SECONDS - 60)))
check("an entry older than the freshness window sends nothing", len(SENT) == 0)

reset()
dn.notify_transaction(txn(created_at="2020-01-01T00:00:00Z"))
check("a years-old entry sends nothing", len(SENT) == 0)

reset()
dn.notify_transaction(txn(created_at=now_stamp(-10)))
check("a 10-second-old entry still sends", len(SENT) == 1)

# The realistic disaster: someone loops the whole ledger and calls notify.
reset()
for i in range(2241):
    dn.notify_transaction(txn(id=f"old{i}", created_at="2026-07-01T12:00:00Z"))
check("replaying the entire 2,241-entry ledger sends nothing", len(SENT) == 0, f"{len(SENT)} sent")


# ── Gate 3: burst cap ─────────────────────────────────────────────────────────
print("\ngate 3 — a runaway loop is clipped")

reset()
for i in range(2000):
    dn.notify_transaction(txn(id=f"f{i}"))
check(f"2,000 fresh entries are capped at MAX_BURST ({dn.MAX_BURST})",
      len(SENT) == dn.MAX_BURST, f"{len(SENT)} sent")
check("...and the suppression is recorded for the log",
      tp._suppressed.get(dn.DISCORD_TXN_CHANNEL, 0) > 0, tp._suppressed)


# ── Sizing against measured league activity ───────────────────────────────────
# These are the numbers the cap exists to distinguish between. Sourced from the
# real ledger, so if league activity grows past them the test says so rather
# than messages silently going missing on a busy day.
print("\nsizing — real activity must never be clipped")

BUSIEST_REAL_DAY   = 52    # 2026-06-21, live transactions created
TIGHTEST_REAL_10M  = 19    # densest actual 10-minute window in the ledger
DRAFT_DAY_EXPECTED = 80    # ~30 pick signings + trades, with headroom

for label, n in [("the busiest real day (52)", BUSIEST_REAL_DAY),
                 ("the tightest real 10-min burst (19)", TIGHTEST_REAL_10M),
                 ("an expected draft day (80)", DRAFT_DAY_EXPECTED)]:
    reset()
    for i in range(n):
        dn.notify_transaction(txn(id=f"s{i}"))
    check(f"{label} is delivered in full", len(SENT) == n, f"{len(SENT)}/{n}")

# The old 20/300s setting would have clipped a burst that actually happened.
check("cap clears the tightest real burst with room to spare",
      dn.MAX_BURST >= TIGHTEST_REAL_10M * 3, f"{dn.MAX_BURST}")
check("cap still stops a full-ledger runaway well short",
      dn.MAX_BURST < 500, f"{dn.MAX_BURST}")


# ── Queue depth backstop ──────────────────────────────────────────────────────
print("\nqueue depth — backpressure when something dumps")

reset()
tp._enqueue = lambda msg: (SENT.append(msg["payload"]["embeds"][0]), tp._queue.put(msg))[0]
for i in range(tp.MAX_QUEUE + 60):
    dn.notify_transaction(txn(id=f"q{i}"))
check("a backlog past MAX_QUEUE stops accepting", len(SENT) <= tp.MAX_QUEUE, f"{len(SENT)} accepted")
while not tp._queue.empty():
    tp._queue.get_nowait()
tp._enqueue = lambda msg: SENT.append(msg["payload"]["embeds"][0])


# ── Kill switch ───────────────────────────────────────────────────────────────
print("\nkill switch — unconfigured means inert")

reset()
dn.DISCORD_TXN_CHANNEL = ""
for i in range(100):
    dn.notify_transaction(txn(id=f"x{i}"))
check("no channel configured sends nothing", len(SENT) == 0)
dn.DISCORD_TXN_CHANNEL = "123456789"

reset()
tp.DISCORD_BOT_TOKEN = ""
dn.notify_transaction(txn())
check("no bot token sends nothing", len(SENT) == 0)
tp.DISCORD_BOT_TOKEN = "test-token"


# ── Embed content ─────────────────────────────────────────────────────────────
print("\nembed content")

reset()
dn.notify_transaction(txn(
    type="sign",
    details={"player": "curry-stephen", "team": "GSW",
             "contract": {"type": "player", "salaries": {"26-27": "$50,000,000", "27-28": "$54,000,000"}},
             "signing_method": "bird_rights", "bird_rights_type": "QVFA"}))
e = SENT[0]
check("title names team and type", e["title"] == "GSW — Signing", e["title"])
check("description carries years and total", "2 yrs · $104.0M" in e["description"], e["description"])
check("...and the funding method", "Bird Rights (QVFA)" in e["description"])
check("player field links to the profile",
      any("/players/?p=curry-stephen" in f["value"] for f in e["fields"]))
check("footer names the submitter", "tester" in e["footer"]["text"], e["footer"]["text"])

reset()
dn.notify_transaction(txn(
    type="renounce", created_by="someowner",
    details={"player": "curry-stephen", "team": "GSW", "_source": "owner_self_serve"}))
check("owner self-serve is marked in the footer",
      "team owner" in SENT[0]["footer"]["text"], SENT[0]["footer"]["text"])

reset()
dn.notify_transaction(txn(type="release",
    details={"player": "curry-stephen", "team": "GSW",
             "dead_cap": {"26-27": "$10,000,000", "27-28": "$5,000,000"}}), None)
check("release reports dead cap", "Dead cap: $15.0M" in SENT[0]["description"], SENT[0]["description"])

reset()
dn.notify_transaction(txn(
    type="trade",
    details={"teams": ["GSW", "LAL"], "transfers": [
        {"from_team": "GSW", "to_team": "LAL", "assets": [{"type": "player", "slug": "curry-stephen"}]},
        {"from_team": "LAL", "to_team": "GSW", "assets": [
            {"type": "player", "slug": "james-lebron"},
            {"type": "pick", "year": 2028, "round": 1, "orig": "LAL", "protection": 4}]},
    ]}))
e = SENT[0]
check("trade title lists both teams", e["title"] == "GSW · LAL — Trade", e["title"])
check("trade groups by receiving team", "**GSW** receives:" in e["description"] and "**LAL** receives:" in e["description"])
check("trade resolves player names", "Stephen Curry" in e["description"] and "Lebron James" in e["description"])
check("trade shows pick protection", "top-4 prot." in e["description"], e["description"])
check("no Player field on a trade", not any(f["name"] == "Player" for f in e["fields"]))

reset()
dn.notify_transaction(txn(), forced_checks=["hard_cap", "roster_max"])
e = SENT[0]
check("forced checks are surfaced", any("Overridden" in f["name"] for f in e["fields"]))
check("...naming each one",
      any("hard_cap" in f["value"] and "roster_max" in f["value"] for f in e["fields"]))
check("...and recolour the embed", e["color"] == 0xF59E0B, hex(e["color"]))

reset()
dn.notify_transaction(txn(description="Sign-and-trade fallout"))
check("the submitter's note is included", "Sign-and-trade fallout" in SENT[0]["description"])


# ── Contract detail ───────────────────────────────────────────────────────────
# The headline gives a deal's shape; the year-by-year field is where the actual
# figures and the option years get checked. Shorthand mirrors team.js's
# summarizeContract so Discord and the roster page never describe one deal two
# different ways.
print("\ncontract detail — year by year, options flagged")


def yearly(contract):
    reset()
    dn.notify_transaction(txn(details={"player": "curry-stephen", "team": "GSW", "contract": contract}))
    e = SENT[0]
    f = next((f for f in e["fields"] if f["name"] == "Year by year"), None)
    return e["description"], (f["value"] if f else "")


desc, table = yearly({"type": "player", "salaries": {
    "26-27": "$50,000,000", "27-28": "$54,000,000", "28-29": "$58,000,000"}})
check("a flat deal reads as N yrs", desc.startswith("3 yrs · $162.0M"), desc)
check("every year is listed", all(y in table for y in ("26-27", "27-28", "28-29")), table)
check("figures are full dollars, not rounded millions", "$50,000,000" in table)
check("the table is a code block so columns align", table.startswith("```"))

desc, table = yearly({"type": "player",
    "salaries": {"26-27": "$50,000,000", "27-28": "$54,000,000", "28-29": "$58,000,000"},
    "cap_holds": {"28-29": "PLAYER_OPT"}})
check("a player option shows in the headline shape", desc.startswith("2+1 PO"), desc)
check("...and is named in full on its year", "28-29" in table and "player option" in table, table)

desc, table = yearly({"type": "player",
    "salaries": {"26-27": "$50,000,000", "27-28": "$54,000,000"},
    "cap_holds": {"27-28": "TEAM_OPT"}})
check("a team option reads 1+1 TO", desc.startswith("1+1 TO"), desc)
check("...and is spelled out", "team option" in table, table)

desc, table = yearly({"type": "player",
    "salaries": {"26-27": "$5,000,000", "27-28": "$5,300,000"},
    "cap_holds": {"26-27": "NON_GTD", "27-28": "TEAM_OPT"}})
check("mixed non-guaranteed + option years both show", desc.startswith("1 NG+1 TO"), desc)
check("...non-guaranteed spelled out", "non-guaranteed" in table, table)

# The trailing hold is what the deal rolls into, not a year of it — it must not
# be counted in the year total or it inflates every contract that has one.
desc, table = yearly({"type": "player",
    "salaries": {"26-27": "$20,000,000", "27-28": "$1"},
    "cap_holds": {"27-28": "UFA"}})
check("a trailing UFA hold doesn't count as a contract year", desc.startswith("1 yr · $20.0M"), desc)
check("...but is still shown, so the roll-off is visible", "UFA hold" in table, table)
check("...with its nominal $1 placeholder hidden", "$1\n" not in table and "$1 " not in table, table)

desc, table = yearly({"type": "two-way", "salaries": {}})
check("a two-way deal says so", desc.startswith("Two-Way"), desc)
check("...with no year table to show", table == "", table)


# ── Offer sheets ──────────────────────────────────────────────────────────────
# Two events now: the offer being extended, and the incumbent's decision.
# `teams` is stored [offering, retaining]. Joining with an arrow said the
# opposite of what happened on a non-match — the player leaves for the OFFERING
# team. The header must never imply the wrong destination.
print("\noffer sheets — pending, then resolved")

C = {"type": "player", "salaries": {"26-27": "$20,000,000", "27-28": "$21,000,000"}}
SIDES = {"player": "curry-stephen", "offering_team": "LAL", "teams": ["LAL", "GSW"]}

# An offer that has been extended and is waiting on the incumbent. This is the
# state that used to be invisible — it must announce itself and say who owes a
# decision, or the split reintroduces the bug that forced the original merge.
reset()
dn.notify_transaction(txn(type="offer_sheet", details={
    **SIDES, "contract": C, "deadline": "2026-08-10"}))
e = SENT[0]
check("pending: title leads with the incumbent, who owes the decision",
      e["title"] == "GSW — Offer Sheet", e["title"])
check("...names who must respond", "GSW has 48 hours to match" in e["description"], e["description"])
check("...credits the offering team", "Offered by **LAL**" in e["description"])
check("...and shows the deadline", "2026-08-10" in e["description"])
check("...with no claim about an outcome",
      "stays with" not in e["description"] and "signs with" not in e["description"])
check("pending offers still show the terms",
      any(f["name"] == "Year by year" for f in e["fields"]))

reset()
dn.notify_transaction(txn(type="offer_sheet_decision", details={
    **SIDES, "contract": C, "outcome": "not_matched"}))
e = SENT[0]
check("not matched: title leads with the team that GETS the player",
      e["title"] == "LAL — Offer Sheet Decision", e["title"])
check("...and says plainly where they end up", "signs with LAL" in e["description"], e["description"])
check("...naming the team that passed",
      "Not matched" in e["description"] and "GSW" in e["description"], e["description"])
check("...with no misleading arrow", "→" not in e["title"])

reset()
dn.notify_transaction(txn(type="offer_sheet_decision", details={
    **SIDES, "contract": C, "outcome": "matched"}))
e = SENT[0]
check("matched: title leads with the team that KEEPS the player",
      e["title"] == "GSW — Offer Sheet Decision", e["title"])
check("...and says they stay", "stays with GSW" in e["description"], e["description"])
check("...crediting the match to the incumbent", "GSW matched" in e["description"])
check("roles are labelled, not left to ordering",
      any("offering" in f["value"] and "incumbent" in f["value"] for f in e["fields"]))
check("decisions still show year-by-year",
      any(f["name"] == "Year by year" for f in e["fields"]))

# The legacy combined entry (offer_sheet carrying its own outcome) predates the
# split and must still render correctly — 3 of them are in the live ledger.
reset()
dn.notify_transaction(txn(type="offer_sheet", details={
    **SIDES, "contract": C, "outcome": "not_matched"}))
e = SENT[0]
check("a legacy combined entry still resolves correctly",
      e["title"] == "LAL — Offer Sheet" and "signs with LAL" in e["description"],
      f'{e["title"]} / {e["description"]}')

# An unknown/garbage transaction must not raise into the caller's write path.
reset()
try:
    dn.notify_transaction({"id": "z", "type": "nonsense", "created_at": now_stamp(), "details": {}})
    check("an unrecognised type doesn't raise", True)
except Exception as exc:
    check("an unrecognised type doesn't raise", False, exc)

reset()
dn.load_player_bios = lambda: (_ for _ in ()).throw(RuntimeError("bios unavailable"))
try:
    dn.notify_transaction(txn())
    check("a failure building the embed is swallowed, not raised", len(SENT) == 0)
except Exception as exc:
    check("a failure building the embed is swallowed, not raised", False, exc)
dn.load_player_bios = lambda: BIOS



# ── Delivery: pacing and rate-limit handling ──────────────────────────────────
# A dropped announcement is worse than a late one, so a 429 must be waited out
# and retried rather than logged and discarded. Draft day is exactly when both
# the burst and the rate limit show up together.
print("\ndelivery — a rate-limited burst is delayed, never lost")


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload or {}, text

    def json(self):
        return self._payload


slept = []
tp.time = type("T", (), {
    "sleep": staticmethod(lambda s: slept.append(s)),
    "time": staticmethod(time_mod.time),
})()

posts = {"n": 0}


def fake_post_ratelimit(url, headers=None, json=None, timeout=None):
    posts["n"] += 1
    # 429 twice, then succeed — the shape of a real burst hitting the limit.
    if posts["n"] <= 2:
        return FakeResponse(429, {"retry_after": 0.75})
    return FakeResponse(200)


tp.httpx = type("H", (), {"post": staticmethod(fake_post_ratelimit)})()
slept.clear()
MSG = {"channel": "123456789", "payload": {"embeds": [{"title": "x"}]}}
ok_delivered = tp._post(MSG)
check("a rate-limited message is eventually delivered", ok_delivered is True)
check("...after retrying", posts["n"] == 3, f"{posts['n']} attempts")
check("...honouring Discord's retry_after", any(abs(s - 1.0) < 0.01 for s in slept), slept)

posts["n"] = 0
tp.httpx = type("H", (), {"post": staticmethod(
    lambda url, headers=None, json=None, timeout=None: FakeResponse(403, text="Missing Access"))})()
res = tp._post(MSG)
check("a permissions error fails fast without retrying", res is False)

posts["n"] = 0


def fake_post_5xx(url, headers=None, json=None, timeout=None):
    posts["n"] += 1
    return FakeResponse(503)


tp.httpx = type("H", (), {"post": staticmethod(fake_post_5xx)})()
res = tp._post(MSG)
check("a persistent 5xx gives up after MAX_RETRIES", res is False and posts["n"] == tp.MAX_RETRIES,
      f"{posts['n']} attempts")

check("pacing stays inside Discord's ~5-per-5s channel limit",
      tp.SEND_INTERVAL >= 1.0, f"{tp.SEND_INTERVAL}s")
check("...while still draining a draft day in a couple of minutes",
      DRAFT_DAY_EXPECTED * tp.SEND_INTERVAL <= 180,
      f"{DRAFT_DAY_EXPECTED * tp.SEND_INTERVAL:.0f}s for {DRAFT_DAY_EXPECTED}")


print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

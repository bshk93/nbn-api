"""routers/arenas.py — arena naming rights, and the ledger reading them back.

Pinned:

- **A bid must beat the standing one** by `RAISE`, or open at `OPENING_BID`.
- **The outbid holder is not refunded**; every bid is a sink.
- **Pulling a name refunds it and restores the one before**, at its price.
- **Names are one short line with no links.**
- **A winning bid posts a tweet; pulling the name deletes it.**
- **The ledger names each game for the arena it was played in**: a bought
  name from its day on, a neutral site kept, the home arena otherwise.

Writes only to temp files (the wallet is redirected by tests/__init__.py).

    venv/bin/python -m tests.test_arenas
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

from routers import arenas, tweets, wallet  # noqa: E402
from stats_build import finances as fin  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def status(fn, *a):
    try:
        fn(*a)
    except HTTPException as e:
        return e.status_code
    return 200


tmp = Path(tempfile.mkdtemp(prefix="nbn-arenas-test-"))
arenas.ARENAS_FILE = tmp / "arena-names.json"
sent = []
arenas.inbox.notify_member = lambda who, text, link=None, **k: sent.append(("member", who, text))
arenas.inbox.notify_team = lambda team, text, link=None, **k: sent.append(("team", team, text))
arenas.fin.home_arenas = lambda _d: {"PHX": "Footprint Center"}
arenas.log_write = lambda *a: None
arenas._rebuild = lambda: None
tweets.TWEETS_FILE = tmp / "tweets.json"
tw = lambda: tweets._load_store()["items"]

for m in ("alice", "bob"):
    wallet.credit(m, 1_000, "admin", "test grant")
alice = {"name": "alice", "roles": []}
bob = {"name": "bob", "roles": ["phx"]}
admin = {"name": "boss", "roles": ["admin"]}

print("names")
check("spaces collapse", arenas.clean_name("  The   Dome ") == "The Dome")
check("too short is refused", status(arenas.clean_name, "ab") == 422)
check("too long is refused", status(arenas.clean_name, "x" * 41) == 422)
check("a link is refused", status(arenas.clean_name, "visit kvl.com Arena") == 422)

print("bidding")
check("an untouched arena shows its own name", arenas.get_arena("PHX")["name"] == "Footprint Center")
check("and opens at the opening bid", arenas.get_arena("PHX")["to_beat"] == arenas.OPENING_BID)
check("under the opening bid is refused",
      status(arenas.bid, "PHX", arenas.BidIn(name="Cheap Seats", bid=99), alice) == 409)
r = arenas.bid("PHX", arenas.BidIn(name="Tank Commander Dome", bid=100), alice)
check("a bid names the arena", r["name"] == "Tank Commander Dome" and r["by"] == "alice")
check("and costs the bid", wallet.balance("alice") == 900)
check("the next bid has to beat it by 10%", r["to_beat"] == 110)
check("the home team is told", ("team", "PHX", sent[-1][2]) == sent[-1])
check("a bid posts one tweet", len(tw()) == 1)
check("naming the arena, buyer and price",
      all(x in tw()[0]["text"] for x in ("Tank Commander Dome", "alice", "NB¥100", "PHX")))
check("from the business wire, in the team's colours",
      tw()[0]["handle"] == arenas.TWEET_HANDLE and tw()[0]["team"] == "PHX")
check("tagged with the bid", tw()[0]["ref"] == "arena:PHX:0")
check("a lower bid is refused",
      status(arenas.bid, "PHX", arenas.BidIn(name="Sun Palace", bid=109), bob) == 409)
check("more than the bidder has is refused (402)",
      status(arenas.bid, "PHX", arenas.BidIn(name="Sun Palace", bid=5_000), bob) == 402)
sent.clear()
r = arenas.bid("PHX", arenas.BidIn(name="Sun Palace", bid=110), bob)
check("a winning bid renames it", r["name"] == "Sun Palace")
check("the outbid holder is not refunded", wallet.balance("alice") == 900)
check("and is told", any(s[0] == "member" and s[1] == "alice" for s in sent))
check("the home team isn't told about its own owner's bid", not any(s[0] == "team" for s in sent))
check("an outbid tweet names who lost it",
      len(tw()) == 2 and "alice" in tw()[1]["text"] and "Sun Palace" in tw()[1]["text"])
check("'The' isn't doubled", "the The" not in arenas.tweet_text("PHX", "The Dome", "x", 100, None))
check("every variant fits in a tweet", all(
    len(arenas.tweet_text("PHX", "x" * 40, "m" * 30, 9_999_999, p)) <= 280
    for p in (None, {"by": "o" * 30, "name": "y" * 40}, {"by": "m" * 30, "name": "y" * 40})
    for _ in range(20)))
real_post = tweets.post_one
tweets.post_one = lambda *a, **k: 1 / 0
wallet.credit("alice", 500, "admin", "test grant")
r = arenas.bid("PHX", arenas.BidIn(name="Broken Wire Arena", bid=121), alice)
check("a tweet that fails to post doesn't fail the bid", r["name"] == "Broken Wire Arena")
tweets.post_one = real_post
arenas.pull("PHX", arenas.PullIn(reason="test"), admin)
wallet.debit("alice", 621, "admin", "undo test grant")
check("the unknown team is a 404",
      status(arenas.bid, "XXX", arenas.BidIn(name="Nowhere", bid=500), bob) == 404)

print("pulling")
r = arenas.pull("PHX", arenas.PullIn(reason="not that"), admin)
check("pulling restores the name before", r["name"] == "Tank Commander Dome")
check("at its price", r["to_beat"] == 110)
check("and refunds the pulled bid", wallet.balance("bob") == 1_000)
check("and deletes its tweet", [t["ref"] for t in tw()] == ["arena:PHX:0"])
arenas.pull("PHX", arenas.PullIn(reason="nor that"), admin)
check("pulling the last one goes back to the home arena",
      arenas.get_arena("PHX")["name"] == "Footprint Center"
      and arenas.get_arena("PHX")["to_beat"] == arenas.OPENING_BID)
check("nothing left to pull is a 404",
      status(arenas.pull, "PHX", arenas.PullIn(reason="x"), admin) == 404)
check("history keeps every entry", len(arenas.get_arena("PHX")["history"]) == 3)

print("the ledger's arena per game")
(tmp / "arena-names.json").write_text(json.dumps({"PHX": [
    {"name": "Old Name", "by": "a", "bid": 100, "at": "2026-11-01T03:00:00+00:00"},
    {"name": "Pulled", "by": "b", "bid": 200, "at": "2026-11-05T00:00:00+00:00", "pulled": {"by": "x"}},
    {"name": "New Name", "by": "c", "bid": 300, "at": "2026-11-10T00:00:00+00:00"},
]}))
bought = fin.load_arena_names(tmp)
defaults = {"PHX": "Footprint Center"}
sched = {("2026-11-12", "PHX"): "Arena Ciudad de México"}
at = lambda d: fin.arena_for(d, "PHX", sched, defaults, bought)
check("before any bid, the home arena", at("2026-10-31") == "Footprint Center")
check("from the day it was bought, the bought name", at("2026-11-01") == "Old Name")
check("a pulled name never shows", at("2026-11-06") == "Old Name")
check("a later bid takes over", at("2026-11-10") == "New Name")
check("a neutral site keeps its own name", at("2026-11-12") == "Arena Ciudad de México")
check("no schedule, no bids: nothing", fin.arena_for("2026-11-01", "BOS", {}, {}, {}) is None)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall checks passed")

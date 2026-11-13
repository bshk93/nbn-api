"""Regression tests for the per-member inbox (/api/inbox).

Pins the two properties the retention design depends on:

  * **Unread never ages out on its own.** Pruning only drops entries that are
    both read and older than RETENTION_DAYS — an unactioned notification
    can't silently vanish just because nobody looked at it.
  * **The cap is a hard backstop.** Once a member's inbox exceeds
    MAX_PER_MEMBER, the oldest entries fall off regardless of read state.

    venv/bin/python -m tests.test_inbox
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.inbox as ib  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def raises(name, status, fn):
    try:
        fn()
    except HTTPException as e:
        check(f"{name} → {status}", e.status_code == status)
        return
    check(f"{name} → {status}", False)


STORE = {}
ib._load_store = lambda: STORE
ib._save_store = lambda store: None
ib.log_write = lambda info, msg: None
PREFS = {}
ib._load_prefs = lambda: PREFS
ib._save_prefs = lambda prefs: None

BRYN = {"name": "bryn", "roles": []}

print("\nbasic append + read")
ib.notify_member("bryn", "Your offer was remanded", link="/free-agency")
resp = ib.get_inbox(BRYN)
check("one unread item", resp["unread_count"] == 1)
check("item carries the link", resp["items"][0]["link"] == "/free-agency")

item_id = resp["items"][0]["id"]
ib.mark_read(item_id, BRYN)
resp = ib.get_inbox(BRYN)
check("mark_read clears unread_count", resp["unread_count"] == 0)

raises("marking an unknown id", 404, lambda: ib.mark_read("nope", BRYN))

ib.notify_member("bryn", "second")
ib.notify_member("bryn", "third")
ib.mark_all_read(BRYN)
check("read-all clears everything", ib.get_inbox(BRYN)["unread_count"] == 0)

print("\nretention: unread survives past 90 days, read does not")
STORE["bryn"] = []
old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
STORE["bryn"].append({"id": "old-unread", "ts": old_ts, "text": "old unread", "link": None, "read": False})
STORE["bryn"].append({"id": "old-read", "ts": old_ts, "text": "old read", "link": None, "read": True})
ib.notify_member("bryn", "trigger a prune")
ids = {it["id"] for it in STORE["bryn"]}
check("old unread entry survives", "old-unread" in ids)
check("old read entry is pruned", "old-read" not in ids)

print("\nretention: hard cap trims oldest regardless of read state")
STORE["bryn"] = []
base = datetime.now(timezone.utc)
for i in range(ib.MAX_PER_MEMBER):
    STORE["bryn"].append({
        "id": f"item-{i}",
        "ts": (base - timedelta(minutes=ib.MAX_PER_MEMBER - i)).isoformat(),
        "text": str(i), "link": None, "read": False,
    })
check("primed at the cap", len(STORE["bryn"]) == ib.MAX_PER_MEMBER)
ib.notify_member("bryn", "one more, unread, should push out the oldest unread entry")
check("stays at the cap", len(STORE["bryn"]) == ib.MAX_PER_MEMBER)
check("oldest entry (item-0) fell off", "item-0" not in {it["id"] for it in STORE["bryn"]})

print("\nisolation between members")
STORE.clear()
ib.notify_member("bryn", "for bryn")
ANOTHER = {"name": "someone_else", "roles": []}
check("a second member's inbox starts empty", ib.get_inbox(ANOTHER)["items"] == [])

print("\nkind: stored on write, read off the link when left out")
STORE.clear()
ib.notify_member("bryn", "fa", link="/free-agency")
ib.notify_member("bryn", "poext", link="/committees/pdc#poext")
ib.notify_member("bryn", "fac panel", link="/committees/pdc")
ib.notify_member("bryn", "team option", link="/teams/BOS/")
ib.notify_member("bryn", "coaching", link="/teams/BOS/#coaching")
ib.notify_member("bryn", "explicit", link="/teams/BOS/", kind="games")
ib.notify_member("bryn", "bogus kind falls back", link="/suggestions", kind="nonsense")
ib.notify_member("bryn", "no link")
ib.notify_member("bryn", "trade page", link="/trades/view/?id=abc")
kinds = {it["text"]: it["kind"] for it in STORE["bryn"]}
check("/free-agency → fa", kinds["fa"] == "fa")
check("/committees/pdc#poext → extensions", kinds["poext"] == "extensions")
check("/committees/pdc → fa", kinds["fac panel"] == "fa")
check("/teams/X/ → transactions", kinds["team option"] == "transactions")
check("/teams/X/#coaching → games", kinds["coaching"] == "games")
check("explicit kind wins over the link", kinds["explicit"] == "games")
check("unknown kind falls back to the link", kinds["bogus kind falls back"] == "suggestions")
check("no link → other", kinds["no link"] == "other")
check("/trades/view → trades", kinds["trade page"] == "trades")

print("\nkind: entries stored before it existed are classified on read")
STORE["bryn"] = [{"id": "legacy", "ts": datetime.now(timezone.utc).isoformat(),
                  "text": "old", "link": "/committees/trc/", "read": False}]
resp = ib.get_inbox(BRYN)
check("legacy item reads as trades", resp["items"][0]["kind"] == "trades")
check("legacy item is not rewritten on disk", "kind" not in STORE["bryn"][0])
check("response lists the kinds", [k["id"] for k in resp["kinds"]][:2] == ["fa", "extensions"])

print("\nunread / archive")
STORE.clear()
ib.notify_member("bryn", "one", link="/news/")
iid = STORE["bryn"][0]["id"]
ib.mark_read(iid, BRYN)
ib.mark_unread(iid, BRYN)
check("mark_unread brings it back", ib.get_inbox(BRYN)["unread_count"] == 1)
ib.archive(iid, BRYN)
it = STORE["bryn"][0]
check("archive marks archived + read", it["archived"] and it["read"])
check("archived drops out of unread_count", ib.get_inbox(BRYN)["unread_count"] == 0)
ib.unarchive(iid, BRYN)
check("unarchive clears the flag", STORE["bryn"][0]["archived"] is False)
raises("archiving an unknown id", 404, lambda: ib.archive("nope", BRYN))

print("\nretention: archived ages out like read, even if never opened")
old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
STORE["bryn"] = [{"id": "old-arch", "ts": old_ts, "text": "x", "link": None, "read": False, "archived": True}]
ib.notify_member("bryn", "trigger a prune")
check("old archived entry is pruned", "old-arch" not in {it["id"] for it in STORE["bryn"]})

print("\nread-all by kind")
STORE.clear()
ib.notify_member("bryn", "a", link="/free-agency")
ib.notify_member("bryn", "b", link="/news/")
ib.mark_all_read(kind="fa", info=BRYN)
read = {it["text"]: it["read"] for it in STORE["bryn"]}
check("only the fa message is read", read == {"a": True, "b": False})
raises("an unknown kind", 400, lambda: ib.mark_all_read(kind="zzz", info=BRYN))

print("\nbulk")
STORE.clear()
for t in ("a", "b", "c"):
    ib.notify_member("bryn", t)
ids = [it["id"] for it in STORE["bryn"]]
r = ib.bulk(ib.BulkBody(ids=ids[:2] + ["gone"], op="archive"), BRYN)
check("bulk skips unknown ids and counts the rest", r["updated"] == 2)
check("bulk archived the two", [it.get("archived", False) for it in STORE["bryn"]] == [True, True, False])
raises("an unknown op", 400, lambda: ib.bulk(ib.BulkBody(ids=ids, op="delete"), BRYN))

print("\nmutes: muted kinds aren't written, unless they carry actions")
STORE.clear()
ib.put_prefs(ib.PrefsBody(muted=["news", "news"]), BRYN)
check("prefs stored de-duplicated", PREFS["bryn"]["muted"] == ["news"])
check("get_prefs reports them", ib.get_prefs(BRYN)["muted"] == ["news"])
ib.notify_member("bryn", "muted", link="/news/")
ib.notify_member("bryn", "muted with a button", link="/news/", actions=[{"kind": "x", "label": "Do it"}])
ib.notify_member("bryn", "not muted", link="/free-agency")
texts = [it["text"] for it in STORE["bryn"]]
check("muted message is dropped", "muted" not in texts)
check("muted message with actions still arrives", "muted with a button" in texts)
check("other kinds unaffected", "not muted" in texts)
check("someone else's mutes don't apply", (ib.notify_member("cara", "hi", link="/news/"), len(STORE["cara"]) == 1)[1])
raises("muting an unknown kind", 400, lambda: ib.put_prefs(ib.PrefsBody(muted=["zzz"]), BRYN))
PREFS.clear()

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

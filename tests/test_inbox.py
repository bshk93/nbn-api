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

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

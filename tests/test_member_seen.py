"""`auth._record_member_seen` — the last-seen record `/api/me` keeps.

`/api/me` runs on every page load, almost always with an IP and browser
already on file. It used to rewrite member-seen.json under a lock every time,
so every page load queued on that write. It writes now only when something is
new.

    venv/bin/python -m tests.test_member_seen
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import auth  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


path = Path(tempfile.mkdtemp()) / "member-seen.json"
auth.MEMBER_SEEN_FILE = path
writes = []
real_save = auth._save_json
auth._save_json = lambda p, data: (writes.append(p), real_save(p, data))

print("a first sighting is recorded")
auth._record_member_seen("nelson", "1.2.3.4", "Firefox")
check("one write", len(writes) == 1)
check("stored", json.loads(path.read_text())["nelson"] == {"ips": ["1.2.3.4"], "user_agents": ["Firefox"]})

print("the same sighting again writes nothing")
auth._record_member_seen("nelson", "1.2.3.4", "Firefox")
check("no write", len(writes) == 1)

print("a new IP is one write")
auth._record_member_seen("nelson", "5.6.7.8", "Firefox")
check("two writes", len(writes) == 2)
check("both IPs", json.loads(path.read_text())["nelson"]["ips"] == ["1.2.3.4", "5.6.7.8"])

print("nothing given, nothing written")
auth._record_member_seen("nelson", None, None)
check("still two", len(writes) == 2)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

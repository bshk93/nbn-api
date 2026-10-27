"""`storage._load_json_shared` and the two things built on it: the read-only
bios parse (`players.player_bios_shared`) and the name index
(`boxscores._bios_by_name`).

A big file read on every request is parsed once per change and shared. What
must hold: the same object while the file is unchanged, a fresh one the moment
any write lands (every write goes through `_atomic_write`), and the default for
a missing file.

    venv/bin/python -m tests.test_shared_json
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import boxscores, storage  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp())
path = tmp / "bios.json"
storage._atomic_write(path, json.dumps({"a": {"name": "ONE, A"}}))

print("parsed once while unchanged")
parses = []
real_loads = json.loads
storage.json.loads = lambda s: (parses.append(1), real_loads(s))[1]
first = storage._load_json_shared(path, {})
second = storage._load_json_shared(path, {})
check("same object", first is second)
check("one parse", len(parses) == 1)

print("a write is seen on the next read")
storage._atomic_write(path, json.dumps({"a": {"name": "ONE, A"}, "b": {"name": "TWO, B"}}))
third = storage._load_json_shared(path, {})
check("new object", third is not first)
check("new content", set(third) == {"a", "b"})
check("two parses", len(parses) == 2)
storage.json.loads = real_loads

print("a missing file is the default")
check("default", storage._load_json_shared(tmp / "missing.json", {"x": 1}) == {"x": 1})

print("the name index follows the bios object")
b1 = {"curry-stephen": {"name": "CURRY, STEPHEN"}}
idx1 = boxscores._bios_by_name(b1)
check("built", idx1["CURRY, STEPHEN"][0] == "curry-stephen")
check("kept for the same bios", boxscores._bios_by_name(b1) is idx1)
b2 = {"james-lebron": {"name": "JAMES, LEBRON"}}
idx2 = boxscores._bios_by_name(b2)
check("rebuilt for new bios", idx2 is not idx1 and "JAMES, LEBRON" in idx2 and "CURRY, STEPHEN" not in idx2)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

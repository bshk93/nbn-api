"""Saved quadrant charts — routers/quadrants.py, the /frivolities#quadrant gallery.

Three things worth pinning:

  * **A chart number is permanent.** `seq` only increments, so deleting the
    newest chart never lets its number be handed to the next one.
  * **The config is checked against what the page can draw.** An unknown stat
    key would load as a chart with no axis, so it is refused at save time.
  * **Only the author (or admin) deletes.**

The store is patched into memory, and endpoints are called directly, so
nothing touches quadrants.json in NBS_DATA_DIR.

    venv/bin/python -m tests.test_quadrants
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
import routers.quadrants as qd  # noqa: E402

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


def invalid(name, **body):
    try:
        qd.QuadrantCreate(**body)
    except ValidationError:
        check(f"{name} is refused", True)
        return
    check(f"{name} is refused", False)


STORE = {"seq": 0, "items": []}
qd._load_store = lambda: STORE
qd._save_store = lambda store: None
qd.log_write = lambda info, msg: None

AUTHOR = {"name": "bryn", "roles": []}
OTHER = {"name": "someone_else", "roles": ["phx"]}
ADMIN = {"name": "boss", "roles": ["admin"]}

CONFIG = {"x": "ppg", "y": "ts%", "labels": {"tr": "Efficient scorers"},
          "pick": {"mode": "list", "slugs": ["curry-stephen", "james-lebron"]}}


def make(title="Scorers", config=CONFIG, who=AUTHOR):
    return qd.create_quadrant(qd.QuadrantCreate(title=title, config=config), who)


print("create")
a = make()
check("numbered from 1", a["id"] == 1)
check("author is the token holder", a["author"] == "bryn")
check("defaults fill the rest of the config",
      a["config"]["split"]["mode"] == "median" and a["config"]["dataset"] == "reg")
check("the player list is kept in order",
      a["config"]["pick"]["slugs"] == ["curry-stephen", "james-lebron"])
raises("a blank title", 422, lambda: make(title="   "))

print("\nlist and get")
b = make(title="Defenders", config={"x": "spg", "y": "bpg"}, who=OTHER)
listed = qd.list_quadrants()
check("newest first", [q["id"] for q in listed] == [2, 1])
check("the list carries no configs", all("config" not in q for q in listed))
check("the list names the axes", listed[1]["x"] == "ppg" and listed[1]["y"] == "ts%")
check("get returns the full chart", qd.get_quadrant(1)["config"]["labels"]["tr"] == "Efficient scorers")
raises("get an unknown chart", 404, lambda: qd.get_quadrant(99))

print("\ndelete")
raises("someone else deletes your chart", 403, lambda: qd.delete_quadrant(1, OTHER))
qd.delete_quadrant(2, OTHER)
check("the author may delete their own", [q["id"] for q in STORE["items"]] == [1])
qd.delete_quadrant(1, ADMIN)
check("admin may delete anyone's", STORE["items"] == [])
c = make()
check("a deleted number is never reissued", c["id"] == 3)

print("\nvalidation")
check("a league-median split is accepted",
      qd.QuadrantCreate(title="t", config={"x": "ppg", "y": "rpg", "split": {"mode": "league"}}).config.split.mode == "league")
invalid("an unknown X stat", title="t", config={"x": "vibes", "y": "ppg"})
invalid("an unknown top-N stat", title="t", config={"x": "ppg", "y": "rpg", "pick": {"by": "aura"}})
invalid("too many players", title="t",
        config={"x": "ppg", "y": "rpg", "pick": {"mode": "list", "slugs": [f"p{i}" for i in range(61)]}})
invalid("a top N past the cap", title="t", config={"x": "ppg", "y": "rpg", "pick": {"n": 500}})
invalid("an overlong caption", title="t", config={"x": "ppg", "y": "rpg", "labels": {"tl": "x" * 61}})
invalid("an overlong title", title="x" * 101, config={"x": "ppg", "y": "rpg"})
invalid("an unknown split mode", title="t", config={"x": "ppg", "y": "rpg", "split": {"mode": "vibes"}})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("All quadrant gallery checks passed.")

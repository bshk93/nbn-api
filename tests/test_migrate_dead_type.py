"""migrate_dead_type.py: clears `type: "dead"` only on bios no roster lists,
reports the rest, and writes nothing without --apply. Loaders are stubbed:
nothing here reads or writes the real data dir.

    venv/bin/python -m tests.test_migrate_dead_type
"""
import contextlib
import io
import sys

import migrate_dead_type as M

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


disk = {}
writes = []


def world():
    disk.clear(); writes.clear()
    disk.update({
        "gone": {"type": "dead", "salaries": {"26-27": "$1,000,000"}},   # off every roster
        "still": {"type": "dead"},                                       # still on ATL
        "live": {"type": "player"},
    })
    M.load_player_bios = lambda: {k: dict(v) for k, v in disk.items()}
    M.save_player_bios = lambda d: (writes.append(d), disk.clear(), disk.update(d))
    M._build_team_map = lambda: {"still": "ATL", "live": "ATL"}
    M.deadcap_slugs = lambda: {"gone": "BOS"}
    M._current_league_year = lambda: "26-27"
    M.DATA_DIR = type("D", (), {"__truediv__": lambda s, o: type("F", (), {"exists": lambda s: True})()})()


def run(*argv):
    sys.argv = ["migrate_dead_type.py", *argv]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = M.main()
    return rc, out.getvalue()


print("dry run")
world()
rc, out = run()
check("exits 0", rc == 0)
check("writes nothing", writes == [])
check("lists both dead bios", "gone" in out and "still" in out)
check("names the one held for a person", "['still']" in out)

print("apply")
world()
rc, out = run("--apply")
check("one write", len(writes) == 1)
check("cleared the one on no roster", disk["gone"]["type"] == "")
check("left the rostered one alone", disk["still"]["type"] == "dead")
check("touched no other bio", disk["live"]["type"] == "player")
check("kept his salaries", disk["gone"]["salaries"] == {"26-27": "$1,000,000"})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

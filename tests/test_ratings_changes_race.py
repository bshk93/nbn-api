"""all_ratings_changes' cache can be rebuilt by one request while another
reads it. It used to clear a dict and refill it, and a reader that had matched
the key between the two could hit a KeyError: a 500 on
/api/ratings-changes?latest=1 (CI saw one against live, 2026-11-16).

Two threads, one forcing rebuilds by flipping the file stamps, one reading.
No file is read: stamps, loaders and the diff are stubbed.

    venv/bin/python -m tests.test_ratings_changes_race
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.players as pl  # noqa: E402

sys.setswitchinterval(1e-6)          # switch threads as often as possible
flip = {"n": 0}
pl._file_stamp = lambda path: (flip["n"], str(path))
pl.load_attributes = lambda: {}
pl.load_player_bios = lambda: {}
pl._diff_all_ratings = lambda a, b: [{"date": "2026-11-07"}]

errors, stop = [], threading.Event()


def writer():
    while not stop.is_set():
        flip["n"] += 1
        pl.all_ratings_changes()


def reader():
    for _ in range(200_000):
        try:
            pl.all_ratings_changes()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
            break
    stop.set()


t1, t2 = threading.Thread(target=writer), threading.Thread(target=reader)
t1.start(); t2.start(); t2.join(); stop.set(); t1.join()
print(f"  [{'ok' if not errors else 'FAIL'}] no reader error during rebuilds {errors[:1]}")
sys.exit(1 if errors else 0)

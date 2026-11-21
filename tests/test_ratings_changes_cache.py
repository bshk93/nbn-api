"""GET /api/ratings-changes answers from a cached diff.

- The cached answer equals a fresh diff of the same files.
- A save of player-attributes.json or player-bios.json is picked up on the
  next call; the committee's ticks are never cached.
- One request's ticks never leak into the shared list.
- `latest=1` keeps only the newest scrape date (after `slug`/`since`/`until`).
- Each change carries the snapshot's `nba_team`, null when the snapshot has none.

Files live in a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_ratings_changes_cache
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.players as players  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-ratings-cache-test-"))
players.ATTRIBUTES_FILE = TMP / "player-attributes.json"
players.PLAYER_BIOS_FILE = TMP / "player-bios.json"
players.RATINGS_UPDATED_FILE = TMP / "ratings-updated.json"


def snap(date, ovr):
    return {"date": date, "2k_ovr": ovr, "2k_pos": ["SF"], "attributes": {}, "badges": []}


def write(path, data):
    # Through a temp file and os.replace, as storage._atomic_write does.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


ATTRS = {
    "doe-john": [snap("2026-10-01", 80), snap("2026-11-01", 82),
                 {**snap("2026-11-05", 84), "team": "BOS", "nba_team": "LAL"}],
    "roe-jane": [snap("2026-10-01", 70), snap("2026-11-05", 68)],
}
write(players.ATTRIBUTES_FILE, ATTRS)
write(players.PLAYER_BIOS_FILE, {"doe-john": {"name": "DOE, JOHN"}, "roe-jane": {"name": "ROE, JANE"}})

calls = {"n": 0}
real_load = players.load_attributes


def counting_load():
    calls["n"] += 1
    return real_load()


players.load_attributes = counting_load

print("cache")
a = players.get_ratings_changes(slug=None, since=None, until=None, limit=2000, latest=False)
b = players.get_ratings_changes(slug=None, since=None, until=None, limit=2000, latest=False)
check("three changes, newest first", [e["date"] for e in a["entries"]] == ["2026-11-05", "2026-11-05", "2026-11-01"])
check("second call is the same answer", a == b)
check("second call didn't re-read the attributes", calls["n"] == 1)
check("nba_team comes off the snapshot",
      {(e["slug"], e["date"]): e["nba_team"] for e in a["entries"]}
      == {("doe-john", "2026-11-05"): "LAL", ("roe-jane", "2026-11-05"): None, ("doe-john", "2026-11-01"): None})
check("names come from the bios", {e["name"] for e in a["entries"]} == {"DOE, JOHN", "ROE, JANE"})
fresh = players._diff_all_ratings(ATTRS, json.loads(players.PLAYER_BIOS_FILE.read_text()))
check("cached list equals a fresh diff", players.all_ratings_changes() == fresh)

print("ticks")
players._save_json(players.RATINGS_UPDATED_FILE, {"doe-john|2026-11-05": {"by": "U", "at": "x"}})
c = players.get_ratings_changes(slug="doe-john", since=None, until=None, limit=2000, latest=False)
check("a tick shows without a re-diff", c["entries"][0]["updated"] == {"by": "U", "at": "x"} and calls["n"] == 1)
check("the shared list carries no tick", all("updated" not in e for e in players.all_ratings_changes()))

print("invalidation")
ATTRS["roe-jane"].append(snap("2026-11-10", 71))
write(players.ATTRIBUTES_FILE, ATTRS)
d = players.get_ratings_changes(slug=None, since=None, until=None, limit=2000, latest=False)
check("a new scrape is picked up", d["entries"][0]["date"] == "2026-11-10" and calls["n"] == 2)
write(players.PLAYER_BIOS_FILE, {"doe-john": {"name": "DOE, JOHNNY"}, "roe-jane": {"name": "ROE, JANE"}})
e = players.get_ratings_changes(slug="doe-john", since=None, until=None, limit=2000, latest=False)
check("a bio save is picked up", e["entries"][0]["name"] == "DOE, JOHNNY")

print("filters")
f = players.get_ratings_changes(slug=None, since=None, until=None, limit=2000, latest=True)
check("latest keeps only the newest date", [x["date"] for x in f["entries"]] == ["2026-11-10"])
g = players.get_ratings_changes(slug=None, since=None, until="2026-11-06", limit=2000, latest=True)
check("latest applies after until", [x["date"] for x in g["entries"]] == ["2026-11-05", "2026-11-05"])
h = players.get_ratings_changes(slug=None, since="2026-11-02", until=None, limit=2000, latest=False)
check("since drops older changes", all(x["date"] >= "2026-11-02" for x in h["entries"]) and h["count"] == 3)
i = players.get_ratings_changes(slug=None, since=None, until=None, limit=1, latest=False)
check("limit caps entries, count is the total", len(i["entries"]) == 1 and i["count"] == 4)
check("latest on nothing is empty", players.get_ratings_changes(
    slug="nobody", since=None, until=None, limit=2000, latest=True)["entries"] == [])

print("missing files")
players.ATTRIBUTES_FILE.unlink()
players.load_attributes = lambda: {"x": [snap("2026-01-01", 60), snap("2026-01-02", 61)]}
j = players.get_ratings_changes(slug=None, since=None, until=None, limit=2000, latest=False)
players.load_attributes = lambda: {}
k = players.get_ratings_changes(slug=None, since=None, until=None, limit=2000, latest=False)
check("no file, no cache: each call diffs what the loader gives", j["count"] == 1 and k["count"] == 0)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all ok")

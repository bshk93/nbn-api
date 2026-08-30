"""`routers/poopoo.py` — the team-page slice of the sheet-vs-site diff.

Three things have to stay true, and none of them are about the endpoint:

- **It stays small.** The whole point is that a team page cannot fetch the
  1.1 MB report to read five rows. If the slice starts carrying the per-team
  `sheet`/`site` snapshots again it is no cheaper than the file it replaced.
- **It invents no numbers.** `mag` comes from the job. If this file ever
  computes a magnitude, /poopoo and a team page can disagree about how many
  dollars apart the two sources are, which is the one thing a reconciliation
  surface cannot do.
- **`checked_at` is the mtime, not `generated_at`.** The job only rewrites on
  change, so the two are days apart on a healthy system — reporting the wrong
  one makes a working job look stuck.

Reads live data where it is there; every fixture goes to a temp file.

    venv/bin/python -m tests.test_poopoo_summary
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.poopoo as pp  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


REAL_FILE = pp.POOPOO_FILE
TMP = Path(tempfile.mkdtemp(prefix="nbn-poopoo-test-"))

FIXTURE = {
    "generated_at": "2026-08-27T12:40:21.344972+00:00",
    "season": "26-27",
    "teams": [
        {
            "team": "BKN",
            "diff_count": 4,
            "diffs": [
                {"category": "aggregate", "field": "Guaranteed Salary",
                 "sheet": 159907729.6, "site": 179118525, "mag": 19210795.4},
                {"category": "aggregate", "field": "Hard Cap",
                 "sheet": None, "site": "first_apron", "mag": None},
                {"category": "player_future_years", "field": "Matisse Thybulle",
                 "sheet": ["27-28: UFA $1"], "site": ["27-28: UFA $2,939,305"],
                 "mag": 2939304.0},
                {"category": "player_hold_uncalculated", "field": "X (30-31)",
                 "sheet": "UFA $76,550,265", "site": "not yet calculated",
                 "mag": None},
            ],
            # The two blobs the slice exists to drop.
            "sheet": {"players": [{"name": "filler"} for _ in range(600)]},
            "site": {"players": [{"name": "filler"} for _ in range(600)]},
        },
        {"team": "SAS", "diff_count": 0, "diffs": [], "sheet": {}, "site": {}},
    ],
    "picks": {"rows": [{"filler": True} for _ in range(89)]},
    "completeness": {"rows": [{"filler": True} for _ in range(562)]},
}

print("grouping: a live disagreement is open, a future/uncomputed one is deferred")
check("aggregate is open", pp._group("aggregate") == "open")
check("player_salary is open", pp._group("player_salary") == "open")
check("player_team_conflict is open", pp._group("player_team_conflict") == "open")
check("player_future_years is deferred", pp._group("player_future_years") == "deferred")
check("player_hold_uncalculated is deferred", pp._group("player_hold_uncalculated") == "deferred")
check("an unknown category defaults to open, not hidden",
      pp._group("something_new_the_job_grew") == "open")

print("the slice drops the snapshots and keeps the diffs")
fixture_path = TMP / "poopoo.json"
fixture_path.write_text(json.dumps(FIXTURE))
pp.POOPOO_FILE = fixture_path
out = pp.summary()
bkn = out["teams"][0]
check("available", out["available"] is True)
check("season carried through", out["season"] == "26-27")
check("both teams present, including the clean one", len(out["teams"]) == 2)
check("no sheet snapshot in the slice", "sheet" not in bkn)
check("no site snapshot in the slice", "site" not in bkn)
check("no picks report in the slice", "picks" not in out)
check("no completeness report in the slice", "completeness" not in out)
check("every diff kept", bkn["diff_count"] == 4)
check("open counted", bkn["open_count"] == 2)
check("deferred counted", bkn["deferred_count"] == 2)
check("each diff tagged with its group",
      [d["group"] for d in bkn["diffs"]] == ["open", "open", "deferred", "deferred"])

print("the headline dollar figure is the team-level row, never a sum of rows")
check("headline_mag is the Guaranteed Salary row", bkn["headline_mag"] == 19210795.4)
check("a clean team reports None", out["teams"][1]["headline_mag"] is None)
check("a clean team still appears", out["teams"][1]["diff_count"] == 0)

# The case the sum got wrong: an MLE row and a player row that are both already
# inside the team-level figure. Summing these three reports $37.9M apart on a
# team whose books differ by $19.2M.
nested = {"generated_at": None, "season": "26-27", "teams": [{
    "team": "BKN", "diff_count": 3, "diffs": [
        {"category": "aggregate", "field": "Guaranteed Salary", "sheet": 159907729.6,
         "site": 179118525, "mag": 19210795.4},
        {"category": "mle", "field": "MLE Used", "sheet": 0, "site": 9366000, "mag": 9366000.0},
        {"category": "player_salary", "field": "Julian Champagnie", "sheet": "$0",
         "site": "$9,366,000", "mag": 9366000.0},
    ]}]}
(TMP / "nested.json").write_text(json.dumps(nested))
pp.POOPOO_FILE = TMP / "nested.json"
check("component rows are not added to the team-level one",
      pp.summary()["teams"][0]["headline_mag"] == 19210795.4)
pp.POOPOO_FILE = fixture_path

print("no magnitude is computed here — a diff without `mag` keeps not having one")
no_mag = {"generated_at": None, "season": "26-27", "teams": [{
    "team": "BKN", "diff_count": 1,
    "diffs": [{"category": "aggregate", "field": "Guaranteed Salary",
               "sheet": 1000, "site": 2000}]}]}
(TMP / "nomag.json").write_text(json.dumps(no_mag))
pp.POOPOO_FILE = TMP / "nomag.json"
out_nm = pp.summary()
check("mag stays absent rather than being derived from sheet/site",
      out_nm["teams"][0]["diffs"][0].get("mag") is None)
check("headline_mag is None when the row carries no magnitude",
      out_nm["teams"][0]["headline_mag"] is None)

print("checked_at is when the job last ran; generated_at is when it last changed")
pp.POOPOO_FILE = fixture_path
os.utime(fixture_path, (time.time(), time.time()))
out2 = pp.summary()
check("generated_at is the file's own field",
      out2["generated_at"] == "2026-08-27T12:40:21.344972+00:00")
check("checked_at is the mtime, and is later", out2["checked_at"] > out2["generated_at"])

print("a missing or unreadable file is a state, not a 500")
pp.POOPOO_FILE = TMP / "does-not-exist.json"
gone = pp.summary()
check("missing file: available false", gone["available"] is False)
check("missing file: empty teams", gone["teams"] == [])
(TMP / "torn.json").write_text('{"teams": [{"team": "BK')
pp.POOPOO_FILE = TMP / "torn.json"
torn = pp.summary()
check("half-written file: available false", torn["available"] is False)

print("team filter")
pp.POOPOO_FILE = fixture_path
check("filters to one team", [t["team"] for t in pp.summary("BKN")["teams"]] == ["BKN"])
check("a team with nothing on file is empty, not an error",
      pp.summary("ATL")["teams"] == [])

print("against the live report")
pp.POOPOO_FILE = REAL_FILE
if REAL_FILE.exists():
    live = pp.summary()
    check("live report is available", live["available"] is True)
    check("all 30 teams", len(live["teams"]) == 30)
    payload = len(json.dumps(live, separators=(",", ":")))
    # 16 KB today. The bound is what makes this safe to fetch on every team
    # page load; a snapshot leaking back in would blow straight through it.
    check(f"payload stays small ({payload:,} bytes < 200 KB)", payload < 200_000)
    full = len(REAL_FILE.read_text())
    check(f"and much smaller than the file it slices ({full:,} bytes)", payload < full / 10)
    check("every diff carries a group",
          all("group" in d for t in live["teams"] for d in t["diffs"]))
    check("counts add up",
          all(t["open_count"] + t["deferred_count"] == t["diff_count"] for t in live["teams"]))
else:
    print("  [skip] no live poopoo.json on this box")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all poopoo summary checks passed")

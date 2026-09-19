"""`routers/cap_history.py` — the daily cap/apron snapshot.

What matters about this file is not the endpoint, it is that the series stays
readable years from now, when it is the input to § 7.3's four-year lookback:

- **One row per team per day, never two.** A duplicated day silently
  double-counts in anything built off the series, and the timer can fire twice
  after a reboot.
- **It does no cap math of its own.** The figures come from the validators'
  own helpers, so the history can never disagree with what the office enforced.
  Pinned by recomputing a team both ways.
- **A 0 threshold is unknown, not exceeded.** 27-28 onward are on file with
  cap/apron1/apron2 all literally 0. Read naively, every team is over every
  apron in those seasons — which would be a fabricated freeze under § 7.3.

Reads live data like the rest of the suite; every write goes to a temp file.

    venv/bin/python -m tests.test_cap_history
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.cap_history as ch  # noqa: E402
from routers.constants import DATA_DIR, VALID_TEAMS  # noqa: E402
from routers.storage import _load_json  # noqa: E402
from routers.transactions import (  # noqa: E402
    _compute_team_salary, _compute_team_salary_ex_holds,
    _empty_roster_charge, _real_empty_roster_charge,
)

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-caphist-test-"))
ch.HISTORY_FILE = TMP / "cap-history.jsonl"

print("apron position reads against the thresholds, not around them")
known = {"cap": 164961000, "apron1": 209015000, "apron2": 221686000}
check("under both is None", ch._apron_position(200_000_000, known) is None)
check("exactly at apron1 is first_apron", ch._apron_position(209_015_000, known) == "first_apron")
check("between is first_apron", ch._apron_position(215_000_000, known) == "first_apron")
check("exactly at apron2 is second_apron", ch._apron_position(221_686_000, known) == "second_apron")
check("above apron2 is second_apron", ch._apron_position(240_000_000, known) == "second_apron")

print("an unentered season is unknown, not over the line")
unknown = {"cap": None, "apron1": None, "apron2": None}
check("a huge salary is still None", ch._apron_position(400_000_000, unknown) is None)

print("cap levels: a literal 0 on file normalises to None")
levels_file = TMP / "cap-levels.json"
levels_file.write_text(json.dumps({
    "26-27": {"cap": 164961000, "apron1": 209015000, "apron2": 221686000},
    "27-28": {"cap": 0, "apron1": 0, "apron2": 0},
}))
real_file = ch.CAP_LEVELS_FILE
try:
    ch.CAP_LEVELS_FILE = levels_file
    entered = ch._cap_levels_for("26-27")
    blank = ch._cap_levels_for("27-28")
    missing = ch._cap_levels_for("99-00")
finally:
    ch.CAP_LEVELS_FILE = real_file
check("an entered season keeps its figures", entered["apron1"] == 209015000)
check("a zeroed season reads None for cap", blank["cap"] is None)
check("a zeroed season reads None for apron1", blank["apron1"] is None)
check("a zeroed season reads None for apron2", blank["apron2"] is None)
check("a season not on file reads None", missing["apron2"] is None)
# The reason the normalisation matters, stated as a check rather than a comment.
check("a zeroed season cannot manufacture an apron position",
      ch._apron_position(300_000_000, blank) is None)

print("the rows cover the league")
rows = ch.build_rows(on_date="2026-01-01")
check("one row per team", len(rows) == 30)
check("every team is present", {r["team"] for r in rows} == VALID_TEAMS)
check("all rows carry the given date", all(r["date"] == "2026-01-01" for r in rows))
required = {"date", "season", "team", "salary", "salary_ex_holds", "holds", "cap",
            "apron1", "apron2", "apron_position", "hard_cap", "mle_used",
            "mle_type", "bae_used", "roster", "two_way",
            # /committees/rosters reads these to price a § 2.1a charge rather
            # than recomputing one — dropping them puts the board back to
            # naming the floor with no number.
            "empty_roster_charge", "empty_roster_deficiency"}
check("every row carries the full shape", all(required <= set(r) for r in rows))
check("holds is the difference between the two bases",
      all(r["holds"] == r["salary"] - r["salary_ex_holds"] for r in rows))
check("roster counts are plausible", all(0 <= r["roster"] <= 25 for r in rows))

print("roster counts exempt draft-rights and dead, not just two-way")
# A held, unsigned draft pick or a dead-cap entry is a roster CSV row but
# occupies no standard slot (_is_standard_roster_slot, routers/transactions.py)
# — a team page's own roster count already knows this. This snapshot's count
# silently didn't, and counted a draft-rights row as a live body (a 15-man
# roster with one held pick read as 16/15 on the homepage).
synth_dir = TMP / "synth-data"
synth_dir.mkdir()
(synth_dir / "zzz-roster.csv").write_text("SLUG\na\nb\nc\nd\ne\n")
synth_bios = {
    "a": {"type": "player"},
    "b": {"type": "two-way"},
    "c": {"type": "draft-rights"},
    "d": {"type": "dead"},
    "e": {"type": ""},
}
real_data_dir = ch.DATA_DIR
try:
    ch.DATA_DIR = synth_dir
    synth_counts = ch._roster_counts("zzz", synth_bios)
finally:
    ch.DATA_DIR = real_data_dir
check("draft-rights and dead are excluded from the standard count", synth_counts["roster"] == 2)
check("two-way is still counted separately", synth_counts["two_way"] == 1)

print("the figures are the validators' own, not a second implementation")
bios = _load_json(DATA_DIR / "player-bios.json", {})
season = rows[0]["season"]
mismatched = []
for row in rows:
    # The helpers are the raw books; § 2.1a's Empty Roster Charge is the one
    # thing this module adds on top, and it adds it to both bases equally.
    erc = row["empty_roster_charge"]
    if row["salary"] != _compute_team_salary(row["team"], bios, season) + erc:
        mismatched.append(f"{row['team']} salary")
    if row["salary_ex_holds"] != _compute_team_salary_ex_holds(row["team"], bios, season) + erc:
        mismatched.append(f"{row['team']} ex_holds")
check(f"all 30 teams match the helpers ({', '.join(mismatched) or 'none differ'})",
      not mismatched)

print("§ 2.1a's Empty Roster Charge is on the books, not left off them")
bad_holds = [r["team"] for r in rows
             if r["holds"] != r["salary"] - r["salary_ex_holds"]]
check(f"the charge cancels out of `holds` ({', '.join(bad_holds) or 'all 30 agree'})",
      not bad_holds)
rookie_min = (_load_json(ch.CAP_LEVELS_FILE, {}).get(season, {})
              .get("min_salary_scale") or {}).get("0", 0)
wrong_charge = [
    r["team"] for r in rows
    if r["empty_roster_deficiency"] != max(0, 12 - r["roster"])
    or r["empty_roster_charge"] != max(0, 12 - r["roster"]) * rookie_min
]
check(f"priced at the rookie minimum per slot below 12 ({', '.join(wrong_charge) or 'all 30 agree'})",
      not wrong_charge)
check("a team at or above 12 carries none",
      all(r["empty_roster_charge"] == 0 for r in rows if r["roster"] >= 12))
short = [r for r in rows if r["roster"] < 12]
check(f"a team below 12 carries one ({len(short)} short-handed now)",
      all(r["empty_roster_charge"] > 0 for r in short) or not rookie_min)

print("the real charge and the trade mock are two floors, not one")
levels = {"99-00": {"min_salary_scale": {"0": 1_000_000}}}
check("13 players: no real charge, one slot of trade mock",
      _real_empty_roster_charge(13, "99-00", levels) == (0, 0)
      and _empty_roster_charge(13, "99-00", levels) == (1, 1_000_000))
check("11 players: one slot real, three slots of trade mock",
      _real_empty_roster_charge(11, "99-00", levels) == (1, 1_000_000)
      and _empty_roster_charge(11, "99-00", levels) == (3, 3_000_000))
check("a full roster carries neither",
      _real_empty_roster_charge(15, "99-00", levels) == (0, 0)
      and _empty_roster_charge(15, "99-00", levels) == (0, 0))
check("a season with no minimum scale on file prices nothing",
      _real_empty_roster_charge(9, "99-00", {}) == (3, 0))

print("one row per team per day, never two")
result = ch.snapshot(on_date="2026-01-02")
check("first run writes 30 rows", result["written"] == 30)
again = ch.snapshot(on_date="2026-01-02")
check("second run writes nothing", again["written"] == 0)
check("and says why", again.get("skipped") == "already recorded")
check("the file still holds 30 rows", len(ch.read_history()) == 30)
forced = ch.snapshot(on_date="2026-01-02", force=True)
check("--force re-records", forced["written"] == 30)
check("append-only: the earlier rows are still there", len(ch.read_history()) == 60)

print("reading back")
ch.snapshot(on_date="2026-01-03")
check("by team", all(e["team"] == "UTA" for e in ch.read_history(team="UTA")))
check("by lowercase team too", len(ch.read_history(team="uta")) == len(ch.read_history(team="UTA")))
check("oldest first", ch.read_history()[0]["date"] == "2026-01-02")
check("since is inclusive", all(e["date"] >= "2026-01-03"
                                for e in ch.read_history(since="2026-01-03")))
check("until is inclusive", all(e["date"] <= "2026-01-02"
                                for e in ch.read_history(until="2026-01-02")))
check("a date range narrows to one day",
      {e["date"] for e in ch.read_history(since="2026-01-03", until="2026-01-03")} == {"2026-01-03"})

print("a corrupt line does not take the series down")
with ch.HISTORY_FILE.open("a") as fh:
    fh.write("{ this is not json\n")
check("the rest still reads", len(ch.read_history()) == 90)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

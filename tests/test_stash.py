"""The `stash` transaction — keeping a pick's unsigned draft rights past the
§ 7.1 signing deadline, on § 7.1 (not in 2K, sitting out) or § 7.4 (overseas
contract) grounds.

A stash changes no status: the player stays `draft-rights`. What it adds is a
record on the bio, so an unsigned pick the team chose to keep reads apart from
one nobody signed. These pin the validator, the record the apply path writes,
and that signing, renouncing or voiding drops it.

Pure functions only, bios patched in memory — nothing here writes live data.

    venv/bin/python -m tests.test_stash
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as T  # noqa: E402
import routers.cap_history as CH  # noqa: E402
from fastapi import HTTPException  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def find(checks, cid, passed=None):
    return [c for c in checks if c.check == cid and (passed is None or c.passed == passed)]


def errors(checks):
    return [c for c in checks if not c.passed and c.level == "error"]


BIOS = {
    "pick": {"name": "PICK, TEST", "type": "draft-rights", "draft_year": 2026, "draft_round": 2},
    "rated": {"name": "RATED, TEST", "type": "draft-rights", "draft_year": 2026, "draft_round": 2},
    "signed": {"name": "SIGNED, TEST", "type": "player"},
    "loose": {"name": "LOOSE, TEST", "type": "draft-rights"},
}
T._build_team_map = lambda: {"pick": "LAL", "rated": "LAL", "signed": "LAL"}
T._load_json = lambda path, default: {"rated": [{"2k_ovr": 70}]} if path == T.ATTRIBUTES_FILE else default
CTX = {"bios": BIOS}


def v(player, basis="7.1", note="sitting out 26-27 to rehab"):
    return T._validate_stash(T.StashDetails(player=player, basis=basis, note=note), CTX)


print("\n_validate_stash")
r = v("pick")
check("held, unrated, with a note: legal", not errors(r))
check("...and says the sitting-out half is manual", find(r, "stash_not_in_2k", True))
check("a signed player cannot be stashed", find(v("signed"), "stash_draft_rights", False))
check("rights not on a roster cannot be stashed", find(v("loose"), "stash_draft_rights", False))
check("a blank note is refused", find(v("pick", note="  "), "stash_grounds", False))
check("§ 7.1 refuses a player with 2K ratings", find(v("rated"), "stash_not_in_2k", False))
r = v("rated", basis="7.4", note="Real Madrid, through 2028")
check("...but § 7.4 doesn't depend on 2K", not errors(r))
check("§ 7.4 names the 30-day window", "30 days" in find(r, "stash_grounds", True)[0].message)
try:
    T.StashDetails(player="pick", basis="7.2", note="x")
    check("an unknown basis is rejected by the model", False)
except Exception:
    check("an unknown basis is rejected by the model", True)


print("\n_apply_stash")
saved = {}
T.load_player_bios = lambda: BIOS
T.save_player_bios = lambda b: saved.update(b)
T.log_write = lambda *a, **k: None
team = T._apply_stash(T.StashDetails(player="pick", basis="7.4", note=" Real Madrid "),
                      "2026-09-25", {"name": "x"}, txn_id="abc")
st = BIOS["pick"].get("stash") or {}
check("returns the team", team == "LAL")
check("records basis, league year, date, note, txn",
      st == {"basis": "7.4", "season": "26-27", "date": "2026-09-25",
             "note": "Real Madrid", "txn_id": "abc"})
check("the player stays draft rights", BIOS["pick"]["type"] == "draft-rights")
check("the bios were saved", "pick" in saved)
try:
    T._apply_stash(T.StashDetails(player="signed", basis="7.1", note="x"), "2026-09-25", {})
    check("apply refuses a signed player", False)
except HTTPException:
    check("apply refuses a signed player", True)

check("a renounce snapshots the stash, so a rescind restores it",
      "stash" in T._RENOUNCE_SNAPSHOT_FIELDS)
src = Path(T.__file__).read_text()
for fn in ("_apply_sign_pick", "_apply_renounce", "_apply_void_player"):
    body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
    check(f"{fn} drops the stash", 'bio.pop("stash", None)' in body)
check("stash is a registered validator", T._VALIDATORS.get("stash") is T._validate_stash)


print("\ncap_history._draft_rights")
CH.read_csv = lambda path: (["SLUG"], [{"SLUG": "pick"}, {"SLUG": "signed"}, {"SLUG": "loose"}])
CH.DATA_DIR = Path("/")   # exists() on "/lal-roster.csv" is patched below
orig_exists = Path.exists
Path.exists = lambda self: True if self.name.endswith("-roster.csv") else orig_exists(self)
rows = CH._draft_rights("LAL", BIOS)
Path.exists = orig_exists
check("lists only draft rights", [r["slug"] for r in rows] == ["pick", "loose"])
check("carries the stash basis and season",
      rows[0]["stash"] == {"basis": "7.4", "season": "26-27"})
check("an unstashed pick reads null", rows[1]["stash"] is None)


if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

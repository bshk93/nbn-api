"""Regression tests for `_apply_extension` — the function that actually
writes an agreed extension to a player's bio. Written 2026-08-21 after
noticing it had zero direct test coverage: every other test in this area
(test_extensions.py, test_poext.py) exercises the *validator*, never the
apply path itself, which is where the merge logic (what gets kept from the
old contract, what the new years overwrite, what happens to a superseded
option year) actually lives.

    venv/bin/python -m tests.test_apply_extension
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


BIOS: dict = {}
tx.load_player_bios = lambda: BIOS
tx.save_player_bios = lambda b: None
tx.CAP_LEVELS_FILE = Path("/nonexistent/cap-levels.json")  # .exists() -> False -> {} cap_levels
INFO = {"name": "tester"}


def reset(bio):
    BIOS.clear()
    BIOS["p"] = bio


def contract(salaries, cap_holds=None):
    return tx.ContractIn(type="player", salaries=salaries, cap_holds=cap_holds or {})


print("basic merge: new years appended, everything before the cutoff kept")
reset({
    "salaries": {"24-25": "$5,000,000", "25-26": "$5,000,000", "26-27": "$5,000,000"},
    "cap_holds": {}, "guaranteed": {}, "guarantee_dates": {}, "guarantee_schedule": {},
    "contracts": [],
})
team = tx._apply_extension(
    tx.ExtensionDetails(player="p", team="BOS", kind="veteran",
                        contract=contract({"27-28": "$6,000,000", "28-29": "$6,300,000"})),
    "2026-08-21", INFO, txn_id="tx1")
bio = BIOS["p"]
check("returns the team", team == "BOS")
check("old years untouched", bio["salaries"]["24-25"] == "$5,000,000" and bio["salaries"]["26-27"] == "$5,000,000")
check("new years present", bio["salaries"]["27-28"] == "$6,000,000" and bio["salaries"]["28-29"] == "$6,300,000")
check("exactly 5 salary years total (no years dropped, none duplicated)", len(bio["salaries"]) == 5)
check("a new contracts[] entry was appended", len(bio["contracts"]) == 1 and bio["contracts"][0]["kind"] == "extension")
check("the ledger txn_id is stamped on it", bio["contracts"][0]["txn_id"] == "tx1")

print("\na trailing option year at the cutoff is superseded, not collided with")
# ... GTD, GTD, PLAYER_OPT(26-27) — rule 2 treats the option as not
# guaranteed, so final_gtd_year = 25-26 and the extension's own money starts
# at 26-27, exactly the option year the old deal was contingent on.
reset({
    "salaries": {"24-25": "$5,000,000", "25-26": "$5,000,000", "26-27": "$5,500,000"},
    "cap_holds": {"26-27": "PLAYER_OPT"}, "guaranteed": {}, "guarantee_dates": {}, "guarantee_schedule": {},
    "contracts": [],
})
tx._apply_extension(
    tx.ExtensionDetails(player="p", team="BOS", kind="veteran",
                        contract=contract({"26-27": "$7,000,000", "27-28": "$7,300,000"})),
    "2026-08-21", INFO, txn_id="tx2")
bio = BIOS["p"]
check("the extension's own 26-27 figure wins, not the old option's",
      bio["salaries"]["26-27"] == "$7,000,000")
check("the old PLAYER_OPT tag on 26-27 is gone — no collision with the new guaranteed year",
      bio["cap_holds"].get("26-27") != "PLAYER_OPT")
check("24-25 and 25-26 (the real guaranteed years) are untouched",
      bio["salaries"]["24-25"] == "$5,000,000" and bio["salaries"]["25-26"] == "$5,000,000")

print("\nthe trailing UFA/RFA hold auto-fills, same as a signing")
reset({
    "salaries": {"24-25": "$5,000,000", "25-26": "$5,000,000", "26-27": "$5,000,000"},
    "cap_holds": {}, "guaranteed": {}, "guarantee_dates": {}, "guarantee_schedule": {},
    "contracts": [],
})
tx._apply_extension(
    tx.ExtensionDetails(player="p", team="BOS", kind="veteran", bird_rights_type="Non-QVFA",
                        contract=contract({"27-28": "$6,000,000"}, {"28-29": "UFA"})),
    "2026-08-21", INFO, txn_id="tx3")
bio = BIOS["p"]
check("the 28-29 UFA hold got priced, not left as a placeholder",
      "28-29" in bio["salaries"] and bio["salaries"]["28-29"] not in (None, "", "$0"))
check("28-29 carries the UFA tag", bio["cap_holds"].get("28-29") == "UFA")

print("\nempty contract is refused before anything is written")
reset({"salaries": {"26-27": "$5,000,000"}, "cap_holds": {}, "guaranteed": {},
      "guarantee_dates": {}, "guarantee_schedule": {}, "contracts": []})
try:
    tx._apply_extension(
        tx.ExtensionDetails(player="p", team="BOS", contract=contract({})),
        "2026-08-21", INFO)
    check("empty contract raises", False)
except Exception as e:
    check("empty contract raises (422)", getattr(e, "status_code", None) == 422)
check("bio untouched by the refused call", BIOS["p"]["salaries"] == {"26-27": "$5,000,000"})

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))
sys.exit(1 if FAILS else 0)

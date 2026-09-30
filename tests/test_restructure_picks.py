"""`restructure_picks.py` and the first ruling it applies (memo 2026-01).

The script's own dry run proves a spec gives every team one pick in every draft
order. That is necessary but not enough: a structure could hand out one pick
each and still hand them to the wrong teams. So this pins the 2028 chain against
an independent reading of the ruling, written straight from the memo's five
steps, for all 720 draft orders of its six picks.

Runs on a temp directory holding only those six picks; nothing live is read.

    venv/bin/python -m tests.test_restructure_picks
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="nbn-restructure-test-"))
os.environ["NBS_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

import restructure_picks as R  # noqa: E402
from picks_conveyance import registry, seed_store, resolver, parity  # noqa: E402
from routers import audit  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


SPEC = json.loads((ROOT / "pick_rulings" / "2026-01-sac-2028-first.json").read_text())
ORIGS = ["SAC", "DAL", "MIA", "MEM", "CHA", "PHX"]
CSV = TMP / "draft-picks.csv"
CSV.write_text("YEAR,ROUND,ORIG,OWNER,PICK,PLAYER,PROTECTED,SWAP_OWNER,NOTES,FROZEN,FROZEN_REASON\n"
               "2028,1,SAC,MIA,,,,,,,\n2028,1,DAL,MIL,,,,,,,\n2028,1,MIA,SAC,,,,,,,\n"
               "2028,1,MEM,MEM,,,,,,,\n2028,1,CHA,DAL,,,,,,,\n2028,1,PHX,MIA,,,,,,,\n")
registry.REGISTRY_FILE = TMP / "draft-conveyance-registry.json"
OLD = {"protected": {}, "swap_groups": {"sg_c28_3way": {"members": [], "priority": []}},
       "binary_chains": {}, "chain_members": {}, "ladders": [], "legacy": {}}


def by_the_memo(slot: dict) -> dict:
    """The memo's five steps, by hand. Lower slot is the better pick."""
    better = lambda x, y: x if slot[x] < slot[y] else y
    worse = lambda x, y: y if better(x, y) == x else x
    got = {}
    sac, dal_slot = better("SAC", "DAL"), worse("SAC", "DAL")          # Trade 78
    sac2, got["CHA"] = better(sac, "MIA"), worse(sac, "MIA")           # Trade 15
    got["MEM"], dal_slot = better("MEM", dal_slot), worse("MEM", dal_slot)  # Trade 51
    got["MIL"], got["DAL"] = better(dal_slot, "CHA"), worse(dal_slot, "CHA")  # Trades 53/31
    got["MIA"], got["SAC"] = better(sac2, "PHX"), worse(sac2, "PHX")   # draft-day pool
    return got


def test_ruling():
    print("\n-- the 2028 chain matches the memo in every draft order --")
    new = R.candidate(OLD, SPEC, registry)
    check("the three-way pool is gone", "sg_c28_3way" not in new["swap_groups"])
    check("MEM's pick is in the chain", "2028|1|MEM" in new["chain_members"]["c28"])
    check("the proof passes", R.prove(new, SPEC, registry, seed_store, resolver, CSV) == [])
    store = R.store_for(new, registry, seed_store, CSV)
    wrong = 0
    for order in itertools.permutations(range(1, 7)):
        slot = dict(zip(ORIGS, order))
        owners = resolver.resolve_all(store, {(2028, 1, o): n for o, n in slot.items()})
        served = {team: pick[2] for pick, team in owners.items()}
        if served != by_the_memo(slot):
            wrong += 1
    check("all 720 orders give each team the pick the memo says", wrong == 0)
    rows = [dict(zip(["YEAR", "ROUND", "ORIG", "OWNER"], l.split(",")[:4]))
            for l in CSV.read_text().splitlines()[1:]]
    check("every owner the flat ledger names has a claim on the site",
          parity.owner_mismatches(rows, store) == [])


def test_refuses_a_wrong_spec():
    print("\n-- a structure that loses a pick is refused --")
    bad = json.loads(json.dumps(SPEC))
    bad["nodes"][4]["worse_to"] = "MIA"   # MIA gets both pool picks, SAC none
    problems = R.prove(R.candidate(OLD, bad, registry), bad, registry, seed_store, resolver, CSV)
    check("the proof names a failing draft order", any("resolves to" in p for p in problems))
    wrong_example = json.loads(json.dumps(SPEC))
    wrong_example["examples"][0]["owners"]["SAC"] = 5
    problems = R.prove(R.candidate(OLD, wrong_example, registry), wrong_example,
                       registry, seed_store, resolver, CSV)
    check("a worked example that doesn't hold is refused", any(p.startswith("example 1") for p in problems))


def test_registry_edits_are_logged():
    print("\n-- the registry is on the edit log's allowlist --")
    check("draft-conveyance-registry.json is audited",
          audit.should_audit(TMP / "draft-conveyance-registry.json"))


if __name__ == "__main__":
    test_ruling()
    test_refuses_a_wrong_spec()
    test_registry_edits_are_logged()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nall restructure checks passed")

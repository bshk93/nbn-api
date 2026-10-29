"""POST /api/picks/structure/preview and /apply, on a temp directory.

routers/constants.py hardcodes DATA_DIR, so setting NBS_DATA_DIR alone would
still write live: every path these touch is patched to the temp dir here.

    venv/bin/python -m tests.test_picks_structure
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="nbn-structure-test-"))
os.environ["NBS_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from picks_conveyance import registry, seed_store  # noqa: E402
from routers import audit, picks_structure as PS  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


CSV = TMP / "draft-picks.csv"
CSV.write_text("YEAR,ROUND,ORIG,OWNER,PICK,PLAYER,PROTECTED,SWAP_OWNER,NOTES,FROZEN,FROZEN_REASON\n"
               "2027,2,MIN,CHI,,,,,,,\n2027,2,SAS,SAS,,,,,,,\n")
REG = TMP / "draft-conveyance-registry.json"
REG.write_text(json.dumps({"protected": {}, "swap_groups": {}, "binary_chains": {},
                           "chain_members": {}, "ladders": [], "legacy": {}}))
PS.PICKS_FILE = CSV
PS.RULINGS_FILE = TMP / "pick-rulings.jsonl"
registry.REGISTRY_FILE = REG
audit.EDITS_FILE = TMP / "edits.jsonl"
seed_store.DEFAULT_OUT = TMP / "draft-conveyance.json"
INFO = {"name": "tester", "roles": ["rosters"]}

SPEC = {"kind": "protected", "pick": {"year": 2027, "round": 2, "orig": "MIN"},
        "bands": [{"min": 31, "max": 55, "to": "BOS"}, {"min": 56, "max": 60, "to": "CHI"}],
        "examples": [{"position": 40, "owner": "BOS"}]}


def main():
    print("-- preview writes nothing --")
    before = REG.read_text()
    r = PS.preview_structure(PS.StructureIn(spec=SPEC), info=INFO)
    check("a good spec previews ok", r["ok"] and not r["problems"])
    check("it shows the bands it would serve",
          [l["team"] for l in r["after"][0]["leaves"]] == ["BOS", "CHI"])
    check("the registry is untouched", REG.read_text() == before)
    check("it hides the raw registry", "new_reg" not in r and "old_text" not in r)

    print("-- apply refuses what preview flags --")
    bad = json.loads(json.dumps(SPEC)); bad["bands"][1]["min"] = 57
    try:
        PS.apply_structure(PS.StructureIn(spec=bad, reason="Trade 26, BOS kept 31-55"), info=INFO)
        check("a gapped band is refused", False)
    except HTTPException as exc:
        check("a gapped band is refused", exc.status_code == 422)
    try:
        PS.apply_structure(PS.StructureIn(spec=SPEC, reason="x"), info=INFO)
        check("no reason is refused", False)
    except HTTPException as exc:
        check("no reason is refused", exc.status_code == 422)
    check("nothing written by the refusals", REG.read_text() == before)

    print("-- apply writes, logs and records why --")
    r = PS.apply_structure(PS.StructureIn(spec=SPEC, reason="Trade 26 (2025-07-19): BOS kept 31-55"), info=INFO)
    reg = json.loads(REG.read_text())
    check("the bands are in the registry", "2027|2|MIN" in reg["protected"])
    check("the served store was regenerated", r["store_regenerated"]
          and (TMP / "draft-conveyance.json").exists())
    rulings = [json.loads(l) for l in PS.RULINGS_FILE.read_text().splitlines()]
    check("the reason and spec are kept", rulings[-1]["reason"].startswith("Trade 26")
          and rulings[-1]["spec"] == SPEC and rulings[-1]["actor"] == "tester")

    print("-- a registry that moved since the preview is refused --")
    from picks_conveyance import restructure
    stale = restructure.evaluate(SPEC, REG, CSV)
    REG.write_text(REG.read_text().replace('"BOS"', '"BOS" ', 1))
    try:
        restructure.apply(stale, REG, CSV, audit)
        check("a stale evaluation is refused", False)
    except RuntimeError:
        check("a stale evaluation is refused", True)

    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nall picks/structure checks passed")


if __name__ == "__main__":
    main()

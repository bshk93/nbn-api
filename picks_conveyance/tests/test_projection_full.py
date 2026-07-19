"""Project the full curated store (every node type) to the flat shape.

Asserts the projection never raises, always yields a well-formed flat row, and
produces the expected display for a sample of each node type.

    venv/bin/python -m picks_conveyance.tests.test_projection_full
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import seed_store, registry, projection  # noqa: E402

FLAT_KEYS = {"year", "round", "orig", "owner", "pick", "player", "protected",
             "conveys", "swap_owner", "swap_conveys", "notes", "frozen",
             "frozen_reason", "leaves", "group_id", "ladder", "legacy"}
FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def main():
    store = seed_store.build_store(seed_store.DEFAULT_IN)
    registry.seed_registry_from_curated()
    registry.apply_registry(store)
    by_key = {(p["year"], p["round"], p["orig"]): p for p in store["picks"]}

    # 1. project every pick — no exceptions, well-formed, non-empty owner
    bad = 0
    for p in store["picks"]:
        row = projection.project_to_flat(p, store)
        if set(row) != FLAT_KEYS or not row["owner"]:
            bad += 1
            if bad <= 5:
                print(f"  [FAIL] malformed row {p['year']} R{p['round']} {p['orig']}: {row}")
    print(f"projected all {len(store['picks'])} picks ({bad} malformed)")
    if bad:
        FAILS.append("malformed")

    def proj(k):
        return projection.project_to_flat(by_key[k], store)

    # 2. protected: keeper-is-origin collapses to (owner, protected)
    r = proj((2027, 1, "CHI"))
    check("2027 CHI -> owner DEN, protected 4", (r["owner"], r["protected"]), ("DEN", 4))
    # protected: keeper is a third team (NYK) -> candidates
    check("2027 IND 2nd -> candidates NYK|MIA", proj((2027, 2, "IND"))["owner"], "NYK|MIA")
    # range split, neither is origin -> candidates
    check("2027 GSW 2nd -> candidates UTA|LAC", proj((2027, 2, "GSW"))["owner"], "UTA|LAC")

    # 3. swap group -> pipe-joined priority teams
    check("2028 BOS 1st swap -> BOS|CHI", proj((2028, 1, "BOS"))["owner"], "BOS|CHI")
    check("2027 OKC 2nd swap -> BOS|NOP", proj((2027, 2, "OKC"))["owner"], "BOS|NOP")
    # 3b. 2-team swap -> swap_owner restored (live-draft real-time resolution)
    check("2028 BOS 1st swap_owner -> CHI (counterpart orig)",
          proj((2028, 1, "BOS"))["swap_owner"], "CHI")
    check("2028 CHI 1st swap_owner -> BOS (counterpart orig)",
          proj((2028, 1, "CHI"))["swap_owner"], "BOS")
    # 3c. leaves field (CUTOVER STEP 9) -- addressable leaf_ids for the trade
    # payload to reference; each leaf's own team must round-trip back through
    # team_holds_leaf.
    bos_leaves = proj((2028, 1, "BOS"))["leaves"]
    check("2028 BOS 1st has 2 leaves (BOS, CHI)",
          sorted(l["team"] for l in bos_leaves), ["BOS", "CHI"])
    check("settled pick has empty leaves",
          proj((2029, 1, "ATL"))["leaves"], [])
    # 3d. group_id -- dedup key shared by every pick in the same swap group or
    # binary chain (lets a consumer collapse N picks into 1 displayed entry).
    check("2028 BOS 1st group_id -> swap:sg_bos_chi_28_1",
          proj((2028, 1, "BOS"))["group_id"], "swap:sg_bos_chi_28_1")
    check("2028 CHI 1st shares the same group_id as 2028 BOS 1st",
          proj((2028, 1, "CHI"))["group_id"], proj((2028, 1, "BOS"))["group_id"])
    check("settled pick has null group_id",
          proj((2029, 1, "ATL"))["group_id"], None)

    # 4. binary chain -> candidate teams from the chain
    nop = proj((2030, 1, "NOP"))["owner"].split("|")
    check("2030 NOP chain candidates incl MIL,POR,DET,NOP,HOU",
          set(["MIL", "POR", "DET", "NOP", "HOU"]).issubset(set(nop)), True)
    check("2030 NOP chain group_id -> chain:nmpdh",
          proj((2030, 1, "NOP"))["group_id"], "chain:nmpdh")
    check("2030 MIL shares the same group_id as 2030 NOP (same chain)",
          proj((2030, 1, "MIL"))["group_id"], proj((2030, 1, "NOP"))["group_id"])

    # 5. legacy -> nominal owner preserved from the CSV. Synthetic node: as
    # of 2026-07-19 all 6 real legacy picks have been resolved to real
    # structure (see curated.py), so a real example would only be
    # transient -- this is deliberately not tied to live data.
    legacy_pick = {"year": 2099, "round": 1, "orig": "ATL", "pick": None, "player": None,
                  "notes": "", "frozen": False, "frozen_reason": "",
                  "conveyance": {"type": "legacy", "reason": "test reason", "owner": "BOS"}}
    legacy_proj = projection.project_to_flat(legacy_pick, store)
    check("synthetic legacy owner preserved", legacy_proj["owner"], "BOS")
    check("synthetic legacy field flags it, with the real reason",
          legacy_proj["legacy"], {"reason": "test reason"})
    check("settled non-legacy pick has null legacy field",
          proj((2029, 1, "ATL"))["legacy"], None)

    # 6. ladder -> additive field surfaces protect_top + fallback (previously
    # invisible: a ladder-governed pick showed no different from a plain
    # settled one, per the sas_tor gap)
    sas_tor_ladder = proj((2029, 1, "SAS"))["ladder"]
    check("2029 SAS 1st ladder protect_top -> 10",
          (sas_tor_ladder or {}).get("protect_top"), 10)
    check("2029 SAS 1st ladder fallback -> 2029/2030 SAS 2nds",
          sorted((p["year"], p["orig"]) for p in (sas_tor_ladder or {}).get("fallback", [])),
          [(2029, "SAS"), (2030, "SAS")])
    check("settled non-ladder pick has null ladder",
          proj((2029, 1, "ATL"))["ladder"], None)

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("PROJECTION (full curated store) OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

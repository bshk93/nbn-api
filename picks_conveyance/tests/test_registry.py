"""Registry tests: seeded output matches the old static curated.py path
exactly, and appending a new entry actually persists and takes effect.

    venv/bin/python -m picks_conveyance.tests.test_registry
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import curated, registry, seed_store  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    orig_registry = registry.REGISTRY_FILE
    tmp_registry = Path("/tmp/picks_conveyance_registry_test.json")
    registry.REGISTRY_FILE = tmp_registry
    tmp_registry.unlink(missing_ok=True)

    try:
        # 1. seeded-from-curated registry produces byte-identical output to the
        #    old static curated.apply_curated path
        reg = registry.seed_registry_from_curated(force=True)
        check("registry seeded with all 5 categories",
              all(k in reg for k in
                  ("protected", "swap_groups", "binary_chains", "ladders", "legacy")))

        store_old = seed_store.build_store(seed_store.DEFAULT_IN)
        curated.apply_curated(store_old)
        store_new = seed_store.build_store(seed_store.DEFAULT_IN)
        registry.apply_registry(store_new)

        old_by_key = {(p["year"], p["round"], p["orig"]): p["conveyance"]
                      for p in store_old["picks"]}
        new_by_key = {(p["year"], p["round"], p["orig"]): p["conveyance"]
                      for p in store_new["picks"]}
        check("per-pick conveyance identical to static curated.py path",
              old_by_key == new_by_key)
        for k in ("swap_groups", "binary_swaps", "chains", "ladders"):
            check(f"{k} identical to static curated.py path",
                  json.dumps(store_old[k], sort_keys=True) ==
                  json.dumps(store_new[k], sort_keys=True))

        # 2. appending a brand-new protection actually persists and resolves
        registry.add_protected(
            (2099, 1, "ATL"),
            on={"year": 2099, "round": 1, "orig": "ATL"},
            bands=[{"min": 1, "max": 5, "to": "ATL"},
                  {"min": 6, "max": 60, "to": "BOS"}])
        reg2 = registry.load_registry()
        check("appended protection persists on disk",
              "2099|1|ATL" in reg2["protected"])

        # simulate a fresh pick (not in the real CSV) to confirm apply_registry
        # would wire it up if present in a store
        fake_store = {"picks": [{"year": 2099, "round": 1, "orig": "ATL",
                                 "conveyance": {"type": "settled", "team": "ATL"},
                                 "player": None}]}
        registry.apply_registry(fake_store)
        node = fake_store["picks"][0]["conveyance"]
        check("newly appended entry takes effect on next apply_registry",
              node["type"] == "protected" and node["bands"][0]["max"] == 5)

        # 3. appending a swap group works the same way
        registry.add_swap_group("sg_test_2099",
                                members=[{"year": 2099, "round": 2, "orig": "ATL"},
                                        {"year": 2099, "round": 2, "orig": "BOS"}],
                                priority=["ATL", "BOS"])
        check("appended swap group persists on disk",
              "sg_test_2099" in registry.load_registry()["swap_groups"])

        # 4. docs/picks-conveyance-hardening.md items C/D: add_ladder rejects
        #    a new ladder that collides (on step OR fallback) with an
        #    existing one, instead of silently letting both exist for the
        #    resolver to arbitrarily pick between later.
        registry.add_ladder({
            "id": "ladder_test_1", "from": "MIA", "to": "ORL",
            "steps": [{"year": 2099, "round": 1, "orig": "MIA", "protect_top": 10}],
            "fallback": {"type": "fixed_asset",
                        "picks": [{"year": 2100, "round": 2, "orig": "MIA"}]},
        })
        check("first ladder persists", any(
            L["id"] == "ladder_test_1" for L in registry.load_registry()["ladders"]))

        step_collision_raised = False
        try:
            registry.add_ladder({
                "id": "ladder_test_2_step_collision", "from": "MIA", "to": "TOR",
                # same governed step as ladder_test_1
                "steps": [{"year": 2099, "round": 1, "orig": "MIA", "protect_top": 20}],
                "fallback": None,
            })
        except registry.LadderConflict:
            step_collision_raised = True
        check("add_ladder rejects a step colliding with an existing ladder's step",
              step_collision_raised)

        fallback_collision_raised = False
        try:
            registry.add_ladder({
                "id": "ladder_test_3_fallback_collision", "from": "DAL", "to": "SAC",
                "steps": [{"year": 2099, "round": 1, "orig": "DAL", "protect_top": 15}],
                # same fallback target as ladder_test_1
                "fallback": {"type": "fixed_asset",
                            "picks": [{"year": 2100, "round": 2, "orig": "MIA"}]},
            })
        except registry.LadderConflict:
            fallback_collision_raised = True
        check("add_ladder rejects a fallback colliding with an existing ladder's fallback",
              fallback_collision_raised)

        check("rejected ladders were never persisted (only ladder_test_1 present)",
              [L["id"] for L in registry.load_registry()["ladders"]
               if L["id"].startswith("ladder_test_")] == ["ladder_test_1"])

        whole_registry_collision_raised = False
        try:
            registry._check_ladder_collisions([
                {"id": "x1", "from": "GSW", "to": "SAC",
                 "steps": [{"year": 2099, "round": 2, "orig": "GSW", "protect_top": 40}],
                 "fallback": None},
                {"id": "x2", "from": "GSW", "to": "LAC",
                 "steps": [{"year": 2099, "round": 2, "orig": "GSW", "protect_top": 50}],
                 "fallback": None},
            ])
        except registry.LadderConflict:
            whole_registry_collision_raised = True
        check("_validate_registry's whole-registry sweep also catches a pairwise collision",
              whole_registry_collision_raised)

    finally:
        registry.REGISTRY_FILE = orig_registry
        tmp_registry.unlink(missing_ok=True)

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("REGISTRY TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate + resolve the curated nodes end to end.

Seeds a store from a synthetic picks CSV, applies the curated conveyance,
validates every node, then resolves the whole store under a synthetic
full-league draft order and asserts every contingent (protected / swap /
binary) pick lands on a real team and every legacy pick is skipped. Proves the
reconciliation is executable.

**Fixture, not live data.** Until 2026-09-30 this built its store from the live
`draft-picks.csv` and the live registry, so it was testing whatever the league
had traded since, not `curated.py`. Every retrade since July broke one of the
direction checks below (2031 HOU/MIN, 2027 PHI/TOR/DAL), and with no registry
on disk its `seed_registry_from_curated()` would have *written* the live one.
Now the CSV is every team's own pick for each year the seed covers, and the
registry is seeded from `curated.py` into a temp file, so this pins the seed and
nothing else.

    venv/bin/python -m picks_conveyance.tests.test_curated
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import model, resolver, seed_store, curated, registry  # noqa: E402

TEAMS = sorted(["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET",
                "GSW", "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN",
                "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS",
                "TOR", "UTA", "WAS"])
FAILS = []


def synthetic_positions(years):
    """Deterministic full draft order: R1 = 1..30, R2 = 31..60, team order
    rotated per year so outcomes actually vary across years."""
    pos = {}
    for y in years:
        order = TEAMS[y % 30:] + TEAMS[:y % 30]
        for i, t in enumerate(order):
            pos[(y, 1, t)] = i + 1
            pos[(y, 2, t)] = i + 31
    return pos


FIELDS = ["YEAR", "ROUND", "ORIG", "OWNER", "PICK", "PLAYER", "PROTECTED",
          "SWAP_OWNER", "NOTES", "FROZEN", "FROZEN_REASON"]


def fixture_store(tmp: Path) -> dict:
    """Every team's own 1st and 2nd for each year the seed touches, owned by
    that team, plus a registry seeded fresh from curated.py. Nothing live."""
    keys = (set(curated.PROTECTED) | set(curated.LEGACY)
            | {(m["year"], m["round"], m["orig"])
               for g in curated.SWAP_GROUPS.values() for m in g["members"] if "orig" in m}
            | {k for ks in curated.CHAIN_MEMBERS.values() for k in ks})
    years = sorted({k[0] for k in keys})
    csv_path = tmp / "draft-picks.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for y in years:
            for rnd in (1, 2):
                for t in TEAMS:
                    w.writerow({"YEAR": y, "ROUND": rnd, "ORIG": t, "OWNER": t})
    registry.REGISTRY_FILE = tmp / "draft-conveyance-registry.json"
    registry.seed_registry_from_curated(force=True)
    return registry.apply_registry(seed_store.build_store(csv_path))


def main():
    store = fixture_store(Path(tempfile.mkdtemp(prefix="nbn-test-curated-")))

    # 1. validate every node
    nerr = 0
    for p in store["picks"]:
        try:
            model.validate(p["conveyance"])
        except model.ConveyanceError as e:
            nerr += 1
            print(f"  [FAIL] validate {p['year']} R{p['round']} {p['orig']}: {e}")
    for sid, n in store["binary_swaps"].items():
        try:
            model.validate(n)
        except model.ConveyanceError as e:
            nerr += 1
            print(f"  [FAIL] validate binary_swap {sid}: {e}")
    for gid, g in store["swap_groups"].items():
        for slot in g["priority"]:
            if model.is_node(slot):
                model.validate(slot)
    print(f"validated all nodes ({nerr} errors)")
    if nerr:
        FAILS.append("validation")

    # 2. resolve under a synthetic full draft
    years = sorted({p["year"] for p in store["picks"]})
    pos = synthetic_positions(years)
    owners = resolver.resolve_all(store, pos)

    # 3. every contingent curated pick must resolve; every legacy pick must not
    # (a swap group member can be a dynamic output-ref rather than a fixed
    # pick -- its underlying pick is already covered via CHAIN_MEMBERS, so
    # only plain pick-ref members need adding here)
    contingent = (list(curated.PROTECTED)
                  + [(m["year"], m["round"], m["orig"])
                     for g in curated.SWAP_GROUPS.values() for m in g["members"]
                     if "orig" in m]
                  + [k for ks in curated.CHAIN_MEMBERS.values() for k in ks])
    unresolved = [k for k in contingent if k not in owners]
    if unresolved:
        FAILS.append("unresolved")
        print(f"  [FAIL] {len(unresolved)} contingent picks unresolved: {unresolved[:8]}")
    else:
        print(f"resolved all {len(set(contingent))} contingent picks to a team")

    leaked = [k for k in curated.LEGACY if k in owners]
    if leaked:
        FAILS.append("legacy-leak")
        print(f"  [FAIL] legacy picks resolved (should be skipped): {leaked}")
    else:
        print(f"all {len(curated.LEGACY)} legacy picks correctly skipped by resolver")

    # 4. sample output for eyeballing
    print("\nsample resolved owners under synthetic draft:")
    for k in [(2030, 1, "NOP"), (2030, 1, "DET"), (2028, 1, "ORL"),
              (2028, 1, "WAS"), (2027, 1, "CHI"), (2027, 2, "GSW")]:
        print(f"  {k} -> {owners.get(k)}")

    # 5. deal-specific direction checks for the two picks resolved out of
    # LEGACY 2026-07-19 (real transactions found, not synthetic) -- the
    # generic loop above only proves these resolve at all, not that the
    # actual better/worse direction matches what the real trades granted.
    def check(name, got, want):
        ok = got == want
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
        if not ok:
            FAILS.append(name)

    print("\n2031 HOU/MIN swap (Trade 54 + Trade 73):")
    r_hou_better = resolver.resolve_all(store, {(2031, 1, "HOU"): 5, (2031, 1, "MIN"): 20})
    check("HOU's own pick better -> HOU keeps it",
          r_hou_better.get((2031, 1, "HOU")), "HOU")
    check("HOU's own pick better -> IND keeps the MIN pick",
          r_hou_better.get((2031, 1, "MIN")), "IND")
    r_min_better = resolver.resolve_all(store, {(2031, 1, "HOU"): 25, (2031, 1, "MIN"): 3})
    check("MIN pick better -> HOU swaps for it",
          r_min_better.get((2031, 1, "MIN")), "HOU")
    check("MIN pick better -> IND keeps HOU's own pick instead",
          r_min_better.get((2031, 1, "HOU")), "IND")

    print("\n2027 PHI/CHA/TOR/DAL chain (Trade 18 + Trade 27):")
    r_a = resolver.resolve_all(store, {(2027, 1, "PHI"): 2, (2027, 1, "TOR"): 15, (2027, 1, "DAL"): 8})
    check("PHI best -> CHA takes it", r_a.get((2027, 1, "PHI")), "CHA")
    check("TOR's leftover (15) worse than DAL (8) -> TOR swaps for DAL's",
          r_a.get((2027, 1, "DAL")), "TOR")
    check("DAL keeps TOR's own leftover pick", r_a.get((2027, 1, "TOR")), "DAL")
    r_b = resolver.resolve_all(store, {(2027, 1, "PHI"): 10, (2027, 1, "TOR"): 4, (2027, 1, "DAL"): 25})
    check("TOR best -> CHA takes it", r_b.get((2027, 1, "TOR")), "CHA")
    check("PHI's leftover (10) better than DAL (25) -> TOR keeps it, no swap",
          r_b.get((2027, 1, "PHI")), "TOR")
    check("DAL keeps its own pick (worse than PHI's leftover)",
          r_b.get((2027, 1, "DAL")), "DAL")

    print("\n2027 PHX/OKC/LAC/DET/GSW cascade (Trade 42 + Trade 74 + Trade 83):")
    r_c = resolver.resolve_all(store, {(2027, 1, "OKC"): 3, (2027, 1, "LAC"): 20,
                                       (2027, 1, "DET"): 10, (2027, 1, "GSW"): 15})
    check("OKC best overall -> PHX takes it", r_c.get((2027, 1, "OKC")), "PHX")
    check("DET (10) better than LAC's leftover (20) -> LAC takes DET's pick",
          r_c.get((2027, 1, "DET")), "LAC")
    check("GSW (15) better than LAC's leftover (20) -> DET takes GSW's pick",
          r_c.get((2027, 1, "GSW")), "DET")
    check("LAC's own leftover ends up with GSW (worst of the whole chain)",
          r_c.get((2027, 1, "LAC")), "GSW")
    r_d = resolver.resolve_all(store, {(2027, 1, "OKC"): 18, (2027, 1, "LAC"): 2,
                                       (2027, 1, "DET"): 25, (2027, 1, "GSW"): 9})
    check("LAC best overall -> PHX takes it", r_d.get((2027, 1, "LAC")), "PHX")
    check("OKC's leftover (18) better than DET (25) -> LAC takes OKC's pick",
          r_d.get((2027, 1, "OKC")), "LAC")
    check("GSW (9) better than DET's leftover (25) -> DET takes GSW's pick",
          r_d.get((2027, 1, "GSW")), "DET")
    check("DET's own leftover ends up with GSW (worst of the whole chain)",
          r_d.get((2027, 1, "DET")), "GSW")

    # The 2028 SAC/DAL/MIA/PHX/CHA cluster's seed here was overruled by league
    # office memo 2026-01 (the live chain is pick_rulings/2026-01-sac-2028-first.json,
    # pinned against the memo in tests/test_restructure_picks.py). Its seed is
    # still validated and resolved by the generic checks above, but its
    # direction is no longer asserted: those checks encoded the overruled reading.

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("CURATED NODES OK — all validate, resolve, and legacy-skip correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

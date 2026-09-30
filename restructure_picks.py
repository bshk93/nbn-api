#!/usr/bin/env python3
"""Replace one binary chain in the pick conveyance registry with a ruled structure.

The registry (`draft-conveyance-registry.json`) is what `/api/picks` serves for
every pick inside a swap or protection structure. Trades add to it through
`picks_conveyance.from_trade`, but nothing could *correct* a structure after a
league ruling. The one time that was needed before, the file was edited by hand,
with no check that the result still resolved and no record of what changed.

This is the tool for that. A ruling is written as a spec file in
`pick_rulings/`: the chain's nodes, the picks in it, the teams that must end up
with one pick each, and the worked examples from the ruling. Then:

1. **Dry run by default.** It prints the chain before and after and what the
   site will serve for each pick, and stops. `--apply` is a separate act.
2. **It proves the new structure resolves.** Every possible draft order of the
   chain's picks (6 picks is 720 orders) must give each team exactly one pick,
   and every worked example in the spec must come out exactly as written.
3. **It runs the registry's own validation** (`_validate_registry`: node shape,
   cycles, no pick claimed by two structures) and the weekly parity check
   against `draft-picks.csv`, and reports the change in parity findings.
4. **An applied change is logged** to `edits.jsonl`, like every other write that
   bypasses the transaction ledger, and the served store is regenerated.

    venv/bin/python restructure_picks.py pick_rulings/2026-01-sac-2028-first.json
    venv/bin/python restructure_picks.py pick_rulings/2026-01-sac-2028-first.json --apply

`--data-dir` points it at a scratch copy (it sets NBS_DATA_DIR before the
conveyance modules load, so nothing reads or writes the live directory).
"""
from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--data-dir", type=Path, default=Path("/var/lib/nothing-but-stats"))
    ap.add_argument("--actor", default="restructure_picks.py")
    args = ap.parse_args()

    os.environ["NBS_DATA_DIR"] = str(args.data_dir)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from picks_conveyance import registry, seed_store, resync, resolver, projection, parity
    from routers import audit

    spec = json.loads(args.spec.read_text())
    reg_file = args.data_dir / "draft-conveyance-registry.json"
    csv_file = args.data_dir / "draft-picks.csv"
    registry.REGISTRY_FILE = reg_file
    audit.EDITS_FILE = args.data_dir / "edits.jsonl"

    old_text = reg_file.read_text()
    old_reg = json.loads(old_text)
    new_reg = candidate(old_reg, spec, registry)

    problems = prove(new_reg, spec, registry, seed_store, resolver, csv_file)

    csv_rows = list(csv.DictReader(open(csv_file, newline="")))
    before = parity.owner_mismatches(csv_rows, store_for(old_reg, registry, seed_store, csv_file))
    after_store = store_for(new_reg, registry, seed_store, csv_file)
    after = parity.owner_mismatches(csv_rows, after_store)

    cid = spec["chain_id"]
    print(f"chain {cid}: {len(old_reg['binary_chains'].get(cid, []))} node(s) -> {len(spec['nodes'])}")
    for gid in spec.get("remove_swap_groups", []):
        print(f"  removes swap group {gid}" + ("" if gid in old_reg["swap_groups"] else " (not present)"))
    print(f"  members: {', '.join(m['orig'] for m in spec['members'])}")
    print("\nwhat the site will serve:")
    members = {resolver.key(m) for m in spec["members"]}
    for pick in after_store["picks"]:
        if resolver.key(pick) in members:
            flat = projection.project_to_flat(pick, after_store)
            print(f"  {flat['year']} R{flat['round']} {flat['orig']}: owner {flat['owner']}")
            for leaf in flat.get("leaves") or []:
                print(f"      {leaf['team']}: {leaf['description']}")
    print(f"\nparity findings: {len(before)} before, {len(after)} after")
    for line in sorted(set(after) - set(before)):
        print(f"  NEW  {line}")
    for line in sorted(set(before) - set(after)):
        print(f"  gone {line}")

    if problems:
        print("\nREFUSED:")
        for p in problems:
            print(f"  {p}")
        return 1
    if set(after) - set(before):
        print("\nREFUSED: the change adds parity findings.")
        return 1
    print(f"\nall {factorial(len(spec['members']))} draft orders resolve to one pick per team; "
          f"{len(spec.get('examples', []))} worked example(s) match.")

    if not args.apply:
        print("dry run — nothing written. Re-run with --apply.")
        return 0

    new_text = json.dumps(new_reg, indent=2)
    audit.begin_request("SCRIPT", f"restructure_picks.py {args.spec.name}")
    audit.set_actor(args.actor)
    registry.save_registry(new_reg)
    audit.record(reg_file, old_text, new_text)
    store = resync.resync(csv_path=csv_file, out_path=args.data_dir / "draft-conveyance.json")
    if store is None:
        print("registry written, but regenerating the served store failed — see the log.")
        return 1
    print(f"applied. {spec.get('ruling', '')}")
    return 0


def candidate(old_reg: dict, spec: dict, registry) -> dict:
    new = copy.deepcopy(old_reg)
    cid = spec["chain_id"]
    for gid in spec.get("remove_swap_groups", []):
        new["swap_groups"].pop(gid, None)
    new["binary_chains"][cid] = spec["nodes"]
    new["chain_members"][cid] = [registry._kstr((m["year"], m["round"], m["orig"]))
                                 for m in spec["members"]]
    return new


def store_for(reg: dict, registry, seed_store, csv_file: Path) -> dict:
    """The store `resync` would write, built from `reg` without touching disk."""
    orig = registry.load_registry
    registry.load_registry = lambda: copy.deepcopy(reg)
    try:
        return registry.apply_registry(seed_store.build_store(csv_file))
    finally:
        registry.load_registry = orig


def prove(reg: dict, spec: dict, registry, seed_store, resolver, csv_file: Path) -> list[str]:
    problems = []
    try:
        registry._validate_registry(reg)
    except Exception as exc:
        return [f"registry validation: {exc}"]
    store = store_for(reg, registry, seed_store, csv_file)
    keys = [resolver.key(m) for m in spec["members"]]
    teams = sorted(spec["teams"])
    for order in itertools.permutations(range(1, len(keys) + 1)):
        positions = dict(zip(keys, order))
        owners = resolver.resolve_all(store, positions)
        got = sorted(owners.get(k) or "?" for k in keys)
        if got != teams:
            problems.append(f"draft order {dict(zip([k[2] for k in keys], order))} resolves to {got}")
            if len(problems) > 5:
                break
    by_orig = {k[2]: k for k in keys}
    for i, ex in enumerate(spec.get("examples", []), 1):
        positions = {by_orig[o]: n for o, n in ex["positions"].items()}
        owners = resolver.resolve_all(store, positions)
        got = {owners.get(by_orig[o]): n for o, n in ex["positions"].items()}
        if got != ex["owners"]:
            problems.append(f"example {i}: expected {ex['owners']}, got {got}")
    return problems


def factorial(n: int) -> int:
    return 1 if n <= 1 else n * factorial(n - 1)


if __name__ == "__main__":
    sys.exit(main())

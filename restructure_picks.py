#!/usr/bin/env python3
"""Write a ruled structure into the pick conveyance registry: a binary chain, a
protected pick's bands, or a two-team swap group.

The registry (`draft-conveyance-registry.json`) is what `/api/picks` serves for
every pick inside a swap or protection structure. Trades add to it through
`picks_conveyance.from_trade`, but nothing could *correct* a structure after a
league ruling. The one time that was needed before, the file was edited by hand,
with no check that the result still resolved and no record of what changed.

This is the tool for that. A ruling is written as a spec file in
`pick_rulings/`, one of three kinds (`"kind"`, default `"chain"`):

- `chain`: the chain's nodes, the picks in it, the teams that must end up with
  one pick each, and the worked examples from the ruling.
- `protected`: one pick and its bands (`{min, max, to}`). Proved by resolving
  every slot in the round: each must land with exactly one band's team.
- `swap_group`: two picks and the priority list (best pick first). A slot can
  be a protected node on `"__slot1__"` (the pick that lands there), for a swap
  whose worse pick is itself protected; the worked examples are then the
  proof, and must give the pick to every band's team at least once.

Then:

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
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def __getattr__(name):
    # The checks live in picks_conveyance.restructure, shared with the API's
    # /api/picks/structure endpoints. Imported lazily so --data-dir is set
    # before the conveyance modules read NBS_DATA_DIR.
    from picks_conveyance import restructure
    return getattr(restructure, name)


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
    from picks_conveyance.restructure import candidate, prove, store_for, members_of, slot_range, factorial
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

    kind = spec.get("kind", "chain")
    if kind == "chain":
        cid = spec["chain_id"]
        print(f"chain {cid}: {len(old_reg['binary_chains'].get(cid, []))} node(s) -> {len(spec['nodes'])}")
        for gid in spec.get("remove_swap_groups", []):
            print(f"  removes swap group {gid}" + ("" if gid in old_reg["swap_groups"] else " (not present)"))
    elif kind == "protected":
        k = registry._kstr(resolver.key(spec["pick"]))
        print(f"protected {k}: " + ("replaces the existing bands" if k in old_reg["protected"] else "new"))
    else:
        print(f"swap group {spec['group_id']}: " + ("replaces it" if spec["group_id"] in old_reg["swap_groups"] else "new"))
    print(f"  members: {', '.join(m['orig'] for m in members_of(spec))}")
    members = {resolver.key(m) for m in members_of(spec)}
    for label, store_ in (("now", store_for(old_reg, registry, seed_store, csv_file)), ("after", after_store)):
        print(f"\nwhat the site serves {label}:")
        for pick in store_["picks"]:
            if resolver.key(pick) in members:
                flat = projection.project_to_flat(pick, store_)
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
    if spec.get("kind", "chain") == "protected":
        lo, hi = slot_range(spec["pick"]["round"])
        print(f"\nevery slot {lo}-{hi} lands with exactly one band's team; "
              f"{len(spec.get('examples', []))} worked example(s) match.")
    elif spec.get("kind") == "swap_group" and any(isinstance(x, dict) for x in spec["priority"]):
        print(f"\n{len(spec.get('examples', []))} worked example(s) match, covering every band's team.")
    else:
        print(f"\nall {factorial(len(members_of(spec)))} draft orders resolve to one pick per team; "
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


if __name__ == "__main__":
    sys.exit(main())

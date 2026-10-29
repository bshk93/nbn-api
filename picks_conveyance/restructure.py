"""Checked writes to the pick conveyance registry: one place for the proof
that `restructure_picks.py` (the CLI) and `POST /api/picks/structure/*` (the
rosters committee) both run before anything is written.

A spec is one of three kinds (`"kind"`, default `"chain"`), documented in
`restructure_picks.py`. `evaluate` builds the candidate registry, proves it,
runs the parity check and describes what the site would serve before and
after; it writes nothing. `apply` writes only an evaluation with no problems
and no new parity findings, logs the change to `edits.jsonl` and regenerates
the served store.
"""
from __future__ import annotations

import copy
import csv
import itertools
import json
from pathlib import Path

from . import registry, seed_store, resolver, projection, parity, resync


def evaluate(spec: dict, reg_file: Path, csv_file: Path) -> dict:
    old_text = reg_file.read_text()
    old_reg = json.loads(old_text)
    try:
        new_reg = candidate(old_reg, spec, registry)
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "problems": [f"bad spec: {exc!r}"], "old_text": old_text}
    problems = prove(new_reg, spec, registry, seed_store, resolver, csv_file)
    csv_rows = list(csv.DictReader(open(csv_file, newline="")))
    before_store = store_for(old_reg, registry, seed_store, csv_file)
    after_store = store_for(new_reg, registry, seed_store, csv_file)
    before = parity.owner_mismatches(csv_rows, before_store)
    after = parity.owner_mismatches(csv_rows, after_store)
    new_parity = sorted(set(after) - set(before))
    if new_parity:
        problems = problems + ["the change adds parity findings: " + "; ".join(new_parity)]
    members = {resolver.key(m) for m in members_of(spec)}

    def served(store):
        return [projection.project_to_flat(p, store) for p in store["picks"]
                if resolver.key(p) in members]

    return {"ok": not problems, "problems": problems,
            "before": served(before_store), "after": served(after_store),
            "parity_before": len(before), "parity_after": len(after),
            "new_parity": new_parity, "gone_parity": sorted(set(before) - set(after)),
            "old_text": old_text, "new_reg": new_reg}


def apply(result: dict, reg_file: Path, csv_file: Path, audit) -> dict | None:
    """Write an evaluation that passed. The caller holds whatever request
    context `audit` should record the actor from."""
    if not result["ok"]:
        raise ValueError("refusing to apply an evaluation with problems")
    with registry._lock:
        if reg_file.read_text() != result["old_text"]:
            raise RuntimeError("the registry changed since this was evaluated; evaluate again")
        new_text = json.dumps(result["new_reg"], indent=2)
        reg_file.write_text(new_text)
        audit.record(reg_file, result["old_text"], new_text)
    return resync.resync(csv_path=csv_file, out_path=reg_file.parent / "draft-conveyance.json")


def members_of(spec: dict) -> list[dict]:
    kind = spec.get("kind", "chain")
    if kind == "protected":
        return [spec["pick"]]
    return spec["members"]


def slot_range(rnd: int) -> tuple[int, int]:
    """The draft slots a pick of this round can land in. A 1st's last band is
    often written "4-60", so the bands may run past 30; only 1-30 is checked."""
    return (1, 30) if rnd == 1 else (31, 60)


def candidate(old_reg: dict, spec: dict, registry) -> dict:
    new = copy.deepcopy(old_reg)
    kind = spec.get("kind", "chain")
    if kind == "protected":
        p = spec["pick"]
        new["protected"][registry._kstr((p["year"], p["round"], p["orig"]))] = {
            "on": p, "bands": spec["bands"]}
        return new
    if kind == "swap_group":
        group = {"members": spec["members"], "priority": spec["priority"]}
        if spec.get("txn_ids"):
            group["txn_ids"] = spec["txn_ids"]
        new["swap_groups"][spec["group_id"]] = group
        return new
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
    kind = spec.get("kind", "chain")
    if kind == "protected":
        return prove_protected(store, spec, resolver)
    keys = [resolver.key(m) for m in members_of(spec)]
    if kind == "swap_group" and any(isinstance(slot, dict) for slot in spec["priority"]):
        # A protected slot makes who ends up with a pick depend on where it
        # lands, so no fixed team list holds. The worked examples carry the
        # proof instead, and must cover each band of every nested slot.
        bands = [b for slot in spec["priority"] if isinstance(slot, dict) for b in slot["bands"]]
        covered = {b["to"] for ex in spec.get("examples", []) for b in bands
                   if b["to"] in ex["owners"]}
        missing = sorted({b["to"] for b in bands} - covered)
        problems = [f"no worked example gives the pick to {t}" for t in missing]
        return problems + prove_examples(store, spec, keys, resolver)
    teams = sorted(spec["teams"])
    for order in itertools.permutations(range(1, len(keys) + 1)):
        positions = dict(zip(keys, order))
        owners = resolver.resolve_all(store, positions)
        got = sorted(owners.get(k) or "?" for k in keys)
        if got != teams:
            problems.append(f"draft order {dict(zip([k[2] for k in keys], order))} resolves to {got}")
            if len(problems) > 5:
                break
    return problems + prove_examples(store, spec, keys, resolver)


def prove_protected(store: dict, spec: dict, resolver) -> list[str]:
    problems = []
    k = resolver.key(spec["pick"])
    teams = {b["to"] for b in spec["bands"]}
    lo, hi = slot_range(spec["pick"]["round"])
    seen = set()
    for slot in range(lo, hi + 1):
        owner = resolver.resolve_all(store, {k: slot}).get(k)
        holders = [b["to"] for b in spec["bands"] if b["min"] <= slot <= b["max"]]
        if len(holders) != 1 or owner != holders[0]:
            problems.append(f"slot {slot}: bands give {holders or 'nobody'}, resolver gives {owner}")
        seen.add(owner)
    if teams - seen:
        problems.append(f"band team(s) {sorted(teams - seen)} never get the pick in {lo}-{hi}")
    for i, ex in enumerate(spec.get("examples", []), 1):
        got = resolver.resolve_all(store, {k: ex["position"]}).get(k)
        if got != ex["owner"]:
            problems.append(f"example {i}: slot {ex['position']} expected {ex['owner']}, got {got}")
    return problems


def prove_examples(store: dict, spec: dict, keys: list, resolver) -> list[str]:
    problems = []
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



"""Tests for the validation hardening added after Step 6: SwapGroup/binary-
chain validators (incl. cycle detection), write-path validation-before-save,
and the AmbiguousLeaf guard for a team occupying multiple claims on one pick.

    venv/bin/python -m picks_conveyance.tests.test_validation_hardening
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import model, ownership, registry  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def raises(exc_type, fn):
    try:
        fn()
        return False
    except exc_type:
        return True


def main():
    # 1. swap group validator
    check("valid swap group passes",
          not raises(model.ConveyanceError,
                    lambda: model.validate_swap_group(
                        {"members": [{"year": 2099, "round": 1, "orig": "BOS"},
                                    {"year": 2099, "round": 1, "orig": "CHI"}],
                         "priority": ["BOS", "CHI"]})))
    check("swap group with empty members rejected",
          raises(model.ConveyanceError,
                lambda: model.validate_swap_group({"members": [], "priority": ["BOS"]})))
    check("swap group with bad member ref rejected",
          raises(model.ConveyanceError,
                lambda: model.validate_swap_group(
                    {"members": [{"not": "a pick ref"}], "priority": ["BOS"]})))

    # 1b. slot-binding sentinel (spec §7.5, nested-protected-in-swap-slot):
    # valid on a NESTED node, rejected on a top-level one.
    nested_slot_node = {
        "type": "protected", "id": "n",
        "on": {"year": 2099, "round": 1, "orig": "X"},
        "bands": [{"min": 1, "max": 3, "to": "MIN"},
                 {"min": 4, "max": 60, "to": {
                     "type": "protected", "id": "inner", "on": model.SLOT_BINDING_SENTINEL,
                     "bands": [{"min": 1, "max": 3, "to": "MIN"},
                              {"min": 4, "max": 60, "to": "DET"}],
                 }}],
    }
    check("nested slot-binding sentinel accepted",
          not raises(model.ConveyanceError, lambda: model.validate(nested_slot_node)))
    top_level_slot_node = {"type": "protected", "id": "n",
                           "on": model.SLOT_BINDING_SENTINEL,
                           "bands": [{"min": 1, "max": 60, "to": "MIN"}]}
    check("top-level slot-binding sentinel rejected (only valid nested)",
          raises(model.ConveyanceError, lambda: model.validate(top_level_slot_node)))

    # 2. binary chain validator + cycle detection
    good_chain = [
        {"type": "binary_swap", "id": "s1",
         "a": {"year": 2099, "round": 1, "orig": "A"},
         "b": {"year": 2099, "round": 1, "orig": "B"},
         "better_to": {"ref": "s2", "as": "a"}, "worse_to": "B"},
        {"type": "binary_swap", "id": "s2",
         "a": {"ref": "s1", "output": "better"},
         "b": {"year": 2099, "round": 1, "orig": "C"},
         "better_to": "A", "worse_to": "C"},
    ]
    check("acyclic chain passes", not raises(model.ConveyanceError,
                                             lambda: model.validate_binary_chain(good_chain)))

    cyclic_chain = [
        {"type": "binary_swap", "id": "x1",
         "a": {"ref": "x2", "output": "better"},
         "b": {"year": 2099, "round": 1, "orig": "B"},
         "better_to": "A", "worse_to": "B"},
        {"type": "binary_swap", "id": "x2",
         "a": {"ref": "x1", "output": "better"},   # x2 depends on x1, x1 depends on x2
         "b": {"year": 2099, "round": 1, "orig": "C"},
         "better_to": "A", "worse_to": "C"},
    ]
    check("cyclic chain rejected", raises(model.ConveyanceError,
                                          lambda: model.validate_binary_chain(cyclic_chain)))

    unknown_ref_chain = [
        {"type": "binary_swap", "id": "y1",
         "a": {"ref": "does_not_exist", "output": "better"},
         "b": {"year": 2099, "round": 1, "orig": "B"},
         "better_to": "A", "worse_to": "B"},
    ]
    check("chain referencing unknown node id rejected",
          raises(model.ConveyanceError, lambda: model.validate_binary_chain(unknown_ref_chain)))

    # 3. write path validates before saving
    orig_registry = registry.REGISTRY_FILE
    registry.REGISTRY_FILE = Path("/tmp/picks_conveyance_hardening_test.json")
    registry.REGISTRY_FILE.unlink(missing_ok=True)
    registry.save_registry(registry._empty())

    try:
        check("add_protected rejects a bad band range",
              raises(model.ConveyanceError,
                    lambda: registry.add_protected(
                        (2099, 1, "X"), on={"year": 2099, "round": 1, "orig": "X"},
                        bands=[{"min": 10, "max": 1, "to": "X"}])))
        check("bad add_protected did not write anything",
              "2099|1|X" not in registry.load_registry()["protected"])

        check("add_swap_group rejects empty priority",
              raises(model.ConveyanceError,
                    lambda: registry.add_swap_group(
                        "sg_bad", members=[{"year": 2099, "round": 1, "orig": "X"}],
                        priority=[])))
        check("bad add_swap_group did not write anything",
              "sg_bad" not in registry.load_registry()["swap_groups"])

        # 4. AmbiguousLeaf: team occupies 2 distinct bands
        registry.save_registry(registry._empty())
        reg = registry.load_registry()
        reg["protected"]["2099|1|Y"] = {
            "on": {"year": 2099, "round": 1, "orig": "Y"},
            "bands": [{"min": 1, "max": 3, "to": "BOS"},
                     {"min": 4, "max": 30, "to": "CHI"},
                     {"min": 31, "max": 60, "to": "BOS"}],   # BOS appears twice
        }
        registry.save_registry(reg)
        # Mirrors the real resolved-store shape (apply_registry embeds bands
        # into the node directly) -- ownership.list_leaves reads them from
        # here, while handle_retrade reads/writes the registry independently.
        node = {"type": "protected", "id": "x", **reg["protected"]["2099|1|Y"]}
        pick = {"year": 2099, "round": 1, "orig": "Y", "conveyance": node}
        leaves = ownership.team_leaves(pick, "BOS", {})
        check("team_leaves finds 2 BOS bands", len(leaves) == 2)
        check("handle_retrade with no leaf_id raises AmbiguousLeaf rather than guessing",
              raises(registry.AmbiguousLeaf,
                    lambda: registry.handle_retrade((2099, 1, "Y"), "BOS", "DEN", node)))
        check("ambiguous retrade left the registry untouched",
              registry.load_registry()["protected"]["2099|1|Y"]["bands"] ==
              reg["protected"]["2099|1|Y"]["bands"])

        # leaf_id resolves the same ambiguity precisely
        target_leaf = leaves[1]["leaf_id"]   # the second (31-60) BOS band
        changed = registry.handle_retrade((2099, 1, "Y"), "BOS", "DEN", node,
                                          leaf_id=target_leaf)
        check("leaf_id-precise retrade succeeds", changed is True)
        new_bands = registry.load_registry()["protected"]["2099|1|Y"]["bands"]
        check("only the targeted band changed",
              new_bands[0]["to"] == "BOS" and new_bands[1]["to"] == "CHI"
              and new_bands[2]["to"] == "DEN")

        # 5. NESTED leaf: a band's 'to' is itself a protected node (spec:
        # "a leaf can be a team or another node") -- both the read side
        # (list_leaves) and the write side (handle_retrade w/ leaf_id) must
        # reach into it correctly, at real depth, not just detect it exists.
        registry.save_registry(registry._empty())
        nested_spec = {
            "on": {"year": 2098, "round": 1, "orig": "N"},
            "bands": [
                {"min": 1, "max": 10, "to": "ATL"},
                {"min": 11, "max": 60, "to": {
                    "type": "protected",
                    "on": {"year": 2098, "round": 1, "orig": "N"},
                    "bands": [{"min": 11, "max": 30, "to": "BOS"},
                             {"min": 31, "max": 60, "to": "CHI"}],
                }},
            ],
        }
        reg = registry.load_registry()
        reg["protected"]["2098|1|N"] = nested_spec
        registry.save_registry(reg)
        nested_node = {"type": "protected", "id": "n", **nested_spec}
        nested_pick = {"year": 2098, "round": 1, "orig": "N", "conveyance": nested_node}

        nested_leaves = ownership.list_leaves(nested_pick, {})
        check("list_leaves finds all 3 leaves incl. the nested ones",
              sorted(l["team"] for l in nested_leaves) == ["ATL", "BOS", "CHI"])
        bos_leaf_id = next(l["leaf_id"] for l in nested_leaves if l["team"] == "BOS")
        check("nested leaf_id has the extra index segment",
              bos_leaf_id == "2098-1-N:protected:1:0")

        changed = registry.handle_retrade((2098, 1, "N"), "BOS", "DEN", nested_node,
                                          leaf_id=bos_leaf_id)
        check("nested leaf_id mutation succeeds", changed is True)
        after_nested = registry.load_registry()["protected"]["2098|1|N"]["bands"][1]["to"]
        check("only the nested BOS leaf changed, CHI's sibling leaf untouched",
              after_nested["bands"][0]["to"] == "DEN"
              and after_nested["bands"][1]["to"] == "CHI")
        check("top-level ATL band is untouched by the nested mutation",
              registry.load_registry()["protected"]["2098|1|N"]["bands"][0]["to"] == "ATL")

    finally:
        registry.REGISTRY_FILE = orig_registry

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("VALIDATION HARDENING TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Conveyance node types + validation.

Nodes are plain JSON-serializable dicts tagged by ``type`` (storage is JSON, per
the spec). This module provides constructors and a validator rather than classes,
so the in-memory form and the on-disk form are identical.

Node types (docs/picks-conveyance.md §2.2, §2.4a):
    settled       {type, id, team}
    extinguished  {type}                          -- obligation dies, no compensation
    protected     {type, id, on, bands:[{min,max,to}]}
    swap          {type, id, group}               -- pointer into a SwapGroup
    binary_swap   {type, id, a, b, better_to, worse_to}

A *leaf* (a band ``to``, a ``better_to``/``worse_to`` recipient, a SwapGroup
``priority`` slot) is either a team abbr (str) or a nested node (dict). A pick
ref is {"year","round","orig"}. A node-output ref is {"ref": id, "output":
"better"|"worse"} (operand) or {"ref": id, "as": "a"|"b"} (recipient).
"""
from __future__ import annotations

NODE_TYPES = {"settled", "extinguished", "protected", "swap", "binary_swap",
              "binary", "legacy"}


class ConveyanceError(ValueError):
    """Raised when a node fails structural validation."""


# --- constructors ----------------------------------------------------------

def settled(team: str, id: str | None = None) -> dict:
    n = {"type": "settled", "team": team}
    if id:
        n["id"] = id
    return n


def extinguished() -> dict:
    return {"type": "extinguished"}


def protected(id: str, on: dict, bands: list[dict]) -> dict:
    return {"type": "protected", "id": id, "on": on, "bands": bands}


def swap(id: str, group: str) -> dict:
    return {"type": "swap", "id": id, "group": group}


def binary_swap(id: str, a: dict, b: dict, better_to, worse_to) -> dict:
    return {"type": "binary_swap", "id": id, "a": a, "b": b,
            "better_to": better_to, "worse_to": worse_to}


# --- refs ------------------------------------------------------------------

# A nested `protected` node's `on` binds to whichever pick landed in the
# enclosing leaf's slot, resolved at draft time by resolver._resolve_leaf_for_pick
# — it isn't a fixed pick ref, so it can't be validated as one. Sentinel from
# docs/picks-conveyance.md §7.5. No real data uses this yet, but the resolver
# has supported it since Phase 0; model.validate() needs to recognize it too
# so a legitimately-constructed nested node isn't rejected as malformed.
SLOT_BINDING_SENTINEL = "__slot1__"


def is_pick_ref(x) -> bool:
    return isinstance(x, dict) and "orig" in x and "year" in x and "round" in x


def is_output_ref(x) -> bool:
    return isinstance(x, dict) and x.get("ref") is not None and "output" in x


def is_input_ref(x) -> bool:
    return isinstance(x, dict) and x.get("ref") is not None and "as" in x


def is_node(x) -> bool:
    return isinstance(x, dict) and x.get("type") in NODE_TYPES


# --- validation ------------------------------------------------------------

def validate(node: dict, *, _depth: int = 0, _max_depth: int = 32) -> None:
    """Structurally validate a conveyance node (recursively). Raises
    ConveyanceError on the first problem. Depth guard mirrors the spec's
    cycle/termination guard (§4)."""
    if _depth > _max_depth:
        raise ConveyanceError(f"node nesting exceeds max depth {_max_depth}")
    if not isinstance(node, dict):
        raise ConveyanceError(f"node must be a dict, got {type(node).__name__}")
    t = node.get("type")
    if t not in NODE_TYPES:
        raise ConveyanceError(f"unknown node type {t!r}")

    if t == "settled":
        if not isinstance(node.get("team"), str) or not node["team"]:
            raise ConveyanceError("settled node requires a non-empty 'team'")

    elif t == "extinguished":
        pass

    elif t == "protected":
        on = node.get("on")
        is_slot_binding = (on == SLOT_BINDING_SENTINEL and _depth > 0)
        if not is_pick_ref(on) and not is_slot_binding:
            raise ConveyanceError(
                "protected 'on' must be a pick ref"
                + ("" if _depth > 0 else f" (or {SLOT_BINDING_SENTINEL!r}, but only "
                                         "on a nested node, not the top-level one)"))
        bands = node.get("bands")
        if not isinstance(bands, list) or not bands:
            raise ConveyanceError("protected requires a non-empty 'bands' list")
        _validate_bands(bands)
        for band in bands:
            _validate_leaf(band["to"], _depth=_depth, _max_depth=_max_depth)

    elif t == "swap":
        if not isinstance(node.get("group"), str) or not node["group"]:
            raise ConveyanceError("swap node requires a 'group' id")

    elif t == "binary":
        # membership marker: this pick's owner is decided by a binary-swap chain
        # (resolve_all reads store.binary_swaps directly; this is for display/inventory)
        if not isinstance(node.get("chain"), str) or not node["chain"]:
            raise ConveyanceError("binary marker requires a 'chain' id")

    elif t == "legacy":
        # unmodelable historical conveyance; resolver skips, prose kept in the pick
        if not isinstance(node.get("reason"), str):
            raise ConveyanceError("legacy node requires a 'reason' string")

    elif t == "binary_swap":
        for operand in ("a", "b"):
            v = node.get(operand)
            if not (is_pick_ref(v) or is_output_ref(v)):
                raise ConveyanceError(
                    f"binary_swap '{operand}' must be a pick ref or output ref")
        for slot in ("better_to", "worse_to"):
            v = node.get(slot)
            if v is None:
                raise ConveyanceError(f"binary_swap requires '{slot}'")
            # recipient: team str, input ref, or nested node
            if not (isinstance(v, str) or is_input_ref(v) or is_node(v)):
                raise ConveyanceError(
                    f"binary_swap '{slot}' must be a team, input ref, or node")
            if is_node(v):
                validate(v, _depth=_depth + 1, _max_depth=_max_depth)


def _validate_bands(bands: list[dict]) -> None:
    """Bands must be sorted, contiguous, non-overlapping, each {min,max,to}."""
    last_max = None
    for band in bands:
        if not all(k in band for k in ("min", "max", "to")):
            raise ConveyanceError("each band needs min, max, to")
        lo, hi = band["min"], band["max"]
        if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi:
            raise ConveyanceError(f"bad band range {lo}-{hi}")
        if last_max is not None and lo != last_max + 1:
            raise ConveyanceError(
                f"bands must be contiguous with no gaps/overlaps; "
                f"got ...{last_max} then {lo}")
        last_max = hi


def _validate_leaf(leaf, *, _depth: int, _max_depth: int) -> None:
    if isinstance(leaf, str):
        return
    if is_node(leaf):
        validate(leaf, _depth=_depth + 1, _max_depth=_max_depth)
        return
    raise ConveyanceError(f"leaf must be a team str or a node, got {leaf!r}")


# --- SwapGroup / binary-chain validation ------------------------------------
# These are container structures, not single conveyance nodes (§2.4), so they
# get their own validators here rather than being folded into validate().

def validate_swap_group(group: dict) -> None:
    members = group.get("members")
    if not isinstance(members, list) or not members:
        raise ConveyanceError("swap group requires a non-empty 'members' list")
    for m in members:
        if not is_pick_ref(m):
            raise ConveyanceError(f"swap group member must be a pick ref, got {m!r}")
    priority = group.get("priority")
    if not isinstance(priority, list) or not priority:
        raise ConveyanceError("swap group requires a non-empty 'priority' list")
    for slot in priority:
        if not (isinstance(slot, str) or is_node(slot)):
            raise ConveyanceError(f"priority slot must be a team or a node, got {slot!r}")
        if is_node(slot):
            validate(slot)


def validate_ladder(ladder: dict) -> None:
    """A ladder isn't a single conveyance node (it's a container, like a swap
    group), so it gets its own validator rather than being folded into
    validate(). Shape: {id, from, to, steps:[{year,round,orig?,protect_top}],
    fallback: {type:"fixed_asset", picks:[pick_ref,...]} | None}."""
    if not isinstance(ladder.get("id"), str) or not ladder["id"]:
        raise ConveyanceError("ladder requires a non-empty 'id'")
    if not isinstance(ladder.get("from"), str) or not ladder["from"]:
        raise ConveyanceError("ladder requires a non-empty 'from'")
    if not isinstance(ladder.get("to"), str) or not ladder["to"]:
        raise ConveyanceError("ladder requires a non-empty 'to'")
    steps = ladder.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConveyanceError("ladder requires a non-empty 'steps' list")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("year"), int):
            raise ConveyanceError("ladder step requires an int 'year'")
        if step.get("round") not in (1, 2):
            raise ConveyanceError("ladder step 'round' must be 1 or 2")
        orig = step.get("orig")
        if orig is not None and not isinstance(orig, str):
            raise ConveyanceError("ladder step 'orig', if given, must be a string")
        top = step.get("protect_top")
        if not isinstance(top, int) or top < 0 or top > 60:
            raise ConveyanceError("ladder step requires an int 'protect_top' in 0-60")
    fallback = ladder.get("fallback")
    if fallback is not None:
        if fallback.get("type") != "fixed_asset":
            raise ConveyanceError("ladder fallback 'type' must be 'fixed_asset'")
        picks = fallback.get("picks")
        if not isinstance(picks, list) or not picks:
            raise ConveyanceError("ladder fallback requires a non-empty 'picks' list")
        for pref in picks:
            if not is_pick_ref(pref):
                raise ConveyanceError(f"ladder fallback pick must be a pick ref, got {pref!r}")


def validate_binary_chain(nodes: list[dict]) -> None:
    """Validates each binary_swap node, then checks the ref-graph among them
    for cycles (a cycle would make the resolver's dataflow evaluation recurse
    forever — see resolver._resolve_binary_chains). Raises on the first
    problem found; a clean chain is left untouched."""
    if not isinstance(nodes, list) or not nodes:
        raise ConveyanceError("binary chain requires a non-empty node list")
    by_id = {}
    for n in nodes:
        validate(n)
        if n.get("type") != "binary_swap":
            raise ConveyanceError(f"binary chain member must be type binary_swap, got {n.get('type')!r}")
        by_id[n["id"]] = n

    def refs_of(n):
        out = []
        for operand in (n.get("a"), n.get("b")):
            if is_output_ref(operand):
                out.append(operand["ref"])
        return out

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in by_id}

    def visit(nid, path):
        if nid not in by_id:
            raise ConveyanceError(f"binary chain references unknown node id {nid!r}")
        if color[nid] == GRAY:
            raise ConveyanceError(
                f"cycle detected in binary chain: {' -> '.join(path + [nid])}")
        if color[nid] == BLACK:
            return
        color[nid] = GRAY
        for ref_id in refs_of(by_id[nid]):
            visit(ref_id, path + [nid])
        color[nid] = BLACK

    for nid in by_id:
        if color[nid] == WHITE:
            visit(nid, [])

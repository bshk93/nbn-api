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
        if not is_pick_ref(on):
            raise ConveyanceError("protected 'on' must be a pick ref")
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

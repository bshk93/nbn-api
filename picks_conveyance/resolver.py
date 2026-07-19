"""Draft-time resolver.

Given a store (picks + swap groups + binary-swap chains) and the actual draft
positions for a year, compute the concrete owner of every contingent pick.
Implements the two-pass resolver + binary-swap chains from the spec (§3, §2.4a).

    resolve_all(store, positions) -> { pickkey: owner_team }

`positions` maps a pickkey to that pick's actual 1-indexed draft slot for the
year (1..60). `pickkey` is (year:int, round:int, orig:str); use `key()` to build
one from a pick ref or a pick row (round "1"/"2" or "1st"/"2nd" both accepted).

This is the forward engine used at draft time. The flat-API compatibility view
(picks table today, before any draft) does NOT use it — see projection.py.
"""
from __future__ import annotations

from . import model


def key(ref: dict | tuple) -> tuple:
    """Normalize a pick ref / pick row / tuple to (year:int, round:int, orig)."""
    if isinstance(ref, tuple):
        y, r, o = ref
    else:
        y = ref.get("year", ref.get("YEAR"))
        r = ref.get("round", ref.get("ROUND"))
        o = ref.get("orig", ref.get("ORIG"))
    return (int(y), _round_int(r), o)


def _round_int(r) -> int:
    if isinstance(r, int):
        return r
    r = str(r).strip().lower()
    return 1 if r in ("1", "1st") else 2 if r in ("2", "2nd") else int(r)


class ResolutionError(RuntimeError):
    pass


def resolve_all(store: dict, positions: dict) -> dict:
    """Return {pickkey: owner_team} for every pick whose owner is determinable
    from `positions`. Picks whose inputs aren't all known are omitted (caller
    treats a missing key as 'not yet resolved')."""
    swap_groups = store.get("swap_groups", {})
    binary_swaps = store.get("binary_swaps", {})
    out: dict = {}

    # Pass 1: settled + protected (self-contained per pick)
    for pick in store.get("picks", []):
        node = pick.get("conveyance")
        pk = key(pick)
        if not node:
            continue
        t = node["type"]
        if t == "settled":
            out[pk] = node["team"]
        elif t == "protected":
            owner = _resolve_protected(node, positions)
            if owner is not None:
                out[pk] = owner
        # swap / binary_swap handled in later passes

    # Pass 2: swap groups
    for gid, group in swap_groups.items():
        assigned = _resolve_swap_group(group, positions)
        if assigned:
            out.update(assigned)

    # Pass 3: binary-swap chains (topological by operand dependency)
    out.update(_resolve_binary_chains(binary_swaps, positions))

    # Pass 4: protection ladders (multi-year chained protections)
    out.update(_resolve_ladders(store.get("ladders", []), positions))

    return out


# --- protected -------------------------------------------------------------

def _resolve_protected(node: dict, positions: dict):
    pos = positions.get(key(node["on"]))
    if pos is None:
        return None
    for band in node["bands"]:
        if band["min"] <= pos <= band["max"]:
            return _resolve_leaf(band["to"], pos, positions)
    raise ResolutionError(f"position {pos} outside all bands of {node.get('id')}")


def _resolve_leaf(leaf, pos: int, positions: dict):
    if isinstance(leaf, str):
        return leaf
    if model.is_node(leaf):
        t = leaf["type"]
        if t == "settled":
            return leaf["team"]
        if t == "extinguished":
            return None            # obligation dies; keeper retained upstream
        if t == "protected":
            return _resolve_protected(leaf, positions)
    raise ResolutionError(f"cannot resolve leaf {leaf!r}")


# --- swap groups -----------------------------------------------------------

def _resolve_swap_group(group: dict, positions: dict):
    members = [key(m) for m in group["members"]]
    if any(m not in positions for m in members):
        return None                      # need every member's position
    ordered = sorted(members, key=lambda m: positions[m])   # best (lowest) first
    priority = group["priority"]
    result = {}
    for i, m in enumerate(ordered):
        slot = priority[i] if i < len(priority) else None
        if slot is None:
            continue
        if isinstance(slot, str):
            result[m] = slot
        elif model.is_node(slot):
            # nested node keyed to the pick that landed in this slot (§7.5)
            result[m] = _resolve_leaf_for_pick(slot, m, positions)
        else:
            raise ResolutionError(f"bad priority slot {slot!r}")
    return result


def _resolve_leaf_for_pick(node: dict, pick_key: tuple, positions: dict):
    """Resolve a node whose `on` is the pick occupying this swap slot
    (the __slot1__ binding from the spec)."""
    if node.get("on") == "__slot1__":
        node = {**node, "on": {"year": pick_key[0], "round": pick_key[1],
                               "orig": pick_key[2]}}
    return _resolve_leaf(node, positions.get(pick_key), positions)


# --- binary-swap chains ----------------------------------------------------

def _resolve_binary_chains(binary_swaps: dict, positions: dict) -> dict:
    """Evaluate binary_swap nodes in dependency order. Operands (a/b) drive the
    dataflow (pull); better_to/worse_to route outputs to terminal team owners.
    Returns {pickkey: team} for outputs whose recipient is a team."""
    memo: dict = {}          # swap id -> {"better": pickkey, "worse": pickkey}
    owners: dict = {}

    def operand_key(op):
        if model.is_pick_ref(op):
            return key(op)
        if model.is_output_ref(op):
            sid = op["ref"]
            if sid not in memo:
                _eval(sid)
            m = memo.get(sid)
            return m[op["output"]] if m else None
        raise ResolutionError(f"bad operand {op!r}")

    def _eval(sid):
        if sid in memo:
            return
        s = binary_swaps[sid]
        ka, kb = operand_key(s["a"]), operand_key(s["b"])
        if ka is None or kb is None or ka not in positions or kb not in positions:
            memo[sid] = None
            return
        better, worse = (ka, kb) if positions[ka] <= positions[kb] else (kb, ka)
        memo[sid] = {"better": better, "worse": worse}
        for out_name, recipient in (("better", s["better_to"]),
                                    ("worse", s["worse_to"])):
            if isinstance(recipient, str):
                owners[memo[sid][out_name]] = recipient
            # input-ref / node recipients are consumed downstream

    for sid in binary_swaps:
        _eval(sid)
    return owners


# --- protection ladders ----------------------------------------------------

def _resolve_ladders(ladders: list, positions: dict) -> dict:
    """Walk each ladder's steps in year order. A step conveys (owner = `to`) when
    the origin's position is beyond `protect_top` (or the step is unprotected,
    protect_top == 0); otherwise it stays with `from` and the next year's step is
    tried. If every step stays protected and a `fixed_asset` fallback exists, the
    fallback picks convey to `to`."""
    out: dict = {}
    for L in ladders:
        conveyed = False
        evaluated_all = True
        for step in L["steps"]:
            k = (step["year"], _round_int(step["round"]), L["from"])
            pos = positions.get(k)
            if pos is None:
                evaluated_all = False
                break                      # can't look past an unknown year
            top = step["protect_top"]
            if top == 0 or pos > top:
                out[k] = L["to"]           # conveys
                conveyed = True
                break
            out[k] = L["from"]             # protected this year; roll forward
        if not conveyed and evaluated_all:
            fb = L.get("fallback")
            if fb and fb.get("type") == "fixed_asset":
                for pref in fb["picks"]:
                    out[key(pref)] = L["to"]
    return out

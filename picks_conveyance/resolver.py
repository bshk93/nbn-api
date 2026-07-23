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
    pick_nodes: dict = {}

    # Pass 1: settled + protected (self-contained per pick)
    for pick in store.get("picks", []):
        node = pick.get("conveyance")
        pk = key(pick)
        if not node:
            continue
        pick_nodes[pk] = node
        t = node["type"]
        if t == "settled":
            out[pk] = node["team"]
        elif t == "protected":
            owner = _resolve_protected(node, positions)
            if owner is not None:
                out[pk] = owner
        # swap / binary_swap handled in later passes

    # Pass 2: binary-swap chains (topological by operand dependency) -- run
    # BEFORE swap groups, since a ranked swap group's member can be a dynamic
    # reference to a chain's output (see _resolve_swap_group), not just a
    # fixed pick; the chain must already be resolved for that lookup to work.
    chain_owners, chain_memo = _resolve_binary_chains(binary_swaps, positions)
    out.update(chain_owners)

    # Pass 3: swap groups (may consume chain_memo for dynamic members)
    for gid, group in swap_groups.items():
        assigned = _resolve_swap_group(group, positions, chain_memo)
        if assigned:
            out.update(assigned)

    # Pass 4: protection ladders (multi-year chained protections)
    out.update(_resolve_ladders(store.get("ladders", []), positions, pick_nodes))

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

def _resolve_swap_group(group: dict, positions: dict, chain_memo: dict | None = None):
    """Resolve a swap group's members, ranked by actual draft position.

    A member is either a fixed pick ref, or an output ref
    (`{"ref": sid, "output": "better"|"worse"}`) pointing at an earlier
    binary_swap's result — `chain_memo` (from `_resolve_binary_chains`) is
    where that lookup happens, so this can rank a *dynamic* input (e.g.
    "whichever pick this team ended up with from an earlier swap")
    alongside plain fixed picks. Returns None (deferred) if any member's
    underlying pick isn't yet resolvable — either its position is unknown,
    or (for an output-ref member) the chain it depends on hasn't resolved.

    `priority` may be shorter than `members`: the best-ranked member goes to
    `priority[0]`, next to `priority[1]`, and so on; any member ranked
    beyond the end of `priority` has no named claimant and simply stays
    with whoever originated it (the general "nobody said anything about
    3rd place, so it's obviously unaffected" case). An explicit `None`
    *within* `priority`'s range is different — a deliberate no-claim for
    that specific rank — and still resolves to no assignment, same as
    always.
    """
    chain_memo = chain_memo or {}
    members = []
    for m in group["members"]:
        if model.is_pick_ref(m):
            members.append(key(m))
        elif model.is_output_ref(m):
            sid = m["ref"]
            resolved = chain_memo.get(sid)
            if resolved is None:
                return None               # dependency not yet resolved
            members.append(resolved[m["output"]])
        else:
            raise ResolutionError(f"bad swap group member {m!r}")
    if any(m not in positions for m in members):
        return None                      # need every member's position
    ordered = sorted(members, key=lambda m: positions[m])   # best (lowest) first
    priority = group["priority"]
    result = {}
    for i, m in enumerate(ordered):
        if i >= len(priority):
            result[m] = m[2]             # no named claimant -> stays with orig
            continue
        slot = priority[i]
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

def _resolve_binary_chains(binary_swaps: dict, positions: dict) -> tuple[dict, dict]:
    """Evaluate binary_swap nodes in dependency order. Operands (a/b) drive the
    dataflow (pull); better_to/worse_to route outputs to terminal team owners.
    Returns (owners, memo): `owners` is {pickkey: team} for outputs whose
    recipient is a team; `memo` is {sid: {"better": pickkey, "worse": pickkey}
    | None} for every evaluated node, exposed so a ranked swap group
    (_resolve_swap_group) can use a chain's output as one of its own dynamic
    members, instead of only fixed picks."""
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
    return owners, memo


# --- protection ladders ----------------------------------------------------

def _resolve_ladders(ladders: list, positions: dict, pick_nodes: dict | None = None) -> dict:
    """Walk each ladder's steps in year order. A step conveys (owner = `to`) when
    the origin's position is beyond `protect_top` (or the step is unprotected,
    protect_top == 0); otherwise it stays with `from` and the next year's step is
    tried. If every step stays protected and a `fixed_asset` fallback exists, the
    fallback picks convey to `to`.

    A step's own pick key defaults its `orig` to `L["from"]` (the common case:
    a team's own future pick rolling forward) but a step may set an explicit
    `orig` when the ladder governs someone else's share of a pick that
    originated from a third team (e.g. team A holds the unprotected share of
    team C's pick, and owes team B a fallback if it stays protected).

    `pick_nodes` (pickkey -> conveyance node, from pass 1) lets a step defer to
    a genuine `protected`/`swap`/`binary` node already governing that exact
    pick — e.g. a third party's independent claim layered on via a separate
    trade — instead of blindly overwriting it. The ladder still uses the real
    `positions` lookup to decide whether its own fallback fires; it just
    doesn't clobber a more specific existing answer for the step's own key.

    This same `authoritative` deferral is applied to the FALLBACK assignment
    below too (docs/picks-conveyance-hardening.md item B) — it wasn't
    originally, which let a ladder's fallback silently overwrite a fallback
    pick's own independently-resolved `protected`/`swap`/`binary` outcome
    with no error at all. Unlike the step case, there's no sensible "defer"
    behavior for a fallback collision (two different trades made two
    different real promises about the same physical pick), so this raises
    `ResolutionError` instead of picking a winner. A `ResolutionError` is
    also raised (via `_assign`) if two ladders disagree about the very same
    step or fallback key within this same call — the resolve-time backstop
    for docs/picks-conveyance-hardening.md items C/D, independent of
    whatever write-time guard `add_ladder` may or may not have."""
    pick_nodes = pick_nodes or {}
    out: dict = {}

    def _assign(k, team, ladder_id, what):
        existing = out.get(k)
        if existing is not None and existing != team:
            raise ResolutionError(
                f"ladder {ladder_id!r} {what} {k}: would resolve to {team!r}, but "
                f"{k} was already resolved to {existing!r} by another ladder in "
                f"this same resolution -- two ladders disagree about the same "
                f"pick and need manual reconciliation")
        out[k] = team

    for L in ladders:
        lid = L.get("id", "?")
        conveyed = False
        evaluated_all = True
        for step in L["steps"]:
            k = (step["year"], _round_int(step["round"]), step.get("orig") or L["from"])
            pos = positions.get(k)
            if pos is None:
                evaluated_all = False
                break                      # can't look past an unknown year
            top = step["protect_top"]
            authoritative = pick_nodes.get(k, {}).get("type") in ("protected", "swap", "binary")
            if top == 0 or pos > top:
                if not authoritative:
                    _assign(k, L["to"], lid, "step")     # conveys
                conveyed = True
                break
            if not authoritative:
                _assign(k, L["from"], lid, "step")       # protected this year; roll forward
        if not conveyed and evaluated_all:
            fb = L.get("fallback")
            if fb and fb.get("type") == "fixed_asset":
                for pref in fb["picks"]:
                    fbk = key(pref)
                    fb_node_type = pick_nodes.get(fbk, {}).get("type")
                    if fb_node_type in ("protected", "swap", "binary"):
                        raise ResolutionError(
                            f"ladder {lid!r} fallback pick {fbk}: already has its own "
                            f"{fb_node_type!r} conveyance from an unrelated trade -- "
                            f"can't convey it to {L['to']!r} as fallback compensation "
                            f"without manual resolution")
                    _assign(fbk, L["to"], lid, "fallback")
    return out

"""Tree-based ownership check, now with real leaf-node-id addressing.

`team_holds_claim(pick, team, store)` answers the coarse question: does `team`
have *some* legitimate standing on this pick's conveyance tree — settled
owner, a protected band, a swap-group candidate, a binary-chain possible
recipient? This is the spec §4 "appears as a leaf somewhere" check, and is
what trade validation uses by default.

`list_leaves(pick, store)` / `team_leaves(pick, team, store)` expose the
*specific* leaves, each with a stable, deterministic `leaf_id` — no separate
ID storage needed, they're derived from the pick's structure. When a team
occupies more than one leaf, a trade can pass `leaf_id` to say exactly which
one it's conveying (`team_holds_leaf`), instead of the coarse check refusing
outright (see AmbiguousLeaf in registry.py, still the fallback when no
leaf_id is given and the coarse check would be ambiguous).

leaf_id format: "{year}-{round}-{orig}:{kind}:{position}[:...]" —
    protected:  position = band index (0-based)
    swap:       position = priority-list index (0-based)
    binary:     position = "{chain_node_id}:{better_to|worse_to}"
A leaf may itself be a nested node (spec: "a leaf can be a team or another
conveyance node") rather than a plain team string — `_expand_leaf` recurses
into those and extends the leaf_id with the nested position, so every
addressable team, however deep, still gets a stable id.
Deterministic and human-readable; recomputed from structure each time, not
stored, so it can never drift out of sync with the structure it describes.
"""
from __future__ import annotations


def _pick_prefix(pick: dict) -> str:
    return f"{pick['year']}-{pick['round']}-{pick['orig']}"


def _expand_leaf(value, leaf_id: str, description: str) -> list[dict]:
    """A leaf value is a team string (base case) or a nested conveyance node
    (settled/protected/extinguished — the only types the spec allows to
    nest). Recurses into nested nodes so every reachable team gets its own
    addressable leaf_id, extending the parent id rather than replacing it."""
    if isinstance(value, str):
        return [{"leaf_id": leaf_id, "team": value, "description": description}]
    if isinstance(value, dict):
        vt = value.get("type")
        if vt == "settled":
            return [{"leaf_id": leaf_id, "team": value["team"],
                     "description": f"{description} (settled -> {value['team']})"}]
        if vt == "protected":
            out = []
            for i, band in enumerate(value.get("bands", [])):
                out.extend(_expand_leaf(
                    band.get("to"), f"{leaf_id}:{i}",
                    f"{description} nested band {band['min']}-{band['max']}"))
            return out
        # extinguished, or anything else: no claimant, no leaf
    return []


def list_leaves(pick: dict, store: dict) -> list[dict]:
    """Every leaf position in this pick's structure, regardless of team:
    [{leaf_id, team, description}]. Settled/extinguished/legacy have no
    addressable sub-leaves (nothing to disambiguate) and return []."""
    node = pick["conveyance"]
    t = node.get("type")
    prefix = _pick_prefix(pick)
    out = []

    if t == "protected":
        for i, band in enumerate(node["bands"]):
            leaves = _expand_leaf(
                band.get("to"), f"{prefix}:protected:{i}",
                f"protected band {band['min']}-{band['max']}")
            for leaf in leaves:
                leaf["txn_ids"] = band.get("txn_ids", [])
            out.extend(leaves)

    elif t == "swap":
        group = (store or {}).get("swap_groups", {}).get(node["group"], {})
        for i, slot in enumerate(group.get("priority", [])):
            label = "better" if i == 0 else "worse" if i == 1 else f"slot {i}"
            leaves = _expand_leaf(
                slot, f"{prefix}:swap:{i}", f"swap priority ({label} pick)")
            for leaf in leaves:
                leaf["txn_ids"] = group.get("txn_ids", [])
            out.extend(leaves)

    elif t == "binary":
        sids = (store or {}).get("chains", {}).get(node["chain"], [])
        bs = (store or {}).get("binary_swaps", {})
        for sid in sids:
            s = bs.get(sid, {})
            for slot in ("better_to", "worse_to"):
                leaves = _expand_leaf(
                    s.get(slot), f"{prefix}:binary:{sid}:{slot}",
                    f"binary chain node {sid!r} {slot}")
                for leaf in leaves:
                    leaf["txn_ids"] = s.get("txn_ids", [])
                out.extend(leaves)

    return out


def team_leaves(pick: dict, team: str, store: dict) -> list[dict]:
    """The subset of list_leaves this team occupies."""
    return [leaf for leaf in list_leaves(pick, store) if leaf["team"] == team]


def team_holds_leaf(pick: dict, team: str, leaf_id: str, store: dict) -> bool:
    """Does `team` occupy this SPECIFIC leaf? Used when a trade supplies
    leaf_id to disambiguate a team holding more than one claim."""
    return any(leaf["leaf_id"] == leaf_id and leaf["team"] == team
              for leaf in list_leaves(pick, store))


def team_holds_claim(pick: dict, team: str, store: dict) -> bool:
    """The coarse check: does `team` have ANY claim on this pick (any leaf, a
    settled owner, or a preserved legacy owner)? This is what trade validation
    uses by default — a trade only needs leaf_id when this alone would be
    ambiguous (see registry.count_leaf_occurrences / AmbiguousLeaf)."""
    node = pick["conveyance"]
    t = node.get("type")

    if t == "settled":
        return node["team"] == team
    if t == "extinguished":
        return False
    if t == "legacy":
        return node.get("owner") == team
    if t in ("protected", "swap", "binary"):
        return any(leaf["team"] == team for leaf in list_leaves(pick, store))
    return False

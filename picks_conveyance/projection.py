"""Compatibility projection.

Folds a conveyance pick back into the flat shape the current `/api/picks`
endpoint returns (`roster_picks.pick_to_response`). This is the migration
de-risker (spec §6): the new JSON store can ship *behind* the existing read API,
because every consumer keeps seeing the same rows.

The picks table renders the *nominal* current holder — the display owner before
any draft resolves the contingency — not a draft-position outcome. So the
projection uses nominal display, never the resolver. The resolver is only for
draft time, when actual positions exist.

`project_to_flat(pick, store)` needs the store for swap/binary nodes (to read
the shared swap-group / chain tables). Settled and protected nodes are
self-contained.
"""
from __future__ import annotations

from . import model


class ProjectionError(ValueError):
    pass


def _distinct(seq):
    return list(dict.fromkeys(seq))


def _protected_flat(node: dict, orig: str) -> dict:
    """Map a protected node to the flat (owner, protected) pair when it is a
    genuine keeper-is-origin protection (the only shape the flat model can
    express); otherwise show pipe-joined candidate teams."""
    bands = sorted(node["bands"], key=lambda b: b["min"])
    tail = max(bands, key=lambda b: b["max"])
    protected_bands = [b for b in bands if b is not tail]
    keeper_is_orig = (protected_bands
                      and all(b.get("to") == orig for b in protected_bands)
                      and isinstance(tail.get("to"), str))
    if keeper_is_orig:
        return {"owner": tail["to"],
                "protected": max(b["max"] for b in protected_bands)}
    teams = _distinct(b["to"] for b in bands if isinstance(b.get("to"), str))
    return {"owner": "|".join(teams), "protected": None}


def _swap_candidates(node: dict, store: dict) -> list[str]:
    group = (store or {}).get("swap_groups", {}).get(node["group"], {})
    return _distinct(t for t in group.get("priority", []) if isinstance(t, str))


def _chain_candidates(node: dict, store: dict) -> list[str]:
    """Teams that can end up owning a pick decided by this binary chain."""
    sids = (store or {}).get("chains", {}).get(node["chain"], [])
    bs = (store or {}).get("binary_swaps", {})
    teams = []
    for sid in sids:
        s = bs.get(sid, {})
        for slot in ("better_to", "worse_to"):
            v = s.get(slot)
            if isinstance(v, str):
                teams.append(v)
    return _distinct(teams)


def nominal_owner(pick: dict, store: dict | None = None) -> str:
    """Display owner for the flat picks view (single team or pipe-joined
    candidates)."""
    node = pick["conveyance"]
    t = node.get("type")
    if t == "settled":
        return node["team"]
    if t == "protected":
        return _protected_flat(node, pick["orig"])["owner"]
    if t == "swap":
        return "|".join(_swap_candidates(node, store)) or pick["orig"]
    if t == "binary":
        return "|".join(_chain_candidates(node, store)) or pick["orig"]
    if t == "legacy":
        return node.get("owner") or pick["orig"]
    if t == "extinguished":
        return pick["orig"]
    raise ProjectionError(f"nominal_owner: unhandled node type {t!r}")


def project_to_flat(pick: dict, store: dict | None = None) -> dict:
    """Return the `pick_to_response`-shaped dict for one conveyance pick.

    Reproduces the flat contract for every node type. `swap_conveys` is left
    None here (the live endpoint fills it via enrich_swap_conveys); `conveys` is
    derived when a numeric protection threshold is present."""
    node = pick["conveyance"]
    t = node.get("type")
    pick_num = pick.get("pick")

    owner = nominal_owner(pick, store)
    protected = None
    swap_owner = None
    if t == "protected":
        protected = _protected_flat(node, pick["orig"])["protected"]
    elif t == "settled" and pick.get("needs_structure") and pick.get("_flat"):
        # not-yet-modeled flat-structured row: pass its structured fields through
        # so the preview stays a faithful superset of /api/picks (no regression)
        protected = pick["_flat"].get("protected")
        swap_owner = pick["_flat"].get("swap_owner")
    conveys = (pick_num > protected) if (pick_num is not None and protected is not None) else None

    return {
        "year":          int(pick["year"]),
        "round":         int(pick["round"]),
        "orig":          pick["orig"],
        "owner":         owner,
        "pick":          pick_num,
        "player":        (pick.get("player") or "").strip() or None
                         if pick.get("player") is not None else None,
        "protected":     protected,
        "conveys":       conveys,
        "swap_owner":    swap_owner,
        "swap_conveys":  None,
        "notes":         pick.get("notes", "") or "",
        "frozen":        bool(pick.get("frozen", False)),
        "frozen_reason": pick.get("frozen_reason", "") or "",
    }

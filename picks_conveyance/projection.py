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
    priority = group.get("priority", [])
    named = _distinct(t for t in priority if isinstance(t, str))
    members = group.get("members", [])
    if len(members) > len(priority):
        # rank(s) beyond the named priority default to staying with whoever
        # originated them (see model.validate_swap_group) — since which
        # specific member ends up at that rank isn't knowable pre-draft,
        # include every team that could possibly land there rather than
        # silently omitting a real possibility.
        fallback = set()
        for m in members:
            fallback |= model.possible_origs(m, store)
        named = _distinct(named + sorted(fallback))
    return named


def _swap_owner_flat(node: dict, pick: dict, store: dict):
    """For a 2-member swap group, the flat `swap_owner` field: the OTHER
    member's orig team. Restores enrich_swap_conveys' live-draft real-time
    resolution (routers/roster_picks.py) for the common 2-team case — it looks
    up the counterpart's *current* pick number by this team abbreviation.
    N-member groups (3+) have no single counterpart, so left unset (None);
    those are only reached via a `binary` chain node in this model, not `swap`."""
    group = (store or {}).get("swap_groups", {}).get(node["group"], {})
    members = group.get("members", [])
    if len(members) != 2:
        return None
    other = next((m for m in members
                 if not (m["year"] == pick["year"] and m["round"] == pick["round"]
                        and m["orig"] == pick["orig"])), None)
    return other["orig"] if other else None


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


def _group_id(node: dict) -> str | None:
    """Stable id shared by every pick that's part of the same swap group or
    binary chain — i.e. every pick whose owner is jointly decided by the same
    multi-pick contingency. None for node types that decide only their own
    pick (settled/protected/legacy/extinguished). Lets a consumer collapse
    the N picks a chain/group spans into a single displayed entry per team,
    instead of showing what looks like N separate picks when the team is
    actually only ever going to end up with one of them."""
    t = node.get("type")
    if t == "swap":
        return f"swap:{node['group']}"
    if t == "binary":
        return f"chain:{node['chain']}"
    return None


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
    elif t == "swap":
        swap_owner = _swap_owner_flat(node, pick, store)
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
        # Additive field, not part of the original flat contract: every
        # addressable leaf on this pick with its stable leaf_id, for a trade
        # to reference via TradeAsset.leaf_id when a team holds more than one
        # (routers/transactions.py CUTOVER STEP 9). Empty for settled/legacy/
        # extinguished picks, which have nothing to disambiguate.
        "leaves":        _leaves_field(pick, store),
        # Additive, like `leaves`: non-null only for swap/binary nodes, shared
        # across every pick belonging to the same group/chain. See _group_id.
        "group_id":      _group_id(node),
        # Additive: non-null only when a ladder step governs this exact pick —
        # surfaces the protect_top threshold and fixed-asset fallback that
        # nothing else in the flat contract shows (a ladder is a container in
        # `store["ladders"]`, not part of this pick's own `conveyance` node,
        # so without this a ladder-governed pick displays no different from
        # an ordinary settled one — see the sas_tor/sas_was/mem_bkn gap).
        "ladder":        _ladder_field(pick, store),
        # Additive: non-null only for `legacy` nodes — a real historical deal
        # too tangled to model structurally (docs/picks-conveyance.md §5),
        # frozen from re-trade until manually converted. Without this, a
        # legacy pick's `owner`/`protected`/`swap_owner` look exactly like an
        # ordinary settled pick (single team, no leaves, nothing flagged) even
        # though the truth is a multi-team cascade living only in `notes`
        # prose — e.g. OKC's 2027 1st shows `owner: "OKC"` with nothing to
        # indicate PHX/LAC are actually the real parties per its own notes.
        # `reason` is the short internal label (curated.py's LEGACY dict);
        # the full prose is still `notes`, unchanged.
        "legacy":        {"reason": node.get("reason")} if t == "legacy" else None,
    }


def _leaves_field(pick: dict, store: dict | None) -> list[dict]:
    if pick["conveyance"].get("type") not in ("protected", "swap", "binary"):
        return []
    from . import ownership
    return ownership.list_leaves(pick, store or {})


def _ladder_field(pick: dict, store: dict | None) -> dict | None:
    """First ladder step matching this pick's own (year, round, orig) — a
    pick is governed by at most one ladder step in practice. Returns
    {from, to, protect_top, fallback: [pick_ref, ...]} or None."""
    for ladder in (store or {}).get("ladders", []):
        for step in ladder.get("steps", []):
            step_orig = step.get("orig") or ladder.get("from")
            if (step["year"] == pick["year"] and step["round"] == pick["round"]
                    and step_orig == pick["orig"]):
                fb = ladder.get("fallback")
                return {
                    "from": ladder["from"],
                    "to": ladder["to"],
                    "protect_top": step["protect_top"],
                    "fallback": (fb or {}).get("picks", []) if fb else [],
                    "txn_ids": ladder.get("txn_ids", []),
                }
    return None

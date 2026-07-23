"""Persistent, growable registry of curated conveyance structures.

Replaces the static `curated.py` snapshot as the runtime source for `resync`.
`curated.py`'s dicts remain as the **one-time seed** (the 41-pick reconciliation
from 2026-07-19) — `seed_registry_from_curated()` writes them into this
persistent JSON file once. From then on, the write path (transactions.py's
`_apply_trade`, roster_picks.py's `upsert_pick`) appends new entries here
whenever a trade creates a protection or swap, so future contingent trades get
real structural modeling automatically instead of falling back to flat
passthrough. `curated.py`'s dicts are not read again after the initial seed.

Storage: `NBS_DATA_DIR/draft-conveyance-registry.json`, same shape as the
`PROTECTED` / `SWAP_GROUPS` / `BINARY_CHAINS` / `LADDERS` / `LEGACY` dicts in
curated.py, but with tuple pick-keys as JSON-safe "year|round|orig" strings.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .seed_store import DATA_DIR

REGISTRY_FILE = DATA_DIR / "draft-conveyance-registry.json"
_lock = threading.Lock()


class RetradeBlocked(Exception):
    """Raised when a re-trade targets a legacy (frozen-from-re-trade) pick."""


class SwapConflict(Exception):
    """Raised when a new swap-group registration would collide with a pick
    that's already a member of an existing swap group —
    docs/picks-conveyance-hardening.md item E. Before this check existed,
    `from_trade.register_swap` had no analog of `register_protection`'s
    existing-structure check (`get_protected_spec`); a second `swap_with` on
    an already-swap-structured pick would silently overwrite the group
    (fresh priority list, prior txn_ids discarded) under the same
    deterministically-derived group id."""


class LadderConflict(Exception):
    """Raised when a ladder's steps or fallback picks collide with another
    ladder's steps or fallback picks — docs/picks-conveyance-hardening.md
    items C/D. Before this check existed, nothing stopped two ladders from
    governing (or naming as fallback compensation) the same
    (year, round, orig); the resolver would silently pick whichever ladder
    happened to be last in the registry's list, with no error."""


class AmbiguousLeaf(Exception):
    """Raised when `handle_retrade` is called with no `leaf_id` and the team
    occupies more than one distinct leaf position within a pick's structure,
    so a coarse (pick-key + team) re-trade can't safely tell which one is
    being conveyed. Real leaf-node-id addressing exists (`leaf_id`, see
    `_mutate_leaf_path` / ownership.list_leaves) and is what
    `_check_retrade_allowed` (routers/transactions.py) actually uses to
    resolve this case in practice — this exception is the fallback for
    calling `handle_retrade` directly without one, not the normal path a real
    trade takes. No real pick currently triggers it (verified 2026-07-19)."""


def _kstr(k: tuple) -> str:
    return f"{k[0]}|{k[1]}|{k[2]}"


def _kparse(s: str) -> tuple:
    y, r, o = s.split("|")
    return (int(y), int(r), o)


def _empty() -> dict:
    return {"protected": {}, "swap_groups": {}, "binary_chains": {},
            "chain_members": {}, "ladders": [], "legacy": {}}


def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return _empty()
    return json.loads(REGISTRY_FILE.read_text())


def save_registry(reg: dict) -> None:
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2))


def seed_registry_from_curated(force: bool = False) -> dict:
    """One-time migration: write curated.py's dicts into the persistent
    registry. No-op if the registry already exists, unless force=True."""
    if REGISTRY_FILE.exists() and not force:
        return load_registry()
    from . import curated
    reg = _empty()
    for k, spec in curated.PROTECTED.items():
        reg["protected"][_kstr(k)] = {
            "on": spec["on"],
            "bands": [{"min": lo, "max": hi, "to": to} for lo, hi, to in spec["bands"]],
        }
    reg["swap_groups"] = dict(curated.SWAP_GROUPS)
    for cid, nodes in curated.BINARY_CHAINS.items():
        reg["binary_chains"][cid] = nodes
    for cid, members in curated.CHAIN_MEMBERS.items():
        reg["chain_members"][cid] = [_kstr(k) for k in members]
    reg["ladders"] = list(curated.LADDERS)
    for k, reason in curated.LEGACY.items():
        reg["legacy"][_kstr(k)] = reason
    _validate_registry(reg)
    with _lock:
        save_registry(reg)
    return reg


def _validate_registry(reg: dict) -> None:
    """Run the same structural checks the write path applies to new entries
    against the whole registry — including the seed data — so a malformed
    curated entry is caught here rather than only surfacing later as a
    confusing resolver/display bug."""
    from . import model
    for ks, spec in reg["protected"].items():
        model.validate({"type": "protected", "id": "tmp", **spec})
    for gid, group in reg["swap_groups"].items():
        model.validate_swap_group(group)
    for cid, nodes in reg["binary_chains"].items():
        model.validate_binary_chain(nodes)
    for ladder in reg.get("ladders", []):
        model.validate_ladder(ladder)
    _check_ladder_collisions(reg.get("ladders", []))


def _ladder_keys(ladder: dict) -> set[tuple]:
    """Every (year, round, orig) this ladder governs or names as fallback
    compensation — the full set of picks a NEW ladder must not collide with.
    Mirrors the key computation resolver._resolve_ladders uses for both."""
    keys = set()
    for step in ladder.get("steps", []):
        keys.add((step["year"], step["round"], step.get("orig") or ladder["from"]))
    fb = ladder.get("fallback") or {}
    for pref in fb.get("picks", []):
        keys.add((pref["year"], pref["round"], pref["orig"]))
    return keys


def _check_ladder_collisions(ladders: list[dict], new: dict | None = None) -> None:
    """Raise LadderConflict if `new` (when given) collides with any ladder in
    `ladders`, or — when `new` is omitted — if any two ladders in `ladders`
    collide with each other (the whole-registry sweep `_validate_registry`
    runs). A collision is any shared (year, round, orig) between either
    ladder's steps or fallback picks: a pick can only be one ladder's step or
    one ladder's fallback target at a time."""
    if new is not None:
        new_keys = _ladder_keys(new)
        for existing in ladders:
            shared = new_keys & _ladder_keys(existing)
            if shared:
                raise LadderConflict(
                    f"new ladder {new.get('id', '?')!r} collides with existing "
                    f"ladder {existing.get('id', '?')!r} on {sorted(shared)} — a "
                    f"pick can only be governed by one ladder's step or named as "
                    f"one ladder's fallback at a time; needs manual reconciliation")
        return
    for i, a in enumerate(ladders):
        a_keys = _ladder_keys(a)
        for b in ladders[i + 1:]:
            shared = a_keys & _ladder_keys(b)
            if shared:
                raise LadderConflict(
                    f"ladder {a.get('id', '?')!r} collides with ladder "
                    f"{b.get('id', '?')!r} on {sorted(shared)} — a pick can only "
                    f"be governed by one ladder's step or named as one ladder's "
                    f"fallback at a time; needs manual reconciliation")


def add_protected(pick_key: tuple, on: dict, bands: list[dict],
                  txn_entry: dict | None = None) -> None:
    """Register a new protected-pick structure (called by the write path when
    a trade sets a protection threshold). Validated before it ever reaches
    disk — a malformed band structure fails loudly here instead of quietly
    corrupting the store on the next resync. `txn_entry` ({"id", "date"}), if
    given, is stamped onto every band — the whole node is being created by
    this one trade, so every band it produces traces back to it (mirrors how
    binary chains already carry `txn_ids` — see ownership.list_leaves)."""
    from . import model
    if txn_entry:
        bands = [{**b, "txn_ids": [txn_entry]} for b in bands]
    model.validate({"type": "protected", "id": "tmp", "on": on, "bands": bands})
    with _lock:
        reg = load_registry()
        reg["protected"][_kstr(pick_key)] = {"on": on, "bands": bands}
        save_registry(reg)


def get_protected_spec(pick_key: tuple) -> dict | None:
    """Read-only lookup of the raw {on, bands} spec currently registered for
    a pick, or None if it has no protected structure yet. Lets a caller (e.g.
    from_trade.register_protection) tell a brand-new protection apart from
    one that needs to subdivide an existing band."""
    reg = load_registry()
    return reg["protected"].get(_kstr(pick_key))


def subdivide_protected_band(pick_key: tuple, from_team: str, to_team: str,
                             threshold: int, txn_entry: dict | None = None) -> None:
    """A pick already has real protected structure (e.g. an earlier trade's
    band split gave a third team a claim on part of it) and this trade adds a
    NEW protection threshold on top of whichever single band `from_team`
    currently occupies. Splits that one band into [band.min, threshold]
    (stays with from_team) and [threshold+1, band.max] (conveys to to_team),
    leaving every other band untouched — the N-way generalization
    `add_protected`'s blind overwrite couldn't do: a pick can accumulate band
    splits across multiple trades instead of the newest trade erasing
    whatever an earlier trade's counterparty was owed. `txn_entry`
    ({"id", "date"}), if given, is stamped onto the two NEW bands this split
    produces — the sibling band(s) this trade didn't touch keep whatever
    txn_ids (if any) they already had.

    Raises ValueError if from_team doesn't occupy exactly one band (ambiguous
    or absent — same posture as handle_retrade's ambiguity check) or if
    threshold falls outside that band's range."""
    from . import model
    with _lock:
        reg = load_registry()
        spec = reg["protected"].get(_kstr(pick_key))
        if spec is None:
            raise ValueError(f"{pick_key} has no existing protected structure to subdivide")
        matches = [b for b in spec["bands"] if b.get("to") == from_team]
        if len(matches) != 1:
            raise ValueError(
                f"{pick_key}: {from_team} occupies {len(matches)} band(s) — "
                f"can't unambiguously subdivide")
        band = matches[0]
        if not (band["min"] <= threshold < band["max"]):
            raise ValueError(
                f"{pick_key}: threshold {threshold} outside {from_team}'s "
                f"band {band['min']}-{band['max']}")
        new_bands = []
        for b in spec["bands"]:
            if b is band:
                keep = {"min": b["min"], "max": threshold, "to": from_team}
                convey = {"min": threshold + 1, "max": b["max"], "to": to_team}
                if txn_entry:
                    keep["txn_ids"] = [txn_entry]
                    convey["txn_ids"] = [txn_entry]
                new_bands.append(keep)
                new_bands.append(convey)
            else:
                new_bands.append(b)
        model.validate({"type": "protected", "id": "tmp", "on": spec["on"], "bands": new_bands})
        spec["bands"] = new_bands
        save_registry(reg)


def add_ladder(ladder: dict) -> None:
    """Register a new protection ladder (called by the write path when a
    trade sets a ladder-style protect_top + fixed-asset fallback). Validated
    before it reaches disk, same posture as add_protected/add_swap_group.

    Also checked against every existing ladder for a step/fallback collision
    (docs/picks-conveyance-hardening.md items C/D) — raises LadderConflict
    rather than silently letting two ladders both claim the same pick, which
    the resolver could previously only catch by accident (whichever ladder
    happened to be last in the list quietly won)."""
    from . import model
    model.validate_ladder(ladder)
    with _lock:
        reg = load_registry()
        _check_ladder_collisions(reg["ladders"], new=ladder)
        reg["ladders"].append(ladder)
        save_registry(reg)


def find_swap_group_for(pick_key: tuple) -> str | None:
    """The group id of the swap group `pick_key` is a plain (fixed pick-ref)
    member of, or None if it isn't in any. Read-only lookup, mirrors
    `get_protected_spec`'s role for protection — lets a caller
    (`from_trade.register_swap`) tell a brand-new swap apart from one that
    would collide with an already-established group, the same distinction
    `register_protection` already makes via `get_protected_spec`. Only
    matches fixed pick-ref members (a dynamic output-ref member has no
    identity of its own to compare against a plain pick_key)."""
    reg = load_registry()
    for gid, group in reg.get("swap_groups", {}).items():
        for m in group.get("members", []):
            if (isinstance(m, dict) and "orig" in m
                    and (m.get("year"), m.get("round"), m.get("orig")) == pick_key):
                return gid
    return None


def add_swap_group(group_id: str, members: list[dict], priority: list,
                   txn_entry: dict | None = None) -> None:
    """Register a new 2-team swap group (called by the write path when a
    trade sets a swap partner). Validated before it reaches disk. `txn_entry`
    ({"id", "date"}), if given, is stamped at the group level (mirrors how
    binary chains already carry `txn_ids` — see ownership.list_leaves)."""
    from . import model
    group = {"members": members, "priority": priority}
    if txn_entry:
        group["txn_ids"] = [txn_entry]
    model.validate_swap_group(group)
    with _lock:
        reg = load_registry()
        reg["swap_groups"][group_id] = group
        save_registry(reg)


def _parse_leaf_id(leaf_id: str) -> dict:
    """Parse a leaf_id (see ownership.py's docstring for the format) into its
    structural parts. Raises ValueError on anything malformed — a bad
    leaf_id should fail loudly, not silently mutate the wrong thing."""
    parts = leaf_id.split(":")
    if len(parts) < 3:
        raise ValueError(f"malformed leaf_id: {leaf_id!r}")
    prefix, kind = parts[0], parts[1]
    try:
        year_s, round_s, orig = prefix.split("-", 2)
        pick_key = (int(year_s), int(round_s), orig)
    except ValueError:
        raise ValueError(f"malformed leaf_id prefix: {leaf_id!r}")
    try:
        if kind == "protected":
            return {"pick_key": pick_key, "kind": "protected",
                    "index_path": [int(p) for p in parts[2:]]}
        if kind == "swap":
            return {"pick_key": pick_key, "kind": "swap",
                    "index_path": [int(p) for p in parts[2:]]}
    except ValueError:
        raise ValueError(f"malformed leaf_id index path: {leaf_id!r}")
    if kind == "binary":
        # binary_swap recipients are a team or a {ref} to another chain node
        # (already a full addressing mechanism) — never a nested node the way
        # protected/swap leaves can be, so there's no deeper path to parse.
        if len(parts) != 4:
            raise ValueError(f"malformed binary leaf_id: {leaf_id!r}")
        return {"pick_key": pick_key, "kind": "binary",
                "node_id": parts[2], "slot": parts[3]}
    raise ValueError(f"unknown leaf kind in leaf_id: {leaf_id!r}")


def _terminal_team(value):
    """A band/priority-slot value's team when it's a leaf (not a further-
    nested protected container): a plain string, or a nested `settled` node
    (which the read side collapses to the same leaf_id as a plain string —
    see ownership._expand_leaf — so it's handled identically here)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("type") == "settled":
        return value.get("team")
    return None


def _mutate_leaf_path(container: list, index_path: list, from_team: str,
                      to_team: str, txn_entry: dict | None = None) -> None:
    """Recursively mutate the leaf at `index_path` within `container` (a
    protected `bands` list or a swap `priority` list — both are plain lists
    where element i's leaf value lives at `container[i]` for priority, or
    `container[i]["to"]` for bands; both are handled via `_leaf_slot` below).
    Verifies the current occupant matches `from_team` before mutating —
    mismatch raises ValueError rather than mutating the wrong thing.

    `txn_entry` ({"id", "date"}), if given, is APPENDED (not replaced) to the
    terminal band's `txn_ids` when that band is a dict (a protected band) —
    the leaf's provenance is a chain (created by trade A, re-traded by trade
    B, ...), not a single fact, and the tooltip already joins multiple linked
    transactions. A swap priority slot is a bare team string, not a dict, so
    it has nowhere to carry a per-slot stamp — the caller stamps the swap
    GROUP itself instead (see handle_retrade)."""
    i, *rest = index_path
    get, set_ = _leaf_slot(container, i)
    value = get()
    if not rest:
        cur_team = _terminal_team(value)
        if cur_team != from_team:
            raise ValueError(f"leaf does not currently belong to "
                             f"{from_team!r} (found {value!r})")
        set_(to_team)
        if txn_entry:
            item = container[i]
            if isinstance(item, dict):
                item.setdefault("txn_ids", []).append(txn_entry)
        return
    if not (isinstance(value, dict) and value.get("type") == "protected"):
        raise ValueError("leaf_id path continues past a leaf that isn't a "
                         "nested protected node")
    _mutate_leaf_path(value["bands"], rest, from_team, to_team, txn_entry=txn_entry)


def _leaf_slot(container: list, i: int):
    """container[i] is either a band dict ({'to': ...}) or a bare leaf value
    (a swap priority slot) — return (getter, setter) for whichever it is."""
    item = container[i]
    if isinstance(item, dict) and "to" in item:
        return (lambda: item["to"]), (lambda v: item.__setitem__("to", v))
    return (lambda: container[i]), (lambda v: container.__setitem__(i, v))


def handle_retrade(pick_key: tuple, from_team: str, to_team: str, node: dict,
                   leaf_id: str | None = None, txn_id: str | None = None,
                   txn_date: str | None = None) -> bool:
    """A pick with an EXISTING conveyance structure is being re-traded — this
    trade supplied no new protection/swap_with, so `from_team` is conveying
    whatever claim it holds *within* that existing structure to `to_team`.
    Updates the registry in place so the structure reflects the new party,
    rather than going stale the way the flat model always did (see the
    2028 BOS 2nd / DAL-vs-MIA finding in the migration worksheet — this is
    the ongoing version of that same bug, not a one-off).

    Leaf-precise: pass `leaf_id` (from ownership.list_leaves/team_leaves) to
    say exactly which claim is being conveyed — required when `from_team`
    occupies more than one leaf, since without it there's no safe way to
    tell which one a plain re-trade means. Without `leaf_id`, falls back to
    auto-detect: fine when there's exactly one occurrence (the common case),
    raises AmbiguousLeaf rather than guessing when there's more than one.
    Nested leaves (a band/slot whose value is itself a `protected` node —
    the spec's "a leaf can be a team or another node" rule) are fully
    mutable via leaf_id, at any depth — `_mutate_leaf_path` recurses to
    whatever level the leaf_id addresses, mirroring the read side's
    `ownership._expand_leaf`.

    `txn_id`/`txn_date`, when given, get APPENDED to whatever `txn_ids` the
    mutated band/group/chain-node already carries — a re-trade doesn't erase
    the leaf's provenance, it extends it (created by trade A, re-traded by
    trade B, ...), matching the tooltip's existing multi-txn join. Without
    this, a re-traded leaf would keep showing only its original creating
    trade forever, silently wrong the moment it moves again.

    Also propagates to any protection ladder governing this exact pick (see
    `_retrade_ladders`) — a ladder lives outside the pick's own conveyance
    node, so the node-type dispatch below never touches it on its own; a
    `settled` pick with no other structure can still change here if a
    ladder points at it.

    Returns True if the registry was changed, False if there was nothing to
    update (a plain `settled` pick with no governing ladder, or a leaf_id
    that matched nothing). Raises RetradeBlocked for `legacy` (frozen from
    re-trade by design). Raises AmbiguousLeaf per the fallback case above,
    or ValueError for a malformed/mismatched leaf_id. Re-validates the
    mutated structure (incl. the binary-chain cycle check) before
    persisting."""
    from . import model

    t = node.get("type")
    if t == "legacy":
        raise RetradeBlocked(
            f"{pick_key} is a legacy pick (frozen from re-trade until "
            f"manually converted to real structure): {node.get('reason')}")

    txn_entry = {"id": txn_id, "date": txn_date} if txn_id else None

    with _lock:
        reg = load_registry()
        changed = False

        if leaf_id is not None:
            parsed = _parse_leaf_id(leaf_id)
            if parsed["pick_key"] != pick_key:
                raise ValueError(f"leaf_id {leaf_id!r} belongs to pick "
                                 f"{parsed['pick_key']}, not {pick_key}")
            if parsed["kind"] != t:
                raise ValueError(f"leaf_id {leaf_id!r} kind {parsed['kind']!r} "
                                 f"doesn't match this pick's node type {t!r}")

            if t == "protected":
                spec = reg["protected"].get(_kstr(pick_key))
                if spec:
                    _mutate_leaf_path(spec["bands"], parsed["index_path"],
                                      from_team, to_team, txn_entry=txn_entry)
                    changed = True
                    model.validate({"type": "protected", "id": "tmp", **spec})

            elif t == "swap":
                group = reg["swap_groups"].get(node["group"])
                if group:
                    _mutate_leaf_path(group["priority"], parsed["index_path"],
                                      from_team, to_team)
                    if txn_entry:
                        group.setdefault("txn_ids", []).append(txn_entry)
                    changed = True
                    model.validate_swap_group(group)

            elif t == "binary":
                chain_nodes = reg["binary_chains"].get(node["chain"], [])
                target = next((n for n in chain_nodes
                              if n.get("id") == parsed["node_id"]), None)
                if target:
                    slot = parsed["slot"]
                    if target.get(slot) != from_team:
                        raise ValueError(
                            f"leaf_id {leaf_id!r}: {slot} is "
                            f"{target.get(slot)!r}, not {from_team!r}")
                    target[slot] = to_team
                    if txn_entry:
                        target.setdefault("txn_ids", []).append(txn_entry)
                    changed = True
                    model.validate_binary_chain(chain_nodes)

        # --- no leaf_id given: auto-detect (fine when unambiguous) ---
        elif t == "protected":
            spec = reg["protected"].get(_kstr(pick_key))
            if spec:
                matches = [b for b in spec["bands"] if b.get("to") == from_team]
                if len(matches) > 1:
                    raise AmbiguousLeaf(
                        f"{pick_key}: {from_team} occupies {len(matches)} "
                        f"bands — can't tell which one this trade conveys "
                        f"without a leaf_id")
                if matches:
                    matches[0]["to"] = to_team
                    if txn_entry:
                        matches[0].setdefault("txn_ids", []).append(txn_entry)
                    changed = True
                    model.validate({"type": "protected", "id": "tmp", **spec})

        elif t == "swap":
            group = reg["swap_groups"].get(node["group"])
            if group:
                count = group["priority"].count(from_team)
                if count > 1:
                    raise AmbiguousLeaf(
                        f"{pick_key}: {from_team} occupies {count} priority "
                        f"slots in swap group {node['group']!r} — needs a leaf_id")
                if count == 1:
                    idx = group["priority"].index(from_team)
                    group["priority"][idx] = to_team
                    if txn_entry:
                        group.setdefault("txn_ids", []).append(txn_entry)
                    changed = True
                    model.validate_swap_group(group)

        elif t == "binary":
            chain_nodes = reg["binary_chains"].get(node["chain"], [])
            occurrences = [(n, slot) for n in chain_nodes
                          for slot in ("better_to", "worse_to")
                          if n.get(slot) == from_team]
            if len(occurrences) > 1:
                raise AmbiguousLeaf(
                    f"{pick_key}: {from_team} occupies {len(occurrences)} "
                    f"slots in binary chain {node['chain']!r} — needs a leaf_id")
            if occurrences:
                n, slot = occurrences[0]
                n[slot] = to_team
                if txn_entry:
                    n.setdefault("txn_ids", []).append(txn_entry)
                changed = True
                model.validate_binary_chain(chain_nodes)

        if _retrade_ladders(reg, pick_key, from_team, to_team, txn_entry):
            changed = True

        if changed:
            save_registry(reg)
        return changed


def _retrade_ladders(reg: dict, pick_key: tuple, from_team: str, to_team: str,
                     txn_entry: dict | None) -> bool:
    """Propagate a re-trade to any protection ladder governing this exact
    pick. `add_ladder`/`from_trade.register_ladder` store a ladder as a
    container OUTSIDE the pick's own `conveyance` node — the governed pick
    itself is typically left a plain `settled` node — so the node-type
    dispatch in `handle_retrade` above never sees it. Without this, a
    re-trade of a ladder-governed pick to a new team left the ladder's
    `from` pointing at the team that no longer holds it, which isn't just a
    stale display field: `resolver._resolve_ladders` credits `from` as the
    keeper whenever the pick stays protected, so a stale `from` would
    resolve the pick to the WRONG team at real draft time (the exact class
    of bug — an intermediate leg of a chain silently lost — this whole
    conveyance model exists to fix for every other node type).

    Matches by step identity (year, round, orig — the same tuple
    `resolver._resolve_ladders` keys off via `step.get("orig") or
    ladder["from"]`), not node type, so this fires whether the governed
    pick is a plain settled pick (the common case — `register_ladder` is
    only ever called alongside a plain, un-contingent pick trade) or one
    that separately also carries its own protected/swap/binary structure
    from an unrelated trade. Only rewrites `from`: a ladder's `to` (the
    fallback beneficiary) isn't reachable as a leaf on this pick at all
    today (see `ownership.list_leaves` — no `ladder` case), so there's
    nothing for a retrade of THIS pick to convey on that side.

    A ladder with multiple steps (a manually-curated multi-year chain, not
    the single-step shape `register_ladder` writes) still shares one
    `from`/`to` for the whole ladder by design (spec §2.5) — matching any
    one step updates the whole ladder, consistent with that.

    Pins the matched step's `orig` explicitly the first time it's touched.
    Every curated seed ladder (the 5 pre-existing ones as of 2026-07-23:
    sas_was, mem_bkn, sas_tor, ...) was written with `step["orig"]` omitted,
    relying on the `step.get("orig") or ladder["from"]` fallback — safe only
    because `from` had never been mutated before this function existed. The
    instant a retrade changes `from` below, that same fallback would start
    computing a DIFFERENT (year, round, orig) key for every subsequent call
    — silently detaching the ladder from the real pick it governs, and
    breaking `resolver._resolve_ladders`'s identical fallback the same way
    at real draft time. Verified against a sandbox copy of the live
    registry: a second retrade of the same real pick was silently dropped
    (`changed=False`) before this pin was added. `register_ladder`-created
    ladders already set `orig` explicitly and are unaffected."""
    changed = False
    for ladder in reg.get("ladders", []):
        if ladder.get("from") != from_team:
            continue
        for step in ladder.get("steps", []):
            step_key = (step["year"], step["round"], step.get("orig") or ladder["from"])
            if step_key == pick_key:
                if not step.get("orig"):
                    step["orig"] = pick_key[2]
                ladder["from"] = to_team
                if txn_entry:
                    ladder.setdefault("txn_ids", []).append(txn_entry)
                changed = True
                break
    return changed


def apply_registry(store: dict) -> dict:
    """Merge the persistent registry into a seeded store — the runtime
    replacement for curated.apply_curated. Same override semantics (skips
    already-drafted picks; see curated.py's set_node docstring)."""
    from . import model
    reg = load_registry()
    store.setdefault("swap_groups", {})
    store.setdefault("binary_swaps", {})
    store.setdefault("chains", {})
    store.setdefault("ladders", [])
    by_key = {(p["year"], p["round"], p["orig"]): p for p in store["picks"]}

    def set_node(k, node):
        p = by_key.get(k)
        if p is None:
            return   # registry references a pick outside the current horizon; skip
        if p.get("player"):
            return   # already drafted: trust the settled(OWNER) the base seed set
        p["conveyance"] = node
        p.pop("needs_structure", None)
        p.pop("_flat", None)

    for ks, spec in reg["protected"].items():
        k = _kparse(ks)
        set_node(k, {"type": "protected", "id": f"p_{ks.replace('|','_')}",
                     "on": spec["on"], "bands": spec["bands"]})

    for gid, g in reg["swap_groups"].items():
        store["swap_groups"][gid] = g
        for m in g["members"]:
            if not model.is_pick_ref(m):
                continue   # dynamic member (output-ref into a binary_swap
                          # chain) -- its underlying pick's base conveyance
                          # is tagged "binary" by whichever chain it
                          # actually belongs to (see the binary_chains loop
                          # below), not "swap" by this group
            set_node((m["year"], m["round"], m["orig"]),
                     {"type": "swap", "id": f"s_{gid}", "group": gid})

    for cid, nodes in reg["binary_chains"].items():
        store["chains"][cid] = [n["id"] for n in nodes]
        for n in nodes:
            store["binary_swaps"][n["id"]] = n
        for ks in reg["chain_members"].get(cid, []):
            set_node(_kparse(ks), {"type": "binary", "chain": cid})

    store["ladders"].extend(reg["ladders"])

    for ks, reason in reg["legacy"].items():
        k = _kparse(ks)
        prev = by_key.get(k, {}).get("conveyance", {})
        set_node(k, {"type": "legacy", "reason": reason, "owner": prev.get("team")})

    store["meta"] = store.get("meta", {})
    store["meta"]["curated"] = {
        "protected": len(reg["protected"]), "swap_groups": len(reg["swap_groups"]),
        "binary_chains": len(reg["binary_chains"]), "ladders": len(reg["ladders"]),
        "legacy": len(reg["legacy"]),
    }
    return store

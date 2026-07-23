# Picks conveyance — overwrite/collision hardening (spec)

**Status: fully built, 2026-07-23** — all items A–F shipped the same day this
spec was written (commits: A `beadddf`-and-earlier, B `1979329`, C/D `ee71add`,
E `c8bfde2`, F `45b85dc`). Companion to `picks-conveyance.md` (the model) and
`picks-migration-worksheet.md` (the data reconciliation). This doc exists
because a targeted review of two specific mechanisms (2026-07-23) found a real
bug, and pulling on that thread found a family of related ones — the sweep
below was written up before touching any more code, per the same
"spec first, then build" convention `picks-conveyance.md` itself followed, then
implemented in the stated order (B, C+D, E, F) once confirmed.

## Root cause, stated once

The conveyance model has several mechanisms that reference a pick from
*outside* that pick's own `conveyance` node — a `ladder`'s governed step, a
`ladder`'s `fallback` targets, a `SwapGroup`'s members. Every one of these is
stored in its own registry container, addressed by `(year, round, orig)` or a
group id, not attached to the pick object itself. **Nothing in the system
checks, at write time or resolve time, whether two of these cross-references
land on the same pick and disagree.** Every finding below is the same root
cause showing up in a different corner: a write path that blindly creates/
overwrites a container, or a resolve path that blindly assigns without
checking for a competing claim already established elsewhere.

This matters because the model's own stated design philosophy
(`picks-conveyance.md` §5) is "structured-or-not-booked... a signal that the
trade itself is ambiguous and must be clarified with the parties before it's
booked." A silent overwrite is the exact failure mode that philosophy exists
to prevent — it's just currently only enforced *within* a single mechanism
(protected bands), not *across* mechanisms.

---

## A. Ladder retrade didn't propagate to `ladder["from"]` — FIXED

Shipped 2026-07-23 (`registry.py` `_retrade_ladders`, commits fixing the
original gap and then a follow-up bug found by sandbox-testing the fix
against the real production registry — curated ladders omit `step["orig"]`
and rely on a `from`-fallback that broke the instant `from` started being
mutated). Regression tests in `test_retrade.py`. Documented here only so this
file is the complete list of what this sweep covers, fixed and open.

## B. Ladder fallback overwrites an independently-resolved pick — FIXED

`resolver._resolve_ladders` (resolver.py:227) has an `authoritative` guard
that stops a ladder from clobbering a pick that already has its own
`protected`/`swap`/`binary` structure from an unrelated trade — but that
guard is only applied to the ladder's **own governed step** (`out[k] = ...`
at lines 260/265). The **fallback assignment** three lines later —
`for pref in fb["picks"]: out[key(pref)] = L["to"]` (line 270) — has no such
guard. It unconditionally overwrites whatever the fallback-target pick's own
conveyance already resolved to.

**Reproduced** (`resolve_all` called directly against a synthetic store): a
pick with its own `protected` node correctly resolves to `ZZZ` from its real
draft position; an unrelated ladder whose fallback names that same pick
overwrites it to `CCC`:
```
Pick X (2030,1,AAA) resolved owner: CCC
  -> its OWN protected node says this should be ZZZ (position 20 is outside top-5)
```

**Live exposure:** checked all 3 real fallback targets in production
(`2029 SAS 2nd`, `2030 SAS 2nd`, `2031 OKC 1st`) — none currently carry their
own independent structure, so this hasn't misfired yet. It fires the instant
a future trade adds protection/a swap onto any pick already named as someone's
ladder fallback.

**Proposed fix:** extend the existing `authoritative` check to the fallback
loop. Unlike the step case (which has a sensible fallback behavior — defer to
the more specific structure), a genuine collision here means two different
trades made two different real-world promises about the same physical pick.
Silently picking one is wrong either way `authoritative` resolves it, so
propose **raising `ResolutionError`** (matching the pattern already used in
`_resolve_protected` for an out-of-range position) rather than silently
deferring — this is a data conflict that needs a human, not a rule.

**Shipped** (`1979329`): the fallback loop now raises `ResolutionError` on
an authoritative collision, and the fix was generalized into a small
`_assign` helper shared by the step and fallback loops, so it also raises
if two ladders disagree about the very same key within one `resolve_all`
call — the resolve-time backstop for C/D below. Verified clean against the
real production registry (all 4 live ladders); regression coverage in
`test_ladders.py`.

## C. Two ladders can govern the same step, silently — FIXED

`add_ladder` (registry.py:187) never checks whether an existing ladder
already has a step at the same `(year, round, orig)`. `_resolve_ladders`
iterates `for L in ladders: ...` writing into one shared `out` dict — if two
ladders disagree about the same pick, whichever is later in list order wins
outright.

**Reproduced:**
```
resolved owner at position 55: QQQ
L_first (protect_top=10) says: 55 > 10 -> conveys to RRR
L_second (protect_top=60) says: 55 <= 60 -> stays QQQ
resolver silently picked whichever ladder is LAST in the list, no error, no warning
```

This was actually noted (but not fixed) during the *first* pass over this
model, before today's session — flagging it again now because it's the same
root cause as B/D, not a new discovery.

**Proposed fix:** `add_ladder` should check every existing ladder's steps for
a `(year, round, orig)` collision with the new ladder's steps and reject
(raise) if found — same posture as `register_protection`'s pre-check via
`get_protected_spec`.

**Shipped** (`ee71add`): `add_ladder` now calls `_check_ladder_collisions`
before persisting, raising `LadderConflict`. Also wired into
`_validate_registry` (which, as a side effect of writing this, was found to
never call `model.validate_ladder` at all — fixed too, so ladders are now
structurally validated on load/seed same as everything else). Regression
coverage in `test_registry.py`.

## D. Two ladders can name the same fallback pick, silently — FIXED

Same mechanism as C, other end of the ladder: nothing checks whether a
fallback pick named by a new ladder is already named by an existing ladder's
fallback (or is already someone's governed step, a narrower case of B).
Whichever ladder resolves last in `resolve_all`'s ladder loop wins.

**Proposed fix:** fold into the same `add_ladder` guard as C — check the new
ladder's fallback picks against every existing ladder's steps *and*
fallback picks, not just steps against steps.

**Shipped** (`ee71add`, same commit as C): `_ladder_keys` collects both a
ladder's step keys and its fallback-pick keys into one set, so
`_check_ladder_collisions` catches both shapes with one check.

## E. `register_swap` has no existing-structure guard — FIXED

`from_trade.register_protection` checks `registry.get_protected_spec(pick_key)`
before deciding whether to create fresh structure or subdivide existing
structure (`from_trade.py:47`). `from_trade.register_swap` has no analogous
check (`from_trade.py:106`) — it computes a deterministic `gid` from the two
picks' `(year, round, orig)` and calls `registry.add_swap_group(gid, ...)`
unconditionally. `add_swap_group` (registry.py:199) does
`reg["swap_groups"][group_id] = group` — a fresh dict literal, wholesale
replacing whatever was there.

Concretely: if a pick already has swap structure (possibly already re-traded
via `handle_retrade`, so its `priority` no longer matches the original two
teams) and a *new* trade sets `swap_with` on it again — legitimate scenario:
renegotiating swap terms — `register_swap` recomputes the same `gid` (it's
derived only from the two picks' identities, not from any existing group
state) and silently discards the current priority list and all prior
`txn_ids`, replacing them with a fresh 2-entry group as if no re-trade had
ever happened.

Also: `_check_retrade_allowed`'s ambiguous-leaf check is explicitly skipped
whenever `asset.swap_with` is set (`transactions.py` — "only relevant when
this trade ISN'T creating new structure"), so nothing at the validation layer
catches this either.

**Proposed fix:** give `register_swap` the same existing-check
`register_protection` has: look up whether either pick is already a member of
a swap group (or carries other structure) before calling `add_swap_group`,
and either merge/extend deliberately or reject with a clear error forcing a
retrade (`handle_retrade`'s swap branch) instead of a fresh registration.

**Shipped** (`c8bfde2`): added `registry.find_swap_group_for` (read-only
lookup, mirrors `get_protected_spec`'s role) and used it in
`register_swap` to raise `SwapConflict` — chose reject-with-clear-error
over merge/extend, since there's no safe generic "subdivide" equivalent for
a swap the way there is for a protected band. No changes needed in
`transactions.py`: `_register_trade_swap` already fails open around this
call (same as the other two registration paths), so the flat trade still
completes and the failure surfaces via logs. Verified against all 15 real
swap groups; regression coverage in `test_from_trade.py`.

## F. `apply_registry`'s `set_node` has no mutual-exclusion check — FIXED

`apply_registry` (registry.py:525) processes `reg["protected"]`, then
`reg["swap_groups"]`, then `reg["binary_chains"]`, then `reg["legacy"]`, in
that fixed order, calling `set_node(k, node)` for each — which does
`p["conveyance"] = node` unconditionally, last writer wins. If a data bug (or
a future write-path bug) ever put the same pick key into two of these dicts
simultaneously, `apply_registry` would silently pick whichever container is
processed later, with zero indication anything was wrong.

This is a narrower, lower-priority case than B–E (it requires the registry to
already be in a bad state, rather than being itself the mechanism that
produces one) but it's cheap insurance and the natural place to catch a
mistake from any of the other fixes above before it reaches the live site.

**Proposed fix:** add a cross-container check to `_validate_registry` (already
the function that validates the whole registry on load/seed): collect every
pick key referenced by `protected`, `swap_groups` members, `binary_chains`
members, and `ladders` steps/fallbacks, and raise if any key appears in more
than one. (Legacy is deliberately exempt — it's the human-override mechanism
and is allowed to supersede.)

**Shipped** (`45b85dc`) — **narrower than proposed above, deliberately**:
ladders turned out to belong on the *exempt* side, not the checked side.
`apply_registry` never calls `set_node` for a ladder's governed pick at all
(`store["ladders"].extend(...)` only) — a ladder step correctly co-exists
with a `protected`/`swap`/`binary` node on the same pick via the resolver's
`authoritative` defer (already tested: `test_ladders.
test_ladder_defers_to_existing_protected_node`), so flagging that overlap
as an error would have broken a real, intentional, already-supported shape.
Verified this doesn't false-positive by constructing a real-shaped
ladder+protected overlap and confirming it validates cleanly. The shipped
check (`_check_structural_exclusivity`) covers only
protected/swap_groups/binary_chains — the three containers `set_node`
actually applies unconditionally with no defer logic between them.
Regression coverage in `test_registry.py` (a real collision, and a clean
non-overlapping control case).

## Not in scope

- **Binary chains** have no `register_binary_chain` write path at all — they're
  only ever created by `curated.py`'s one-time manual seed, never by a live
  trade. None of C/D/E's "two live trades collide" shape applies to them
  today. If a live write path for binary chains is ever added, apply the same
  guard pattern.
- **Concurrency** (multiple in-flight requests racing on `registry.py`'s
  `threading.Lock`) is out of scope — `nbn-api` runs as a single uvicorn
  worker (confirmed via `systemctl cat nbn-api`), so there's no cross-process
  race to guard against, and this file's `_lock` already serializes
  same-process writes.

---

## Implementation order (as built)

1. **B** (resolver fallback guard) first — the resolve-time backstop,
   protecting against existing registry data even without C/D/E.
2. **C + D together** (`add_ladder` collision guard) — same function, same
   check, done at once.
3. **E** (`register_swap` existing-structure guard) — independent of 1–2.
4. **F** (`_validate_registry` cross-container check) last, once B/C/D/E
   were already in place to have prevented new bad states from forming.

Every fix got a synthetic regression test reproducing the bug against a case
shaped like the real data first (empty `step["orig"]`, real `gid`
derivation, a real ladder+protected overlap, etc.), confirmed it failed
without the fix, then confirmed it's caught after — and every fix was also
run against the live production registry/store to confirm no false
positives before committing. Commits: A (`beadddf` and the commit before
it), B `1979329`, C/D `ee71add`, E `c8bfde2`, F `45b85dc`.

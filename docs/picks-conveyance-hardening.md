# Picks conveyance — overwrite/collision hardening (spec)

**Status: spec, not yet built** (except item A, already shipped — see below).
Companion to `picks-conveyance.md` (the model) and `picks-migration-worksheet.md`
(the data reconciliation). This doc exists because a targeted review of two
specific mechanisms (2026-07-23) found a real bug, and pulling on that thread
found a family of related ones — this is the sweep, written up before touching
any more code, per the same "spec first, then build" convention
`picks-conveyance.md` itself followed.

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

## B. Ladder fallback overwrites an independently-resolved pick — CONFIRMED, unfixed

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

## C. Two ladders can govern the same step, silently — CONFIRMED, unfixed

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

## D. Two ladders can name the same fallback pick, silently — CONFIRMED, unfixed

Same mechanism as C, other end of the ladder: nothing checks whether a
fallback pick named by a new ladder is already named by an existing ladder's
fallback (or is already someone's governed step, a narrower case of B).
Whichever ladder resolves last in `resolve_all`'s ladder loop wins.

**Proposed fix:** fold into the same `add_ladder` guard as C — check the new
ladder's fallback picks against every existing ladder's steps *and*
fallback picks, not just steps against steps.

## E. `register_swap` has no existing-structure guard — CONFIRMED, unfixed

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

## F. `apply_registry`'s `set_node` has no mutual-exclusion check — CONFIRMED, unfixed, lower priority

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

## Implementation order

1. **B** (resolver fallback guard) first — it's the resolve-time backstop and
   protects against *existing* registry data even if C/D/E are never
   triggered again, including any latent bad state already on disk.
2. **C + D together** (`add_ladder` collision guard) — same function, same
   check, do both at once.
3. **E** (`register_swap` existing-structure guard) — independent of 1–2,
   can be done in either order relative to them.
4. **F** (`_validate_registry` cross-container check) last — depends on
   nothing above, but is most useful once B/C/D/E exist to have already
   prevented new bad states from forming, so it's purely a detector for
   whatever's left.

Each fix gets a synthetic regression test the same way A/B were verified today
— reproduce the bug against a case shaped like the real data first (empty
`step["orig"]`, real `gid` derivation, etc.), confirm it fails without the
fix, then confirm it's caught after.

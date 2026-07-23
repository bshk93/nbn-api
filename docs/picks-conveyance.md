# Draft-pick conveyance model (design spec)

**Status: LIVE since 2026-07-19.** This document was written as the up-front
design spec (below), then the model described here was actually built and cut
over to production the same day and in the days after — but this file was
never updated to say so, which made it actively misleading (caught 2026-07-23).
Ground truth for "is this live right now": `PICKS_READ_SOURCE=conveyance` in
`nbn-api/.env` (currently set) makes `GET /api/picks` / `GET /api/picks/{team}`
serve from this model, not the flat CSV; `PICKS_OWNERSHIP_ENFORCE` (default,
i.e. unset) makes trade validation authoritative against it too. If either
env var is ever unset, treat this doc's "live" claim as needing re-verification
against the code, not assumed. The sections below (2 onward) are still an
accurate description of the shipped model's shapes — they were the spec the
build was checked against — but this doc is **not actively maintained** as
day-to-day work continues (new legacy picks resolved, new node types added,
etc.); it was last substantively true as prose on 2026-07-19. For what's
actually changed since, `git log -- picks_conveyance/` is more current than
either this file or `docs/picks-migration-worksheet.md` (also last updated
2026-07-19, same staleness problem).

Companion notes live in the working memory as `project-picks-conveyance-model`
and `project-pick-owner-oversimplification`.

---

## 1. Why the flat model fails

Each pick today is one CSV row keyed by `(YEAR, ROUND, ORIG)` with:

- a single `OWNER` string, **overwritten on every trade** — so an A→B→C chain
  loses the B leg unless someone hand-writes it into `NOTES`;
- a single `PROTECTED` integer (`conveys = pick > protected`) — one year, one
  threshold, no keeper/compensation link;
- a single `SWAP_OWNER` — only the 2-pick "my pick vs. their pick" case;
- `NOTES` free text — which is where all the real structure has leaked to. 63
  of 480 live rows carry notes; a handful encode multi-team cascades no field
  captures at all.

The redesign keeps the one part that's already right — **immutable pick
identity** — and replaces the single mutable `OWNER` with a resolvable
conveyance tree plus an append-only event log.

---

## 2. Core model

### 2.1 Pick

```jsonc
{
  "year": 2027,
  "round": "1st",              // string, matches existing ROUND values ("1st"/"2nd")
  "orig": "CHI",               // (year, round, orig) is the immutable primary key
  "conveyance": <Node>,        // who resolves to own it — section 2.2
  "player": null,              // terminal fact once the pick is used in a draft
  "frozen": false,             // trade-lock; orthogonal to conveyance
  "frozen_reason": null,
  "legacy": false,             // section 5 — resolver skips, humans resolve from notes
  "notes": "",                 // kept verbatim; primary record for legacy picks
  "events": [ <TransferEvent | ResolveEvent> ]   // section 2.6
}
```

`frozen` / `frozen_reason` carry forward the existing pick-freeze columns
(`FROZEN`, `FROZEN_REASON`) unchanged — they are "can this move right now,"
not "who gets it," so they stay a flag on the pick, never a conveyance node.

### 2.2 Conveyance node

A tagged union. Every node has a stable `id` (needed for leaf-addressing in
trade validation, section 4). A node's **leaves may be either a plain team
string or another node** — this one rule is what makes re-trades, stacked
conditions, and swap-of-swaps fall out without special cases.

**`settled`** — one team, unconditionally. The straight-trade case.
```jsonc
{ "type": "settled", "id": "n1", "team": "DEN" }
```

**`protected`** — a piecewise map of `on`'s actual draft position → outcome.
Bands must cover the pick's whole possible range with no gaps/overlaps. An
outcome is a team, a nested node, or `{ "type": "extinguished" }` (obligation
dies, no compensation).
```jsonc
{
  "type": "protected", "id": "n2",
  "on": { "year": 2027, "round": "1st", "orig": "CHI" },  // whose slot decides
  "bands": [
    { "min": 1, "max": 4,  "to": "CHI" },   // protected: keeper retains
    { "min": 5, "max": 60, "to": "DEN" }    // conveys
  ]
}
```
The keeper on a protected band is whichever team is named in that band — and
per the confirmed rule it is the team that *traded the pick away* (B), **not**
a revert to the original owner (A).

**`swap`** — a pointer into a shared `SwapGroup` (section 2.4). A swap's
outcome can't be computed from one pick in isolation, so the node only holds
the group id.
```jsonc
{ "type": "swap", "id": "n3", "group": "sg1" }
```

### 2.3 Nesting example (leaf is a node)

A team that acquires a contingent interest and re-conditions it — "I'll take it
if it conveys, but only if it lands outside the top 20" — replaces a leaf team
with a `protected` node. No new machinery; the resolver already recurses into
whatever it finds at a leaf, with a plain team as the base case.

### 2.4 SwapGroup

A shared entity across N picks, not a per-pick field.
```jsonc
{
  "id": "sg1",
  "members": [ {"year":2027,"round":"1st","orig":"OKC"},
               {"year":2027,"round":"1st","orig":"LAC"} ],
  "priority": [ "PHX", "LAC" ],   // best pick -> priority[0], next -> priority[1], ...
  "txn_id": null
}
```
Resolves by sorting members by actual draft position ascending and zipping
against `priority`. A 2-pick "better-of" swap is just N=2. A `priority` slot
may itself be a node (see the worked 2029 example in section 7).

`SwapGroup` models a **simultaneous** N-way swap. It cannot express an
**ordered chain of dependent swaps** — where each step swaps for "whatever team
X holds *after* the previous step." That case (surfaced by the live 2030
NOP/MIL/POR/DET/HOU chain, section 7.x) needs the binary-swap primitive below.

### 2.4a BinarySwap (ordered / dependent swaps)

A binary swap of two operands, exposing two **named outputs** (`better`/`worse`)
so later swaps can reference a specific result of an earlier one. This is the
general primitive; an N-way `SwapGroup` is the simultaneous special case.

Each output has an **explicit recipient** (`better_to` / `worse_to`), each of
which is either a team (terminal) or a downstream node input `{"ref":ID,"as":"a"|"b"}`.
An explicit recipient is required because the worse pick does not always go to
"the other operand's holder" — e.g. the 2028 ORL/MIL/WAS deal (section 7.5c)
sends the worse to MIL *by name* while the better feeds the next swap.

```jsonc
{
  "type": "binary_swap", "id": "s2",
  "a": {"year":2030,"round":"1st","orig":"POR"},   // operand: a pick ref, or
  "b": {"ref":"s1","output":"better"},              //   another swap's output
  "better_to": "POR",                    // team (terminal) or {"ref":ID,"as":"a"|"b"}
  "worse_to":  {"ref":"s3","as":"b"}     //   (downstream node input)
}
```

A chain is a list of `binary_swap` nodes resolved in order once positions are
known: resolve `a` and `b` to concrete picks (following any `ref`), compare draft
position, route the better to `better_to` and the worse to `worse_to`. See the
worked 2030 chain (7.5b) and 2028 ORL/MIL/WAS (7.5c) in section 7.

### 2.5 ProtectionLadder (multi-year chained protection)

```jsonc
{
  "id": "pl1", "from": "B", "to": "C",
  "steps": [ {"year":2027,"round":"1st","protect_top":10},
             {"year":2028,"round":"1st","protect_top":8},
             {"year":2029,"round":"1st","protect_top":0} ],   // 0 = unprotected
  "fallback": { "type": "convert", "year": 2029, "round": "2nd" },
  //          | { "type": "fixed_asset", "pick": {"year":..,"round":..,"orig":..} },
  "status": "pending",
  "resolved": null,
  "txn_id": null
}
```
All steps are written up front at trade time — future-year `Pick` rows already
exist via the horizon-ensuring logic — not materialized lazily.

### 2.6 Event log

Two event kinds, both append-only. Current state is the fold of the log, not a
single overwritten field.

- **`TransferEvent`** — `{kind, from, to, node_id, date, txn_id}` where `kind`
  ∈ `trade | protect | join_swap | retrade_contingent`. Preserves the full
  A→B→C chain the flat `OWNER` field destroys.
- **`ResolveEvent`** — emitted by the resolver (section 3), **not** just a
  status flip: `{kind:"resolve", date, actual_position, step_fired, outcome}`.
  Records *why* a pick landed where it did ("DET finished 9th → top-8
  protection conveyed") so the audit chain is complete end to end.

### 2.7 Transaction linkage

Every `TransferEvent`, `SwapGroup`, and `ProtectionLadder` carries a `txn_id`
pointing at the real transaction ledger (`TRANSACTIONS_FILE`,
`routers/transactions.py`, fetchable via `GET /api/transactions/{id}`) — not a
parallel id scheme. `null` is allowed and expected for legacy rows whose
originating trade predates the transaction log or the still-incomplete Discord
backfill; it can be filled in later and must not block migrating the pick.

---

## 3. Resolver

Two passes, run once a draft year's actual team→position order is known. It
**skips any pick with `legacy: true`** (section 5).

1. **Protections / ladders.** For each unresolved `protected` pick (or active
   ladder step), compare `on`'s actual position against the band / `protect_top`:
   - beyond threshold → conveys; owner = the conveying band's team; ladder step
     resolved.
   - within threshold → protected; owner = the keeper (the `from` team, **not**
     a revert to the original owner); advance to the next ladder step, or apply
     `fallback` if none remain.
   - **If a protected pick that stays with its keeper is also a member of a
     swap group, it is withdrawn from that group for the year** (see below).
   - append a `ResolveEvent`.

2. **Swap groups.** Once every member pick in a group has a resolved position
   for the year, drop any withdrawn members, sort the rest by position, zip
   against `priority`, write results back, flip each member's node to
   `settled`, append `ResolveEvent`s.

**Protection × swap interaction (the compound case this redesign exists for).**
A pick can be both a swap member and protected. Pass 1 must run first and mark
withdrawals before Pass 2 reads group membership; a protected-and-kept pick
does not participate in its swap that year, and the group resolves over its
remaining members. Without this rule the resolver is ambiguous in exactly the
shape that motivated the rewrite.

---

## 4. Trade validation

`_validate_trade` (`routers/transactions.py`) walks the conveyance tree of each
pick in a proposed trade and requires the sending team to own the **specific
leaf node** it is conveying — addressed by `node_id`, not merely "appears
somewhere in the tree." Because leaves can be nodes, a team can legitimately
sit at several leaves of one pick; naming the node is what distinguishes "B
sends its own `from` slot" from "B tries to send C's if-it-conveys share." A
re-trade replaces *that node* in place and appends a `retrade_contingent`
`TransferEvent`.

- Re-trading an unresolved contingent/protected interest **is allowed** (not
  frozen until resolution) — confirmed decision.
- **Cycle / depth guard:** reject any trade whose resulting tree would exceed a
  max node depth or reference a pick already in its own ancestry.
- A `legacy` pick is **frozen from re-trade** (section 5): it must be converted
  to structured nodes before it can move again.

---

## 5. `legacy` escape hatch

The model does **not** have to express every historical monstrosity before it
can ship. Any pick whose conveyance is too tangled to model structurally is
tagged `legacy` and handled by a human.

- **Resolver skips it.** It surfaces "manual — see notes" instead of computing
  an owner; `notes` prose is kept **verbatim**. Display shows a "manually
  resolved" badge so the site is honest that no engine stands behind that pick.
- **Reuses the freeze mechanism.** `legacy` is a `frozen_reason`, and a legacy
  pick is **frozen from re-trade**. Upgrade-on-touch: the moment it needs to
  move again, a human first converts its notes into structured nodes. The
  legacy set therefore only ever *shrinks*.
- **Going forward: structured-or-not-booked.** The ban is on *unstructured*
  trades, not *complex* ones — the model already expresses a great deal (even a
  5-team cascade is a swap-group-of-swap-outcomes). Every new pick trade must be
  enterable as conveyance nodes; if the committee can't express it in the
  builder, that's a signal the *trade itself* is ambiguous and must be clarified
  with the parties **before** it's booked. This is a feature: it forces "what
  happens if DET finishes 9th?" to be answered at trade time, not discovered
  years later in prose.

---

## 6. Storage

None of this fits the flat CSV columns — nested ranges, ladders, cross-pick
links, and N-ary swap groups need real structure. JSON is the fit, consistent
with `trading-block.json`, `trade-exceptions.json`, and `team-state.json`.

**Compatibility read projection (the migration de-risker).** Every current
reader consumes flat `(YEAR,ROUND,ORIG,OWNER,…)` rows — `buildPicksTable`
(`teams/team.js`), `/draft`, `tradeblock/index.html`, and the live draft board
(`_owners_swap_aware` / `_pick_owners` in `routers/draft.py`). Do **not** rewrite
them all at once. The new model exposes a `current_owner(pick, as_of_year)` fold
that projects the event log back to the old row shape, and the existing pick
read endpoints are served from that projection. JSON storage + resolver then
ship *behind the existing read API*, and consumers migrate one at a time. Note
there are **two** pick files — `draft-picks.csv` (inventory) and
`draft-live-picks.csv` (live board) — and both must read from the same source
of truth or they drift.

---

## 7. Worked examples (acceptance test)

Derived from the live 63-row `NOTES` triage (snapshot 2026-07-18). The model is
"complete" iff every non-legacy row below maps to structured nodes. Row indices
reference the triage dump.

### 7.1 Simple threshold protection with extinguish — row 00
`2027 CHI 1st`, owner DEN, top-4 protected, "obligation extinguished if
protected."
```jsonc
{ "type": "protected", "id": "n_chi27",
  "on": {"year":2027,"round":"1st","orig":"CHI"},
  "bands": [ {"min":1,"max":4,"to":"CHI"},      // protected: DEN gets nothing, no comp
             {"min":5,"max":60,"to":"DEN"} ] }
```

### 7.2 Protection with a keeper ≠ original owner — row 13
`2027 IND 2nd`: falls 31–55 → stays NYK (keeper); 56–60 conveys to MIA.
```jsonc
{ "type": "protected", "id": "n_ind27_2",
  "on": {"year":2027,"round":"2nd","orig":"IND"},
  "bands": [ {"min":31,"max":55,"to":"NYK"},
             {"min":56,"max":60,"to":"MIA"} ] }
```

### 7.3 Range-split (the oversimplification case) — row 55
`2031 GSW 2nd`: 31–55 → HOU, 56–60 → OKC. (Live `OWNER` is stale `SAS`.)
```jsonc
{ "type": "protected", "id": "n_gsw31_2",
  "on": {"year":2031,"round":"2nd","orig":"GSW"},
  "bands": [ {"min":31,"max":55,"to":"HOU"},
             {"min":56,"max":60,"to":"OKC"} ] }
```

### 7.4 Two-pick swap right — rows 04 / 06
DET may swap `2027 LAL 1st` for `2027 HOU 1st` (takes the better).
```jsonc
// SwapGroup
{ "id":"sg_det27", "txn_id":null,
  "members":[ {"year":2027,"round":"1st","orig":"LAL"},
              {"year":2027,"round":"1st","orig":"HOU"} ],
  "priority":[ "DET", "HOU" ] }   // DET gets the better; HOU keeps the worse
// both picks' conveyance: { "type":"swap", "id":..., "group":"sg_det27" }
```

### 7.5 Compound: swap whose loser is protected — rows 38 / 39
`DAL gets higher of MIA/MIN, DET gets lower; if the lower is top-3, MIN gets
it instead.` The swap's second `priority` slot is itself a `protected` node —
the nesting rule at work.
```jsonc
{ "id":"sg_miamin29", "txn_id":null,
  "members":[ {"year":2029,"round":"1st","orig":"MIA"},
              {"year":2029,"round":"1st","orig":"MIN"} ],
  "priority":[ "DAL",
    { "type":"protected", "id":"n_lower29",
      "on":"__slot1__",                      // the pick that lands in the lower slot
      "bands":[ {"min":1,"max":3,"to":"MIN"},
                {"min":4,"max":60,"to":"DET"} ] } ] }
```
> Open modeling detail flagged by this row: a `protected` node inside a swap
> slot needs to reference "the pick that landed in this slot," not a fixed
> `(year,round,orig)`. The `"__slot1__"` sentinel above is a placeholder for
> that binding — resolve during the build (section 8, open question).

### 7.5b Ordered swap chain — 2030 NOP/MIL/POR/DET/HOU
Five teams, but a *linear* chain of dependent binary swaps, not a simultaneous
group. Each step swaps for "whatever the previous step left with a team," so it
uses `binary_swap` (section 2.4a) with outputs referenced downstream — the case
that a static `SwapGroup` cannot express.
```jsonc
[ { "type":"binary_swap", "id":"s1", "a":{"y":2030,"r":"1st","orig":"NOP"},
    "b":{"y":2030,"r":"1st","orig":"MIL"},
    "better_to":{"ref":"s2","as":"b"}, "worse_to":"MIL" },
  { "type":"binary_swap", "id":"s2", "a":{"y":2030,"r":"1st","orig":"POR"},
    "b":{"ref":"s1","output":"better"},
    "better_to":"POR", "worse_to":{"ref":"s3","as":"b"} },
  { "type":"binary_swap", "id":"s3", "a":{"y":2030,"r":"1st","orig":"DET"},
    "b":{"ref":"s2","output":"worse"},
    "better_to":"DET", "worse_to":{"ref":"s4","as":"a"} },
  { "type":"binary_swap", "id":"s4", "a":{"ref":"s3","output":"worse"},
    "b":{"y":2030,"r":"1st","orig":"HOU"},
    "better_to":"NOP", "worse_to":"HOU" } ]
```

### 7.5c 3-way redistribution — 2028 ORL/MIL/WAS
ORL takes the best of the three; MIL takes the worse of {MIL,ORL}; WAS takes the
leftover. A 2-step `binary_swap` chain — note the worse of S1 goes to MIL *by
name*, which is why outputs need explicit recipients (section 2.4a).
```jsonc
[ { "type":"binary_swap", "id":"s1", "a":{"y":2028,"r":"1st","orig":"MIL"},
    "b":{"y":2028,"r":"1st","orig":"ORL"},
    "better_to":{"ref":"s2","as":"a"}, "worse_to":"MIL" },
  { "type":"binary_swap", "id":"s2", "a":{"ref":"s1","output":"better"},
    "b":{"y":2028,"r":"1st","orig":"WAS"},
    "better_to":"ORL", "worse_to":"WAS" } ]
// ORL = best of {MIL,ORL,WAS};  MIL = worse of {MIL,ORL};  WAS = residual
```

### 7.6 Legacy — rows 02, 03, 05, 08 (one deal) and 01, 09, 11 (another)
The 2027 DET 5-team cascade (PHX/OKC/LAC/DET/HOU, then DET↔GSW) and the
PHI/CHA/TOR/DAL swap chain. Tagged `legacy: true`, `frozen_reason: "legacy"`,
notes kept verbatim, resolver skips. Convert to structured nodes only if/when
re-traded. Row 52 (`2031 HOU`, note explicitly says meaning unclear) is legacy
too, and its partner row 53 is held with it until 52 is clarified.

---

## 8. Migration plan

The 63-row triage splits the work into phases; the compatibility projection
(section 6) is built first so the read path never breaks.

| Phase | Rows | Work |
|---|---|---|
| **0. Projection** | all | Build JSON store + `current_owner()` fold; serve existing read endpoints from it. No behavior change. |
| **1. Trivial** | 218 untouched + 220 straight trades + 60 with `PLAYER` | Mechanical: `settled` nodes / terminal player facts. |
| **2. Clean structured** | rows 00, 13, 55 (protection) + 04/06, 20/27/28, 38/39 (swaps) | Direct model per section 7. Fix stale `OWNER` fields while here (07, 20, 27, 28, 32, 55). |
| **3. Enrichment** ⟵ *needs owner walkthrough* | 33 bare-`"protected"` + 8 `"conditional: X/Y"` | Recover the missing threshold **and** counterparty for each from the cached league sheet / trade record, then model as `protected`. These aren't cascades — just under-specified. |
| **4. Legacy** | rows 01,02,03,05,08,09,11,52[+53] + the 2028 MIA/SAC/DAL/MEM/NYK/CHA cluster | Tag `legacy`, keep prose, move on. (2030 NOP/MIL/POR/DET/HOU chain was a legacy *candidate* but structured cleanly with `binary_swap` — see 7.5b.) |
| **5. Hygiene** | rows 07, 56 | Data fixes (07 `owner=NOP` should be `settled(NYK)`; 56 `NYK*` asterisk), no model needed. |

**Phase 3 is a guided walkthrough, not a lookup.** The bare-`protected` and
`conditional` rows don't carry their own thresholds; the owner and I will talk
through each trade to recover them before they can become structured nodes.
This is expected and fine — it's enrichment, not modeling risk.

---

## 9. Open questions

- **Slot-bound protection reference** (section 7.5): concrete binding for a
  `protected` node that lives inside a swap `priority` slot and must key off
  "the pick that landed here," not a fixed identity.
- **Synthetic fallback identity**: a `fixed_asset` fallback points at a real
  `(year,round,orig)` and is fine; only a `convert`-to-a-2nd fallback mints a
  *new* asset. Lean toward deriving its id deterministically from the ladder id.
- **Exact `_validate_trade` tree walk** — leaf-node addressing + cycle guard
  are specified conceptually (section 4); the implementation isn't written.
- **`ROUND` typing** — keep the existing string values (`"1st"`/`"2nd"`)
  throughout, including ladder `steps[].round`, or joins against pick rows break.

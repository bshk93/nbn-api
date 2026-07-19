# Picks migration — Phase 3 reconciliation worksheet (WIP)

Working record of the 41 under-specified `NOTES` rows as they get resolved with
the owner. Companion to `picks-conveyance.md` (the spec) and the memory
`project-picks-conveyance-model`. Each entry: the resolved structured node, the
evidence, and who confirmed it. **Not yet applied to any file** — this is the
decision log the migration will be built from.

Method note (learned the hard way): when tracing a pick through
`transactions.json`, search **both** the abbreviation (`OKC`) and the full name
(`Oklahoma`) — descriptions mix them, and an abbrev-blind search missed Trade 73
below on the first pass.

---

## LOCKED

### 2027 SAS 2nd + 2027 OKC 2nd — swap pair
**Resolution:** `SwapGroup{2027 SAS 2nd, 2027 OKC 2nd} → priority [BOS, NOP]`
(BOS = better of the two, NOP = worse of the two).
**Provenance (full chain, from `transactions.json`):**
- Worse-of-two: WAS *(Trade 23, 2021-08-02)* → MIN *(Trade 64, 2023-12-16)* →
  HOU *(Trade 17, 2024-07-01)* → **NOP** *(Trade 73, 2026-02-06)*.
- Better-of-two: SAS → **BOS** *(Trade 70, 2025-02-07)*.
**Supersedes:** my earlier wrong call of BOS/HOU — HOU was correct only until
Trade 73, where HOU conveyed its worse-of-two share to NOP (written "2027 OKC
2nd"). BOS is not a party to Trade 73.
**CSV fix implied:** `2027 SAS 2nd` note `"conditional: BOS/HOU"` → **BOS/NOP**.
`2027 OKC 2nd` note `"BOS/NOP"` was already correct. Both rows currently
`owner=BOS`; real ownership is swap-determined at the 2027 draft.
**Confirmed by owner:** 2026-07-19.

### 2027 SAS 1st — rolling protection tail
**Resolution:** `ladder(SAS → WAS)`: 2026 SAS 1st top-14 protected (conveys to
WAS if outside top-14); 2027 SAS 1st unprotected (conveys to WAS). The
under-specified `2027 SAS 1st owner=SAS "protected"` row is the **2027
unprotected tail** — conveys to WAS if the 2026 pick stayed protected.
**Provenance:** 2026 SAS 1st: POR *(Trade 61, 2023-11-16)* → **WAS** *(Trade 49,
2026-01-22)*. WAS is current holder.
**Supersedes:** SAS-sheet "Portland" note — stale, predates Trade 49.
**Confirmed by owner:** 2026-07-19.

### 2032 LAL 1st + 2032 MIN 1st — swap (not stale)
**Resolution:** `swap{2032 MIN 1st, 2032 LAL 1st} → [MIN, LAL]` (MIN holds swap right).
**Provenance:** Trade 30 *(2025-08-01)* — MIN receives "2030 LAL 1st Swap Rights,
2032 LAL 1st Swap Rights." (2030 LAL/MIN swap in block A confirmed by same trade.)
**Supersedes:** my earlier "no cell note → likely settled(orig)" guess — it's a swap.
**Confirmed by owner:** 2026-07-19.

### 2030 CHI 1st — stale owner
**Resolution:** `swap{2030 UTA 1st, 2030 CHI 1st} → [UTA, CHI]`; **owner → CHI** (not UTA).
**Provenance:** Trade 65 *(2026-02-05)* — UTA receives "2030 CHI 1st **swap rights**,"
i.e. a swap right, not ownership. Matches sheet note.
**Confirmed by owner:** 2026-07-19.

### 2030 DAL 1st + 2032 DAL 1st — MIA owns, swap right on top
**Resolution:**
- `swap{2030 TOR 1st, 2030 DAL 1st(MIA)} → [TOR, MIA]`
- `swap{2032 UTA 1st, 2032 DAL 1st(MIA)} → [UTA, MIA]`
Both DAL-origin picks are **owned by MIA** (owner=MIA is correct); a swap right
sits on top (TOR for 2030 per sheet, UTA for 2032 per Trade 55).
**Provenance:** Trade 1 (De'Aaron Fox deal) — MIA receives "2030 DAL 1st, 2032 DAL
1st." Then UTA swap right via Trade 55 *(2026-02-03)*; TOR swap right per sheet.
**Corrects earlier error:** I wrongly flagged owner=MIA as a mis-book (confusing it
with the 2032 DAL **2nd** MIA got in Trade 32). MIA genuinely owns both 1sts.
**Confirmed by owner:** 2026-07-19.

---

### 2028 MIA/SAC/DAL/MEM/NYK/CHA cluster — LEGACY
**Resolution:** `legacy` tag, `frozen_reason: "legacy"`, prose kept verbatim,
frozen-from-retrade. Four sequential conditional swaps across six teams (SAC
swaps DAL-or-MIA, then MEM swaps the lesser of DAL/SAC, then NYK swaps into CHA,
then MIA swaps a PHX pick vs SAC's result). Convert only if re-traded.
**Confirmed by owner:** 2026-07-19.

### 2030 NOP/MIL/POR/DET/HOU chain — structured as a sequential swap chain
**Resolution:** an ordered chain of **binary swaps**, each with `better`/`worse`
outputs, later swaps referencing earlier outputs:
1. `S1 = swap(NOP-30, MIL-30)`  → better→NOP, worse→MIL
2. `S2 = swap(POR-30, S1.better)` → POR takes better; loser → NOP
3. `S3 = swap(DET-30, S2.worse)`  → DET takes better; loser → NOP
4. `S4 = swap(S3.worse, HOU-30)`  → NOP takes better; HOU takes worse
**Spec impact:** revealed that the static N-way `SwapGroup` can't express
*ordered, dependent* swaps ("swap for whatever X holds after the prior step").
Model swaps as binary nodes with named `better`/`worse` outputs; N-way SwapGroup
becomes the simultaneous special case. Logged in the spec.
**Confirmed by owner:** 2026-07-19.

### 2028 ORL/MIL/WAS 1st — 3-way redistribution
**Resolution:** 2-step `binary_swap` chain:
1. `S1 = swap(MIL-28, ORL-28)` → worse→MIL, better→S2
2. `S2 = swap(S1.better, WAS-28)` → better→ORL, worse→WAS

Net: ORL = best of {MIL,ORL,WAS}; MIL = worse of {MIL,ORL}; WAS = residual.
Verified across all three orderings.
**Owner confirmed:** WAS gets a pick back (true 3-way, not an empty-handed swap-right), 2026-07-19.
**Spec impact:** forced `binary_swap` outputs to have **explicit** `better_to`/
`worse_to` recipients (worse→MIL by name), generalizing the earlier "taker" form.
Logged in spec §2.4a + §7.5c.

---

## BLOCK B COMPLETE ✅

All 12 decision rows resolved. Remaining migration work is **block A** — the ~29
straightforward rows the sheet fully specifies (swaps / range-splits / ladders),
which just need mechanical transcription into nodes, no further decisions.

---

## BLOCK A — formalized 2026-07-19 (sheet-specified, no decisions)

Structured nodes for the remaining under-specified rows. Source = `fresh.xlsx`
cell Notes (each row's F-column note). Grouped by shared entity; each group lists
the CSV rows it covers. `prot(PICK)[band→team]`, `swap{picks}→[priority]`,
`ladder(...)`, `binary_swap` chains per spec §2.4a.

**Range-split 2nds (protected bands):**
```
2027 GSW 2nd   prot(GSW-27-2)[31-55→UTA, 56-60→LAC]
2027 LAC 2nd   prot(LAC-27-2)[31-38→LAC, 39-60→NYK]
2028 IND 2nd   prot(IND-28-2)[31-44→TOR, 45-60→POR]
```

**Simultaneous better-of swaps (1sts) — SwapGroup:**
```
swap{BOS-28,CHI-28}→[BOS,CHI]          covers rows: 2028 BOS, 2028 CHI
swap{OKC-28,DEN-28}→[BOS,OKC]          covers rows: 2028 DEN, 2028 OKC
swap{POR-29,DEN-29}→[DEN,POR]          covers rows: 2029 DEN, 2029 POR
swap{GSW-29,PHI-29}→[GSW,PHI]          covers rows: 2029 GSW, 2029 PHI
swap{MIN-30,LAL-30}→[MIN,LAL]          covers rows: 2030 LAL, 2030 MIN
swap{NOP-31,CHA-31}→[NOP,CHA]          covers rows: 2031 CHA, 2031 NOP
(2032 UTA-32/DAL-32 swap → covered in Block B: swap{UTA-32,DAL-32(MIA)}→[UTA,MIA])
```

**Simultaneous better-of swaps (2nds) — SwapGroup:**
```
swap{CHA-28-2,BKN-28-2}→[CHA,CHI]      covers rows: 2028 BKN, 2028 CHA
swap{BOS-28-2,TOR-28-2}→[MIA,TOR]      covers row:  2028 TOR (BOS-2 is elsewhere)
swap{IND-32-2,HOU-32-2}→ better_to IND, worse_to CLE   covers rows: 2032 HOU, 2032 IND
```

**Ladders / fallbacks:**
```
2027 MEM 2nd   ladder(MEM→BKN): 2026-2nd top-37 protected, else 2027 MEM 2nd conveys → settled(BKN) tail
2029 SAS 2nd   fixed_asset fallback of ladder(SAS 1st→TOR, top-10): if 29 SAS 1st stays protected → TOR
2030 SAS 2nd   second fixed_asset fallback of same SAS→TOR ladder → TOR
```

**Compound (binary_swap chain) — 2028 SAS/NOP then DET tail:**
covers rows: 2028 SAS, 2028 NOP, 2028 DET
```
S1 = swap(SAS-28, NOP-28):  worse→NOP,  better→S2.b
S2 = swap(DET-28, S1.better): better→DET, worse→SAS
```
Net: SAS/NOP swap (SAS better, NOP worse); then DET swaps DET-28 for SAS's better
pick (DET better, SAS worse).

---

## ALL 41 UNDER-SPECIFIED ROWS RESOLVED ✅ (2026-07-19)

Block B (12 decision rows) + Block A (29 sheet-specified) complete. Next step is
implementation, not more reconciliation: build the JSON store + resolver +
compatibility projection per the spec's migration plan (Phase 0 first). The
straightforward buckets (218 untouched + 220 straight trades + 60 drafted) remain
mechanical and untouched by this worksheet.

---

## PHASE 0 BUILT ✅ (2026-07-19) — branch `picks-conveyance-phase0` (nbn-api), uncommitted

New package `picks_conveyance/` (no live wiring; nothing reads its store yet):
- `model.py` — node constructors + structural validation (settled / extinguished /
  protected / swap / binary_swap), depth guard, band contiguity check.
- `resolver.py` — draft-time engine: `resolve_all(store, positions)` → {pickkey:
  owner}. Two-pass (protected, swap groups) + binary-swap chain evaluator.
- `projection.py` — `project_to_flat(pick)`: folds a conveyance pick back to the
  `pick_to_response` shape (the migration de-risker).
- `seed_store.py` — CSV → JSON store; settled node per row; flags the 11
  `PROTECTED`/`SWAP_OWNER` rows `needs_structure`.
- `tests/` — **parity test**: projection reproduces `/api/picks` byte-for-byte for
  all **469 settled picks** (11 skipped = the flat-structured rows). **resolver
  test**: all worked cases pass, incl. the 2030 binary chain + nested-protection
  swap (rows 38/39).

Run (in venv): `venv/bin/python -m picks_conveyance.tests.test_projection_parity`
and `... .tests.test_resolver`.

## PHASE 2 BUILT ✅ (2026-07-19) — same branch, still uncommitted, still no live wiring

- `curated.py` — the whole worksheet as executable, validated data: 6 protected
  (incl. range-splits), 14 swap groups, 3 binary chains, 3 ladders, 10 legacy
  markers. `apply_curated(store)` overrides the seeded settled placeholders.
- `model.py` — added `binary` (chain-membership marker) and `legacy` node types.
- `projection.py` — `project_to_flat(pick, store)` now folds **every** node type
  to the flat shape: keeper-is-origin protections collapse to (owner, protected);
  splits / swaps / chains show pipe-joined candidate teams; legacy keeps its
  nominal owner. `nominal_owner` implemented for all types.
- Tests (all green via `tests/run_all`): `test_curated` (validate + resolve all
  45 contingent picks under a synthetic full draft, 10 legacy correctly skipped),
  `test_projection_full` (project every node type, spot-checks per type), plus the
  Phase 0 parity + resolver suites still pass.

## PREVIEW ENDPOINT LIVE ✅ (2026-07-19) — additive, non-breaking

- Curated store seeded to `NBS_DATA_DIR/draft-conveyance.json` (480 picks: 425
  settled, 6 protected, 28 swap members, 11 binary members, 10 legacy; 5 still
  `needs_structure` = flat swap rows never reconciled, passed through via `_flat`).
- New router `routers/picks_preview.py` → `GET /api/picks-preview`: loads the
  store, projects to flat, runs `enrich_swap_conveys` to match the live endpoint.
  Registered in `main.py`; **`/api/picks` untouched.** Returns 503 if unseeded.
- Live diff vs `/api/picks`: **435 identical, 45 differ, 0 unexplained
  regressions.** All 45 diffs are contingent rows showing the improved model
  (single nominal owner → candidate set / correct thresholds). The only 3 rows
  dropping a flat `swap_owner` are 2 legacy (2027 DAL, 2031 MIN) + 1 now modeled
  in the binary chain (2030 HOU) — all intentional.
- Regen store after editing curated: `venv/bin/python -m
  picks_conveyance.seed_store --curated`. Service picks it up with no restart
  (endpoint reloads the file per request).

## FOLLOW-UPS (2026-07-19)

- **Ladder resolution pass — DONE.** `resolver._resolve_ladders` (Pass 4) walks
  steps in year order (conveys when beyond `protect_top` / unprotected; else stays
  and rolls forward; `fixed_asset` fallback when all steps stay protected).
  `test_ladders` covers SAS→WAS, MEM→BKN, SAS→TOR fallback. All green.
- **5 leftover swap rows — 3 reconciled, 1 legacy, 1 PENDING:**
  - `2026 BOS/WAS` → swap [WAS, BOS] (sheet WAS F77). DONE.
  - `2032 HOU/NOP` → swap [NOP, HOU] (sheet HOU F87). DONE.
  - `2028 CHA` (owner DAL, swap MIL) → legacy, part of the 2028 SAC/DAL/MIA/MEM/
    NYK/CHA cluster. DONE.
  - `2029 BOS` + `2029 HOU` + `2029 NOP` → **DONE (binary chain `nop29`).** The
    apparent tangle dissolved once the live picks file + recent ledger were read
    over the stale Trade 42 backfill: **Trade 30** (2026-07-10) *returned* NOP's
    own 2029 1st to NOP **and** gave NOP a NOP-favor swap right on BOS's 2029 1st;
    **Trade 34** (2026-07-14) gave NOP a swap right on HOU's 2029 1st. Two
    genuinely separate swap rights on NOP's one pick → resolves as NOP takes the
    best of {NOP,BOS,HOU}, the team it took from gets NOP's pick, third keeps own.
    Encoded as a 2-step binary chain (a flat 3-way sort-zip assigns the losers
    wrong; verified). Confirmed by owner 2026-07-19.

## ALL 480 PICKS MODELED ✅ — store `needs_structure` = 0

Every pick now has a real conveyance node (settled / protected / swap / binary /
legacy / ladder). Live diff `/api/picks` vs `/api/picks-preview`: **427 identical,
53 differ, 0 unexplained** — all 53 are contingent-row improvements; the 8 rows
that drop a flat `swap_owner` are all now modeled as swaps/chains/legacy.

## CUTOVER STEP 1 — DUAL-WRITE ✅ (2026-07-19)

The write side of the cutover: `picks_conveyance/resync.py` regenerates the
conveyance store (seed + curate, full re-derivation, not a per-mutation patch)
after every write to the flat CSV. Wired into the single choke point all 6
`save_picks()` call sites share (`roster_picks.save_picks`) — covers trades
(`_apply_trade`), draft-pick awards (`_apply_pick`), manual admin upserts/
deletes, and horizon-year creation, with **one** hook. Fails open internally
(logged, never raised) so a bug here can never block or corrupt a real trade.
`test_resync` (in `tests/run_all`) proves correctness against the live CSV and
fail-open against a broken path. Verified live end-to-end via the real
`save_picks()` (no-op content, confirmed the store's mtime advances). Restarted
service, no import errors, existing traffic unaffected.

**Result:** `/api/picks-preview` will no longer drift stale — every future pick
mutation keeps it current automatically. `/api/picks` itself is still untouched
(reads not cut over).

**`enter-transaction` skill (nbn-today `.claude/commands/enter-transaction.md`):
checked, no required changes** — the transaction payload contract and
`/api/picks`/`/api/picks/{team}` read semantics (pipe-compound OWNER, `"?"`
placeholder, PROTECTED/SWAP_OWNER carryover) are all unchanged in this step.
Added one optional pointer (§4c) to `/api/picks-preview` as supplementary
disambiguation info for contingent picks — read-only, not the write path.
**Will need a real rewrite of §4c once reads are cut over** (Step 2 below) —
the pipe-compound/`"?"` semantics description is specific to the flat model.

## CUTOVER STEP 2 — READ CUTOVER ✅ LIVE (2026-07-19)

`GET /api/picks` and `GET /api/picks/{team}` now serve from the conveyance
model. Flag-gated: `PICKS_READ_SOURCE=conveyance` in nbn-api `.env` (currently
**set**, i.e. live); unset reverts to the exact old flat behavior, one restart.
`/api/picks` and `/api/picks-preview` are now identical (same source) — the
preview endpoint stays as a permanent no-op alias, harmless to leave.

**Two real consumer bugs found and fixed before flipping the flag on**
(compound-owner values, now common at 53/480 rows, exposed both):

1. **`tradeblock/index.html`** (`activateEditPanel`) — client re-filtered
   `/api/picks/{team}` results with `p.owner === myTeam` (exact match), which
   is *more* restrictive than the server's own `team in owner.split("|")`
   check the endpoint already applies. Any compound-owner pick silently
   vanished from **both** candidate teams' "my picks" list. Fix: dropped the
   redundant client-side filter; the server result is already correct.
2. **`draft/index.html`** (`renderFutureEditable`, the BOD pick-editing
   table) — **real risk, not just display**: Save PUTs *every visible row*
   unconditionally as a full-row replace, and `owner` is a required field. A
   compound owner has no matching `<select>` option, so it silently defaulted
   to the browser's first option (`ATL`) — meaning saving *any* unrelated edit
   on a page that included a contingent pick would have silently collapsed
   that pick's owner to `ATL`, discarding its swap/protection structure. Fix:
   track whether the admin actually touched that row's owner `<select>`
   (`p._ownerTouched`, via a `change` listener); untouched rows now send back
   the original `p.owner` (including compound) instead of the select's stale
   default. Added a visible amber hint under the dropdown showing the real
   compound value. Syntax-checked both files with `node --check`.

**One conveyance-model bug found by this exercise, fixed in `curated.py`:**
`apply_curated`'s `set_node` was overriding a pick's conveyance node
unconditionally, even for picks that had **already been drafted** (have a
`player` recorded) — e.g. the 2026 BOS/WAS swap pair, resolved months ago,
was being redisplayed as still-contingent (`owner: "WAS|BOS"`) because the
static curated snapshot predates the draft and doesn't know it resolved. Fix:
`set_node` now skips any pick with a `player` already set — the base seed's
`settled(OWNER)` (mirroring the flat CSV's already-resolved OWNER) is trusted
over the stale curated override. Verified: 0 already-drafted picks anywhere in
live output now show a compound owner. `test_resync`'s assertion updated to
match the correct invariant (any `needs_structure` row must be already-drafted,
not literally zero).

**`enter-transaction.md` §4c — NOT yet rewritten.** The pipe-compound-OWNER /
`"?"` explanation is now serving MORE compound rows than before (53 vs.
whatever pre-existed), but the underlying mechanics it describes
(`team in owner.split("|")`, `GET /api/picks/{team}`) are **unchanged** —
`/api/picks/{team}` still does the exact same compound-aware filtering, just
sourced differently now. So the skill's guidance remains **accurate**, just
less necessary to lean on `/api/picks-preview` as a separate reference now that
`/api/picks` itself carries the richer data. Worth a pass to fold the
`/api/picks-preview` pointer back into a plain `/api/picks` mention, but not
urgent since nothing in the skill is currently wrong.

**Verified live:** all pick endpoints 200, `/api/picks` == `/api/picks-preview`
byte-identical, zero already-drafted picks with compound owner, `swap_owner`
restored for all 2-team swaps (live-draft real-time resolution intact), full
test suite green (`tests/run_all`).

**Rollback:** remove `PICKS_READ_SOURCE=conveyance` from nbn-api `.env`,
restart — reverts to the flat CSV instantly, independent of everything else.

## CUTOVER STEP 3 — WRITE PATH BUILDS REAL STRUCTURE ✅ LIVE (2026-07-19)

Owner's explicit direction: the flat CSV stops being how picks are modeled —
new trades must produce real conveyance structure automatically, not fall
back to `needs_structure` passthrough forever. Two sub-pieces:

**3a. `curated.py` → persistent, growable registry.** The static Python file
could never grow on its own. New module `picks_conveyance/registry.py`:
`NBS_DATA_DIR/draft-conveyance-registry.json`, one-time-seeded from
`curated.py`'s dicts (`seed_registry_from_curated`), with `add_protected`/
`add_swap_group` for the write path to append to. `apply_registry` replaces
`apply_curated` as what `resync.py`/`seed_store.py --curated` actually run.
**Verified byte-identical output** to the old static path before cutting over
(per-pick conveyance, swap_groups, binary_swaps, chains, ladders all diffed
equal; only a cosmetic meta field differed, since fixed).

**3b. `_apply_trade` builds real nodes at write time.** New module
`picks_conveyance/from_trade.py`. Two directions, each verified against real
documented data before shipping (not guessed):
- **Protection**: keeper if it stays protected = `from_team` (the trade's
  sending side — the only place this is knowable; the CSV afterward only
  remembers the *current* owner). Conveys to `to_team` beyond the threshold.
  Matches the model rule confirmed since the start of this project.
- **Swap**: traced against the real, note-documented 2031 HOU/IND/MIN row
  ("HOU holds the right to swap... whichever is more favorable to HOU") —
  confirmed in *both* branches that the **named team (`swap_with`) always
  gets the more favorable pick**, `to_team` gets the less favorable. My first
  pass at this reasoning (from the JS alone) got the direction backwards;
  re-derived against the real row before encoding it. `register_swap` looks
  up the counterpart pick (same lookup `enrich_swap_conveys` does at display
  time — the pick `swap_with` currently owns for the same year/round); if
  none is found, returns `False` and the pick stays flat-passthrough exactly
  as before this feature existed — **never silently drops data**.

Wired into `routers/transactions.py`'s `_apply_trade` via
`_register_trade_protection`/`_register_trade_swap`, both **fail-open**
(logged, never raised — confirmed live: fed bad input, got a full traceback in
the log and a clean `None` return, no exception surfaced).

**Verified end-to-end against the real wired functions** (not just the
underlying module) using synthetic far-future pick-keys (2099) so nothing
touched real data: both registered correctly, fail-open confirmed, registry
restored byte-for-byte to its pre-test state afterward. Full test suite green
(`test_registry`, `test_from_trade` added; `tests/run_all` — 8 suites).

**What Step 3 does NOT yet do — deliberately separate, not started:**
Trade *validation* (whether a team is allowed to trade a given pick) still
reads the flat `OWNER` field directly (`owner.split("|")`), not the
conveyance tree. The spec's leaf-addressed ownership check (§4 — a team may
only convey the specific node it actually holds) is designed but not built.
This is a bigger, separate change to core trade-legality logic and needs its
own deliberate pass, not bundled into this one.

## CUTOVER STEP 4 — SHADOW OWNERSHIP VALIDATION ✅ LIVE (2026-07-19)

Before ever letting the tree-based ownership check block a real trade, it
needs to run *alongside* the existing flat check and prove itself quiet.
Built and shipped the shadow layer:

- **`picks_conveyance/ownership.py`** — `team_holds_claim(pick, team, store)`:
  does `team` have *any* legitimate standing on a pick's tree (settled owner,
  a protected band, a swap-group candidate, a binary-chain possible
  recipient)? This is the coarse "appears as a leaf somewhere" check from spec
  §4 — not yet the precise "which specific leaf" addressing that real
  enforcement will eventually need.
- **Wired into `_apply_trade`'s existing ownership check**
  (`_shadow_check_ownership`, `routers/transactions.py`) — computed on every
  real trade alongside the flat `owner.split("|")` check, logs a `WARNING` on
  disagreement, **never raises, never affects the trade**. Silent on
  agreement to keep the log meaningful. Verified fail-open (bad/missing store
  input → silent skip, no exception) and verified against a real, live
  disagreement before wiring (see below).

**Immediate finding, on the first real sweep:** ran the check across all 480
picks × 30 teams. **74 cases** where the tree recognizes a legitimate claim
the flat check doesn't — expected and correct: these are contingent-pick
candidates who currently can't even attempt to re-trade their share because
the flat `OWNER` column only ever names one team. **1 case the other,
concerning direction** (flat allows a trade the tree check would have
rejected): `2028 BOS 2nd`, flat `OWNER=DAL`, but the curated swap group
(`sg_bos_tor_28_2`) still said `priority=[MIA,TOR]` — stale. **Trade 35**
(2026-07-15, four days before this session) had moved the swap right from MIA
to DAL (*"DAL receives ... 2028 BOS/TOR 2nd ..."*), and the curated
reconciliation never got updated. Fixed: `curated.py` + the live registry
entry both corrected to `priority=[DAL,TOR]`. Re-swept: **0 concerning cases
remain.** This is exactly what shadow mode is for, and it caught a real stale
entry on its very first run.

**Not yet built:** the precise leaf-node-id addressing (distinguishing which
of several claims a team holds, when they hold more than one) — deferred; see
Step 5 below for why "replay history to test first" doesn't actually work,
and the owner's decision on how to proceed instead.

## CUTOVER STEP 5 — OWNERSHIP ENFORCEMENT ✅ LIVE (2026-07-19)

**Owner's explicit decision:** move to the new system now and observe in
production, rather than build more pre-validation first. Two things were
discussed and explicitly declined before this:
- *Replay historical trades to test first* — doesn't actually work: the flat
  CSV only ever stores current state, not point-in-time snapshots, so
  checking an old trade against today's tree checks it against ownership
  *after* everything that's happened since, not the state at the time. A
  correct replay would need a full chronological rebuild from the transaction
  log, which hits the known-incomplete Discord backfill (418/485 trades) and
  can't distinguish real bugs from data gaps.
- *A dry-run/validate-only endpoint* — offered as a safe alternative, declined
  in favor of just moving to the new system and watching for real breakage.

**What changed:** `_check_pick_ownership` (`routers/transactions.py`,
replacing `_shadow_check_ownership`) — the tree-based check
(`picks_conveyance.ownership.team_holds_claim`) is now **authoritative** for
every real trade, superseding the flat `owner.split("|")` check it grew out
of. This is the actual point of the whole conveyance model: a team can now
legitimately trade a contingent share (e.g. CHI's stake in the 2028 BOS/CHI
swap) that the flat OWNER field could never represent and the old check would
always have rejected.

Two safety nets, neither a hedge on the decision itself:
- **Infra fallback**: if the conveyance store can't be loaded or doesn't have
  the pick, falls back to the flat check — a file-read failure must not be
  able to halt all trading.
- **Explicit revert lever**: `PICKS_OWNERSHIP_ENFORCE=flat` in nbn-api `.env`
  + restart → instantly back to flat-only, no code change, if this needs
  turning off in a hurry.
- Any disagreement between the two checks is still logged (not just on
  rejection) — real trades keep providing evidence as they happen, same
  visibility Step 4 had, just now the tree check actually decides.

**Verified before going live** — 5 cases against the real wired function,
using real data: (1) normal settled ownership unaffected — no regression;
(2) the widening case, CHI's contingent swap share now correctly tradeable
where flat would have rejected it; (3) an unrelated team still correctly
rejected — no false approval; (4) the DAL/TOR fix from Step 4 now correctly
recognized; (5) infra-failure fallback confirmed, no crash. Full test suite
green. Restarted, service healthy, `/api/picks` serving 480 rows normally.

**Not built, deliberately out of scope for now:** precise leaf-node-id
addressing (a team holding *multiple* contingent claims on the same pick
needs to specify which one it's conveying — not yet possible; today a team
holding any claim can convey it, which is coarser than the full spec design
but was judged an acceptable starting point). Cycle/depth guard from spec §4
also not built.

## CUTOVER STEP 6 — RE-TRADE HANDLING ✅ LIVE (2026-07-19)

Found immediately after Step 5 went live, by re-reading the write path
carefully: Step 3's node-construction only fires when a trade supplies a
*new* `protection`/`swap_with`. A plain re-trade of an already-contingent
pick (e.g. CHI conveying its stake in the 2028 BOS/CHI swap to some new
team) supplies neither — it would only flip the flat `OWNER` and leave the
registry's swap group/protected bands/binary chain unchanged, going stale on
every future re-trade the same way the one-off DAL/TOR case did in Step 4.
Since ownership enforcement is now what actually decides these trades, this
wasn't hypothetical — it was the exact scenario the redesign exists to
support, silently breaking display the moment it happened. Fixed same day.

- **`registry.handle_retrade(pick_key, from_team, to_team, node)`** —
  updates the registry in place: swap groups get `from_team` replaced with
  `to_team` in `priority`; protected bands get any `to: from_team` replaced;
  binary-chain nodes get any `better_to`/`worse_to` matching `from_team`
  replaced. `settled` picks are a no-op (the flat OWNER write already covers
  them). Coarse by design, matching the ownership check's coarseness:
  replaces *every* occurrence of `from_team` in the structure, since the
  trade payload names a pick, not a specific leaf-id (leaf-precise
  addressing is still the same known future step).
- **`legacy` picks are now actually frozen from re-trade** — the second gap
  flagged alongside this one, fixed together since it's the same underlying
  "handle re-trade of a contingent pick correctly" concern. Raises
  `RetradeBlocked` → surfaced as a real `HTTPException(422)` in the
  validation pass (`_check_not_legacy`), same shape and placement as the
  existing flat `FROZEN` check — a legacy pick was tagged legacy specifically
  because nothing could confidently model it; trading it further would only
  compound that, so it now actually blocks, not just displays a warning.
- Wired via `_check_not_legacy` (validation pass, blocks) and
  `_handle_pick_retrade` (mutation pass, fail-open sync) in
  `routers/transactions.py`. `test_retrade.py` covers all 5 node types.

**Verified against the real wired functions** with real data (not just the
registry module directly): re-traded CHI's actual 2028 BOS/CHI stake to MIA,
confirmed the registry updated (`[BOS,CHI]` → `[BOS,MIA]`), restored it
immediately after; attempted to trade the real 2027 DET legacy pick, got a
clean `HTTP 422` with the preserved reason string. Full test suite green,
service restarted, healthy.

## CUTOVER STEP 7 — VALIDATION HARDENING ✅ LIVE (2026-07-19)

Requested pass: re-read the write path carefully (same method that caught
Steps 4 and 6's bugs) and fix leaf-node-id addressing + the cycle/depth
guard. Findings and fixes:

**Real-data check first, not assumption:** swept all 480 picks for a team
occupying more than one distinct leaf within its own pick structure (the
scenario full leaf-addressing exists to solve) — **zero cases**. Building a
full trade-payload schema change (new leaf-id field, skill rewrite, etc.) for
a scenario with no live instances would be exactly the "push risk onto
humans for no current benefit" trade-off flagged when validation enforcement
was first discussed. Built the right-sized version instead:

- **`registry.count_leaf_occurrences(pick_key, team, node)`** — how many
  distinct leaves a team occupies in a pick's structure. 0/1 = fine, >1 =
  `AmbiguousLeaf`.
- **`AmbiguousLeaf`** — raised instead of guessing whenever a re-trade would
  be ambiguous. Checked in the **validation pass** (`_check_retrade_allowed`,
  renamed from `_check_not_legacy` since it now covers both), same
  placement/shape as the `FROZEN` and legacy checks — rejects with a clear
  `422` *before* any mutation, not discovered after the fact in the
  fail-open mutation pass. `handle_retrade` also checks internally (defense
  in depth) before mutating anything.
- Real leaf-node-id addressing (a trade specifying exactly *which* leaf) is
  still the fuller fix and still not built — this is the safe stand-in for
  as long as the ambiguous case has zero real instances.

**Cycle/depth guard:**
- `model.py` gained `validate_swap_group` and `validate_binary_chain` —
  SwapGroups/binary chains are container structures, not single conveyance
  nodes, so they needed their own validators alongside the existing
  `validate()`. `validate_binary_chain` does a proper DFS cycle check over
  the ref-graph (a `binary_swap` node's operand can reference another node's
  output) — a cycle here would make the resolver's dataflow evaluation
  recurse forever; now it's a clean `ConveyanceError` instead.
- **The write path never actually validated before persisting** — a real,
  separate gap found during the re-read: `add_protected`/`add_swap_group`
  wrote straight to the registry with no structural check at all. Both now
  validate before saving; a bad write is rejected outright rather than
  silently corrupting the store for the next resync to trip over.
  `handle_retrade` re-validates the specific structure it just mutated
  before persisting, too.
- **The curated seed data itself is now validated**, not just new write-path
  entries — `seed_registry_from_curated` runs the full registry through
  `_validate_registry` before saving. Re-ran it forced against the real,
  already-DAL-fixed `curated.py`: validates clean, confirmed the DAL/TOR fix
  survived the re-seed.

**Tests:** `test_validation_hardening.py` — swap group validator, cyclic vs.
acyclic chains, unknown-ref rejection, write-path-rejects-bad-input (and
confirms nothing gets written on rejection), `AmbiguousLeaf` on a synthetic
2-band conflict (confirms the registry is left untouched). All pass, plus
the full existing suite still green.

**Re-verified the two Step-6 real scenarios still work** after all the
hardening: CHI's real 2028 BOS/CHI re-trade to MIA still passes validation
and updates correctly; the real 2027 DET legacy pick is still blocked with
the same clear error. Service restarted, healthy, `/api/picks` serving 480
rows.

**Still not built** (unchanged from before this pass, now confirmed to be
the actual remaining items after a careful re-read): real leaf-node-id
addressing in the trade payload (only matters once a real ambiguous case
exists); the resolver (`resolver.resolve_all`) is not wired into any live
endpoint — nothing currently calls it with real draft positions during an
actual live draft event, so contingent picks display their candidate set but
nothing yet auto-resolves them to a single winner when a real draft happens.
That's a distinct, separate integration (into `draft.py`'s live-draft
system) from everything built in Steps 1–7.

## CUTOVER STEP 8 — enter-transaction SKILL UPDATE ✅ DONE (2026-07-19)

Actually out of date now, unlike at the Step 2 check: `nbn-today/.claude/commands/enter-transaction.md`
§4c said outright *"trades still submit and validate against the flat
`/api/picks` fields"* and pointed to `/api/picks-preview` as a separate,
richer reference — both wrong as of Step 5 (`/api/picks` itself is now
conveyance-sourced; `/api/picks-preview` is now just an identical alias).
Rewrote §4c: corrected the write-path claim, dropped the now-redundant
picks-preview pointer, and — the concrete gap — documented the two new `422`
rejection reasons a pick trade can hit (legacy-frozen, ambiguous-leaf) as
intentional signals needing manual reconciliation, not bugs to retry past.
The resolution WORKFLOW itself (`/api/picks/{team}`, pipe-compound matching)
is unchanged and still correctly documented — no behavior change needed
there, only the accuracy of what the skill claims about the system.

## Remaining known items after Steps 1–8 (not started, tracked for later)

- **Resolver not wired into any live draft event** — `resolver.resolve_all`
  is tested but never called with real draft positions anywhere in
  production; contingent picks display their candidate set but nothing
  auto-resolves them to a winner when a real draft happens.
- **`draft.py`'s live-draft show runs its own isolated picks file**
  (`draft-live-picks.csv`, forked from the real ledger on first write, reset
  between drafts) — deliberately out of scope since Phase 0. Wiring the
  resolver in eventually raises a real design question of how it interacts
  with that fork, not just where to call it from.
- **Leaf-node-id addressing** — deferred, zero real instances of the
  ambiguity it would solve (Step 7).
- **`upsert_pick` (manual admin edit endpoint) bypasses all of Steps 5–7's
  checks by design** — it's a raw override tool for committee corrections,
  not a simulated trade, so it has no `from_team`/`to_team` and can't use the
  same ownership/legacy/ambiguity logic. Can still make the registry go
  stale the same way trades used to before Step 6; acceptable given the
  endpoint's purpose, but worth knowing it's there.

## CUTOVER STEP 9 — REAL LEAF-NODE-ID ADDRESSING ✅ LIVE (2026-07-19)

Owner's call: build the actual addressing capability now, not just the
safety net around its absence — "in case this happens." Step 7's
`AmbiguousLeaf` guard is still there as the fallback, but a trade can now
*resolve* the ambiguity instead of only being told it exists.

- **`ownership.list_leaves(pick, store)`** — every addressable leaf on a
  pick, each with a stable `leaf_id` and a human-readable description.
  Deterministic, derived from structure each call, never stored — can't
  drift out of sync with what it describes. Format:
  `{year}-{round}-{orig}:{kind}:{position}` (`protected` → band index,
  `swap` → priority index, `binary` → `{chain_node_id}:{slot}`).
  `team_leaves`/`team_holds_leaf` are the team-scoped views. Recurses into
  **nested leaves** (a band/slot whose value is itself a node, per the
  spec's "a leaf can be a team or another node" rule) — a real correctness
  gap in the first draft of this rewrite: the initial version only handled
  plain team-string leaves and would have silently returned `False` for a
  team nested inside one. No real data hits this today, but it would have
  been a regression from the pre-Step-9 coarse check, which did handle it.
  Caught and fixed before shipping, not after.
- **`registry.handle_retrade(..., leaf_id=None)`** — when given, parses the
  leaf_id and mutates *only* that specific band/priority-slot/chain-slot,
  verifying it currently holds `from_team` first (mismatch → `ValueError`,
  nothing written). Without one, falls back to the Step-7 auto-detect
  behavior (fine when unambiguous, `AmbiguousLeaf` when not).
- **`TradeAsset` gained `leaf_id: Optional[str]`.** Wired through
  `_check_pick_ownership` (leaf-precise via `team_holds_leaf` when supplied),
  `_check_retrade_allowed` (rejects with the *specific available leaf_ids
  listed* when ambiguous and none supplied — this is the actual improvement
  over Step 7's plain refusal), and `_handle_pick_retrade` (passes through to
  the precise mutation).
- **`GET /api/picks`/`GET /api/picks/{team}` gained an additive `leaves`
  field** (`projection.py`) on every protected/swap/binary pick, so a
  `leaf_id` can be *discovered*, not just received in an error message —
  makes the TradeAsset docstring's claim ("get valid values from
  `GET /api/picks/{team}`") actually true rather than aspirational.

**Verified end-to-end through the real wired functions** (not just the
underlying modules), using a synthetic ambiguous pick (no real pick is
ambiguous, so this had to be synthetic — real-data-only testing isn't
possible for a feature with zero live trigger cases): ownership check passes
coarsely without `leaf_id`; `_check_retrade_allowed` rejects and lists both
`leaf_id` options with descriptions; resubmitting with the specific `leaf_id`
passes ownership precisely, passes retrade-check silently, and
`_handle_pick_retrade` mutates *only* the targeted band — confirmed the
other band holding the same team was untouched. Live: `GET /api/picks`
correctly exposes `leaves` for the real 2028 BOS/CHI swap pick.

**Test/parity fallout from the new field, all fixed same pass:** `leaves` is
additive (every row now carries it, `[]` for settled/legacy/extinguished) —
broke `test_projection_parity`'s exact-match check (every settled row now
has an extra key the old flat API never had) and `test_projection_full`'s
hardcoded key-set check. Both updated deliberately (assert `leaves == []`
for settled picks, then exclude it from the parity diff — an intentional
schema extension, not an accidental regression) rather than papered over.

**`enter-transaction.md` updated** — the ambiguous-leaf rejection section now
describes the actual resolution path (read the listed options or the pick's
`leaves` field, match the description against what `TEXT` means, resubmit
with `leaf_id`) instead of "surface it and stop." Payload shape listing in
§5 gained `leaf_id?`.

## CUTOVER STEP 10 — NESTED-LEAF WRITE SUPPORT + SWEEP ✅ LIVE (2026-07-19)

Owner pushback, correctly: leaving nested-leaf *mutation* unbuilt after
fully building nested-leaf *reading* wasn't a reasonable scoping call, it
was an inconsistent, half-finished mirror of my own design — "zero real
cases" justified not needing it urgently, not leaving it broken. Built
properly:

- **`registry._mutate_leaf_path`** — recursively mutates a leaf at any
  depth (protected bands-of-bands, or a nested-protected value inside a
  swap priority slot), verifying the current occupant matches `from_team`
  at each level before touching anything. Mirrors `ownership._expand_leaf`'s
  traversal exactly — same leaf_id format on both sides, so an id the read
  side produces is always parseable by the write side.
- `handle_retrade`'s `leaf_id` branch no longer raises on a nested id — it
  recurses.
- **Real 2-level nested test**: a band whose `to` is itself a `protected`
  node with its own 2 bands. Verified: mutating the deeply-nested leaf
  changes *only* that leaf — its sibling nested leaf and the unrelated
  top-level sibling band both confirmed untouched afterward.

**Comprehensive sweep done in the same pass** (searched the codebase for
every remaining "not yet"/"TODO"/"not built" marker instead of relying on
memory of what had been mentioned) rather than defer yet another discovery:

- Found and fixed one stale docstring (`AmbiguousLeaf`) still describing
  leaf-id addressing as unbuilt after Step 9 shipped it.
- Found and fixed a real, separate inconsistency: the resolver has
  supported the `__slot1__` slot-binding sentinel (spec §7.5, a nested
  protected node whose `on` binds to "whichever pick lands in this slot")
  since Phase 0, but `model.validate()` (added later, Step 7) didn't
  recognize the sentinel and would have rejected a legitimately-constructed
  node using it. Fixed: `model.SLOT_BINDING_SENTINEL`, valid only on a
  nested node (`_depth > 0`), rejected at the top level. No real data uses
  this yet, but the two pieces (resolver support, node validation) were
  built at different times and had drifted out of sync with each other —
  exactly the kind of asymmetry worth catching in one sweep rather than
  piecemeal.

Full test suite green (22 checks in `test_validation_hardening.py` alone),
service restarted, healthy.

**What's left is now genuinely just the two items with explicit prior
owner sign-off to defer** (resolver/live-draft wiring — deferred to next
draft; `upsert_pick` bypass-by-design — a different tool, not a trade
simulation, never claimed to share this checking). No other "narrow gap"
caveats remain from this sweep.

- 2028 ORL/MIL/WAS 1st 3-way priority
- 2028 MIA cluster (SAC/DAL/MIA/MEM/NYK/CHA) — model or legacy?
- 2030 NOP/MIL/POR/DET/HOU chain — model or legacy?
- Stale OWNER fields: 2030 CHI (owner=UTA), 2030 DAL (owner=MIA), 2032 DAL (owner=MIA)
- 2032 LAL 1st + 2032 MIN 1st — no cell note; likely settled(orig)

## TXN LINKAGE + UI SURFACE (2026-07-19) — owner asked "what created this pick"
for the 2028 SAS/NOP/DET binary chain (`snd`), which the site showed as bare
`Owner TBD` with an uninformative `"protected"` note and nothing explaining why.

**Root cause:** `GET /api/picks` already returns a `leaves` array (team +
description per possible outcome, via `ownership.list_leaves`) for every
protected/swap/binary pick, but (1) the curated binary-chain steps never
carried a `txn_id` back to the real trade that created them, and (2)
`teams/team.js`'s `buildPicksTable` never rendered `leaves` at all — only the
flat `notes`/`swap_owner`/`protected` fields, which for the new-model picks are
either stale or empty.

**Fixed both sides:**
- `picks_conveyance/curated.py`: `_bs()` now takes an optional `txn_ids`
  (list of `{id, date}`) per binary_swap step. Backfilled real citations found
  by grepping `transactions.json` for each chain: `snd` (Trade 71 2022-02-10 +
  Trade 46 2026-01-21, both fully cited), `nmpdh` (Trade 11 2024-06-22 + Trade
  29 2025-07-30 + Trade 46 2026-01-21 — 3 of 4 steps; the NOP/HOU tail step has
  no transactions.json record and is left `[]`, not guessed), `nop29` (Trade 30
  2026-07-10 + Trade 34 2026-07-14, both cited), `omw` (only the ORL/WAS leg
  found, Trade 23 2021-08-02; the MIL/ORL leg has no record). Missing
  citations are left as an honest `[]`, same policy as the rest of this
  project — a gap is recorded as a gap, not papered over.
- `picks_conveyance/ownership.py`: `list_leaves`'s binary branch now copies
  each step's `txn_ids` onto every leaf it produces, so they ride along in the
  `GET /api/picks` response with no separate endpoint needed.
- **The registry, not just curated.py, had to be patched** — `curated.py`'s
  dicts are only a one-time seed (`registry.seed_registry_from_curated` is a
  no-op once `draft-conveyance-registry.json` exists), so editing curated.py
  alone would never reach the live store. Patched the persisted registry's
  `binary_chains` entries directly (merging in `txn_ids` by step id, nothing
  else touched), then regenerated `draft-conveyance.json` via
  `seed_store --curated`. Full test suite green after; service restarted so
  the `ownership.py` code change took effect (`GET /api/picks` is read
  live from imported Python, not just the JSON store).
- `teams/team.js` (`buildPicksTable`): any pick row with a non-empty `leaves`
  array now gets a tooltip (click-to-toggle on touch, hover on pointer) on its
  Team cell, showing the real trade description(s) behind the swap/chain —
  fetched lazily from `GET /api/transactions/{id}` and cached across the page.
  Falls back to an explicit "not yet linked" message when a leg has no citation
  rather than showing nothing. Verified live via puppeteer against
  `nbn.today/teams/SAS/`: clicking the `NOP | DET | SAS` cell on the 2028 1st
  rows surfaces both Trade 71 and Trade 46's full text; spot-checked 4 other
  team pages for zero regressions/console errors.

**Found, not fixed (pre-existing, out of scope for this pass):** the 2026 BOS
1st (the BOS/WAS swap resolved and already drafted — `dybantsa-aj`) still
carries a stale `needs_structure: true` / `_flat` pair in the regenerated
store. Cause: `curated.apply_curated`'s `set_node` intentionally skips
overwriting `conveyance` once a pick has a `player` (correct — a drafted pick's
flat `OWNER` is the historical fact, not the curated snapshot), but it returns
before popping `needs_structure`/`_flat`, so the meta flag never clears for any
already-drafted pick that was also flagged at the original flat-CSV seed. Ends
up in the `meta.needs_structure` count but doesn't affect the pick's displayed
owner/player (both correct). Worth a one-line fix (pop the two keys before the
early return) next time this file is touched, not urgent enough to justify
re-touching it now.

## GROUP DEDUP FOR DISPLAY (2026-07-19, same day) — owner caught a real overcount

Follow-up to the txn-linkage fix above: on the SAS team page, the `snd` chain
(2028 SAS/NOP/DET) showed as **3 separate rows** in Owner TBD, which reads as
"SAS might get 3 picks here" — wrong. A swap group / binary chain spans N real
picks (one per member team), but resolves to a bijection: each team ends up
with **exactly one** of the N, never more. Showing all N rows on every member
team's page overstates it N-fold.

**Fix:**
- `picks_conveyance/projection.py`: new additive field `group_id` on the flat
  response — `"swap:{group_id}"` for swap-group picks, `"chain:{chain_id}"` for
  binary-chain picks, `None` otherwise. Every pick sharing a swap group or
  chain gets the same `group_id`, giving a stable dedup key without the
  consumer needing to parse `leaf_id` strings. Same additive-field pattern as
  `leaves` (Step 9) — broke the two exact-key-set tests (`test_projection_parity`,
  `test_projection_full`) the same way `leaves` did; fixed the same way (pop
  before comparison / add to `FLAT_KEYS`), plus new spot-checks that two
  same-group picks share a `group_id` and a settled pick's is `None`.
- `teams/team.js` (`buildPicksTable`): new `dedupeByGroup` collapses same-
  `group_id` rows in the "Owner TBD" section to one representative (preferring
  the row whose `orig` is the page's own team), tagging it with every `orig`
  it stood in for (`_groupOrigs`) so `buildLeavesTooltip` can open with "One
  {1st|2nd} conveys here — originally X/Y/Z's own pick, exact origin TBD"
  before the underlying trade text.
- Verified live via puppeteer: SAS's page now shows exactly 1 row for the
  `snd` chain (was 3) with the origin-list header in the tooltip; BOS/CHI's
  simple 2-team swap also correctly collapses to 1 row per page. Spot-checked
  12 team pages (SAS, DET, NOP, WAS, MIA, BOS, CHI, MIN, LAL, UTA, DAL, HOU)
  for row-count sanity (each dropped by exactly N-1 per group it belongs to,
  as expected) and zero console errors. Full `picks_conveyance` suite green,
  service restarted.

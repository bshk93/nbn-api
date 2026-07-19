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

**Remaining = the real cutover (separate step):** point picks-table consumers /
`/api/picks` at the store (behind a flag), and eventually the write path
(trade/transaction handlers) so new deals update the conveyance store directly.

- 2028 ORL/MIL/WAS 1st 3-way priority
- 2028 MIA cluster (SAC/DAL/MIA/MEM/NYK/CHA) — model or legacy?
- 2030 NOP/MIL/POR/DET/HOU chain — model or legacy?
- Stale OWNER fields: 2030 CHI (owner=UTA), 2030 DAL (owner=MIA), 2032 DAL (owner=MIA)
- 2032 LAL 1st + 2032 MIN 1st — no cell note; likely settled(orig)

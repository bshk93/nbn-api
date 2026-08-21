# Contract Extensions (§ 6.2 / § 6.3) — design spec

**Status: Phase A + E LIVE, deployed 2026-08-21.** `"extension"` is in
`_VALIDATORS`, the `create_transaction` type whitelist, and `_detail_models`.
`POST /api/validate/extension` and `POST /api/transactions` (type=`extension`)
both work against real production data. § 6.2 and § 6.3 checks run as
described in § 5 below, with the corrections listed in
`nbn-today/docs/poext-extension-pipeline.md` § 11 already applied in code.

**Not yet built:** the `/api/poext/*` committee pipeline (Phase C — claim,
negotiate, ballot, finalize), the `/extensions` team-facing page (Phase D),
and the § 4.5 six-month trade-freeze check in `_validate_trade` (Phase F). A
real extension today goes through the same manual hand-off free agency
already uses: the committee decides, the office enters it via `/transactions`
(or the transaction simulator), and the validator here is what tells them
whether it's legal. § 6.2 and § 6.3's rulebook badges should move from 👁 to
🔒+👁 to reflect this (see § 12 below).

Ground truth for "is this live" is the presence of `"extension"` in
`_VALIDATORS`, not this sentence — `docs/picks-conveyance.md` spent four days
claiming to be a spec after it was live, and became actively misleading.

---

## 1. Why the signing path cannot be reused

The obvious shortcut — "an extension is just a signing with later years" — is
wrong in a way that produces confident, backwards numbers. Measured against
production on 2026-08-07, submitting a real extension shape (Robert Williams,
BOS, new money starting 29-30) to `POST /api/validate/sign`:

```
BOS salary now            $145,567,284
"new_salary" it computed  $0
PROJECTED after signing   $126,667,284
=> implied change         -$18,900,000
roster 15 -> 16
[ERROR] BOS already has 15 standard players; release a player before signing.
```

It reports the team getting **$18.9M cheaper** for extending a player. The
cause is structural, not a missing check:

- `_validate_sign` reads Year 1 as `salaries[cur_season]`. An extension has no
  current-season entry (§ 6.2: "salary always kicks in the following season
  after finalization"), so `new_sal` is `$0`.
- `_signee_existing_hold` then backs out the player's *live contract salary*
  as a hold being replaced. It isn't a hold; it stays on the books.
- `_count_standard_roster` adds a body for a player already rostered.

An extension **adds future years to a live contract**. Every cap figure in
`_validate_sign` is built around **replacing a current-season figure**. These
are different operations that happen to both involve salary schedules.

## 2. Rules to implement

### § 6.2 eligibility
1. Existing contract ≥ 3 years total length.
2. Only in the final year of the contract, **or** Year 4 of a 5-year contract.
3. Player has ≥ 2 years service with the current team, **or** holds Bird Rights
   with the team via trade.
4. May not be offered at the league minimum.
5. Must comply with cap and Bird Rights rules.

### § 6.2 terms
6. Minimum length 2 years guaranteed.
7. Year 1 of the extended term ≤ **140% of prior salary**, or **140% of EAPS**,
   "whichever applies" — see §7, this is the spec's biggest open question.
8. Raises ≤ 8% of Year 1 of the extended term; extend-and-trade ≤ 5% (§ 3.9).
9. May include player option years.
10. Extension salary begins after the **final fully guaranteed year** of the
    original contract.

### § 6.2 / § 4.5 trade interaction
11. A player who signs an extension may not be traded for **6 months from the
    announcement date**. This is a *trade-side* check, not an extension-side
    one — see §6.
12. Conditional trade + extension: if Team B wants a player only on agreeing an
    extension, Team A submits the pitch on B's behalf. Process, not validation.

### § 6.3 submission windows
| Kind | Eligible | Window |
|---|---|---|
| Rookie Scale | Former 1st-round picks in the 4th and final year of the rookie deal | Up to the day before the regular season starts |
| Veteran (non-expiring) | Veterans on a multi-year deal, not in their final year | Up to the day before the regular season starts |
| Veteran (expiring) | Veteran in the final year | Any time up to June 30 |

Note rules 2 and § 6.3 disagree at the edges — rule 2 permits "final year or
Year 4 of a 5-year deal", while § 6.3's veteran-non-expiring row permits any
multi-year deal not in its final year. Flag for the committee (§9).

## 3. Data model

```python
class ExtensionDetails(BaseModel):
    player: str
    team: str
    kind: str                      # "rookie_scale" | "veteran" | "extend_and_trade"
    contract: ContractIn           # the EXTENDED TERM only — never the live years
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None
    announced_date: Optional[str] = None   # § 4.5 6-month clock; defaults to txn date
    attested_contract_start: Optional[str] = None  # season string; required by the
        # UI (not the schema) when _player_acquisition_index has no record for the
        # player. Decided 2026-08-19 — see nbn-today/docs/poext-extension-pipeline.md
        # § 2.3a/D15. Never trusted as a definite basis: always warn-severity, unless
        # it contradicts other partial ledger history the player does have (a trade,
        # a release), which promotes to error. Not persisted anywhere — re-asked on
        # every proposal for a player still missing a ledger record, deliberately
        # (rare enough that the real fix is the BACKLOG [P3] backfill, not a cache).
```

`contract.salaries` is keyed by the seasons the **new money** covers, and must
not overlap the existing contract. `contract.cap_holds` carries option years and
the trailing UFA/RFA hold exactly as a signing does — that machinery
(`_autofill_fa_hold_amounts`, the `_check_contract_raises` hold exclusion) is
already correct and should be reused unchanged.

Store on the bio the same way a signing does: append to `bio["contracts"]` with
`txn_id`, merge into `salaries`/`cap_holds`. **Do not** clear existing years.

## 4. The hard part: when does the new money start

Rule 10 keys off "the final fully guaranteed year of the original contract."
The data to compute that is largely absent (checked 2026-08-07):

| Field | Populated |
|---|---|
| `guaranteed` | 18 / 1018 bios |
| `guarantee_dates` | **0** |
| `guarantee_schedule` | 21 / 1018 |

So for ~98% of players there is no explicit guarantee record. Proposed rule,
in precedence order:

0. Discard seasons whose `cap_holds` entry is `UFA` or `RFA` outright. Those
   are not contract years at all — they are the trailing free-agent hold, and
   they *do* carry a salary figure (auto-filled by
   `_autofill_fa_hold_amounts`), so a naive "last salaried season" scan picks
   the hold and lands a year late. This step is easy to omit; an earlier draft
   of this spec did.
1. If `guaranteed` has entries, the last season with a **full** guarantee (i.e.
   `guaranteed[season] == salaries[season]`) is the final guaranteed year.
2. Otherwise, treat every remaining salaried season as guaranteed **except**
   those whose `cap_holds` entry is `NON_GTD`, `PLAYER_OPT`, or `TEAM_OPT`.
   The last remaining season is the final guaranteed year.
3. The extension's first season must be exactly `_season_shift(that, +1)`.

Rule 2 is the one that will actually fire, and it is a *convention* rather than
a reading of the rulebook — ratify it (§9) instead of letting it become de
facto law by shipping. Mismatch between the submitted first season and the
computed one should be an **error** naming both seasons: silently accepting it
puts money in the wrong league year and corrupts every downstream cap figure.

**Four things this convention does not handle.** None block Phase 1, but each
is a real limit, and the last one needs a decision:

- **Partial guarantees are invisible to it.** A season can be $15M with only
  $5M guaranteed — precisely what `guaranteed` / `guarantee_schedule` exist to
  record (18 and 21 bios respectively). Rule 2 treats such a year as fully
  guaranteed and so starts the extension a year late. Rule 1 covers this
  wherever the data exists; rule 2 cannot.
- **It has no time dimension.** `guarantee_dates` is "the date after which that
  season's salary becomes fully guaranteed, cleared once the date passes", so
  guarantee status *changes over the season*. The convention is timeless and
  would give different answers either side of such a date. Latent rather than
  live — `guarantee_dates` is empty for every player today — but it should be
  settled as "evaluated as of the extension's announced date" before that
  changes.
- **It cannot distinguish "never guaranteed" from "not recorded".** Both look
  like an absent key. With `guaranteed` populated for 18 of 1018 bios, nearly
  every answer rests on that ambiguity.
- **Option years are a judgment, not a fact — DECIDED 2026-08-18, fully.** An
  option year never counts as "guaranteed" for rule 2's computation, and is
  treated as **declined outright** by the act of signing the extension — not a
  probabilistic guess, an assumption baked into what an extension *is*. So on a
  deal ending `... GTD, GTD, PLAYER_OPT`, the extension's first season lands on
  the same season as that option year and **supersedes** it: the extension's
  salary figure for that season is what the player is actually paid, and the
  option is mooted rather than colliding with it. § 3's "must not overlap the
  existing contract" means the *guaranteed* years, not an option year the
  extension itself is declining on the player's behalf — those aren't locked
  terms, they're a contingency the extension resolves. No separate handling
  needed at merge time: the existing "current-season-onwards entries are
  replaced by each new contract" convention (CLAUDE.md, Player fields) already
  does exactly this when the extension's `salaries` are merged into the bio.

## 5. Validation checks

Naming follows the existing convention (`check` slug, `level`, plain-language
`message` citing the section). Severity rationale matters more than the list:
where a rule depends on data known to be thin, it warns.

| Check | Level | Rule | Notes |
|---|---|---|---|
| `extension_eligibility` | error / warning | 1, 2 | **Corrected 2026-08-19 — was wrongly rated pure error.** Not derivable from `salaries` + `cap_holds` alone: `salaries` is left-truncated at 25-26, so read that way 0 of 502 rostered players are eligible. Derive contract start from the ledger (`_player_acquisition_index`, same as § 3.8) instead. **Error** from a definite ledger basis, **warn** from an indefinite one (no ledger record) or from a submitter attestation with nothing to contradict it — a gap can only produce false *ineligibility*, never a false approval, so error-by-default would wrongly refuse legal extensions. **Error** if an attestation contradicts other partial ledger history the player does have. See `nbn-today/docs/poext-extension-pipeline.md` § 2.3/§ 2.3a/D15 for the full reasoning and the attestation mechanic. |
| `extension_window` | warning | § 6.3 | Needs a regular-season start date the system does not have (same gap as proration — see BACKLOG). Warn until there is one. |
| `extension_service` | error / warning | 3 | Reuse `_bird_tenure`. Error on a definite basis, warn on `trade_floor`/unknown — the same asymmetry § 3.8 uses, and for the same reason. |
| `extension_not_minimum` | error | 4 | Reuse `_min_salary_floor`. |
| `extension_min_length` | error | 6 | ≥ 2 years. |
| `extension_max_year1` | error / warning | 7 | Error against 140% of prior salary. **Warn** if the EAPS branch is the operative one — `eaps` is `0` for 26-27 and `None` for 25-26, so that comparison cannot currently be made at all. |
| `extension_raises` | error | 8 | Reuse `_check_contract_raises` with `pct=0.08`, or `0.05` when `kind == "extend_and_trade"`. Needs the function to take an explicit pct rather than the `bird_pct` bool. |
| `extension_start_season` | error | 10 | §4 above. |
| `extension_cap_position` | error | 5 | Hard cap / apron for the **first extended season**, not the current one. |

Deliberately **not** an extension check: the § 4.5 6-month trade restriction.
It belongs in `_validate_trade`, keyed off the most recent `extension`
transaction for that player. Putting it here would validate the wrong event.

## 6. Trade-side work (§ 4.5)

Add `_check_extension_trade_restriction` to `_validate_trade`: for every player
leg, look up the latest `extension` txn; if `announced_date + 6 months` is in
the future relative to the trade date, error. This is cheap once extensions
exist as ledger entries, and reuses the mtime-cached ledger index built for
`_bird_tenure` (`_player_acquisition_index`) — extend that index rather than
adding a second ledger scan.

## 7. Fact sheet

`_extension_fact_sheet`, keyed on the **first extended season**, not the
current one. Must not reuse `_signing_fact_sheet`, whose whole frame is
current-season replacement. Fields:

- current contract: years remaining, salary by season, final guaranteed year
- extended term: first season, years, salary by season, options, trailing hold
- the 140% ceiling, which basis it used (prior salary vs EAPS), and the headroom
- team salary in the first extended season, with the apron/hard-cap position
  **for that season** — noting that cap levels for 27-28 onward exist in
  `cap-levels.json` and are all zero, not merely absent (corrected — see
  `nbn-today/docs/poext-extension-pipeline.md` § 2.2). A zero threshold must
  report *cannot evaluate*, never a pass; only 25-26 and 26-27 carry real
  figures today

The invariant from the signing sheet carries over: **no parallel cap math.**
Build every figure from the same helpers the validator used.

## 8. Prerequisites and blockers

These are real, and two of them gate correctness rather than polish:

1. **EAPS is unset** (`26-27: 0`, `25-26: null`). Rule 7's second branch is
   uncomputable. Either the committee sets EAPS in Cap Settings, or rule 7 is
   redefined as prior-salary-only. Until then that half warns.
2. **Guarantee data is ~empty** — drives rule 10 via the §4 convention, which is
   now ratified (§4 point 4, §9 item 2) — the data sparsity itself is unchanged,
   but it's no longer an open-question risk, just a case where rule 2 fires far
   more often than rule 1.
3. ~~`/api/rookie-scale` returns `{}`~~ **CLOSED.** Populated for 2025 and 2026
   via `build/load_rookie_scale.py`. This item was stale — see
   `nbn-today/docs/poext-extension-pipeline.md` § 9 blocker 5.
4. **No regular-season start date** anywhere in the system. Blocks § 6.3
   windows; identical gap to the proration item.

## 9. Open questions for the committee

1. ~~Likely a non-issue — confirm the reading.~~ **DECIDED 2026-08-18: strict
   reading.** § 6.2 rule 2 is the hard eligibility test; § 6.3's three buckets
   are scheduling only, not an independent grant. Rookie Scale ("4th and final
   year") and Veteran-expiring are both final years; Veteran-non-expiring means
   exactly "Year 4 of a 5-year contract", the only non-final position § 6.2
   permits. § 6.3's looser gloss ("a multi-year deal not in their final year")
   is imprecise wording, not a wider rule. This is what makes the real
   population ~32 players rather than most of the league (§ 2.3 of
   `nbn-today/docs/poext-extension-pipeline.md`).
2. **DECIDED 2026-08-18, fully.** "Final fully guaranteed year" is the §4
   rule-2 convention, ratified, including the option-year sub-question: an
   option year never counts as guaranteed, and is treated as **declined
   outright** by the act of extending — so when rule 2 lands the extension's
   first season on the same season as a trailing option year, the extension
   **supersedes** it rather than colliding with it. No implementation gap:
   merging the extension's `salaries` into the bio already overwrites
   current-season-onward entries.
3. **Still open — needs to be put to the committee explicitly**, not inferred.
   When does 140%-of-EAPS apply instead of 140%-of-prior-salary? "Whichever
   applies" is not implementable as written, and EAPS is unset for every
   season on file regardless, so the branch can't be exercised even once this
   is answered without also setting EAPS in Cap Settings.
4. ~~Does an extension reset § 3.8 Bird tenure?~~ **DECIDED 2026-08-07: no.**
   An extension adds years to a live contract; the player never reaches free
   agency, so service accrues uninterrupted. Locked into the code — the
   exclusion is documented at the `release` branch of
   `_player_acquisition_index` and pinned by `tests/test_bird_rights_tenure.py`
   ("an extension is not recorded as an acquisition event"), because adding it
   there later "for completeness" would reset every extended player's clock and
   silently downgrade their tier.
5. ~~Do extensions count against the § 4.5 trade limit or any roster rule?~~
   **DECIDED 2026-08-07: no.** An extension consumes nothing — not a trade-limit
   slot, not a roster spot. `_validate_extension` must therefore not run any
   roster-count check, which is one of the things the signing path would have
   wrongly imported.
6. ~~Extend-and-trade cites § 8(e)(2), which does not exist.~~ **RESOLVED
   2026-08-07:** dangling citation removed from the § 3.9 table; the 5% figure
   stands. Defining the extend-and-trade mechanism itself is now a BACKLOG
   item — `ExtensionDetails.kind` already reserves `"extend_and_trade"` as the
   value selecting that ceiling.

## 10. Phasing

**Phase 1 — read-only.** `ExtensionDetails`, `_validate_extension`,
`_extension_fact_sheet`, `POST /api/validate/extension`, and an Extension tab in
the simulator. No apply path, no submission. Ships the whole rubric with zero
risk to live data, and lets the committee run real cases through it before
anything writes. This is the same order the simulator itself was built in and
it worked well.

**Phase 2 — submission.** Register in `_VALIDATORS`, the `create_transaction`
whitelist, `_detail_models`, and the apply dispatch; write `_apply_extension`.
Requires §9 questions 2 and 4 answered.

**Phase 3 — trade restriction.** § 4.5 6-month check in `_validate_trade`
(§6), which is only meaningful once Phase 2 is producing ledger entries.

**Phase 4 — windows.** § 6.3 enforcement, once a season-start date exists.

## 11. Tests

New `tests/test_extensions.py`, following the existing house style (plain
`check(name, cond)`, real production figures, a docstring explaining *why* each
rule is shaped as it is). Must cover:

- eligibility boundaries: 2-year prior contract rejected, 3-year accepted, Year
  3 of a 5-year rejected, Year 4 accepted, final year accepted
- start season: derived from `guaranteed` when present; derived from
  `NON_GTD`/option exclusion when not; mismatch errors
- 140% ceiling on both branches, including the EAPS-unset warn path
- 8% vs 5% raise ceilings, `extend_and_trade` selecting the latter
- minimum-length and not-at-minimum rejections
- service requirement reusing `_bird_tenure`, including the `trade_floor` warn
- **cap position measured in the first extended season, not the current one** —
  the specific bug the signing path would have introduced
- § 4.5: a trade 5 months after an extension errors, 7 months passes

Then replay every historical extension-shaped transaction, as was done for
§ 3.8 and § 3.12, and report the false-positive count before anything blocks.

## 12. Docs to update on ship

- `rulebook/index.html` § 6.2 and § 6.3 badges 👁 → 🔒 + 👁, with a "what the
  system enforces" paragraph (§ 3.8 has the pattern to copy)
- `nbn-today/CLAUDE.md` — validation endpoints table and transaction table
- `nbn-api/docs/transactions.md` — the type list
- the `enter-transaction` skill — it resolves freeform text into transaction
  types and will not know `extension` exists
- `BACKLOG.md` — closes "Extension window UI"; § 6.2 leaves the § 3.10/§ 3.11
  manual-review list
- this file's status header

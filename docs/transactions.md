# Transactions

Transactions are the canonical way to mutate player/roster/picks state. Each call to `POST /api/transactions` atomically applies the change and appends a record to `transactions.json`.

All transaction endpoints require the `rosters` role.

---

## Request shape

```json
{
  "type": "sign",
  "date": "YYYY-MM-DD",
  "description": "optional free text",
  "details": { ... }
}
```


---

## Transaction types

### `sign` — Free agency signing

Adds player to a team roster and sets their contract.

**Details:**
```json
{
  "player": "slug",
  "team": "GSW",
  "contract": {
    "type": "player",
    "salaries": { "25-26": "$10,000,000", "26-27": "$11,000,000" },
    "cap_holds": "26-27:UFA"
  }
}
```

**Mutates:**
- `player-bios.json` → sets `salaries`, `cap_holds`, `type`
- `{team}-roster.csv` → appends `{SLUG, TYPE}` row
- If player was previously `"dead"` and had dead cap in `salaries` (old format), migrates it to `dead_cap` before overwriting

**Validation:** player must not already be on any roster. Exception: a player currently held only as a UFA/RFA cap hold on another team (their contract has lapsed, they haven't been renounced) can be signed elsewhere directly — the old team's hold and roster row are cleared automatically, no separate renounce needed.

**Auto-computed trailing cap hold (§ 3.10):** if `contract.cap_holds` names a season as `UFA`/`RFA` but `contract.salaries` didn't already price that season, the dollar figure is computed automatically from the last real contract year's salary × the Bird-rights percentage (150%/190% Full Bird, 130% Early Bird, 120% Non-Bird), clamped to that player's min/max salary for the hold season, and written into `player-bios.json`'s `salaries`. Bird tier comes from `bird_rights_type` if given, else is derived from how many consecutive prior seasons the player's own `contracts` history shows them with this team (3+ = Full Bird/QVFA, 2 = Early Bird/EQVFA, else Non-Bird). A Full Bird hold additionally needs to know whether the prior salary is above or below that season's EAPS — if EAPS isn't on file yet for that season, pass `eaps_assumption: "above"` or `"below"` or the request 422s; the resulting figure is recorded as a placeholder in `player-bios.json`'s `cap_hold_notes[season]` pending real EAPS. Not yet implemented: the rookie-scale-final-year (250%/300%) and coming-off-a-minimum-contract carve-outs in § 3.10 — both still need an explicit dollar figure in `contract.salaries` for now. Same logic applies to `offer_sheet` (via its internal `sign` call), `convert_twoway`, and `sign_pick`, all of which also accept `bird_rights_type`/`eaps_assumption`.

---

### `offer_sheet` — Extend an RFA offer sheet (§ 3.15)

Records an offer being **extended**. Signs nobody: the player stays on the
incumbent's roster with their RFA hold, and the offering team is charged a cap
hold until it resolves.

**Details:**
```json
{
  "player": "slug",
  "offering_team": "LAL",
  "contract": { "type": "player", "salaries": { "26-27": "$8,000,000", "27-28": "$8,500,000" } },
  "signing_method": "ntmle",
  "bird_rights_type": null
}
```

No `outcome` — that is a separate decision by a different team, and collapsing
the two attributed the incumbent's choice to whoever typed the transaction.
`retaining_team` and `deadline` are stamped server-side (the incumbent is a fact
about who holds the RFA rights, not a claim the offering team gets to make).

**Validation** — judged against the **offering** team, who commit the money
whatever the incumbent later decides:
- Hard-fail: player must carry an `RFA` hold **for the current season**; the
  offer must cover 2+ years; `offering_team` can't be the player's own team; the
  player must not already have an open offer.
- Soft `checks`: hard cap and signing-method funding for the offering team,
  § 3.12 minimum salary, § 3.8 Bird declaration, and roster space at
  `ROSTER_MAX` (they must be able to honour the offer if it goes unmatched).

`_rfa_eligibility` is the single eligibility rule, shared with the validator.
It tests the **current** season deliberately: `_apply_sign` refuses a cross-team
signing unless the player holds a current-season UFA/RFA hold, so an "earliest
hold" reading would accept a player still under contract — the offer would
validate, hold real cap room, then fail at the decision.

**Mutates:** nothing but the ledger. The § 3.15 hold is *derived* from the open
offer by `_pending_offer_hold`, not written anywhere, so it can't be left behind.

---

### `offer_sheet_decision` — Match or pass (§ 3.15)

Resolves an open offer. This is the step that signs somebody.

**Details:**
```json
{
  "offer_id": "id of the offer_sheet being answered",
  "outcome": "matched"
}
```

`matched` keeps the player with the incumbent; `not_matched` sends them to the
offering team. Both sign the player to the *offer's* terms — the contract is
read from the offer, never resubmitted, so the two can't disagree.

**Validation:** the offer was already scored against the offering team. What's
new here is the incumbent's side — matching is allowed over the Cap, but a hard
cap still binds (§ 1.3), and nothing checked that before the split. Hard-fails an
unknown, already-resolved, or wrong-typed `offer_id`.

**Mutates:** everything `sign` does, applied to the signing team. The stored
record carries `player`, `teams`, `signing_team`, `outcome` and the offer's
contract. The bio's contract history entry gets `offer_sheet_outcome` and
`offer_sheet_id`.

**Open offers:** `GET /api/offer-sheets/open` (public, optional `?team=`) lists
every unresolved offer with its deadline and `overdue` flag. Derived from the
ledger — an offer is open exactly when no `offer_sheet_decision` names it — so
there is no second store to drift.

> **Why the split is safe this time.** An earlier two-step design was merged into
> one transaction precisely because an offer could be submitted with no follow-up,
> silently leaving the player on nothing but their old RFA hold (it bit Dyson
> Daniels' matched sheet in production). The difference now is that pending is a
> state the system can see and price: `_open_offer_sheets` enumerates them, the
> offering team pays a cap hold the whole time, and the UI nags past the deadline.
> Do not reintroduce a path that records an offer without those.

**Legacy entries.** Three combined-era `offer_sheet` records carry their own
`outcome` and were applied on submission. They are read as already-resolved
everywhere (`_open_offer_sheets` skips them, `_player_acquisition_index` still
credits their signing), and are not migrated.

---

### `pick` — Draft pick selection

Same as `sign` but also assigns the pick in `draft-picks.csv`.

**Details:**
```json
{
  "player": "slug",
  "team": "GSW",
  "pick": { "year": 2026, "round": 1, "orig": "GSW", "pick_number": 7 },
  "contract": {
    "type": "player",
    "salaries": { "26-27": "$8,000,000" },
    "cap_holds": "26-27:RFA"
  }
}
```

**Mutates:**
- `player-bios.json` → same as sign
- `{team}-roster.csv` → same as sign
- `draft-picks.csv` → sets `PLAYER` field on the matching pick row

---

### `option` — Option decision

Accept or decline a player/team option.

**Details:**
```json
{
  "player": "slug",
  "decision": "accept",
  "option_type": "PLAYER_OPT",
  "year": "26-27",
  "cap_hold_type": "UFA",
  "cap_hold_amount": "$5,000,000"
}
```

`cap_hold_type`, `cap_hold_amount`, `bird_tier`, and `eaps_assumption` are only used when `decision = "decline"`. `cap_hold_amount` is optional — if omitted, it's auto-computed the same § 3.10 way `sign` does (see above), using the last remaining salary and `bird_tier` (or a tenure-derived default if `bird_tier` isn't given either). Pass `cap_hold_amount` explicitly to override the computed figure.

**Mutates (`accept`):**
- `player-bios.json` → removes the option year from `cap_holds` (converts `PLAYER_OPT`/`TEAM_OPT` entry to no hold)

**Mutates (`decline`):**
- `player-bios.json` → replaces option year's `cap_holds` entry with `cap_hold_type` (e.g. `UFA`); sets `salaries[year]` to `cap_hold_amount` (given or auto-computed)

---

### `release` — Release a player

Removes player from their roster and converts guaranteed money to dead cap.

**Details:**
```json
{
  "player": "slug"
}
```

**Dead cap logic:** for each future salary year —
- `TEAM_OPT`, `UFA`, `RFA` holds → $0 dead cap (skipped)
- `NON_GTD` → if released on/after guarantee date: full salary; else `guaranteed[year]` if set; else $0
- All other years (fully guaranteed, PLAYER_OPT) → `guaranteed[year]` if set, else full salary

**Mutates:**
- `player-bios.json` → sets `dead_cap` (merged with any existing), clears `salaries`/`cap_holds`/`guaranteed`/`guarantee_dates`, sets `type = "dead"`
- `{team}-roster.csv` → removes the player's row

---

### `renounce` — Renounce a free-agent cap hold (§ 3.10)

Clears a UFA/RFA cap hold, takes the player off the roster and makes them an
unsigned free agent. No dead cap: a player carrying only a hold is under no
contract, which is what separates this from `release`.

**Details:**
```json
{
  "player": "slug"
}
```

The team is never submitted — it's resolved from whichever roster carries the
player's `SLUG` row.

**Validation** — `_renounce_eligibility` is the single § 3.10 test, shared by the
validator, the apply path and `POST /api/validate/renounce`, so the simulator and
the roster page can't offer a renounce the apply path would reject:
- **Error (blocks):** the player must be on a roster and their *earliest* cap hold
  must be a `UFA`/`RFA` no later than the upcoming FA season. A player under
  contract is released (§ 5.1); one with a live option has it declined (§ 6.1)
  first. Note this is a test on the earliest hold, not on the current season's —
  a player finishing a contract sits as a hold for *next* season, and that's the
  ordinary renounceable case.
- **Warning (forceable):** resulting standard-roster count against the § 2.1
  14-player minimum and the § 2.1a 12-player charge floor (priced at the rookie
  minimum); and the § 3.8 Bird tenure being forfeited. These warn rather than
  block because the rulebook charges for them rather than forbidding them.

**Mutates:**
- `player-bios.json` → drops `salaries`/`guaranteed`/`guarantee_dates`/
  `guarantee_schedule` from the hold season on, clears the renounced `cap_holds`,
  sets `type = ""`. Prior-season earnings are preserved as career history.
- `{team}-roster.csv` → removes the player's row
- The team's trading-block entry is scrubbed
- Stored record gets `"team"` and `"_snapshot"` — the pre-renounce values of every
  field above. **The snapshot is the only restore source**; a renounce erases the
  very state a ledger replay would need, so it's recorded at the event rather than
  reconstructed later.

**Owner self-serve:** `POST /api/self/renounce` lets a team's *owner* run this
from their own roster page. Narrower than this endpoint on three counts: the team
is derived from the player's roster row and matched against the caller's owner
tenure (never taken from the body), `force` is unavailable so error-level checks
are fatal, and the date is server-stamped so a transaction can't be backdated into
a prior league year. Ownership is a tenure *position*, not a role — every FO
member carries the team role, but a GM or coach fails `auth.is_team_owner`.

---

### `rescind_renounce` — Restore a renounced free agent (§ 3.10)

Undoes a `renounce` from its stored snapshot. Serves both the rulebook case
(holds renounced to fund an RFA offer sheet the retaining team then matched) and
plain correction of a mistaken renounce — including one an owner entered from
their roster page.

**Details:**
```json
{
  "txn_id": "id of the renounce being undone"
}
```

**Validation:**
- **Hard-fail (not forceable):** no such transaction; it isn't a `renounce`; it
  predates snapshotting; it was already rescinded; or the player has since joined
  a roster (restoring them would duplicate the player and overwrite a real
  contract with a stale hold).
- **Warning (forceable):** § 3.10's restrictions — rescinding may not move the team
  from under the cap to over it, nor increase the figure of a team already over.
  Warnings rather than errors because the same mechanism is the correction path,
  and refusing to restore a player because the restoration costs room would leave
  the books wrong a different way.

**Mutates:**
- `player-bios.json` → restores every snapshotted field verbatim
- `{team}-roster.csv` → re-adds the player's row (appended; roster CSV order is
  not meaningful)
- The source renounce gets `"_rescinded": true`, so it can't be undone twice and
  the Transactions log drops its `undo` button

Not covered: the trading-block entry scrubbed by the renounce isn't restored.

---

### `void_player` — Void a player with no cap hit

Removes a player from their roster with no dead cap and no remaining obligation. Rulebook §5.1 limits this to: real-life retirement (incl. medical), the player not being present in the current NBA 2K build, or an unwanted 2nd-round pick/UDFA voided by the July 31 deadline. Manual-review only — no automatic checks, and no automatic trigger; always entered by hand.

**Details:**
```json
{
  "player": "slug",
  "reason": "retired"
}
```

**Mutates:**
- `player-bios.json` → clears `cap_holds`; drops future `salaries`/`guaranteed`/`guarantee_dates`/`guarantee_schedule` (past seasons preserved); sets `type = ""`
- `{team}-roster.csv` → removes the player's row
- Does **not** write to `{team}-deadcap.csv` — that's the whole difference from `release`

---

### `set_hard_cap_level` — Manually set/clear a team's hard cap

Sets a team's season hard-cap level directly, going through the transaction log instead of the silent `PUT /api/team-state/{team}` side door (which leaves no reason/author trail — see the "17 teams with empty hard_cap_reason" audit finding). Unlike the automatic trigger path (`_maybe_set_hard_cap`, used internally by `sign`/`trade` for BAE/NTMLE/TMLE absorption), this can also lower or clear the level. No automatic triggers are wired to this type yet (e.g. sign-and-trade, mid-season buyout re-sign — rulebook §1.4 rows C/D remain manual).

**Details:**
```json
{
  "team": "GSW",
  "level": "first_apron",
  "reason": "NTMLE trade absorption"
}
```

`level` must be `"first_apron"`, `"second_apron"`, or `"default"` (clears back to no team-specific cap — only the league-wide absolute hard cap applies).

**Mutates:**
- `team-state.json` → sets `hard_cap` (`null` for `"default"`) and `hard_cap_reason` for the team's current season

---

### `trade` — Multi-team trade

Moves players and/or picks between teams.

**Details:**
```json
{
  "transfers": [
    {
      "from_team": "GSW",
      "to_team": "LAL",
      "assets": [
        { "type": "player", "slug": "curry-stephen" },
        { "type": "pick", "year": 2027, "round": 1, "orig": "GSW" }
      ]
    },
    {
      "from_team": "LAL",
      "to_team": "GSW",
      "assets": [
        { "type": "player", "slug": "james-lebron" }
      ]
    }
  ],
  "legality": "legal"
}
```

Optional pick fields: `protection` (sets `PROTECTED`), `swap_with` (sets `SWAP_OWNER`).

Optional sign-and-trade fields: `is_sign_and_trade` (bool, default `false`), `sign_and_trade_txn_id` (optional string). A sign-and-trade is submitted as **two separate transactions** — a `sign` (with `signing_method: "sign_and_trade"` for display) followed by this `trade` — there's no atomic combined type. Nothing in the data lets the API infer that a sign and a later trade are related, so the caller must declare it explicitly with `is_sign_and_trade: true`; `sign_and_trade_txn_id` optionally records the paired `sign` transaction's `id` so the log is traceable from the trade back to the contract that enabled it.

**Mutates:**
- For each player asset: removes from `from_team` roster CSV, adds to `to_team` roster CSV
- For each pick asset: updates `OWNER` in `draft-picks.csv`; if `protection`/`swap_with` set, updates those fields too
- Does not touch `player-bios.json` (salaries/contract stay as-is)
- If `is_sign_and_trade` is true: every team that is the `to_team` of at least one **player** asset in this trade is hard-capped at First Apron in `team-state.json` (rulebook §1.4 row C) — teams only receiving picks in the same trade are not affected

---

### `convert_twoway` — Convert two-way to standard contract

Promotes a two-way player to a full player contract. No dead cap, no roster move.

**Details:**
```json
{
  "player": "slug",
  "contract": {
    "type": "player",
    "salaries": { "26-27": "$2,000,000" },
    "cap_holds": "26-27:UFA"
  }
}
```

**Mutates:**
- `player-bios.json` → sets `type = "player"`, replaces `salaries`/`cap_holds`, clears `guaranteed`/`guarantee_dates`
- `{team}-roster.csv` → clears the `TYPE` field on the player's row

**Validation:** player must currently have `type = "two-way"` and be on a roster.

---

## Deleting a transaction

`DELETE /api/transactions/{id}` removes the record from `transactions.json` but **does not reverse the bio/roster mutations**. Use it only to clean up test entries or mistakes where you've already manually corrected the data.

---

## Stored transaction record

After applying, the record stored in `transactions.json` adds a `team` (or `teams` for trades) field to `details` for filtering:

```json
{
  "id": "abc123",
  "type": "sign",
  "date": "2026-04-10",
  "created_by": "Se-Ho Kim",
  "created_at": "2026-05-20T02:20:08Z",
  "description": "",
  "details": {
    "player": "curry-stephen",
    "team": "GSW",
    "contract": { ... }
  }
}
```

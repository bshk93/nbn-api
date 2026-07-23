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

---

### `offer_sheet` — RFA offer sheet (§ 3.15)

Records **and immediately applies** a team extending an offer sheet to another team's restricted free agent. This is one atomic transaction, not two — once `outcome` is known there's no independent decision left, so it directly signs the player (internally calling the same logic as `sign`, above) to `retaining_team` if `matched` or `offering_team` if `not_matched`. Entered after the outcome is known (no live 48-hour match-period clock is modeled — see rulebook § 3.15 for the real-time procedure GMs follow out of band).

> An earlier version of this type only *recorded* the offer and required a separate follow-up `sign` transaction to actually apply the contract. That two-step design shipped a real bug on first use: an offer sheet was submitted with `outcome: "matched"`, the follow-up `sign` was never submitted, and the player's contract silently never updated — nothing in the flow forced or even flagged the second step. `offer_sheet` is now self-contained specifically because of that failure.

**Details:**
```json
{
  "player": "slug",
  "offering_team": "LAL",
  "contract": {
    "type": "player",
    "salaries": { "26-27": "$8,000,000", "27-28": "$8,500,000" },
    "cap_holds": {}
  },
  "outcome": "matched"
}
```

`outcome` is `"matched"` (retaining team keeps the player, signed to `contract`) or `"not_matched"` (player signs with `offering_team` on `contract`). `retaining_team` is not submitted — it's derived from whichever roster currently carries the player's `SLUG` row.

**Validation (hard-fail, not the soft `checks` path):**
- Player must currently carry an `RFA` (not `UFA`) cap hold for the upcoming season.
- `offering_team` must differ from the retaining team.
- `contract.salaries` must cover at least 2 years (§ 3.15's "at least 2 guaranteed years" — this only checks year count, not that each year is actually guaranteed).

**Mutates:**
- `player-bios.json` → same as `sign`, applied to whichever team the outcome resolves to
- `{team}-roster.csv` → same as `sign` (old team's roster row cleared automatically if `not_matched`)
- Stored record gets `"teams": [offering_team, retaining_team]` (same convention as `trade`) for `GET /api/transactions?team=`, and each bio's `contracts` history entry gets `signing_method: "offer_sheet_matched"` or `"offer_sheet_not_matched"` so it's distinguishable from an ordinary re-sign.

**Not yet built:** rescission (§ 3.10/§ 3.15) — if the offering team renounced other cap holds to afford the offer and the retaining team matches, the rulebook lets them restore those renouncements. No `rescind_renounce` transaction type exists yet; do it by hand.

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

`cap_hold_type` and `cap_hold_amount` are only used when `decision = "decline"`.

**Mutates (`accept`):**
- `player-bios.json` → removes the option year from `cap_holds` (converts `PLAYER_OPT`/`TEAM_OPT` entry to no hold)

**Mutates (`decline`):**
- `player-bios.json` → replaces option year's `cap_holds` entry with `cap_hold_type` (e.g. `UFA`); sets `salaries[year]` to `cap_hold_amount`

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

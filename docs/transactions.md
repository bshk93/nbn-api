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

**Validation:** player must not already be on any roster.

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

**Mutates:**
- For each player asset: removes from `from_team` roster CSV, adds to `to_team` roster CSV
- For each pick asset: updates `OWNER` in `draft-picks.csv`; if `protection`/`swap_with` set, updates those fields too
- Does not touch `player-bios.json` (salaries/contract stay as-is)

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

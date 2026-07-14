# Data Model

All files live in `/var/lib/nothing-but-stats/`.

---

## player-bios.json

The source of truth for all player identity and contract data. Keyed by slug (`"last-first"` lowercase, hyphenated).

```json
{
  "achiuwa-precious": {
    "name": "ACHIUWA, PRECIOUS",
    "pos": ["PF", "C"],
    "dob": "1999-09-19",
    "college": "Memphis",
    "country": "Nigeria",
    "draft_year": 2020,
    "draft_round": 1,
    "draft_pick": 17,
    "photo_url": "https://...",
    "height": "6'8\"",
    "weight": 225,
    "wingspan": "7'1\"",
    "type": "player",
    "cap_holds": "27-28:PLAYER_OPT,28-29:UFA",
    "salaries": {
      "25-26": "$6,312,716",
      "26-27": "$6,628,352"
    },
    "guaranteed": {},
    "guarantee_dates": {},
    "dead_cap": {}
  }
}
```

### Field reference

| Field | Type | Description |
|---|---|---|
| `name` | string | `"LAST, FIRST"` uppercase |
| `pos` | string[] | Subset of `["PG","SG","SF","PF","C"]` |
| `dob` | string | ISO date `"YYYY-MM-DD"` or `""` |
| `college`, `country` | string | |
| `draft_year`, `draft_round`, `draft_pick` | int\|null | |
| `photo_url`, `height`, `wingspan` | string | |
| `weight` | int\|null | Pounds |
| `type` | string | `"player"`, `"two-way"`, `"dead"`, or `""` |
| `cap_holds` | string | Comma-separated `YEAR:TYPE` pairs — see below |
| `salaries` | object | `{"YY-YY": "$N"}` — current contract years only |
| `guaranteed` | object | `{"YY-YY": "$N"}` — guaranteed portion for NON_GTD years |
| `guarantee_dates` | object | `{"YY-YY": "YYYY-MM-DD"}` — date salary becomes fully guaranteed |
| `dead_cap` | object | `{"YY-YY": "$N"}` — accumulated dead cap from releases |

### cap_holds format

Comma-separated `YEAR:TYPE` pairs, e.g. `"26-27:PLAYER_OPT,27-28:UFA"`.

Valid types: `UFA`, `RFA`, `PLAYER_OPT`, `TEAM_OPT`, `NON_GTD`

- `PLAYER_OPT` / `TEAM_OPT` — option years (salary entry exists in `salaries`)
- `UFA` / `RFA` — cap hold after contract ends (salary entry = hold amount)
- `NON_GTD` — non-guaranteed year; `guaranteed` and/or `guarantee_dates` may have additional data

### type field

- `"player"` — standard contract, on a roster
- `"two-way"` — two-way contract, on a roster
- `"dead"` — released; `salaries` is empty, `dead_cap` holds the amounts
- `""` — prospect / unsigned player

### Backward compat note

Players released before the `dead_cap` field was introduced have their dead cap stored in `salaries` (with `type = "dead"`). The frontend handles both cases; if patching these manually, move the values from `salaries` to `dead_cap` and clear `salaries`.

---

## Roster CSVs — `{abbr}-roster.csv`

New format (post-migration):

| Column | Description | Example |
|---|---|---|
| `SLUG` | Player slug | `barnes-scottie` |

All other player data (name, pos, age, ovr, type, salary) is joined from `player-bios.json` and `ovr-history.json` at render time. OVR is **not** stored in the roster CSV — it comes exclusively from `ovr-history.json`. Contract `type` (`"two-way"` etc.) lives in `player-bios.json`, not the roster CSV — transaction handlers that rewrite a roster row write only `SLUG`; any other key is silently dropped on write (`extrasaction="ignore"`).

One file per team: `atl-roster.csv`, `bkn-roster.csv`, … `was-roster.csv`.

---

## Picks CSVs — `{abbr}-picks.csv` and `draft-picks.csv`

| Column | Description | Example |
|---|---|---|
| `YEAR` | Draft year | `2026` |
| `ROUND` | Round | `1st` or `2nd` |
| `TEAM` | Description shown on team page | `Own`, `from NYK`, `SAC/DAL` |
| `TYPE` | Direction | `own` or `acquired` |

`draft-picks.csv` is the master picks file used by the draft board. It has additional columns:

| Column | Description |
|---|---|
| `ORIG` | Origin team abbreviation (uppercase) |
| `OWNER` | Current owner abbreviation |
| `PICK` | Pick number (or blank if TBD) |
| `PLAYER` | Slug of player who was drafted (blank until used) |
| `PROTECTED` | Top-N protection number, or blank |
| `SWAP_OWNER` | For swap picks: the other team |
| `NOTES` | Free-text notes |

---

## ovr-history.json

Keyed by player slug. Each value is a list of entries sorted ascending by date.

```json
{
  "adebayo-bam": [
    { "date": "2026-05-19", "ovr": 89 }
  ]
}
```

The most recent entry is the player's current OVR. `GET /api/ovr/current` returns just the latest entry per slug as `{ slug: ovr }`.

---

## transactions.json

Array of transaction objects, newest first.

```json
[
  {
    "id": "6545d971961aa835",
    "type": "sign",
    "date": "2026-04-10",
    "created_by": "Se-Ho Kim",
    "created_at": "2026-05-20T02:20:08Z",
    "description": "",
    "details": { ... }
  }
]
```

See **[transactions.md](transactions.md)** for the `details` shape of each type.

---

## trading-block.json

Keyed by team abbreviation (uppercase). Each value is a list of `{player, notes}` objects.

```json
{
  "OKC": [
    { "player": "Desmond Bane", "notes": "Looking for younger talent / draft capital" }
  ]
}
```

---

## trade-exceptions.json

Trade Exceptions (TPEs), rulebook § 4.1a. Keyed by team abbreviation (uppercase); each value is a list of exception objects for that team. Not itself a tradeable asset — stays with the team that banked it.

Creation/consumption is currently manual (`POST`/`PATCH`), ported over from the league's roster/cap spreadsheet — nothing computes a TPE from a trade transaction, and the trade builder does not yet draw one down.

```json
{
  "NYK": [
    {
      "id": "a1b2c3d4e5f6",
      "amount": 4200000,
      "remaining": 4200000,
      "acquired_date": "2026-01-15",
      "expires_date": "2027-01-15",
      "note": "From the Smith-for-Jones trade"
    }
  ]
}
```

`GET` responses add a computed `expired` bool (`expires_date` < today). Endpoints:
- `GET /api/trade-exceptions` — all teams (public)
- `GET /api/trade-exceptions/{team}` — one team's list (public)
- `POST /api/trade-exceptions/{team}` — create; body `{amount, acquired_date?, expires_date?, note?}` (`rosters` role). `expires_date` defaults to `acquired_date + 365 days`.
- `PATCH /api/trade-exceptions/{team}/{id}` — update `remaining`/`expires_date`/`note` (`rosters` role)
- `DELETE /api/trade-exceptions/{team}/{id}` (`rosters` role)

---

## cap-levels.json

Keyed by season string. Used to display cap context on the site.

```json
{
  "25-26": {
    "cap": 154647000,
    "apron1": 195945000,
    "apron2": 207824000
  }
}
```

---

## tokens.json

```json
{
  "<64-char hex token>": {
    "name": "Se-Ho Kim",
    "roles": ["admin"]
  }
}
```

---

## Read-only CSVs (generated externally — do not edit)

These are written by the R simulation pipeline and consumed by the front-end. Do not modify them through the API.

| File(s) | Used by |
|---|---|
| `{abbr}-seasons.csv`, `{abbr}-players.csv` | Team pages |
| `player_seasons.csv`, `player_seasons_playoffs.csv`, `player_awards.csv` | Player profiles |
| `standings-history.csv`, `playoff-brackets.csv` | Standings page |
| `game-highs-{p,r,a,s,b,3pm}.csv` | Game highs leaderboards |
| `totals-{p,r,a,s,b,3pm}.csv` | Totals leaderboards |
| `h2h-alltime.csv`, `h2h-owners.csv`, `h2h-playoffs.csv` | Head-to-head page |
| `owner_stats.csv` | Owners page |
| `hof.csv` | Hall of Fame |
| `league-history.csv` | League history |
| `playoff-classics.csv`, `playoff-series-margins.csv` | NBNTV Classics |

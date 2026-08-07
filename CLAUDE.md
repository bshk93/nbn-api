# NBN API — Claude Guide

FastAPI backend for the NBN fantasy basketball league. Runs as a systemd service on port 8001, proxied through nginx at `nbn.today/api/`.

## Source & data

- **Code:** `main.py` (single file, ~1200 lines)
- **Data directory:** `/var/lib/nothing-but-stats/` — all JSON and CSV files live here
- **Web root symlink:** `/var/www/nbn.today` → symlinked CSVs, served statically

## Service management

```bash
sudo systemctl restart nbn-api
systemctl status nbn-api
journalctl -u nbn-api -f          # live logs
journalctl -u nbn-api -n 50       # last 50 lines
```

## Auth

Bearer token. Tokens stored in `tokens.json` as `{ "<hex>": { "name": "...", "roles": [...] } }`.

Roles: `admin` (full access + token management), `rosters` (edit players, rosters, picks, transactions), per-team roles (`atl`, `gsw`, etc.) for trading block only.

`admin` satisfies any role check. See `get_token_info` / `has_role` / `require_role` in `main.py`.

**Admin token** is in `tokens.json`. To create a token:
```bash
curl -X POST https://nbn.today/api/tokens \
  -H "Authorization: Bearer ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Name", "roles": ["rosters"]}'
```

## Data model reference

→ **[docs/data-model.md](docs/data-model.md)** — player-bios.json fields, CSV formats, all JSON files

## Transaction system

→ **[docs/transactions.md](docs/transactions.md)** — every transaction type, what each mutates, request shapes

## Discord slash commands

`routers/discord.py` — a single signed endpoint, `POST /api/discord/interactions`,
that Discord calls for every slash command. Stateless reads against the static
stats CSVs; responses return inline (type 4). No bot gateway / long-running
process.

- **Signature:** every request is Ed25519-verified against `DISCORD_PUBLIC_KEY`
  (PyNaCl). Until that env var is set the endpoint 401s everything — which is the
  correct response to Discord's endpoint-validation probe.
- **Config:** `DISCORD_PUBLIC_KEY` is loaded from `/home/skim/projects/nbn-api/.env`
  (gitignored) via the unit's `EnvironmentFile`. After editing `.env`,
  `sudo systemctl restart nbn-api`.
- **Adding a command:** add its definition to `COMMANDS` in
  `register_discord_commands.py` and a handler branch in `dispatch()` in
  `routers/discord.py`, then re-run the register script. Set `DISCORD_GUILD_ID`
  to register to one server instantly (global registration takes ~1h).
- **Commands:** all take a `player` name option (fuzzy-resolved against players
  who have stats rows); each replies with a team-colored embed (name links to the
  profile, photo as a full-width `image` so it never narrows the tables).
  - `/stats` — season-by-season averages (reg + playoffs), one row per season;
    mid-season trades list teams chronologically joined with `/` (e.g. `BKN/ORL`).
  - `/career` — career totals + per-game averages (reg + playoffs).
  - `/awards` — honors (rings, MVP/DPOY/etc., All-NBN, All-Star…), counted with
    the seasons earned, ordered by `_AWARD_ORDER`.
  - `/team team:<abbr> [season:<YY-YY>]` — a team-season's roster + per-game
    stats (sorted by scoring) with a record/seed/margin header from
    `data/{abbr}-seasons.csv`. Season defaults to the most recent on record.
    `TEAM_NAMES` maps abbr → full name; title links to the team page.
  - `/leaders [stat] [season] [team]` — top-10 regular-season totals for a stat
    (`LEADER_STATS`), aggregated from `player_seasons.csv` with optional
    season/team filters. `stat` is a Discord choice list (default Points).
  - `/compare player1 player2 [season]` — two players' per-game averages side by
    side; career by default, or one season.
  - `/standings [season]` — East/West standings from `standings-history.csv`,
    sorted by `SEED_NUM`. Defaults to most recent season.
  - `/playoff-series year team1 team2` — series result from
    `playoff-brackets.csv` (round via `ROUND_NAMES`, 4 = Finals). Colored to the
    winner. `year`/season args accept `YY-YY` or a 4-digit year (`_norm_season`).
    Also appends game-by-game scores + series P/R/A leaders read from the raw
    `allstats-playoffs-{YY}.csv` in NBS_DATA_DIR (`_series_games_and_leaders`);
    gracefully omitted if that file is missing.
  - `/nbyen-leaders` — net worth leaders: liquid NB¥ + NBN Wall Street equity,
    via `invest.get_all_holdings()` (matches the site's valuation, incl. short
    equity). Shows NET / CASH / STK columns, ranked by net worth.
  - `/h2h team1 team2` — all-time regular-season + playoff record (the `h2h-*.csv`
    matrices; cell is "W-L" from row team's perspective).
  - `/champions` — every season's champion + runner-up, derived from
    `PLAYOFF_RESULT` in `standings-history.csv` (NOT `league-history.csv`, whose
    schema is multi-row-per-season).

  **Economy + identity** (these write NB¥ / members.json):
  - Members are linked to Discord via an optional `discord_id` field in
    `members.json`. `member_by_discord_id()` reverse-looks-up the signed invoker
    id (trustworthy) to a member. The interaction payload's invoker is passed
    into `dispatch(data, invoker)`.
  - `/whoami` — (ephemeral) your Discord id + linked member.
  - `/link member user` — (admin) set a member's `discord_id`. Authorized by
    `DISCORD_ADMIN_ID` env (bootstraps the first admin) OR an already-linked
    `admin`-role member (`_is_discord_admin`). One Discord id maps to one member.
  - `/nbyen` — (ephemeral) your NB¥ from `member-balances.json`.
  - `/trades [member]` — per-stock realized + unrealized P&L via
    `invest.compute_member_pnl()` (realized replayed from trade history,
    unrealized = open positions marked to current market price). Defaults to the
    invoker's linked member; embed is green/red by net P&L; `•` marks open
    positions.
  - `/tip user amount [message]` — moves NB¥ via `tips.perform_tip()` (the shared
    money-move, also behind `POST /api/tips`). Both parties must be linked. Posts
    a public confirmation; `message` shows for tips ≥ 25 (`TIP_MESSAGE_THRESHOLD`).
  - Setup: set `DISCORD_ADMIN_ID` in `.env` (the owner's Discord user id, from
    `/whoami`) so the first `/link` is authorized; then link everyone else.

  - `/help` — ephemeral embed listing every command, grouped. Hand-maintained in
    `_HELP_GROUPS`; update it when adding/removing a command.

## Google Sheets export (`POST /api/trade-sheet`)

`routers/google_sheets.py`. Takes an `.xlsx` upload (multipart: `file`, `name`),
uploads it to Google Drive with `mimeType: application/vnd.google-apps.spreadsheet`
so Drive converts it to a native Sheet, shares it `anyone: reader`, and returns
`{id, url}`. Used by the Transaction Simulator's "Create Google Sheet" button (trade mode) — the
browser builds the workbook (`nbn-today/transaction-sim/xlsx.js`) and posts it here.

Requires a valid member token (`get_token_info` — any role). It is a write into
a real Google account, so it is deliberately not public.

**Credential.** One long-lived refresh token for a single Google account, in
`$NBS_DATA_DIR/google-oauth.json` (mode 0600):
`{client_id, client_secret, refresh_token, folder_id}`. Held server-side on
purpose — that is what lets GMs export with no OAuth consent screen. Scope is
`drive.file`, i.e. per-file: the credential can only see files it created
itself, never the rest of that Drive. That also keeps it off Google's
sensitive-scope list, so no app verification is needed.

Set it up once with `authorize_google.py` (its docstring has the Cloud Console
steps). Without the file the endpoint returns 503 with an explanatory message
and the front end falls back to a plain `.xlsx` download. If Google starts
returning 502 "credential was revoked", re-run the same script.

Access tokens are cached in-process until ~60s before expiry; there is no
persistent token store beyond the refresh token.

## Docs discipline

Keep `CLAUDE.md` and `docs/` in sync with every code change. If a change affects an endpoint, a data field, a transaction type, or any behavior described in the docs, update the relevant doc in the same commit.

## Key design notes

- All writes are synchronous; no database. Concurrent writes to the same file are guarded by `_txn_lock` and `_picks_lock` threading locks.
- `player-bios.json` is the source of truth for all player identity and contract data. Roster CSVs only store `SLUG`; everything else is joined from bios (and OVR from `ovr-history.json`) at render time. Transaction handlers that rewrite a roster row (`_apply_sign` etc.) write a bare `{"SLUG": ...}` — any other key is dropped on write (`extrasaction="ignore"`), so don't rely on a roster CSV carrying anything beyond `SLUG`.
- Dead cap is stored in `bio["dead_cap"]` (dict keyed by season). Old players released before this field existed may have dead cap in `bio["salaries"]` instead — the frontend handles both.
- OVR history is separate from bios: `ovr-history.json` keyed by slug, each value a list of `{date, ovr}` entries.

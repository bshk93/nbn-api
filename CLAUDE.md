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

→ **[docs/transactions.md](docs/transactions.md)** — all 6 types, what each mutates, request shapes

## Key design notes

- All writes are synchronous; no database. Concurrent writes to the same file are guarded by `_txn_lock` and `_picks_lock` threading locks.
- `player-bios.json` is the source of truth for all player identity and contract data. Roster CSVs only store `SLUG` + `OVR` (+ optional `TYPE`); everything else is joined from bios at render time.
- Dead cap is stored in `bio["dead_cap"]` (dict keyed by season). Old players released before this field existed may have dead cap in `bio["salaries"]` instead — the frontend handles both.
- OVR history is separate from bios: `ovr-history.json` keyed by slug, each value a list of `{date, ovr}` entries.

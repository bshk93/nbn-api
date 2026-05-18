# NBN API

A lightweight backend that lets rosters members edit team rosters and draft picks directly on nbn.today — no more Google Sheets.

## How it works

The API runs as a systemd service on the same server as nbn.today. When a rosters member clicks the **Edit** button on any team page, the frontend sends their token to the API, which verifies it and writes the updated CSV back to `/var/lib/nothing-but-stats/`. Since the team pages read those same CSV files, the changes are live immediately.

## Roles

| Role | Can do |
|------|--------|
| `rosters` | Edit any team's roster and draft picks |
| `admin` | Everything rosters can do + create/revoke tokens |

There is one admin (you). Everyone else gets a `rosters` token.

---

## Adding a new rosters member

Run this command on the server, replacing the name:

```bash
curl -X POST https://nbn.today/api/tokens \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Their Name", "role": "rosters"}'
```

The response looks like:

```json
{
  "token": "a3f8c2d1e9b4...",
  "name": "Their Name",
  "role": "rosters"
}
```

Send them the `token` value over Discord (DM only, not in a public channel). They paste it into the browser prompt the first time they click Edit on any team page. It gets saved in their browser and they never have to enter it again.

## Listing all active tokens

```bash
curl https://nbn.today/api/tokens \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

Returns a list of all tokens with their associated name and role. Token values are shown in full so you can identify and revoke specific ones.

## Revoking a token

If someone leaves the rosters or their token is compromised, revoke it by passing the token value:

```bash
curl -X DELETE https://nbn.today/api/tokens/TOKEN_TO_REVOKE \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

Revocation is instant — their next save attempt will fail and their browser will clear the stored token automatically, prompting them to enter a new one if they try again.

---

## How rosters members use it

1. Go to any team page on nbn.today (e.g. `/teams/GSW`)
2. Click the small **Edit** button next to the **Roster** or **Draft Picks** heading
3. The first time, a prompt appears asking for their token — they paste it in and click Continue
4. The table becomes editable:
   - Click any cell and type to change a value
   - Click **×** on a row to delete it
   - Click **+ Add row** at the bottom to add a new player or pick
5. Click **Save** when done, or **Cancel** to discard changes

The token is saved in their browser (localStorage), so they only ever enter it once per browser. If they switch computers or clear their browser data, they'll need to enter it again — just send the same token again, no need to create a new one.

---

## Roster column reference

When editing a roster, the columns are the raw CSV fields:

| Column | Description | Example |
|--------|-------------|---------|
| `PLAYER` | Full player name | `Scottie Barnes` |
| `POS` | Position(s) | `SF/PF` |
| `AGE` | Age as integer | `24` |
| `OVR` | Overall rating | `86` |
| `TYPE` | Contract type | `player`, `two-way`, or `dead` |
| `CAP_HOLDS` | Option/hold flags | `29-30:PLAYER_OPT,30-31:UFA` |
| `25-26`, `26-27`, etc. | Salary by year | `$38,661,750` |

**CAP_HOLDS format:** comma-separated `YEAR:TYPE` pairs. Valid types: `UFA`, `RFA`, `PLAYER_OPT`, `TEAM_OPT`, `NON_GTD`. Leave blank if no holds.

## Picks column reference

| Column | Description | Example |
|--------|-------------|---------|
| `YEAR` | Draft year | `2026` |
| `ROUND` | Round | `1st` or `2nd` |
| `TEAM` | Team origin or destination | `Own`, `SAC/DAL`, `from NYK` |
| `TYPE` | Pick direction | `own` or `acquired` |

---

## Server details

- **API process:** `systemctl status nbn-api`
- **Logs:** `journalctl -u nbn-api -f`
- **Restart:** `sudo systemctl restart nbn-api`
- **API code:** `/home/skim/projects/nbn-api/main.py`
- **Token file:** `/var/lib/nothing-but-stats/tokens.json` (JSON, human-readable)
- **Port:** 8001 (internal only, proxied through nginx)

## If something breaks

**"Invalid token" error on save:** The token was rejected. Check that the token in `tokens.json` matches what was sent. Revoking and reissuing a fresh token is the easiest fix.

**API not responding:** Check `systemctl status nbn-api`. If it's stopped, `sudo systemctl start nbn-api`. If it keeps crashing, check `journalctl -u nbn-api -n 50` for the error.

**Changes not showing up:** The API writes directly to the CSV files in `/var/lib/nothing-but-stats/`, which are symlinked into the web root. If the write succeeded (Save showed "Saved!") the page should reflect the change on the next load. Hard refresh (Ctrl+Shift+R) if needed.

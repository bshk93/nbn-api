# Discord transaction backfill (in progress)

Goal: pull historical trade-announcement messages out of the league's old Discord
channels and log them so they show up on player profiles (`/players?p=slug` ->
"Transaction History", already wired to `GET /api/transactions?player=`).

**Scope, deliberately narrow:** only player movement (from_team -> to_team) plus the
original announcement text as a note. Not attempting to reconstruct draft picks,
cap/salary state, or run any legality validation for these — they predate the live
transaction system and current roster data already reflects their effects.

Status as of 2026-07-10: **361 of 485 messages submitted to the live transaction
log** (322 auto-resolved + 39 human-reviewed low-confidence promotions out of
"flagged"). Picking this up again should start at "Next steps" below, which now
covers only the remaining 124.

---

## Pieces built

### 1. `historical: true` flag on `POST /api/transactions` (routers/transactions.py)

Added `historical: bool = False` to `TransactionIn`. When set (only valid for
`type: "trade"`), `create_transaction` routes to `_create_historical_trade()`
instead of `_apply_trade()` + `_run_validation()` — it validates that team abbrs
are real and player slugs exist in `player-bios.json`, then appends the record
directly via `_append_transaction()`. **No roster/cap/team-state mutation happens.**
This was necessary because the normal trade path re-applies moves live — replaying
a 2021 trade through it would incorrectly re-move players who have since moved
again per current data.

Stored record shape (reuses the existing `trade` schema so player-page rendering
needs zero changes):
```json
{
  "type": "trade",
  "date": "2021-03-26",
  "description": "<verbatim original Discord message>",
  "details": {
    "transfers": [{"from_team": "X", "to_team": "Y", "assets": [{"type": "player", "slug": "..."}]}],
    "teams": ["X", "Y"],
    "historical": true
  }
}
```

Tested end-to-end against the live service (create -> shows under
`?player=` filter -> rosters unaffected -> deleted). Error paths tested: unknown
player slug, unknown team, `historical: true` with a non-trade type.

**This is uncommitted**, on branch `draft-trades-manual-advance`, mixed with
unrelated in-progress changes to `constants.py`, `roster_picks.py`,
`docs/data-model.md`. The `transactions.py` diff itself is clean and isolated
(+45 lines, `git diff --stat routers/transactions.py`) — safe to split out and
commit separately whenever that's wanted.

### 2. `fetch_discord_transactions.py` — pulls raw Discord history

Read-only against Discord. Requires `DISCORD_BOT_TOKEN` (already in
`nbn-api/.env`, loaded via `EnvironmentFile=` in the systemd unit — source it
into a subshell rather than printing the file, e.g.
`(set -a; source .env; set +a; venv/bin/python3 fetch_discord_transactions.py)`
so the token never lands in a transcript). Also needs the **Message Content
Intent** enabled in the Discord dev portal (done — user flipped it this
session).

Auto-detects the guild (bot must be in exactly one), lists channels, matches
any channel whose name contains "transaction" (case-insensitive — catches both
`transactions-2025` and `2024-transactions` orderings), paginates full history
per channel via `GET /channels/{id}/messages`, strips each message down to
`{id, channel, author, timestamp, edited_timestamp, content, attachments}`
(edits already resolve to final content via the API), writes one JSON file per
channel to `/var/lib/nothing-but-stats/discord-transactions-raw/`.

**The bare `#transactions` channel (no year) was deliberately excluded** — per
the user, those trades are already reflected in the live `/api/transactions`
log. Confirmed no date overlap either way: the last dated-channel message
(`transactions-2025.json`) is 2026-02-06; the earliest live `type=trade` entry
is 2026-06-20.

Current fetch covers **485 messages across 6 channels**: `2020-transactions`,
`2021-transactions`, `2022-transactions`, `2023-transactions`,
`2024-transactions`, `transactions-2025` (2020-11 through 2026-02-06).

### 3. `resolve_discord_trades.py` — parses raw messages into candidate trades

Deterministic parser (regex + fuzzy name matching), not an LLM pass — the
format turned out regular enough (484/485 messages start `Trade N:` followed by
one `TEAM receives: assets` block per side) that this was worth automating
rather than reading all 485 by hand. Still refuses to guess on anything
genuinely ambiguous — see buckets below.

Key logic:
- **Team resolution**: alias table (`TEAM_ALIASES`) mapping abbr + city +
  nickname (e.g. "Sixers", "Cavaliers", "Knicks") to the 30 abbrs. Messages use
  all of: abbr, full city name, and nickname — inconsistently.
- **Block splitting**: regex finds `TEAM (receives|recieves)?: ...` at line
  starts (handles the "recieves" typo and the plain `TEAM: assets` form with no
  "receives" word), captures until the next such line or end of message.
- **Asset splitting**: paren-depth-aware comma split, plus a second pass
  splitting on top-level " and "/" & " (messages often list two players joined
  that way instead of with a comma).
- **Pick filtering**: regex excludes anything that looks like a pick reference
  (4-digit year, "1st"/"2nd", "pick", "swap", "protect", "right(s) to", bare
  `#NN` pick-number fragments, `NN-NN` protection ranges) — since scope is
  players only, picks are just dropped, not parsed.
- **Player resolution**: exact match on `"first last"` (built from
  `player-bios.json`'s `"LAST, FIRST"` field) after stripping periods
  (`D.J.` -> `DJ`) and `Jr/Sr/II/III/IV` suffixes; falls back to `difflib`
  fuzzy match (cutoff 0.85); falls back further to **last-name-only** match
  when a surname uniquely identifies one bio (handles nicknames like "Mo Bamba"
  -> `bamba-mohamed`, "Svi Mykhailiuk" -> `mykhailiuk-sviatoslav" without a
  hand-built nickname dictionary).
- **From/to assignment**: only attempted for exactly 2 team blocks (the other
  side's players came from this side, and vice versa). **3+ team trades are
  never auto-resolved** — the text only states what each team *receives*, not
  who sent what, and that can't be reconstructed without historical roster
  state we don't have. These are flagged for a human to resolve per-trade.

Output: `/var/lib/nothing-but-stats/discord-transactions-resolved.json`, shape
`{"resolved": [...], "flagged": [...]}`. `resolved[i]` has `date`,
`description` (verbatim original text), `channel`, `discord_id`, `transfers`
(ready to paste into the `historical` trade payload as-is). `flagged[i]` has
the same fields plus `reasons: [...]`.

## Current numbers (485 messages)

| Bucket | Count | Status |
|---|---|---|
| Auto-resolved | 322 | **Submitted 2026-07-10** via `submit_discord_trades.py` |
| Low-confidence player match, cleanly promotable | 39 | **Submitted 2026-07-10** — re-parsed via `parse_message` directly (not persisted by `resolve_discord_trades.py`, which only writes `reasons` for flagged entries, not `blocks`), validated (unique ids, valid teams/slugs, no self-trades), eyeballed the full player-name list for sanity. Saved as `discord-transactions-promoted.json` — kept separate from `resolved` so a from-scratch rerun of the resolver can't clobber this reviewed work. |
| 3-5 team trades | 60 | Still flagged. Needs a human decision per trade on from/to — can't be automated (see "3+ team trades" note below) |
| Picks-only trades | 39 | Not actionable — no players moved, nothing to log. Correctly a no-op, not really "flagged" |
| Unresolved player text | 15 | Real gaps: missing comma in source text ("Caris Levert Jaxson Hayes" concatenated), or player never got a `player-bios.json` entry (e.g. Dzanan Musa, John Petty — journeymen who may never have been rostered in-league) |
| Malformed parse (0 or 1 team blocks found) | 15 | Format outliers, not yet individually reviewed |
| Low-confidence match, other reasons also present | ~25 | Flagged for a reason beyond just the fuzzy match (e.g. also multi-team) — folded into whichever other bucket applies once that's resolved |

**Submission mechanics:** `submit_discord_trades.py` tracks submitted
`discord_id`s in `discord-transactions-submitted.json`, written immediately
after each successful POST (not batched) — safe to rerun any time, already-
submitted entries are skipped. `DRY_RUN=1` previews, `--limit N` for
spot-testing, `--promoted PATH` to point at a reviewed-promotions file
(defaults to `discord-transactions-promoted.json`).

**Why 3+ team trades can't auto-resolve:** the Discord message format only
states what each team *receives*, never who sent it. With exactly 2 team
blocks, "the other block" is the only possible sender, so from/to can be
inverted safely. With 3+, a player in "Team A receives" could have come from
any other side — not derivable from text alone (`resolve_discord_trades.py:172`).

---

## Next steps (in rough order)

1. ~~Decide a de-dup safeguard before submitting anything.~~ Done —
   `submit_discord_trades.py`'s state-file tracking, described above.
2. ~~Skim the 322 auto-resolved + cleanly-promotable low-confidence trades,
   submit via `POST /api/transactions` with `historical: true`.~~ Done —
   361 submitted 2026-07-10.
3. Work through the 60 multi-team trades one at a time — each needs a human
   (or an LLM read of the full message plus judgment) to assign from/to per
   asset, since the text doesn't state it explicitly.
4. Decide what to do about the 15 unresolved-player messages: skip the
   unresolvable player (log the rest of the trade without them), create
   missing `player-bios.json` stub entries first, or skip the whole message.
   No decision made yet.
5. Read the 15 malformed-parse messages directly and either hand-fix the
   parser regex for whatever pattern they share, or hand-enter them.
6. Revisit the ~25 low-confidence matches that also had another issue, once
   that other issue's bucket (3) or (5) is resolved.
7. Once nbn-api changes are validated end-to-end, split the `transactions.py`
   diff out of the `draft-trades-manual-advance` branch's other WIP and commit
   it separately (or ask whether it should just ride along with that branch).

## Open item unrelated to the backfill itself

`NBN_ADMIN_TOKEN` got printed in plaintext into this session's transcript on
2026-07-10 (user ran `export $DISCORD_BOT_TOKEN` — missing `=`, which bash
expands to bare `export`, dumping every exported var). Treat it as burned;
rotate via `POST /api/members/{admin-username}/rotate-token` when convenient.
Not yet done as of this writing.

## Rerunning from scratch

```bash
cd /home/skim/projects/nbn-api
(set -a; source .env; set +a; venv/bin/python3 fetch_discord_transactions.py)
venv/bin/python3 resolve_discord_trades.py
```

# Discord transaction backfill (in progress)

Goal: pull historical trade-announcement messages out of the league's old Discord
channels and log them so they show up on player profiles (`/players?p=slug` ->
"Transaction History", already wired to `GET /api/transactions?player=`).

**Scope, deliberately narrow:** only player movement (from_team -> to_team) plus the
original announcement text as a note. Not attempting to reconstruct draft picks,
cap/salary state, or run any legality validation for these — they predate the live
transaction system and current roster data already reflects their effects.

Status as of 2026-07-10 (session 2): **378 of 485 messages submitted to the
live transaction log.** Remaining 107 breaks down as 67 multi-team trades
(needs human from/to judgment, see below) + ~40 picks-only/malformed no-ops
with nothing to log either way. Picking this up again should start at "Next
steps" below.

## Session 4 (2026-07-11): manual multi-team trade resolution + a real live-data bug

Went through the 64 flagged multi-team (3/4/5-team) trades with the user,
newest-first (their idea — recent trades are easier to recall). Method that
worked well: for each unresolved player, check `GET /api/transactions?player=`
for their last known team, and cross-check against `players/player_seasons.csv`
(the per-season `TEAM` column splits into multiple rows the season a player
changes teams, which independently confirms a move even for players with zero
logged transactions — e.g. founding-era players who've simply never been
traded/signed since the log began).

**Hard rule, learned the hard way:** a player's `from_team` must always be one
of the teams the trade *text itself* names as a receiver — never an outside
team, even when that's what their last tracked transaction says. Caught live:
Jabari Walker's last transaction had him on SAC, but the trade only named
NYK/SAS/ORL; using SAC produced a bogus 4th team. The real answer (confirmed
by the user) was ORL — and the very next multi-team trade worked through
(one day earlier chronologically, since we're going backwards) turned out to
be the missing SAC→ORL leg that had simply not been backfilled yet. **A
mismatch between tracked history and the trade's named participants means a
transaction is missing upstream, not that a new team belongs in the trade.**
Full writeup: [[feedback-trade-backfill-team-inference]].

**This rule surfaced a real bug already live in production**, not just a risk
for future reconstructions: 3 previously-submitted historical trades had
silently collapsed a 3-team trade into 2, dropping the 3rd team's players
entirely (a Jan 2026 DET/NOP/SAS trade missing DET; an Aug 2025 CHI/BKN/MIA
trade missing CHI; a Jul 2023 NOP/SAC/GSW trade missing NOP). None of the
three were in `discord-transactions-promoted.json` — they'd been entered
off-pipeline in an earlier session, bypassing `resolve_discord_trades.py`'s
parser (which correctly flags all three as unresolvable 3-team trades and
would never have auto-promoted them wrong). Fixed via delete + reconstruct +
resubmit.

**Audit for more of these** (worth rerunning after any future off-pipeline
edit): re-parse every live historical trade's stored `description` with
`resolve_discord_trades.BLOCK_RE`, diff the set of teams the *text* mentions
against the stored `details.teams`. Ran clean (0 mismatches) across all 388
live historical trades after the 3 fixes.

Result: 8 of the 64 multi-team trades resolved and submitted this session
(388 total historical trades now live, up from 378 — includes the 3 bug
fixes, which replace existing records rather than add new ones, so the net
new content is 8 trades + 3 corrections). 56 multi-team trades remain,
still going newest-first next time this is picked up.

## Session 3 (2026-07-11): expanded to fa-news (signings + options)

Extended the same backfill idea to the league's `fa-news` channels (dated
`2020-fanews`/`2021-fanews` — no hyphen — and `2022-fa-news`…`2025-fa-news`).
The bare current-era `fa-news` channel was excluded per the same policy as
`#transactions`: it's live/current, not historical.

**Scope decision (user, 2026-07-11):** signings (including UDFA signings once
they resolve to an actual signing, not the procedural round/window
announcements) **and** team/player option accept/decline decisions. Releases,
renounces, two-way conversions, retirements, and trade-block chatter that also
live in these channels are explicitly out of scope for this pass.

**API change:** `historical: true` on `POST /api/transactions` was trade-only;
extended to also accept `type=sign` and `type=option` (`_create_historical_sign`,
`_create_historical_option` in `routers/transactions.py`, sharing a new
`_append_historical` helper with the trade path). Validates player slug (and
team, for signs) exist, skips `_apply_sign`/`_apply_option` and validation
entirely — same rationale as trades: replaying would re-apply moves current
data already reflects. Contract terms are **not** reconstructed from text
(`contract: {}` on historical signs) — the verbatim `description` carries the
prose deal terms; only `option`'s structured fields (decision/type/year) are
populated since those drive display. Tested end-to-end against the live
service (create sign + option, verify via `?player=`, confirm roster
untouched, delete) before submitting anything at scale.

**Pipeline:** `resolve_discord_fa_signings.py` reuses `resolve_discord_trades.py`'s
player-resolution machinery (name_map, nickname aliases, false-match blocklist)
unchanged via import, but needed its own team-matching: fa-news messages
consistently spell out the full "City Nickname" ("The Houston Rockets sign…")
rather than the single-token team references trade headers use, so
`FULL_TEAM_NAMES` adds the 30 full names as extra aliases. Messages also
frequently @-mention a team's Discord role instead of naming it in text — the
guild's role names happen to already match team nicknames 1:1, so a one-time
`GET /guilds/{id}/roles` fetch produced `discord-role-team-map.json`
(role_id -> abbr), and role mentions are substituted with the plain abbr
before pattern matching (never in the stored `description`, which stays
verbatim). One message can yield multiple candidate records (one signing
each), unlike trades where a message is one transaction — `resolved`/`flagged`
entries are per-candidate, several can share a `discord_id`.

Patterns covered: team-first and player-first signings (`"TEAM sign(s)
PLAYER…"`, `"PLAYER (has signed|signs) with (the) TEAM…"`, with or without a
leading "The"), single- and dual-player team-option accept/decline (`"…'s
YYYY-YY team option…"`, `"X's and Y's options of A and B respectively"`), and
player-option accept/decline (`"PLAYER accepts his player option…"`, `"PLAYER
(has) opted out of…"`). A single message containing **both** signing and
option language (e.g. "declining his Player Option and signing a new deal") is
flagged as compound rather than guessed at — only 4 in the whole corpus. A
one-off list-format batch of ~30 option decisions with no team/type stated per
line (2024-06-12, `2024-fa-news`) is flagged whole rather than guessed at
per-player.

**Live-overlap handling:** unlike the transactions channels (clean handoff —
last dated message predates the first live trade), `2025-fa-news` runs through
2026-05-21, past when live `type=sign` records start (2026-04-10). Confirmed
by cross-reference that the 2 auto-resolved + 3 flagged candidates dated on/
after the cutoff are exact duplicates (by player + date) of 5 of the 8 live
`sign` records. `resolve_discord_fa_signings.py` excludes anything dated
`>= LIVE_SIGN_CUTOFF` ("2026-04-10") into its own `excluded_live_overlap`
bucket rather than silently dropping it.

**Result after two rounds, 2081 messages across 6 channels:**

| Bucket | Count |
|---|---|
| Resolved (submitted) | 1352 (1123 sign, 229 option) |
| Flagged (needs human review) | 209 (110 unresolved player, 96 low-confidence match, 2 compound sign+option, 1 list-format batch) |
| Skipped (out of scope or unmatched) | 611 (496 no sign/option language — renounce/waiver/retirement/trade-block chatter; ~down from 108+74 option/sign-template misses after round 2) |
| Excluded (2025-fa-news live overlap) | 6 |

First pass (session start) landed 1305; a second pass same session fixed three
concrete gaps found by spot-checking `skipped`/`flagged` samples (user's
instinct that fa-news "should parse easily" was right — these were regexes
missing common phrasings, not fundamentally hard cases):
- `TEAM_OPTION_RE` required a possessive marker (`'s`/`s'`) after the player
  name; some messages have none ("...decline Trey Lyles 2020-21 team
  option") — made it optional.
- `PLAYER_OPTION_ACCEPT_RE`/`DECLINE_RE` only matched "accepts/opted out of
  ... player option"; missed the equally common "declines his player option
  worth $X" and "has opted in to his $X player option" phrasings — added both.
- Signing clauses naming two players ("sign Reggie Perry and Immanuel
  Quickley", "signing Jordan Nwora (pick 48) and Markus Howard(pick 51)")
  were captured as one garbled string and dropped or mismatched — added
  `_split_players()` (parenthetical-stripping + "and"/"&" split, same idea as
  the trades parser's `split_assets`) so each name resolves independently.

Verified the full 1352 for correctness with a bulk sanity check (bio's last
name must appear in the matched raw text) — 0 failures — in addition to
spot-checking individual records via `GET /api/transactions?player=`
(coherent per-player histories, e.g. `wade-dean`'s option decline -> 3 later
signings in date order).

**Remaining 209 flagged + 611 skipped are not yet worked** — same next-step
shape as the trades multi-team bucket: needs a human pass (mostly players
with no bio entry at all — verified against `/api/players`, nothing to parse
better there) or another round of parser refinement for smaller recurring
shapes (negated statements like "did not sign", qualifying-offer signings,
name-in-quotes nicknames, a handful of messed-up multi-player UDFA-batch
messages).

Scripts: `resolve_discord_fa_signings.py`, `submit_discord_fa_signings.py`
(state file `discord-fa-signings-submitted.json`, keyed by a composite
`discord_id:kind:slug:team:decision:year` since one message can yield several
candidates — a plain discord_id would collide). Raw messages fetched into
`discord-fa-signings-raw/` via the existing `fetch_discord_transactions.py`
(run twice — `--pattern fanews` and `--pattern fa-news`, since the two eras
spell the channel name differently — then the bare `fa-news.json` output
deleted).

### Session 2 changes (parser fixes + corrections)

Went back through `resolve_discord_trades.py` and found several real parser
bugs, not just ambiguity, that were silently mis-parsing or mis-attributing
messages:

- **Paren-depth bug in the "and"/"&" splitter**: `split_assets` did a
  paren-aware comma split, then a *second*, non-paren-aware pass splitting on
  " and "/" & ", which incorrectly sliced inside parentheticals like
  "(best between HOU and ORL)" and dropped fragments as phantom unresolved
  players. Fixed by making the whole tokenizer paren-depth-aware in one pass;
  also added "+" as a top-level joiner (e.g. "Blake Griffin + 2021 MIA 1st").
- **Team header regex too rigid**: only matched "TEAM receives:" with a
  literal colon. Real messages also used markdown bold (`*TEAM receives:*`),
  semicolons (`TEAM Recieves;`), no punctuation at all (`TEAM receives Player`),
  "TEAM Gets:", "TEAM receivers:" (typo), and "TEAM: Trade N: ..." on the same
  line as the header. All now handled (`normalize_for_parsing`,
  broadened `_VERB`/`_HEADER_TAIL` patterns). This alone fixed 14 of the 15
  "malformed parse" messages (the 15th has no second team block in the actual
  Discord text — genuinely incomplete, and moot since it names no player).
- **`PICK_RE` too broad on "rights to X"**: the pick-swap-right pattern
  `\bright(s)? to\b` also matched "draft rights to VJ Edgecombe" / "rights to
  Grant Riller" etc. — real player draft-rights assets, not picks — causing
  them to be silently dropped. Narrowed to require "swap" specifically
  (`\bright(s)?\s+to\s+swap\b`). **This had already caused 6 live trades to be
  submitted missing a player** (NYK/RJ Luis, NOP/VJ Edgecombe+Ben Saraf,
  CHA/VJ Edgecombe, NYK/Miles McBride, SAC/Johnny Furphy) — fixed by
  delete + resubmit of those 6 `historical` records via the admin API
  (`DELETE /api/transactions/{id}` has no roster side effects for historical
  trades, so this is safe).
- **Nickname/abbreviation gaps**: last-name-only fallback refuses when a
  surname isn't unique (6 Martins, 2 Wagners, 2 Youngs in bios), so "Mo
  Wagner", "KJ Martin", "Thad Young", "KCP" all failed. Added exact-string
  `NICKNAME_ALIASES` for these four plus "Cam Johnson" (see below).
- **Two false-positive name matches found by manual eyeball review**: "Cheick
  Diallo" got last-name-matched to `diallo-hamidou` (Hamidou Diallo) and
  "Jacob Evans" to `evans-isaiah` (Isaiah Evans) — real, *different* players
  who happen to be the only bio entry with that surname. Added
  `FALSE_MATCH_BLOCKLIST` to refuse the fallback for these specific tokens
  (drops the player, keeps the rest of the trade, per the missing-player
  policy below) rather than silently misattribute. **One of these three
  categories of bug had already reached production**: a "Cam Johnson" mention
  was fuzzy-matched to `johnson-aj` (wrong) instead of `johnson-cameron`
  (right, and already in bios) in the *original* human-reviewed promotion —
  i.e. the manual eyeball pass in session 1 missed it too. Fixed the alias,
  corrected the stale `discord-transactions-promoted.json` entry, and
  delete+resubmitted that live record. **Take away for future review passes:
  don't assume a human-reviewed promotion is automatically correct** — a
  same-surname collision is easy to miss at a skim.
- **One-off text fixes** (`MESSAGE_TEXT_FIXES`, keyed by discord_id): "PXC" ->
  "PHX" typo, and a missing comma in "Caris Levert Jaxson Hayes" (two players
  run together).
- **Missing-player policy decided**: for the 4 real gaps (Dzanan Musa, Zhaire
  Smith, John Petty, Yang Hansen — real journeymen never added to
  `player-bios.json`) and the 2 false-match cases above, the chosen policy is
  **skip the player, log the rest of the trade** — no bio page exists for
  them anyway, so nothing is lost by omitting that one asset. Implemented by
  having `resolve_player` return unresolved silently (no longer a blocking
  "reason") rather than flagging the whole message.

Net effect: 322+39 (session 1) -> 378 submitted. The multi-team bucket grew
from 60 to 67 because several previously-"malformed parse" messages turned
out, once parseable, to be 3/4/5-team trades.

**Known residual risk, not yet addressed**: the fuzzy matcher
(`difflib.get_close_matches`, cutoff 0.85) can match a mention to the wrong
real person when their full "first last" strings happen to be similar (this
is exactly how the Cam Johnson bug happened). No full re-audit of all
already-submitted fuzzy-matched entries across all 378 was done this
session — only the ~45 flagged-as-low-confidence-this-run were eyeballed.
If something looks wrong on a player's transaction history page, this is the
first place to check.

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
| Submitted to live transaction log | 378 | 322 original auto-resolved + 39 original promotions + 17 more auto-resolved after session-2 parser fixes (see above) |
| 3-5 team trades | 67 | Still flagged. Needs a human decision per trade on from/to — can't be automated (see "3+ team trades" note below). Grew from 60 as previously-malformed messages became parseable and turned out to be multi-team. |
| Picks-only / truly unfixable no-op | ~43 | Not actionable — no players moved (or, for 1 message, no second team block exists in the actual Discord text). Correctly excluded, not really "flagged" |

Note: `resolve_discord_trades.py`'s flagged-bucket output is stateless per run
— it doesn't know which discord_ids are already in `discord-transactions-
submitted.json` or `-promoted.json`, so messages that are actually done will
still show up as "flagged" on a fresh run. Always cross-reference against the
submitted-state file before treating a flagged entry as new work.

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
3. **Work through the 67 multi-team trades one at a time** — each needs a
   human to assign from/to per asset, since the text doesn't state it
   explicitly. Started this in session 2 but the user chose to defer after
   the first 2 (skipped, no memory of the specific direction) rather than
   grind through 67 rounds — resume when there's appetite for it. Use
   `parse_message` to get per-team resolved player blocks (as done earlier
   this session) so each trade can be presented compactly rather than
   re-typed from raw text.
4. ~~Decide what to do about the 15 unresolved-player messages~~ Done —
   policy is skip-the-player-log-the-rest, implemented in `resolve_player`.
5. ~~Read the 15 malformed-parse messages~~ Done — fixed via broadened
   header regex + text normalization; 14/15 fixed, 1 has no second team block
   in the source text and is moot (no player named).
6. ~~Revisit the low-confidence matches~~ Done — eyeballed all 45 remaining
   low-confidence-only flagged messages (43 turned out already submitted from
   session 1, 1 was new, 1 more surfaced from the malformed-parse fix), plus
   caught and fixed 3 wrong-attribution bugs (Cam Johnson, Cheick Diallo,
   Jacob Evans — see session-2 notes above), including one that had already
   reached production from the session-1 review.
7. Once nbn-api changes are validated end-to-end, split the `transactions.py`
   diff out of the `draft-trades-manual-advance` branch's other WIP and commit
   it separately (or ask whether it should just ride along with that branch).
   Note `resolve_discord_trades.py`'s bug fixes from this session are also
   uncommitted — worth bundling into the same commit.

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

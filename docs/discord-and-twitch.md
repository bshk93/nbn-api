# Discord and Twitch

Every path between the API and Discord or Twitch that is not a transaction embed: slash commands, the #roster-log mirror, the live banner and the go-live post. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

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
  - `/roster team:<abbr>` — current depth chart. Starters are the team's saved
    rotation (`GET /api/rotations`, via `_starting_five`) when it has one, else
    the computed five — the same pick as the team page and `/preview`.
  - `/lineups team1 team2` — both teams' starting fives side by side (same
    `_starting_five`), oriented away/home off their next game this season with
    a link to its `/preview` page; teams stay in the order given if none.
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

## #roster-log mirror (`routers/roster_log_relay.py`)

The one place this service **reads** Discord. `#roster-log` is the committee's
list of changes still to be entered into the sheets and the site; a human used to
copy each message into it by hand from four channels. This polls those channels
(`GET /channels/{id}/messages?after=`, every 60s — REST reads don't need the
Message Content intent, and no gateway is involved) and reposts new **parent**
messages (`type` 0 and 19) verbatim via `discord_transport`'s shared queue.
Destination is `DISCORD_ROSTER_LOG_CHANNEL`; unset, the module never starts.

It **relays, it does not interpret** — no summarizing, no deciding whether a
message "is" a transaction. A human enters what the line says, and a wrong
paraphrase there is worse than a line that didn't need entering. Bot posts are
allowed per source (`SOURCES`): skipped on `#fa-news`, where our own FFA clock
posts aren't sheet changes, and relayed on `#roster-log-nbn-today`, which is
nothing but applied transactions — except the ones the submitter already
entered by hand, see below. Duplicates across sources are intended.

**Per-transaction opt-out.** `POST /api/transactions` takes
`relay_to_roster_log: bool = False`. `discord_notify.build_embed` stamps the
decision into that transaction's own embed footer (`NO_RELAY_MARKER`, "not
relayed to #roster-log") rather than a separate store this module would have to
query — the footer already travels with the exact message this module reads.
`_opted_out` checks it and `is_relayable` folds it in, so an opted-out
transaction is skipped the same way any non-relayable message is: cursor
advances past it, nothing sent. `_embed_text` never extracts the footer into
relayed text, so the marker can't leak into a message that *is* relayed.
Default is `False` because most transactions get typed into #roster-log by
hand while being worked — this exists for the "already logged, don't repeat
it" case, which is the exception, not the common path.

Four anti-dump gates, since the sources hold thousands of old messages:
seeding is silent (a new cursor starts at the newest id and relays nothing),
messages older than `MAX_AGE_HOURS` are skipped, the destination has a burst cap,
and each poll examines at most `MAX_PER_POLL` per source. **A refused send is a
throttle, not a drop** — unlike `discord_notify`, the cursor is not advanced past
a message that wasn't enqueued, so a clipped burst is relayed next cycle. Cursors
live in `roster-log-relay.json`; deleting it re-seeds (and relays nothing).

Because seeding skips history, carrying an older message across is deliberate and
manual: `GET /api/roster-log/candidates?source=…` lists what's there with the exact
text each would become, `POST /api/roster-log/relay` sends up to 25 named ids, and
`GET /api/roster-log/status` shows the cursors. All three are admin-only.

Each entry is posted as a **bare embed** — description and colour, no title,
source label, author or timestamp. Plain text ran consecutive entries together
and `#roster-log` is read as a checklist of separate things to enter, so the card
is a *boundary*, not a header; anything added to it is content the source message
didn't have. An image attachment is hung off the card as `image`, since a URL
inside an embed description is only a link.

Relayed text carries `allowed_mentions: {"parse": []}` — the team-role pings in
nearly every `#fa-news` signing still *render*, they just don't notify twice.
`tests/test_roster_log_relay.py` pins all of the above.

## Twitch live status (`routers/stream_live.py`)

`GET /api/stream/live`, public, behind the homepage's live banner: whether
twitch.tv/nothingbutnet is live right now, with title, game and viewers. It
answers from an in-memory cache at most 60s old, so every visitor shares one
Helix call a minute.

- **It uses a client-credentials app token, not `TWITCH_REFRESH_TOKEN`.**
  `/helix/streams` needs no user consent, and refreshing the broadcaster's token
  from here would race `fetch_twitch_subs.py`, which rewrites it in `.env`
  after each run. `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` are all it reads.
- **Any failure reads as offline, with a 200**, and is cached like a success.
  No banner is harmless; a broken homepage or a false "LIVE" isn't.

Pinned by `tests/test_stream_live.py` (no network).

### Go-live post to #leaguetalk (`routers/stream_notify.py`)

Twitch EventSub pushes `stream.online` to `POST /api/twitch/eventsub` within
seconds of the stream starting; the banner above only learns of it when someone
loads the site. Two shapes, and nothing else in either:

- **Stream:** "🔴 NBN is live", then for each stream game (tip-off order) a
  line `@AWAY (W-L) at @HOME (W-L)` — role tags, records off
  `standings-history.csv`, so going into the game. Only those roles ping;
  unreadable standings drop the records, not the post.
- **Box scores:** "🔴 Box Scores live @here", the header alone: no game, no
  team named or tagged. `allowed_mentions` is `{"parse": ["everyone"]}`, which
  is what lets `@here` notify whoever's online; the bot needs "Mention
  @everyone, @here, and All Roles" in #leaguetalk or `@here` stays text.

(The posts once carried a random-player or front-office joke line under each
game, and box scores one random game; both were taken out on request.)

"live" links to nbn.today, as `[live](<https://nbn.today>)`: the angle
brackets stop Discord adding the site's generic preview card. The night is the date the stream started in league time, less
`NIGHT_ROLLOVER_HOURS` (6), so a box-score stream past midnight stays with its
games (`night_of`). Every game that date counts, played or not: streams happen
on their date, while a day is marked done late or never and box scores land
over the next days, so "unplayed" said nothing about tonight. (The earliest-
unplayed rule once called a game night's stream the previous day's box scores,
because that day was never marked done.) Only a date with no games, a catch-up
stream, falls back to the earliest date in the last week with a game not yet
played. An unreadable schedule posts "NBN is live" with no pings.

- **The post opens with the server's `:minionannouncement:`**, hardcoded as
  `<:minionannouncement:1564839468823744572>` (`LIVE_EMOJI`): a bot's bare
  `:name:` stays text. `DISCORD_LIVE_EMOJI` overrides it in the same `<:name:id>`
  form (send `\:name:` in Discord to get one).
- **Every request is HMAC-verified** against `TWITCH_EVENTSUB_SECRET` and
  refused when over 10 minutes old. Unset, the endpoint 403s everything.
  `DISCORD_LEAGUETALK_CHANNEL` unset, nothing posts.
- **One post per stream, never a restart.** `stream.offline` is subscribed
  too and stamped in `stream-announce.json`: a stream back within
  `RESTART_GRACE_MINUTES` (30) of going offline, as the same kind of post as
  the last, is a restart and posts nothing; later is a new stream. A change of
  kind is never a restart, since the box scores go live minutes after the game
  stream ends. With no offline notice after the last post,
  the fallback is nothing within `RESTART_GAP_HOURS` (12). A redelivered
  message id is dropped; a stream that began over 15 minutes before we hear of
  it, or a rerun, isn't announced.
- **The streamer's titles name the night and the game** — "NBN 10/29: MEM @
  POR", "NBN 10/29: Box Scores" (`parse_title`). The title's date is the night
  (year: whichever is nearest the stream's start), and the `AWAY @ HOME` games
  it names (`@`, `at` or `vs`; both sides real teams, `TEAM_ALIASES` for GS,
  NO, SA…) are the stream games, flagged on the schedule or not; one the
  schedule doesn't have that night is logged and posted anyway. A typo'd or
  missing matchup falls back to the schedule's flags for that night.
- **The title says which stream it is.** On each go-live the channel's title
  is read from Helix `/channels` (current as soon as it's set, unlike
  `/streams`; `fetch_title`, app token as above). A title matching
  `BOX_TITLE_RE` ("box score(s)", any case) is the box-score post; any other title is the stream post,
  outright: the schedule only fills in the matchups and pings, and a night with
  no stream game found posts "NBN is live" with none rather than box scores. If the title can't be read, the fallback is
  the night: once it has had its stream post (`stream_day`), any later stream
  that night is box scores, since the stream games have no box score yet and
  would still read as unplayed. Under that fallback only, a game stream that
  drops and comes back reads as the box scores.
- **Subscribing is a one-off**, after deploying with the secret set:
  `set -a && source .env && set +a && venv/bin/python subscribe_twitch_eventsub.py create`,
  then `list` should show `stream.online` and `stream.offline` both `enabled`.
  Re-running `create` is safe (an existing type is a 409, left alone). A
  revocation is logged at WARNING.

Pinned by `tests/test_stream_notify.py` (no network).

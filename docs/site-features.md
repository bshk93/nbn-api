# Features behind single pages

One section per feature: what the endpoint serves, the rule it must keep, and its test. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

## Homepage team card (`routers/home.py`)

`GET /api/home/team/{team}`, public: everything the signed-in homepage card
shows, in one request. It used to take about a dozen, each quick on the server,
and over HTTP/1.1 the card waited behind the rest of the page for a connection.
It returns the team's games, this season's results, the standings rows, its
cap-history row, its injuries, its starting five and per-game lines, and the
same five and lines for its next opponent (the Matchup tab).

- **No rule of its own.** Each part comes from its source's helper:
  `_starting_five` (discord.py, the port of lineup.js), `team_season_lines`,
  the game index, `build_rows(teams=[team])`, `load_injuries`. A change to one
  of those reaches the card with nothing to update here.
- **Private chips stay separate.** Coaching settings, waivers and FA offers need
  the member's token; this endpoint has no auth.

Pinned by `tests/test_home_team.py`.

### Shared reads (`_load_json_shared`)

`storage._load_json_shared` parses a JSON file once per distinct content and
hands every caller the same object. It still reads the file on every call and
keys on a hash of the bytes, not on mtime/inode (timestamps are coarse, inodes
are reused), and decodes as `read_text()` does, so it returns exactly what
`_load_json` would; only the parse is saved. `players.player_bios_shared()` and
`ovr_shared()` are built on it, for player-bios.json (~620KB) and
ovr-history.json.

**Read-only by contract.** Mutating the shared object changes what the next
request reads. Anything that loads, edits and saves keeps `load_player_bios()`
/ `_load_json`, which hand out their own copy. Today it backs `_bios_by_name`
(which also keeps its index until the bios change), `GET /api/players`, the
gamelog, `_roster_augmented`, `cap_history.build_rows` and the homepage card;
helpers that take `bios=` / `ovr_history=` let one request share one parse.
Before widening it, check the new caller never writes into what it reads.
Pinned by `tests/test_shared_json.py`.

`_record_member_seen` (`/api/me`, every page load) writes member-seen.json only
when a value is new or the member is seen for the first time, so the file holds
exactly what it did when every call wrote (`tests/test_member_seen.py`).

**Compatibility is pinned by `tests/test_api_compat.py`**: every endpoint and
helper moved onto the shared parse is computed both ways (shared, and with the
old `_load_json` forced back in) and must match, as must the defaults of the
helpers that gained optional parameters.

**Shared responses (`storage.shared_json_response`)** go one step further for
the two biggest team-page reads, `GET /api/players` (no `?slugs=`) and
`GET /api/attributes/current`: the whole encoded answer is kept until one of
its files changes, since FastAPI encoding a 600–900KB dict was most of their
cost. Its `build()` may read nothing but the files it is keyed on. A date, a
caller or a third file would be served stale. Pinned by `tests/test_etag.py`.

**ETags (`etag.py`)**: every 200 JSON answer to a GET gets an `ETag` and
`Cache-Control: private, no-cache`, and a matching `If-None-Match` gets an
empty 304. It saves the download, not the work. An endpoint that sets its own
`Cache-Control` or `ETag`, or a cookie, is left alone. Also `tests/test_etag.py`.
nginx used to add `Cache-Control: no-store` to every `/api` answer, which
would cancel this. Since 2026-11-01 it adds it only when the API sent none
(`map` in `/etc/nginx/conf.d/nbn-api-cache.conf`, used by the `/api/` blocks
of nbn.today, pdc.nbn.today and dev.nbn.today).

### The gamelog index and all rosters at once

`GET /api/players/{slug}/gamelog` answers from `_rows_by_player`
(`routers/boxscores.py`): each raw `allstats-*.csv` parsed once into
`{PLAYER: rows}` and kept until that file's mtime or size changes. It used to
parse all 157k rows on every player page, 1.4s with the GIL held, which also
stalled whatever else was in flight on the one worker. The rows are the file's
own dicts, so treat them as read-only.

`GET /api/rosters/slugs` (public) is every team's roster as slugs, for a page
that needs all 30 (the homepage's Milestone Watch fetched 30 CSVs for it).

Pinned by `tests/test_gamelog_index.py` (temp dir, no live data).

## Tonight graphic and the 11am card (`routers/tonight.py`, `routers/tonight_card.py`)

| Route | Auth | What |
|---|---|---|
| `GET /api/tonight?date=` | Public | One game day (default: `game_day()`): every game with records, odds, a star per side and storylines; the featured game and its teams' colours; the previous night's top three lines; `next` slate when the date has none. Drawn by nbn-today's `tonight-hero.js` (the homepage poster and carousel) |
| `GET /api/tonight/card.png?date=` | Public | The same answer as a 1200x675 PNG (Pillow). Cached in `$DATA_DIR/tonight-cards/` by a hash of the answer; `v=` is ignored, it only busts Discord's cache. 404 on a date with no games |

- **One answer, two pictures.** Who is featured, each star and every storyline
  are decided in `tonight.py` only; the page and the card just draw it. Each game's
  `line` (`{margin, fav, spread, p_home}`, the homepage slate's spread) is
  `playoff_odds.game_line` on `stream_planner.ratings_now()`, the planner's own
  strength, so the slate and the planner's closeness can't disagree. The
  rules (stream game first, else win% + playoff stakes; the hot hand is best
  mean Game Score over the last 5 games *for this team*, 3 minimum, else top
  OVR in `home._five`) are its docstring. Career milestones are added on the
  page from milestones.js, which is their one home.
- **The card reads the site checkout** (`NBN_SITE_DIR`, as og.py does) for
  `build/team-colors.json`, `logos/`, `logo.png` and the Barlow/Inter woff2s,
  with DejaVu as the fallback font. Headshots come from `headshots.headshot_path`.
- **The post** is `post_tonight.py`, run by `nbn-tonight.timer` at 11:00
  America/New_York: one message to `DISCORD_LEAGUETALK_CHANNEL`, the card as the
  embed's image URL (so `discord_transport` stays JSON). Nothing on a day with no
  games; at most one post per date (`tonight-posted.json`), so the timer's
  `Persistent=true` catch-up can't double-post. `--dry-run` prints the message.
  Install once: copy both units to `/etc/systemd/system/`, then
  `sudo systemctl daemon-reload && sudo systemctl enable --now nbn-tonight.timer`.

Pinned by `tests/test_tonight.py`.

## Link previews (`routers/og.py`)

Six pages on the site are one shell serving many items, with the item fetched
from the API after load:

| Page | Endpoint | What the card says |
|---|---|---|
| `/news/view/?id=` | `GET /api/og/news` | headline, byline, a 200-char excerpt, the article's cover image |
| `/players/?p=` | `GET /api/og/player` | positions, height, age, team, OVR, career per-game line, headshot |
| `/proposals/view/?id=` | `GET /api/og/proposal` | number, title, state or outcome, author, excerpt |
| `/members/{name}` | `GET /api/og/member` | current team, seasons, record, championships, avatar |
| `/recap/?team=&season=` | `GET /api/og/recap` | season, team, record, seed, result, the team's top player by Game Score, the team card |
| `/boxscores/?date=&game=AWAY-HOME` | `GET /api/og/boxscore` | final score, date, each side's top scorer, the winner's team card. Without `game` it is the page's own card |

No link unfurler runs JavaScript, so all of them fetched the shell and got the
placeholder Open Graph tags `nbn-today/build/og_tags.py` bakes into it — every
article unfurled in Discord as the same "Article — NBN News" card, every player
as the same "Players — NBN" one. `routers/og.py` renders the head each item
should have had.

**They only get used because nginx routes to them.** `/etc/nginx/sites-enabled/nbn.today`
carries a `$nbn_unfurler` map over `$http_user_agent` and rewrites those six
paths to internal locations that proxy here; every other visitor gets the real
page. The map is **social unfurlers only, deliberately not search engines** —
these endpoints answer with a head-only stub, and nothing that indexes pages
should ever be handed one. A new preview client is one entry in that map; a
new per-item page is a builder here plus a location block there.

Player names on every card go through `routers/names.py`, a mirror of the
site's `names.js` ("CJ McCollum", not the "Cj Mccollum" `_display_name` gives),
pinned by `tests/test_names.py`.

Three rules hold across all six, and `tests/test_og.py` pins them:

- **Nothing unpublished, ever.** A draft article, a draft proposal, an unknown
  id — all fall back to that page's own static card. A crawler that gets a 404
  shows no card at all, which is worse than a plain one.
- **The fallback is read from the page, not restated in Python.** `_static_head`
  lifts the `NBN:og` block straight out of the shell under `NBN_SITE_DIR`
  (default `/var/www/nbn.today`), so the generic card stays whatever
  `og_tags.py` last wrote and can't drift from it.
- **An image that can't be made absolute is dropped** for the default card.
  Discord ignores a relative `og:image` and shows no picture at all.

One nginx detail that has already bitten once: a member name is a **path**
segment and several contain spaces (`/members/Darth Awn`). A location's named
capture matches on `$uri`, which nginx has already percent-decoded, and feeding
that into `proxy_pass` puts a raw space in the upstream request line — every
such name 400s. The name is pulled out of `$request_uri` instead, which is
still encoded, and arrives as an ordinary query value.

One thing to keep in mind when adding to a card: it is public. A proposal's
live tally is privileged (`live_results` in `_proposal_view` is BOD-only), so
the proposal card stops at the state — `Voting open`, or the outcome once
decided.

## NBN Twitter (`routers/tweets.py`)

Made-up posts reacting to each game day, shown on the homepage card. A cast of
fictional accounts (stats account, insider, beat writers, fan accounts, the
players) written at the end of the daily box score run
(`nbn-today/.claude/commands/parse-boxscores.md` § 8). No model runs here; the
session doing the box scores writes them.

| Route | Auth | What |
|---|---|---|
| `GET /api/tweets` | Public | newest first; `?limit=` (≤100), `?before=<n>` to page, `?team=` |
| `POST /api/tweets` | `stats` or `rosters` | `{date, tweets: [{handle, name, text, teams?, slug?, team?, likes?, retweets?}]}`, up to 20 |
| `DELETE /api/tweets/{n}` | `stats` or `rosters`, or the member who posted it | |
| `POST /api/tweets/member` | any member | one tweet, `{text, identity: self\|profile\|anon, profile_id?, handle?, name?, team?, save?}`; 10 a day |
| `GET`/`POST /api/tweets/profiles`, `DELETE /api/tweets/profiles/{n}` | member (delete: owner or admin) | your own handle and saved identities |
| `PUT`/`DELETE /api/tweets/profiles/{n}/avatar`, `GET /api/tweets/avatars/{key}.jpg` | owner (delete: or admin); get is public | a saved identity's picture, multipart `file` |
| `GET /api/tweets/{n}/poster` | admin | who really posted |

- **The text is fiction; the numbers are not.** The skill checks every stat in
  a tweet against the committed box score. The server can't, so it only checks
  the shape: 280 characters, a real handle, real teams.
- **`teams` is `[away, home]` or empty**, so the card can link the tweet to its
  box score. `slug` marks a player's own account (the card shows his face);
  `team` is a fan account's team.
- Stored in `tweets.json` with a monotonic `seq`, like quadrants. It is
  written league content, so it is backed up with the data dir.
- **Members post as themselves or a made-up identity** (a scoop account),
  saved as a profile in `tweet-profiles.json` if they like. Who really posted
  is stored as `poster` and dropped from the public list, along with `author`;
  only `GET /api/tweets/{n}/poster` (admin) shows it. Signed in, your own
  member tweets come back with `mine: true`.
- **A saved identity can have a picture**, cropped to a 128px square and
  stored in `tweet-avatars/` under a random key that changes on each upload
  (so it caches forever and doesn't name the owner). Tweets pick up their
  profile's current picture when listed, as `avatar`; forgetting the profile
  deletes the file. Free, unlike the NB¥500 member avatar in `auth.py`.
- **A handle belongs to whoever used it first** (`_handle_owner`): a member's
  own handle, a saved profile, the cast, or a past tweet. Others get a 409.
  Official batches (`POST /api/tweets`) aren't checked against it.
- **Every stored tweet is also posted to `#twitter`** (`DISCORD_TWEETS_CHANNEL`),
  one embed per tweet, including the arena tweets from `post_one`. Deleting a
  tweet here leaves its Discord post up; the transport keeps no message ids.

Pinned by `tests/test_tweets.py`.

## Quadrant charts and headshots (`routers/quadrants.py`, `routers/headshots.py`)

Behind the Quadrants tab on `nbn-today/frivolities/index.html`: pick a stat per
axis, caption the four quadrants, and each player is drawn as his face at his
real values.

| Route | Auth | What |
|---|---|---|
| `GET /api/quadrants`, `GET /api/quadrants/{n}` | Public | the gallery (newest first, no configs); one chart |
| `POST /api/quadrants` | any member | `{title, config}` — the page's own config object, validated against `STAT_KEYS` |
| `DELETE /api/quadrants/{n}` | its author, or `admin` | |
| `GET /api/players/{slug}/headshot` | Public | the bio's `photo_url`, cropped square to 160px JPEG and cached in `$DATA_DIR/headshots/` |

- **A saved chart is a recipe, not a picture.** `quadrants.json` stores the
  config and the page redraws from live stats, so a chart moves with the
  numbers. Numbers come from a monotonic `seq`, like suggestions.
- **`STAT_KEYS` mirrors `AXIS_VARS` on the page** (plus `ovr`). A stat added
  there needs adding here, or charts using it can't be saved.
- **The headshot proxy exists for the PNG export.** basketball-reference sends
  no CORS headers, and a canvas that draws a cross-origin image can't be
  exported. It only ever fetches the URL on the bio — the route takes a slug,
  never a URL — so it is not an open proxy. The cache name hashes the URL, so
  a new photo replaces the old one; a failed fetch is a 404 (the page draws
  initials) remembered for an hour.
- **Fetches are paced per host** (`HOST_INTERVAL`, 3.5s): Sports-Reference
  jails a client past ~20 requests a minute. Warm the whole cache once with
  `venv/bin/python fetch_headshots.py` (about an hour; re-runs fetch only what's
  missing). `headshots/` is rebuildable, so keep it in the data dir's
  `.gitignore` alongside `irl/`.

Pinned by `tests/test_quadrants.py` and `tests/test_headshots.py`.

## IRL feed (`routers/irl.py`)

Real-world game logs and injury news for the players on a fantasy roster,
served to the IRL tabs on `/nbnfl` and the NBN team pages (`nbn-today/irl-feed.js`). **Kept apart from NBN's stats on purpose** — it
reads no box score, roster CSV or bio, and the stats build never reads it.

- **Rosters**, per sport (`SPORTS[...]["rosters"]`). NFL: hand-kept in
  `irl-rosters.json` (sport → roster key → `[{name, espn_id}]`). NBA: NBN's own
  `{abbr}-roster.csv`, read live, with slug → ESPN id in `irl-ids.json`. Each
  fetch auto-maps a newly rostered player (never draft rights): an exact name
  AND date of birth on ESPN's NBA rosters, else a name search (full, then last
  name alone — that catches nicknames like Bub Carrington) over the NBA and G
  League, accepted only when exactly one hit has the same birthday and last
  name. Who's left is in `irl/unmapped-nba.json`, retried daily, and listed on
  `/committees/rosters` → Data Quality, where `rosters` sets an id (or `null`,
  "no ESPN record") via `PUT /api/irl/ids/{sport}/{slug}`. Never match at
  read time. Players with no stat
  columns (linemen, the unmapped) are hidden by the page via `has_stats`.
  An id with no record in the league (a G League or college player — ESPN
  ids span leagues) is stored with `bio: null` and hidden the same way. Set
  a slug to `null` in `irl-ids.json` to stop auto-mapping retrying it; each
  retry costs ~31 requests plus a search or two.
- **Cache**: `irl/{sport}/{espn_id}.json`, written by `fetch_irl.py` from
  `nbn-irl.timer` (hourly; a player is refetched once older than the sport's
  `refresh_minutes` — NFL 50, NBA 170). A season already over when fetched is `closed`
  and never refetched (`--force` overrides). Not backed up: `irl/` is in the
  data dir's `.gitignore`, since one run rebuilds it.
- **Source**: ESPN's unofficial JSON. stats.nba.com and cdn.nba.com 403 this
  server. ESPN's WAF 403s a User-Agent containing a URL; keep `USER_AGENT` a
  plain token.
- `SPORTS` sets seasons kept and `max_games` per sport (NFL two full
  seasons; NBA newest 50 games across two).

Pinned by `tests/test_irl.py` (no network).

## League lore (`routers/lore.py`)

The league's history, compiled from all of #leaguetalk (2020 on), for NBN Today
writers — people and agents. Plain markdown in `$DATA_DIR/lore/`, one file per
document, named by its stem: `lore` (the storyline digest — start here),
`sagas` (the big stories in full), and the season notes (`2020`, `2020-21`, …).

- `GET /api/lore` lists the documents with their `## ` sections;
  `GET /api/lore/{name}` returns one document's markdown;
  `GET /api/lore/search?q=&doc=&limit=` returns every block (a list item with its
  continuation lines, a paragraph, or a table) containing all the query words,
  digest first, then sagas, then the notes.
- **Reads need a member token, not public.** Writes (`PUT /api/lore/{name}`, whole
  document) need `curator` or `bod`.
- **No content filtering, by design.** The banned members, the alt scandal, the
  DJ/Embiid saga and the rest are the point: writers are meant to bring them up.
  Don't add a "safe" layer. The one standing exclusion is in how the notes were
  written, not in code: no off-topic politics, and no members' private lives
  (health, family, work).
- History is kept by the data dir's git backup (every 10 min), not an in-app log.
  `log_write` records who replaced which document.

Pinned by `tests/test_lore.py`.

## Tetris leaderboard (`routers/tetris.py`)

`GET /api/tetris/leaderboard` (public, top 20) and `POST /api/tetris/scores`
(member token, `{score, lines}`) behind the homepage's Tetris card. Each
member's best game in `tetris-scores.json`. The game runs in the browser, so a
score is the client's word: `_plausible` refuses only what no game with that
many lines could reach. **Don't attach NB¥ or anything else to it** while that
stays true. Pinned by `tests/test_tetris.py`.

## Arena naming rights (`routers/arenas.py`)

Any member bids NB¥ to name any team's arena (`POST /api/arenas/{team}`,
`{name, bid}`); `GET /api/arenas` is public. A bid must beat the standing one
by 10% (100 to open), the price never decays, and an outbid holder is **not**
refunded, so every bid is a sink (`arena_bid`). An admin pulls a name with
`DELETE /api/arenas/{team}`, which refunds it (`arena_refund`) and restores the
name before it. History is kept in `arena-names.json`, never deleted: the
ledger (`stats_build/finances.py`, `arena_for`) names each game for the arena in
force that day, defaulting to the real arena the schedule names. Cosmetic: no
revenue, cap or rule reads it. Pinned by `tests/test_arenas.py`.

## Stream planner (`routers/stream_planner.py`)

`GET /api/stream-planner?from=&to=` (`streamer`, admin passes; default the
next 7 days, at most 31) behind the Planner tab on `/committees/stream`. One
scheduled stream game a day, bought streams on top: it ranks each day's
unflagged games by points (`WEIGHTS`) and says why, per factor. Its docstring
is the whole model.

- **Read-only.** The page flags a pick with the existing
  `POST /api/schedule/{id}/stream`; nothing here writes, and nothing picks on
  its own.
- **No rule of its own for strength or injuries.** Ratings are the hourly
  playoff-odds run's (`playoff-odds.json`); Out players come off through
  `roster_priors` on `MINUTES` weights, the same scale; with no odds run yet
  the roster prior stands alone. Owner head-to-head is attributed by game day
  from `load_owner_tenures` (`stats_build/pipeline.py`), like `owner_stats`.
- **A flagged, unbought game locks its day**; a day whose only stream is
  bought still gets its pick. Fair share counts scheduled streams only; the
  repeat penalty counts bought ones too (it's the same team on stream).
- **The cooldown is a rule, not a weight** (`COOLDOWN_DAYS`, 7). A game with
  a team on stream within 7 days either side (set, bought, or picked earlier in
  the plan) is still ranked, marked `cooldown`, but can't be the day's pick
  unless every game that day clashes (then the furthest clash, `forced`). Fair
  share alone kept dragging one under-exposed team back every few days (SAS,
  Nov 2026). Quality ranks tied ratings equally, never by team name.
- **Days are scored in order**, and each day's pick steers the days after it:
  the caller's choice when it sent one (`picks=` game ids, `skip=` dates),
  otherwise the best game off cooldown (`projected_pick`, `pick_source`). The
  page sends Chuck's current selection, so a swap re-ranks the rest of the range.
- Each game also carries this season's `meetings` (score once played, and
  whether streamed) and both teams' `last_stream`; the response carries
  `exposure` (with conference and division) and `balance` for the side tally.
- **Saving a plan is `POST /api/schedule/stream-batch`** (`routers/schedule.py`,
  `streamer`): every pick is checked first and any bad day is a 409 naming all
  of them, with nothing written; then one save and one `log_write`. Pinned by
  `tests/test_schedule.py`.
- **Milestones are the page's** (`milestones.js`, +5), so there is one
  milestone implementation rather than a Python mirror.
- **`priority=`** picks a preset from `PRIORITIES` (`balanced`, `coverage`,
  `rivalry`, `quality`, `stakes`): multipliers on `WEIGHTS`, applied to each
  factor's `pts` and `max` after scoring. `weights` in the response is the
  effective set and `priorities` lists the presets for the page's control.
  The cooldown is a rule under every preset.
- **`open_days=N`** replaces `to`: walk until N open days (games, no locked
  stream), at most 31 out. `to` is the last day walked; `open_days_short` says
  it ran out first. It's the page's 7/14 days with Hide saved days on.

The scoring (`plan` and its helpers) is pure, so the tests run without a data
dir. Pinned by `tests/test_stream_planner.py`.

## Inbox (`routers/inbox.py`)

Per-member messages in `inboxes.json`; mutes in `inbox_prefs.json` (both NBS_DATA_DIR).

- **Every message has a `kind`** (one of `KINDS`: `fa`, `extensions`, `trades`,
  `tradeblock`, `transactions`, `games`, `news`, `suggestions`, `finances`,
  `committees`, `account`, `other`). Callers normally leave it out and it is read
  off the link (`kind_for_link`); pass `kind=` only where the link would mislead
  (a plain `/teams/X/` link reads as `transactions`, so `game_day_notify`'s
  coaching receipt passes `games`). Entries written before `kind` existed are
  classified the same way on read, never rewritten on disk.
- **Mutes skip the write, not the read.** A muted kind is never stored, so
  unmuting doesn't bring back what was missed — and a message with `actions`
  always arrives, muted or not.
- **Archived = read + hidden.** It leaves `unread_count`, and prunes like a read
  entry (90 days).
- **A finished to-do is marked read for everyone.** A message that asks for
  something can carry a `ref` (`notify_*(..., ref=)`); when the thing is done,
  the owning module calls `mark_read_where(match)` and every copy, in every
  inbox, is marked read. Used by `game_day_notify`: a day's "needs box score
  screenshots" message (ref `screenshots-needed:{date}`) clears once every game
  that date has both sides uploaded or a box score entered. A test that drives
  that path stubs the inbox store, or it writes the live `inboxes.json`.
- `GET /api/inbox` also returns `kinds` (id + label, in display order) and
  `muted`. The page's weekly digest is computed in the page, so it stays live.
- Routes: `POST {id}/read|unread|archive|unarchive`, `POST read-all[?kind=]`,
  `POST bulk {ids, op}` (unknown ids skipped), `GET|PUT prefs {muted}`.
- Test stubs of `notify_*` take `**_`, so a new optional parameter doesn't break them.

Pinned by `tests/test_inbox.py`.

## Trading block nudge (`routers/tradeblock_nudge.py`)

A player on a team's trading block for 7+ days gets the team an inbox message
naming him — one message per team, at most once every 7 days
(`tradeblock-nudges.json`) — with two buttons per player: **Remove** (off the
block) and **Still available** (`snoozed_until` two weeks out, then he's back
in the nudge). `nbn-tradeblock-nudge.timer` runs `sweep_tradeblock_nudges.py`
daily (`--dry-run` lists who'd be nudged and writes nothing). Players only;
picks are never nudged.

- **There is no permanent "keep until traded", on purpose.** The point is a
  block that stays current; a mark that silenced a player for good would
  become everyone's default answer. Removing is the one-click path, and still
  available has to be re-said every two weeks. Don't add a permanent opt-out.
- **`added_at` and `snoozed_until` are server-only.** `_carry_block_fields` in
  `roster_picks.py` keeps both through the whole-block `PUT` and the
  one-player add, and ignores them from the caller. Both live on the entry, so
  they're team-wide and end with the listing (`_scrub_trading_block` drops a
  traded or released player; off-and-back-on is a new listing).
- **Inbox buttons are data, not URLs.** A message's `actions` are
  `{kind, label, short?, group?, ...}`; `POST /api/inbox/{id}/actions/{i}` runs the
  handler the owning module registered for that kind (`inbox.register_action`),
  which checks the clicker's permission itself — receiving a message grants
  nothing. Actions sharing a `group` are alternatives (one done closes the
  rest), and a done action is marked on every copy of the message (`batch`, set
  by `notify_team`). Kinds so far: `tradeblock_remove`, `tradeblock_snooze`.

Pinned by `tests/test_tradeblock_nudge.py`.

## DNA position checks (`note_dna_update` in `routers/coaching_settings.py`)

Loading a player's new ratings (the DNA) into the league's 2K file can
overwrite the positions his team set. So `PUT /api/ratings-changes/updated`
(the updates committee's tick, which whoever loads the DNA must do) opens a
check on his team for the streamer: `dna` in `position-checks.json`, shown as
an item's `dna` on `GET /api/position-checks`, and counted as waiting, so the
next streamer is told once (`game_day_notify.dna_updated`). Mark entered closes
it with `dna: {slug: {date, had}}` on the confirm, and the latest result per
player stays in `dna_done` (`overwritten`, `had`). Rostered players only; a
check for a newer update than the one confirmed stays open; unticking closes
an undone one. The rest is `nbn-today/docs/pages.md` § Roster Settings.
Pinned by `tests/test_dna_position_check.py`.

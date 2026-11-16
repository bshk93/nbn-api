# Box scores, stats and injuries

How a game gets into the raw box scores and stays safe there, how the stats build turns them into the 86 derived CSVs, who started each game, and the injury report. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

## Protecting the raw box scores

`allstats-{season}.csv` / `allstats-playoffs-{yy}.csv` in NBS_DATA_DIR are the
one asset here that cannot be rebuilt — 157,430 rows over six seasons, entered
by hand from screenshots that are kept only 14 days after committing
(`routers/boxscore_shots.py`, deliberate). Every derived CSV and every page on the
site is a function of them. Two defences, from `nbn-today/docs/dev-deploy-setup-spec.md`
Phase 2:

**1. Append-only by contract — `routers/allstats_guard.py`.** There is exactly
one code path that writes these files (`POST /api/boxscores/commit`), and it
goes through `write_allstats`, never `write_csv`. It refuses three things, each
a real failure mode rather than a hypothetical:

| Refusal | The failure it prevents |
|---|---|
| Fewer rows than are on disk | truncation |
| The rows on disk are not a prefix of the write | a rewrite of history dressed up as a write |
| A column on disk is missing from the header list | `write_csv` uses `extrasaction="ignore"`, so it drops the column from every row silently |

The third is not theoretical: **the older season files genuinely have different
headers** — 20-21 through 23-24 have no `OPP_RAW`, several carry `SEASON`, and
the blank column is `...27` rather than `' '`. Committing a game to an old
season with `REG_ALLSTATS_HEADERS` would have erased a column across the whole
file. Overriding is per-call (`allow_shrink=True`) or per-process
(`NBN_ALLSTATS_ALLOW_SHRINK=1`), and logs at WARNING either way. Hand-editing
the file is outside the guard by construction — it covers the code paths,
which is where the accidents are.

**The commit checks the game before the guard sees it** (`commit_boxscore`,
since 2026-10-05). The guard protects rows already on disk; this keeps a bad
game from becoming one. A 422 names every problem, and nothing is written:

- The season must be the one the date falls in (`season_clock`). While a
  season's file exists, nothing else would stop a game landing in it. Uploads
  are held to the same rule.
- Each row's slug must be a real bio, and PLAYER is written from that bio's
  name, not from what the caller sent. No player twice in one game.
- The game must pass `stats_build/checks.py`'s per-row and per-game checks, the
  same ones the weekly job runs, plus matching OT on both sides and no player
  past the game's length. Don't add an override: a game that fails these is
  one the weekly check would flag, and the file can't be fixed through the API.

**2. Weekly integrity check — `check_stats_integrity.py`** (`nbs-integrity.timer`,
Mondays). Row counts only ever go up; a **closed season never changes at all**,
so its `sha256` must match forever. State is `stats-integrity.json` in the data
dir, which the snapshot timer tracks, so the manifest's history is off-box
beside the files it describes.

Two things about it that are load-bearing:

- **A violation is never written into the manifest.** The stored entry stays as
  it was, so the next run compares against the last *good* state and alerts
  again. Re-baselining on sight would report each corruption once and then treat
  it as the new floor. `--accept` re-baselines deliberately, once a human has
  resolved the finding.
- **The newest season on disk is live alongside the current one.** The season
  clock rolls over July 1 by default (`_current_league_year`, `season_clock.py`)
  but a playoff run can finish after it — the 25-26 finals were 2026-06-18, and
  a slower postseason would have landed in July against a clock already reading
  26-27. Freezing last season the moment the calendar turns would alert on a
  real game.

It also checks the **draft picks** (since 2026-09-24, `picks_conveyance/parity.py`,
`--skip-picks` to turn off): every team `draft-picks.csv` names as an owner
must hold a claim on what `/api/picks` actually serves from the conveyance
store. Writes go to the flat CSV and the store is regenerated from it, but a
registry structure overrides the flat owner, so a trade the registry missed
vanishes from the new owner's page. Three trades entered before the conveyance
write path existed (July 2026) did exactly that and went unnoticed for two
months. The check is one-way on purpose: the flat model is lossy, so the store
legitimately names more parties than the CSV on every contingent pick.

Alerts go to `DISCORD_ALERT_CHANNEL` (inert if unset, like every other feed
here) **and** the unit exits non-zero, so a violation is visible in
`systemctl status` with no channel configured at all. `discord_transport.flush()`
exists for this caller: the send worker is a daemon thread, so a one-shot script
would otherwise exit between the enqueue and the send.

**3. Per-game provenance — `routers/boxscore_provenance.py`.** One JSONL line
per committed game (`boxscore-provenance-{season}.jsonl`), recording who
committed it, who uploaded it, the score, and the file's row count after. It
is the lasting answer to "where did this line come from, and who do I ask about
it?" — kilobytes a season against ~1GB/year for the images, which are kept for
14 days after commit and then deleted (`routers/boxscore_shots.py`). It is a **log, not state**: nothing reads it to make a decision, which is
why every failure in it is swallowed rather than raised. A provenance write must
never be why a real box score fails to commit.

**4. Weekly off-site tarball — `backup_to_drive.py`** (`nbs-drive-backup.timer`,
Sundays). Third tier, and the only one in a different failure domain from the
other two (10-minute local git history; push to the private `bshk93/nbn-data`).
The set is exactly what the snapshot repo tracks (`git ls-files`), so the two
backups cannot describe different things — 214 files, 12.1MB gzipped.

Credentials are the one exclusion and it is deliberately not all-or-nothing:
`google-oauth.json` is dropped outright, while **`members.json` is redacted, not
dropped** — tokens blanked, roles and tenures kept, because a token is one
rotation to replace and a tenure history is irreplaceable. The tarball is
re-opened and asserted before upload, so a shape change in `members.json` fails
the run rather than silently shipping tokens. Uses the existing `drive.file`
credential (it can only see files it created) and grants **no** permission —
the opposite of the trade-sheet export, which shares publicly on purpose. Old
backups are trashed, never deleted, only on this script's own name pattern.

**Restoring** is documented, and was drilled on 2026-08-19: a bare clone of the
backup rebuilt all 86 derived CSVs byte-identically to live. The procedure and
its three gotchas (`chmod 600` after a clone, sessions/tokens deliberately
absent, `owners.csv` regenerated) are in the spec under "Restoring from the
backup".

## Stats aggregation (`stats_build/`)

The stats build. Replaced the R one (`nbn-today/build/job.R`) at the cutover on
2026-08-19. Plan and full detail: `nbn-today/docs/stats-pipeline-port-spec.md`.

**This is what runs in production.** `nbn-today/build/build.sh` — still the
entry point everything triggers — runs `python3 -m stats_build` out of *this*
checkout. So **this repo deploys first**: a site deploy that lands ahead of an
API one gives every build `No module named stats_build`.

R is kept **dormant and permanent** — decided 2026-08-19; it is not being
uninstalled. Two jobs: it is the rollback (`NBN_STATS_ENGINE=r bash
build/build.sh`, on the same `link-public.sh` + `smoke_test.py` tail as the
live path), and it is the only value-level check these 86 files have. Watch
item: the first time the playoff/awards/rings paths run on new data (the 26-27
playoffs), run `harness port` and confirm R agrees.

| | |
|---|---|
| `__main__.py` | the entry point: `python3 -m stats_build`. Resolves the season, refuses to run without the raw box scores, writes 86 files into `NBN_OUT_DIR` |
| `buildargs.py` | resolves build arguments; the season itself comes from `season_clock.py` (the one clock shared with box scores and cap/contract logic — July 1 default, settable via league-state.json) — the entry point and the harness both import it |
| `pipeline.py` | every aggregation |
| `csvio.py` | `readr::write_csv`, byte for byte |
| `rmath.py` | R's `round()` and `mean()` |
| `harness.py` | run both engines, diff every file |

**One guard R did not have.** The entry point refuses to run when the season's
`allstats-{season}.csv` is missing, rather than aggregating zero rows and
writing 86 empty CSVs over 86 good ones. `derived/` is not backed up — the full
recompute *is* its restore path — so a vacuous success is the expensive failure
here, not a loud one.

**The gate**, and it is the whole safety story — `build/smoke_test.py` asserts
columns and row-count floors and *no values at all*, so byte-identical output
against R is the only oracle the derived files have — which is exactly why R
stays. Run this after any change to `pipeline.py`:

    python3 -m stats_build.harness port          # run both, diff every file
    python3 -m stats_build.harness determinism   # R twice, then diff
    python3 -m stats_build.harness diff A B

47 files come out byte-identical. Everything else is an **enumerated** accepted
difference in `harness.py` — not a blanket tolerance, so a real bug cannot hide
behind one:

| Class | Cells | What it is |
|---|---|---|
| `same_double_rendered_differently` | 2 | readr prints a double one digit longer; same value |
| `KNOWN_TIES` | 6 | R's long-double mean wins a `.xx5` tie; listed cell by cell |
| `KNOWN_FIXES` | 10 | R writes `Barton, will` — its title-caser has a small-word list |
| `RATING_COLUMNS` | 465 | the one tolerance: 1e-11, `OFF_RTG`/`DEF_RTG` only |
| `KNOWN_FIXED_FILES` | 1 file | `league-history.csv` — R's version is broken |

**`league-history.csv` is the one file the port deliberately does not
reproduce.** R sums `WL == "W"` over *player* rows rather than games, so a team
with about two playoff wins clears the 16-win champion test: 64 rows for 6
seasons, eleven "champions" in 20-21. `season-summary` already works around it.
Being in `KNOWN_FIXED_FILES` takes it out of the byte gate, so
`tests/test_stats_pipeline.py` carries its real check — never let that be the
only place a file is verified without a test standing in for the gate.

**`stats_build/rmath.py` exists because R's arithmetic differs from Python's:**
`round()` compares against the two representable doubles either side (so
`round(0.05, 1)` is `0`, where Python gives `0.1`), and `mean()` makes a second
compensating pass. Both were found as diffs, and both were verified against R
directly.

**One runtime reader outside the build.** `routers/game_highs.py`
(`GET /api/game-highs`, behind `/stats/highs`) reads the raw files itself and
imports `PLAYER_FIXES`, `_season_label` and `player_slug` from `pipeline.py`,
so its ranks agree with `game-highs-*.csv` (checked equal for all six at
launch). A change to any of those three changes that page too.

**stdlib only. Don't add pandas/numpy** — a dependency the service never
imports at runtime, and numpy's float and NaN rendering would fight `csvio`,
which reproduces `readr::write_csv` byte for byte (NA vs empty are different
values; `-0` keeps its sign; a whole double loses its `.0`).

## Parse now (`routers/parse_request.py`)

`POST /api/boxscores/parse-request` (`stats`/`rosters`) is the **Uploads done:
parse now** button on `/committees/stats`. It fires a Claude Code scheduled
task (formerly a routine) with an API trigger, whose saved prompt runs
`/parse-boxscores` in a fresh cloud session, and returns that session's URL.
`GET` gives the last request and what's in the queue. The fire URL and token
live in `.env` (`NBN_PARSE_ROUTINE_URL`, `NBN_PARSE_ROUTINE_TOKEN`), never in
the browser; unset, POST is a 503 and the page hides the button.

- **Nothing to parse, nothing fired**: no game with both sides up and no
  pending injury report is a 409.
- **A 20-minute window, not a lock** (`COOLDOWN_MINUTES`): a second request
  inside it is a 409 naming the first run, unless `force`. Nothing tells us
  when a run ends; the commit path refuses a game committed twice.
- **The fire text is context only**: the date and counts, nothing a member
  typed. The platform wraps it as untrusted; the task's own prompt is what
  says to run the parse.

Setting it up: create the task at claude.ai/code/routines (repo
`bshk93/nbn-today`, an environment that can reach nbn.today with a `stats`
token), add an API trigger, and put its Fire URL and token in `.env`, then
restart. Requests are kept in `parse-requests.json`. Pinned by
`tests/test_parse_request.py` (no network).

## Who started each game (`routers/game_starters.py`)

2K's box score screen lists players by points, not starters first, so the raw
box scores can't say who started. `commit_boxscore` calls
`game_starters.record_game`, which appends one line per game to
`game-starters.jsonl` with each team's **entered** PG–C five: the
`entered_minutes` snapshot a streamer's "entered" click saves, or the current
order when the record predates that snapshot and has nothing pending. A team
never entered is recorded as unknown, never guessed from its saved order 2K
hasn't seen. A starter with no minutes, or missing from the box, is flagged and
not counted as a start. The last line for a game wins.

**A team whose owner never saves a lineup has nothing to enter**, so 2K picks
its own five (IND went its first six games of 26-27 that way). For that, a
`streamer` or `stats` member records the five 2K actually started for one game,
read off the lineup at tip or the VOD: `POST /api/game-starters/report`, stored
in `game-starters-reports.json`. A report beats the entered five
(`source: "reported"`). Made before the commit, `record_game` picks it up; made
after, it appends a fresh line for the game with only that team replaced. It is
refused if a player isn't in that team's box score (or, before the commit, on
its roster). `GET /api/game-starters/unknown` lists played games still missing
a five, with each team's box score players, for the dashboard's backfill list.

Served by `GET /api/game-starters` and `GET /api/game-starters/counts` (the
player page's GS column and the award race's 6MOY). Recorded from 26-27;
`backfill_game_starters.py` (dry run by default) wrote opening night,
`reconstructed: true`. It is append-only and can't be rebuilt, and the data
backup tracks it by default. Tests that run a real commit must point
`STARTERS_FILE` at a temp dir, as `tests/test_boxscore_commit_gate.py` does.

**6MOY eligibility (rulebook § 8.1) lives here too.** `sixth_man_status`
counts a player's known games (his games, capped by his teams' games with
known starters, from `player_seasons.csv`) and makes him ineligible once he
started at least half of them. It's the same count as the award race in
nbn-today's `award-race.js`, so change both together. `GET /api/game-starters/sixth-man`
(public) serves it to the ballot page, which leaves ineligible players out of
its 6MOY search. `put_team_ballot` (`misc.py`, `_check_sixth_men`) refuses a
6MOY pick who's out with a 422 naming him, and saves nothing. The office's
whole-season `PUT /api/awards-ballots/{season}` isn't checked, so it can still
correct a ballot. Nothing known means eligible: the rule refuses only what the
record shows. Pinned by `tests/test_sixth_man.py`.

## Injury report (`routers/injuries.py`)

The 2K sim's in-game injuries, from the league-wide injury screen the streamer
shows each stream day. Same shape as box scores: upload → pending → a Claude
command parses → commit.

| Route | Auth | What |
|---|---|---|
| `POST /api/injuries/upload` | `stats`/`rosters` (not `streamer`: the upload lives on `/committees/stats`) | one screenshot (`image`, optional `date`, default league today) into that date's pending report — one per date, up to 6 images, since the list scrolls |
| `GET /api/injuries/pending`, `DELETE /api/injuries/pending/{id}[/images/{file}]` | same | what `/parse-injuries` reads; remove a mistaken upload |
| `POST /api/injuries/commit` | `stats`/`rosters` | `{date, report_id?, confirm_heals?, rows:[{slug, status, injury, duration, ovr?, ovr_delta?, team_shown?, player?}]}` |
| `GET /api/injuries`, `GET /api/injuries/history?team=&slug=&since=` | public | the current list; every change, newest first |
| `GET /api/injuries/screenshots[/{id}/{file}]` | public | pending and kept images (14 days, WebP) |

Files in NBS_DATA_DIR: `injuries.json` (current list + `last_report`, audited),
`injury-history.jsonl` (append-only: `new` / `changed` / `healed`),
`pending-injury-reports/`, `injury-report-screenshots/`.

Four rules, pinned by `tests/test_injuries.py`:

- **Off the report means healed.** The report is the whole league's list, so
  anyone on `injuries.json` who isn't on it is cleared (league rule).
- **A long injury that vanishes is confirmed, not assumed.** Healing a player
  last reported at `CONFIRM_HEAL_DAYS` (14) or more at the low end — "2-4
  weeks", "Career" — or with a duration `min_duration_days` can't read, is a
  409 `confirm_heals` that lists them and writes nothing. It goes through only
  when the request names each one in `confirm_heals`, per player, which
  `/parse-injuries` fills from the user's answer after re-checking the
  screenshots. It is the deterministic catch for a missed row.
- **Team is the roster CSV's**, not the 2K logo; a disagreement comes back as
  a warning. A player on no roster falls back to `team_shown`.
- **Discord gets changes only** — `changed` is status, injury or duration, not
  OVR — to `DISCORD_INJURY_CHANNEL` (`#injury-report`). Each line starts with
  the team's role mention (`TEAM_ROLE_IDS`), and `allowed_mentions` names
  exactly those roles; split by team past ~1,700 chars.
  A re-commit of the same report posts nothing. Reports apply in date order; an
  older one is a 409.

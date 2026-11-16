# Edit log, cap history and exports

The value-level edit log for writes outside the transaction ledger, the daily cap history, and the Google Sheets export. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

## The edit log (`routers/audit.py`)

`POST /api/transactions` is audited to the dollar. The side doors that change
the same cap state were not: `PUT /api/players/{slug}` rewrites `salaries`,
`cap_holds`, `guaranteed` and `guarantee_dates`; `PUT /api/roster/{team}`,
`PUT /api/deadcap/{team}` and `PUT /api/picks/...` rewrite the files those
figures are read from. Each wrote one `log_write()` line to journald, recording
*who touched what* and never *what it was before* — so "who moved UTA's
Guaranteed Salary, and when" was unanswerable.

Every write in the API funnels through `storage._atomic_write`, which is the
one place this hooks: the old text is read before the `os.replace`, the diff is
appended after it, to `edits.jsonl` in NBS_DATA_DIR. Read it at
`GET /api/edits` — **public since 2026-09-01** (`key=curry-stephen` for one
player, `file=uta-roster.csv` for one team, `actor=` for one member). The
player page's Edit History section calls it unauthenticated, scoped with
`key=<slug>` so a visitor only ever pulls diffs mentioning that one player —
nothing there renders the endpoint with no `key`, which is the query that
would dump the whole league's contract-diff history at once.

Three properties it must keep:

| Property | Why |
|---|---|
| **Allowlist, not denylist** (`_AUDITED_NAMES` / `_AUDITED_SUFFIXES`) | `members.json` and `sessions.json` hold live credentials (and `tokens.json`, retired, is still listed in case a copy turns up); a value-level diff copies bearer tokens into a plaintext log. `_NEVER` restates that as a hard guard so widening the allowlist can't undo it by accident |
| **Bounded lines** (`_MAX_CHANGES`, `_MAX_VALUE_CHARS`, `_MAX_DIFF_BYTES`) | one entry stays appendable in a single write, and the 27MB allstats CSVs are never diffed |
| **Never breaks a write** | every entry point swallows its own exceptions. An audit log that can 500 a roster save is worse than none |

The actor is carried on a dict the middleware in `main.py` installs and
`get_token_info` **mutates**. Don't change that to a `ContextVar.set()` inside
the dependency: FastAPI runs a sync dependency and its endpoint as two separate
threadpool tasks with independent copies of the context, so the endpoint would
never see it. `tests/test_audit_log.py` exercises that path end to end because
it is the piece that fails silently. Writes with no request behind them
(timers, the relay, CLI scripts) record as `system`.

## Cap history (`routers/cap_history.py`)

A daily row per team — Team Salary on both bases, apron position, hard-cap
level, MLE/BAE state, roster counts — appended to `cap-history.jsonl` in
NBS_DATA_DIR by `nbn-cap-history.timer` (23:40 UTC, `Persistent=true`).

It exists because every cap figure the site shows is *now*. § 7.3's
second-apron pick freeze needs a four-year lookback, and nothing was banking
that time: the freeze was never "blocked until 2029", it was blocked until four
years after someone started recording. Started 2026-08-25.

| Route | Auth | What |
|---|---|---|
| `GET /api/cap-history` | Public | the series; `team`, `since`, `until`, `limit` |
| `GET /api/cap-history/current` | Public | what today's snapshot *would* record, computed live |
| `POST /api/cap-history/snapshot` | `rosters` | record now; `force=true` re-records a recorded day |

Two invariants:

- **It does no cap math of its own.** The figures come from `_compute_team_salary`
  and `_compute_team_salary_ex_holds` — the validators' own helpers — plus
  `_real_empty_roster_charge` on top of both, which is § 2.1a's real charge for
  a roster under **12** standard players (not `_empty_roster_charge`, the wider
  14-player mock the trade validator folds into a *projection*). The charge
  belongs here because these rows are a team's actual books on a date, and
  § 2.1a counts it toward real guaranteed salary, hard cap and aprons included;
  it was missing until 2026-09-19, and a short-handed team's row read low by it
  while the team's own page — which has always charged it, via
  `computeEmptyRosterCharge` in `nbn-today/teams/team.js` — read high. The row
  reports it separately as `empty_roster_charge`, which is what lets
  `/committees/rosters` name the amount without a second implementation, and
  what makes a pre-2026-09-19 row recognisable rather than silently different.
  `tests/test_cap_history.py` recomputes all 30 teams both ways to pin it.
  Don't inline a salary sum here.
- **A `0` threshold is unknown, not exceeded.** 27-28 onward sit in
  `cap-levels.json` with `cap`/`apron1`/`apron2` all literally 0 (the committee
  figures aren't entered — it's a live P1 in `nbn-today/BACKLOG.md`). Read
  naively that puts every team over every apron, which under § 7.3 is a
  fabricated freeze. `_cap_levels_for` normalises 0 to `None` and
  `_apron_position` returns `None` for an unknown threshold.

The snapshot is idempotent per date — a second run on a recorded day is a
no-op, since a duplicated day double-counts in anything built off the series and
the timer can fire twice after a reboot. Run it by hand with
`venv/bin/python snapshot_cap_history.py` (`--dry-run`, `--date`, `--force`).

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

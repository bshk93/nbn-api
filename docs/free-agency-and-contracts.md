# Free agency, options and the season sweeps

The PDC free-agency pipeline, team option windows and the opening-night FA hold sweep. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

## PDC free agency (`routers/free_agency.py`)

Owner-submitted FA offers reviewed by the Free Agency Committee. The design
record is `nbn-today/docs/pdc-free-agency-spec.md`; read it before changing
anything here. Storage is three files under **one** `_fa_lock`: `fa-state.json`
(mode, rounds, per-player status + sub-committee), `fa-offers.json`,
`fa-ballots.json`. Roles: `fac`, `fac_head` (implies `fac` **and `agent`**),
`agent`, `poext`, `poext_head`. The dashboard that renders all of it is
`nbn-today/committees/pdc/index.html`.

Two invariants, neither negotiable:

- **`offer` is a verbatim `SignDetails`.** It is what `POST /api/validate/sign`
  takes and what a future "sign this offer" button would post to
  `POST /api/transactions`. Pitch and promises live *outside* it. Adding a field
  `SignDetails` doesn't accept turns a ~30-line follow-up into a rewrite.
- **Legality has one implementation** — `_validate_sign`, the same call the real
  submit path runs, never with `force`. Nothing in this module does cap math of
  its own; every figure comes from `_signing_fact_sheet` and its helpers.

Derived rules live server-side so there is one of each: `revised_since` (a ballot
cast before an offer was revised), `voided_since` (§ 4.3b — balls on an offer the
head has since voided), `returned_since` (§ 4.7 — a ballot cast before the head
sent the slate back to its agent), `your_conflict` / `assignable` (§ 4.6
conflicts, from `_conflict_team` — a conflict comes from an active *tenure*, not
a team role), `stage` / `claim_refusal` (§ 4.7, below), and `balloted` /
`ballots_cast` on `GET /api/fa/state` (own ballot always, everyone's count
head-only). `tests/test_fa_offers.py` and `tests/test_fa_pool.py` pin the lot.

### The agent stage (§ 4.7)

Between a closed offer window and a sub-committee ballot. Agents **claim**
players off a shared queue (no per-player assignment, no head allocating them),
negotiate the offers down, then either `advance` the survivors or `finalize` an
uncontested one. `claim` / `release` / `advance` / `return-to-agent` (head-only)
plus `PUT /api/fa/offers/{id}/agent-note`.

Four things that are load-bearing:

- **The stage is derived, never stored.** `_agent_stage` reads `status`, the FFA
  clock, `agent.advanced_at` and the finalize record. Storing it would be a
  second copy of what those already determine — the roster-CSV `OVR` trap.
- **`agent` is round-scoped; `blocked_teams` is not.** A claim belongs to its
  window and a reopen mints a new `round_id`, but the bar a claim puts on the
  agent's own team is **permanent** — it survives release and reopen. That
  permanence is the only reason release is safe to offer: otherwise an agent
  claims, reads every rival's figure, releases, and bids.
- **This is the one hard block in a subsystem that otherwise only warns.**
  § 4.6 is warn-don't-block everywhere else; D21 refuses the claim outright.
  `_member_teams` is the **union** of a current front-office tenure and any held
  team role — either alone makes the conflict real.
- **`_require_curator` replaced `_require_reviewer` on remand/void/restore**,
  reversing D14: those are the claiming agent's (while un-advanced) plus
  head/admin, and assigned sub-committee members no longer have them.
  `_require_reviewer` still admits the claiming agent (reading every offer is
  what a claim grants); `_require_ballot_viewer` does not — an agent never sees
  a ballot, on any player.

Nothing is balloted before the advance (`cast_ballot` 422s), and `final.path`
records `agent` vs `committee` — the **route, not the actor**, so a head locking
a player who never went to a sub-committee still reads as uncontested.

**An offer's status is the only thing that decides whether it's in play.**
`_is_live` = not archived and `status in LIVE_STATUSES`, and it is the single
gate behind the ballot options, `_team_commitment`, `_conflict_team`, the
one-live-offer-per-team rule, and every edit/submit/remand guard. `voided`
(§ 4.3b, head-only, reversible via `/restore`) is deliberately outside that set
so all of it follows without a second rule; `archived_at` (§ 4.2, stamped by
finalize) is the other half. If you add a status, decide which side of `_is_live`
it sits on and change nothing else.

## Team option windows (§ 6.1)

Set by the league office 2026-10-19. Two kinds of team option, told apart by
`season_calendar.is_rookie_scale_option` (a first-rounder's 3rd and 4th
seasons, off the bio's draft record):

- **Rookie-scale:** decided any time up to the deadline, October 31 or the
  Monday after a weekend (`team_option_deadline`), in the season before the
  option year.
- **Every other team option:** decided from the day after the previous
  regular season's last game through June 30 (`team_option_window`).

Across the board, an option not decided in its window is declined
automatically as a UFA hold. `_option_window_check` (check id `option_window`)
refuses an option decided outside its window. It's an error, so owners can't
override it; the office can force a correction. `nbn-team-options.timer` runs
`sweep_team_options.py` hourly (`sweep_team_options` in `transactions.py`):
inbox reminders to each team at 48 and 24 hours before any window closes (once
per close date, recorded in `team-option-notices.json`), and the automatic
declines once one has, announced like an owner's own decline. `--dry-run`
lists every undecided option by close date and writes nothing. Pinned by
`tests/test_team_option_sweep.py` and `tests/test_season_start_rules.py`.

**Free-agent holds lapse at opening night** (§ 3.10, set 2026-10-20). A
UFA/RFA hold for the current season still on a roster from opening night on
(midnight league time) is renounced by `nbn-fa-holds.timer`, hourly, through
`sweep_fa_holds.py` (`sweep_fa_holds` in `transactions.py`). Each is a normal
`renounce` with its snapshot, created by `system`, `_source:
"auto_opening_night"`, announced like an owner's own, so `rescind_renounce`
can undo one. An RFA with an open offer sheet is skipped. `--dry-run` lists
the holds. Pinned by `tests/test_fa_hold_sweep.py`.

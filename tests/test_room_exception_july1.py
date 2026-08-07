"""Regression tests for the § 3.2 Room Exception "as of July 1" test.

Room Exception eligibility (unlike NTMLE/TMLE, whose § 1.5/§ 1.6 apron bars
stay live all season on purpose — see test_exception_absorption_split's
test_apron_bars_are_still_live) is supposed to be locked in based on Team
Salary as of the league year's start, not whatever it is at the moment of the
check. Before this, the only implementation of that idea was a live-current-
salary *approximation* used when nothing was recorded yet in team_state — and
`_check_signing_method_funding` (the `sign` path) had no Room Exception
eligibility check at all, only an amount check. This exercises
`_team_salary_as_of_league_year_start`, the real reconstruction that replaces
the approximation, plus its use in both check functions.

Built off the real McCollum/WAS 2026-08-03 signing: WAS's current (post-
signing) Team Salary reads well above the Room ceiling, but its actual
Team Salary as of 2026-07-01 — before the 7/2 Kyrie Irving trade and the
7/13 moves — was comfortably under it, which is what made that signing legal
via the Room Exception despite WAS's current apron position.

    venv/bin/python -m tests.test_room_exception_july1
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []
SEASON = "26-27"


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def _with_fixtures(txns, current_with_holds, current_ex_holds, fn, baseline=None):
    """Run `fn` with `_load_transactions`, `_compute_team_salary`,
    `_compute_team_salary_ex_holds`, `_league_rollovers`, and
    `load_room_zone_baseline` all stubbed to keep this a pure, disk-free test
    that never touches the real production baseline file — the real July-1
    rollover date logic (`_season_start_date`) is left untouched since it's a
    pure string/date function with no I/O of its own once rollovers is
    stubbed to {}."""
    orig_txns = T._load_transactions
    orig_with = T._compute_team_salary
    orig_ex = T._compute_team_salary_ex_holds
    orig_rollovers = T._league_rollovers
    orig_baseline = T.load_room_zone_baseline
    T._load_transactions = lambda: txns
    T._compute_team_salary = lambda team, bios, season: current_with_holds
    T._compute_team_salary_ex_holds = lambda team, bios, season: current_ex_holds
    T._league_rollovers = lambda: {}
    T.load_room_zone_baseline = lambda: baseline or {}
    try:
        return fn()
    finally:
        T._load_transactions = orig_txns
        T._compute_team_salary = orig_with
        T._compute_team_salary_ex_holds = orig_ex
        T._league_rollovers = orig_rollovers
        T.load_room_zone_baseline = orig_baseline


def _trade(date, from_team, to_team, slug, txn_id="t1"):
    return {"id": txn_id, "type": "trade", "date": date, "details": {
        "teams": [from_team, to_team],
        "transfers": [{"from_team": from_team, "to_team": to_team,
                       "assets": [{"type": "player", "slug": slug}]}],
    }}


def _sign(date, team, slug, salary, txn_id="s1"):
    return {"id": txn_id, "type": "sign", "date": date, "details": {
        "team": team, "player": slug,
        "contract": {"salaries": {SEASON: salary}},
    }}


BIOS = {
    "in-player":  {"salaries": {SEASON: "$10,000,000"}},
    "out-player": {"salaries": {SEASON: "$4,000,000"}},
    "signed-player": {"salaries": {SEASON: "$6,000,000"}},
}


def test_reversal_math():
    print("\nreconstruction — reversing known deltas")

    # Trade after rollover: WAS took in $10M, sent out $4M -> net +$6M since
    # July 1. Current is $100M, so July-1 should read $94M.
    txns = [_trade("2026-07-02", "OTH", "WAS", "in-player")]

    def run():
        return T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS)
    with_holds, ex_holds, warning = _with_fixtures(txns, 100_000_000, 100_000_000, run)
    check("no warning on a clean single trade", warning is None)
    check("with-holds reversed by the incoming salary", with_holds == 90_000_000)
    check("ex-holds reversed the same way", ex_holds == 90_000_000)

    # A trade sending a player out lowers current, so July 1 must have been
    # higher by that amount.
    txns2 = [_trade("2026-07-02", "WAS", "OTH", "out-player")]
    with_holds2, _, warning2 = _with_fixtures(
        txns2, 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("outgoing trade reverses to a higher July-1 figure", with_holds2 == 104_000_000)
    check("no warning", warning2 is None)

    # A sign after rollover added $6M -> July 1 must have been $6M lower.
    txns3 = [_sign("2026-07-05", "WAS", "signed-player", "$6,000,000")]
    with_holds3, _, warning3 = _with_fixtures(
        txns3, 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("sign reverses to a lower July-1 figure", with_holds3 == 94_000_000)
    check("no warning", warning3 is None)

    # Transactions on/before the rollover date itself don't count as "since"
    # July 1 — only strictly-after moves get reversed.
    txns4 = [_sign("2026-07-01", "WAS", "signed-player", "$6,000,000")]
    with_holds4, _, warning4 = _with_fixtures(
        txns4, 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("a same-day rollover transaction isn't reversed", with_holds4 == 100_000_000)

    # A transaction for a different team doesn't affect this team's number.
    txns5 = [_sign("2026-07-05", "OTH", "signed-player", "$6,000,000")]
    with_holds5, _, warning5 = _with_fixtures(
        txns5, 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("another team's sign is ignored", with_holds5 == 100_000_000)


def test_abstains_when_it_should():
    print("\nreconstruction — abstains rather than guesses")

    # A player moved twice since rollover: current bio can't tell which
    # figure applied at which trade, so this must refuse to guess.
    txns = [
        _trade("2026-07-02", "OTH", "WAS", "in-player", txn_id="t1"),
        _trade("2026-07-10", "WAS", "THIRD", "in-player", txn_id="t2"),
    ]
    _, _, warning = _with_fixtures(
        txns, 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("a player touched twice triggers an abstention", warning is not None)
    check("abstention names the player", warning and "in-player" in warning)

    # A release for this team since rollover: dead-cap split isn't
    # recoverable from the log entry, so abstain rather than guess.
    release_txn = {"id": "r1", "type": "release", "date": "2026-07-05",
                   "details": {"team": "WAS", "player": "out-player"}}
    _, _, warning2 = _with_fixtures(
        [release_txn], 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("a release since rollover triggers an abstention", warning2 is not None)

    # Same release, but for a DIFFERENT team — must not block WAS's own
    # reconstruction just because it shares a transaction log.
    release_other = {"id": "r2", "type": "release", "date": "2026-07-05",
                     "details": {"team": "OTHER", "player": "out-player"}}
    _, _, warning3 = _with_fixtures(
        [release_other], 100_000_000, 100_000_000,
        lambda: T._team_salary_as_of_league_year_start("WAS", SEASON, BIOS))
    check("another team's release doesn't block this team", warning3 is None)


CAP_LEVELS = {SEASON: {"cap": 164_961_000, "apron1": 209_015_000, "apron2": 221_686_000,
                       "ntmle_amount": 15_044_000, "tmle_amount": 6_064_000, "room_amount": 9_366_000}}
ROOM_CEILING = 164_961_000 - 15_044_000  # 149,917,000


def test_room_eligibility_wraps_reconstruction():
    print("\n_room_exception_july1_eligible")

    under = _with_fixtures(
        [], ROOM_CEILING - 1, ROOM_CEILING - 1,
        lambda: T._room_exception_july1_eligible("WAS", SEASON, BIOS, CAP_LEVELS))
    check("just under the ceiling is eligible", under is True)

    over = _with_fixtures(
        [], ROOM_CEILING + 1, ROOM_CEILING + 1,
        lambda: T._room_exception_july1_eligible("WAS", SEASON, BIOS, CAP_LEVELS))
    check("just over the ceiling is not eligible", over is False)

    abstained = _with_fixtures(
        [{"id": "r1", "type": "release", "date": "2026-07-05",
          "details": {"team": "WAS", "player": "x"}}],
        ROOM_CEILING - 1, ROOM_CEILING - 1,
        lambda: T._room_exception_july1_eligible("WAS", SEASON, BIOS, CAP_LEVELS))
    check("an abstention surfaces as None, not a guess", abstained is None)


def test_sign_path_gate_only_fires_with_bios():
    print("\n_check_signing_method_funding — Room gate is bios-gated")

    # Without bios (existing test-suite call style): no new gate, matches
    # pre-fix behavior exactly — only the amount check applies.
    r = T._check_signing_method_funding(
        "WAS", "room_exception", 9_366_000, ROOM_CEILING + 50_000_000,
        ROOM_CEILING + 50_000_000, SEASON, CAP_LEVELS, {},
    )
    check("no bios passed -> no eligibility gate, amount fits -> passes", r is None)

    # With bios and a reconstruction that says "over the July-1 ceiling":
    # the new gate should reject even though the amount itself would fit.
    def run_over():
        return T._check_signing_method_funding(
            "WAS", "room_exception", 9_366_000, ROOM_CEILING + 50_000_000,
            ROOM_CEILING + 50_000_000, SEASON, CAP_LEVELS, {}, bios=BIOS,
        )
    r2 = _with_fixtures([], ROOM_CEILING + 50_000_000, ROOM_CEILING + 50_000_000, run_over)
    check("bios passed, July-1 over ceiling -> rejected", r2 is not None)
    check("...cites § 3.2", r2 is not None and "§ 3.2" in r2.message)

    def run_under():
        return T._check_signing_method_funding(
            "WAS", "room_exception", 9_366_000, ROOM_CEILING - 50_000_000,
            ROOM_CEILING - 50_000_000, SEASON, CAP_LEVELS, {}, bios=BIOS,
        )
    r3 = _with_fixtures([], ROOM_CEILING - 50_000_000, ROOM_CEILING - 50_000_000, run_under)
    check("bios passed, July-1 under ceiling -> passes", r3 is None)

    # A locked assignment always wins outright, regardless of bios/reconstruction.
    def run_locked():
        return T._check_signing_method_funding(
            "WAS", "room_exception", 9_366_000, ROOM_CEILING + 50_000_000,
            ROOM_CEILING + 50_000_000, SEASON, CAP_LEVELS,
            {"WAS": {SEASON: {"mle_type": "room", "mle_used": 0}}}, bios=BIOS,
        )
    r4 = _with_fixtures([], ROOM_CEILING + 50_000_000, ROOM_CEILING + 50_000_000, run_locked)
    check("already-locked assignment skips reconstruction entirely", r4 is None)


def test_baseline_snapshot_wins_over_reconstruction():
    print("\n_room_exception_july1_eligible — real snapshot beats reconstruction")

    # A release since rollover would normally force an abstention — but with
    # a recorded snapshot on file, reconstruction is never even attempted.
    release_txn = {"id": "r1", "type": "release", "date": "2026-07-05",
                   "details": {"team": "WAS", "player": "x"}}
    baseline = {SEASON: {"WAS": {"with_holds": ROOM_CEILING - 1, "ex_holds": ROOM_CEILING - 1}}}
    eligible = _with_fixtures(
        [release_txn], 999_999_999, 999_999_999,
        lambda: T._room_exception_july1_eligible("WAS", SEASON, BIOS, CAP_LEVELS),
        baseline=baseline)
    check("recorded snapshot is used even though reconstruction would abstain", eligible is True)

    baseline_over = {SEASON: {"WAS": {"with_holds": ROOM_CEILING + 1, "ex_holds": ROOM_CEILING + 1}}}
    eligible2 = _with_fixtures(
        [], 1, 1,
        lambda: T._room_exception_july1_eligible("WAS", SEASON, BIOS, CAP_LEVELS),
        baseline=baseline_over)
    check("recorded snapshot over the ceiling is refused regardless of current/reconstructed salary",
          eligible2 is False)

    # A team with no entry for this season falls through to reconstruction as before.
    eligible3 = _with_fixtures(
        [], ROOM_CEILING - 1, ROOM_CEILING - 1,
        lambda: T._room_exception_july1_eligible("OTHER", SEASON, BIOS, CAP_LEVELS),
        baseline=baseline)
    check("a team absent from the baseline still falls back to reconstruction", eligible3 is True)


def test_snapshot_room_zone_baseline():
    print("\nsnapshot_room_zone_baseline — idempotent, freshness-windowed")

    orig_bios = T.load_player_bios
    orig_with = T._compute_team_salary
    orig_ex = T._compute_team_salary_ex_holds
    orig_rollovers = T._league_rollovers
    orig_save = T.save_room_zone_baseline
    saved = {}

    T.load_player_bios = lambda: {}
    T._compute_team_salary = lambda team, bios, season: 100_000_000
    T._compute_team_salary_ex_holds = lambda team, bios, season: 90_000_000
    T.save_room_zone_baseline = lambda data: saved.update({"data": data})

    try:
        # Rollover 2 days ago (within the default 7-day window): snapshots everyone.
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        T._league_rollovers = lambda: {SEASON: recent}
        store = {}
        T.load_room_zone_baseline = lambda: store
        snapshotted = T.snapshot_room_zone_baseline(SEASON)
        check("a recent rollover snapshots all 30 teams", len(snapshotted) == 30)
        check("saved payload carries the computed figures",
              saved["data"][SEASON]["WAS"]["with_holds"] == 100_000_000
              and saved["data"][SEASON]["WAS"]["ex_holds"] == 90_000_000)

        # Idempotent: run again against the now-populated store, nobody's re-snapshotted.
        T.load_room_zone_baseline = lambda: saved["data"]
        again = T.snapshot_room_zone_baseline(SEASON)
        check("a second pass with everyone already recorded snapshots nobody", again == [])

        # Rollover 30 days ago (outside the window): must not back-date a
        # season that's already been running — refuses rather than snapshot
        # a stale live figure mislabeled as July 1.
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        T._league_rollovers = lambda: {"25-26": old}
        T.load_room_zone_baseline = lambda: {}
        stale = T.snapshot_room_zone_baseline("25-26")
        check("a season whose rollover is long past is not snapshotted", stale == [])

        # A rollover still in the future: also a no-op (nothing to snapshot yet).
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        T._league_rollovers = lambda: {"27-28": future}
        future_result = T.snapshot_room_zone_baseline("27-28")
        check("a season that hasn't rolled over yet is not snapshotted", future_result == [])
    finally:
        T.load_player_bios = orig_bios
        T._compute_team_salary = orig_with
        T._compute_team_salary_ex_holds = orig_ex
        T._league_rollovers = orig_rollovers
        T.save_room_zone_baseline = orig_save


def test_real_was_mccollum_case():
    print("\nreal data — WAS's actual July-1 position (2026-08-03 McCollum signing)")
    import json
    bios = json.loads((T.DATA_DIR / "player-bios.json").read_text())
    cap_levels = json.loads(T.CAP_LEVELS_FILE.read_text())
    with_holds, ex_holds, warning = T._team_salary_as_of_league_year_start("WAS", SEASON, bios)
    if warning:
        check(f"real WAS reconstruction (informational — abstained: {warning})", True)
    else:
        room_ceiling = cap_levels[SEASON]["cap"] - cap_levels[SEASON]["ntmle_amount"]
        check(f"WAS July-1 with-holds (${with_holds:,}) is under the Room ceiling (${room_ceiling:,})",
              with_holds is not None and with_holds < room_ceiling)


def main():
    test_reversal_math()
    test_abstains_when_it_should()
    test_room_eligibility_wraps_reconstruction()
    test_sign_path_gate_only_fires_with_bios()
    test_baseline_snapshot_wins_over_reconstruction()
    test_snapshot_room_zone_baseline()
    test_real_was_mccollum_case()
    print("\n" + "=" * 40)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

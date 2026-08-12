"""Regression tests for routers.waivers — the § 5.1 waiver wire (design record:
nbn-today/docs/waiver-wire-spec.md). Covers the ledger-derived pending-state
enumeration (mirrors `_open_offer_sheets`'s guarantees) and the spec § 5
priority-resolution algorithm: worst qualifying record wins, a 2-way tie is
broken by head-to-head, and anything that survives that (a still-tied 2-way,
or any 3+-way tie) is left unresolved for a manual PDC call rather than
guessed at.

Deliberately does NOT exercise `_apply_waiver_transfer`/`_resolve_one_waiver`'s
actual roster/bio/deadcap file writes — those go through `_apply_sign`, which
is exercised by the existing signing test suites, and building a safe
temp-DATA_DIR fixture for it here was out of scope for this pass. Everything
below either reads a temp CSV this file writes itself, or drives the pure
ledger/priority logic off a synthetic `_load_transactions()`.

    venv/bin/python -m tests.test_waivers
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.waivers as w  # noqa: E402
from routers.storage import write_csv  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def claim(team, method="cap_space", txn_id=None):
    return {"team": team, "signing_method": method, "eaps_assumption": None,
            "txn_id": txn_id or f"claim-{team.lower()}"}


# ── _h2h_record ────────────────────────────────────────────────────────────────

def test_h2h_record():
    print("_h2h_record")
    tmp = Path(tempfile.mkdtemp())
    season = "26-27"
    path = tmp / f"allstats-{season}.csv"
    # Two games: PHX beats SAC on 2027-01-05 (two player rows per team, same
    # result, must dedupe to one game); SAC beats PHX on 2027-01-20.
    rows = [
        {"TEAM": "PHX", "OPP": "SAC", "DATE": "2027-01-05", "WL": "W"},
        {"TEAM": "PHX", "OPP": "SAC", "DATE": "2027-01-05", "WL": "W"},  # 2nd player, same game
        {"TEAM": "SAC", "OPP": "PHX", "DATE": "2027-01-05", "WL": "L"},
        {"TEAM": "SAC", "OPP": "PHX", "DATE": "2027-01-20", "WL": "W"},
        {"TEAM": "PHX", "OPP": "SAC", "DATE": "2027-01-20", "WL": "L"},
        {"TEAM": "PHX", "OPP": "MIL", "DATE": "2027-01-25", "WL": "W"},  # unrelated matchup
    ]
    write_csv(path, ["TEAM", "OPP", "DATE", "WL"], rows)

    orig_allstats_path = w.allstats_path
    w.allstats_path = lambda season_, gt: path if season_ == season and gt == "REG" else orig_allstats_path(season_, gt)
    try:
        wins_phx, wins_sac = w._h2h_record("PHX", "SAC", season)
        check("dedupes multi-player rows to one game — PHX 1 win", wins_phx == 1, wins_phx)
        check("SAC 1 win", wins_sac == 1, wins_sac)
        check("unrelated opponent (MIL) doesn't leak in", wins_phx + wins_sac == 2)

        wins_x, wins_y = w._h2h_record("PHX", "DAL", season)
        check("no meetings at all is (0, 0)", (wins_x, wins_y) == (0, 0))
    finally:
        w.allstats_path = orig_allstats_path


# ── _relevant_record_season ──────────────────────────────────────────────────

def test_relevant_record_season():
    print("\n_relevant_record_season (spec § 5 step 2, Dec 1 cutoff)")
    orig = w._season_for_date
    w._season_for_date = lambda date, *a, **k: "26-27"
    try:
        check("before Dec 1 uses the PRIOR season",
              w._relevant_record_season("2026-11-15") == "25-26")
        check("on Dec 1 itself uses the CURRENT season",
              w._relevant_record_season("2026-12-01") == "26-27")
        check("after Dec 1 uses the CURRENT (live) season",
              w._relevant_record_season("2027-02-01") == "26-27")
    finally:
        w._season_for_date = orig


# ── _priority_order ───────────────────────────────────────────────────────────

def test_priority_order():
    print("\n_priority_order (spec § 5 steps 1-5)")

    check("zero claims", w._priority_order([], "2026-11-15")["status"] == "no_claims")

    orig_season = w._relevant_record_season
    orig_pct = w._team_record_pct
    orig_h2h = w._h2h_record
    w._relevant_record_season = lambda date: "26-27"
    try:
        # Clear worst record wins outright.
        w._team_record_pct = lambda team, season: {"PHX": 0.700, "SAC": 0.200}[team]
        result = w._priority_order([claim("PHX"), claim("SAC")], "2026-11-15")
        check("worst record (SAC) wins outright", result["status"] == "resolved")
        check("...SAC ordered first", result["order"][0]["team"] == "SAC")

        # Identical records, distinct head-to-head — worse H2H record wins the tie.
        w._team_record_pct = lambda team, season: 0.500
        w._h2h_record = lambda a, b, season: (3, 1)  # a beat b 3-1 -> b is "worse", wins priority
        result = w._priority_order([claim("PHX"), claim("SAC")], "2026-11-15")
        check("tied record, H2H breaks it", result["status"] == "resolved")
        check("...team that LOST the season series (SAC) gets priority",
              result["order"][0]["team"] == "SAC", result["order"])

        # Identical records AND a split season series -> unresolved, flagged for PDC.
        w._h2h_record = lambda a, b, season: (2, 2)
        result = w._priority_order([claim("PHX"), claim("SAC")], "2026-11-15")
        check("tied record + tied H2H is NOT auto-resolved", result["status"] == "tied")
        check("...names both tied teams", set(result["tied_teams"]) == {"PHX", "SAC"})
        check("...carries the H2H figures for the PDC post", result["h2h"] == {"PHX": 2, "SAC": 2})

        # No meetings at all this season -> also unresolved (0-0 is still a tie).
        w._h2h_record = lambda a, b, season: (0, 0)
        result = w._priority_order([claim("PHX"), claim("SAC")], "2026-11-15")
        check("no head-to-head meetings this season also flags for PDC", result["status"] == "tied")

        # Three-way tie never attempts a round-robin guess.
        w._team_record_pct = lambda team, season: 0.500
        result = w._priority_order([claim("PHX"), claim("SAC"), claim("DEN")], "2026-11-15")
        check("3-way tie skips H2H entirely and flags for PDC", result["status"] == "tied")
        check("...h2h is explicitly None, not a guess", result["h2h"] is None)
        check("...names all three", set(result["tied_teams"]) == {"PHX", "SAC", "DEN"})

        # Two clearly-worst, one clear best: only the tied pair should be flagged.
        w._team_record_pct = lambda team, season: {"PHX": 0.200, "SAC": 0.200, "DEN": 0.700}[team]
        w._h2h_record = lambda a, b, season: (0, 0)
        result = w._priority_order([claim("PHX"), claim("SAC"), claim("DEN")], "2026-11-15")
        check("DEN's better record excludes it from the tie", result["status"] == "tied")
        check("...only the genuinely tied pair is named", set(result["tied_teams"]) == {"PHX", "SAC"})
    finally:
        w._relevant_record_season = orig_season
        w._team_record_pct = orig_pct
        w._h2h_record = orig_h2h


def test_team_record_pct_reads_real_csv():
    print("\n_team_record_pct (against a real standings-history.csv shape)")
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "standings-history.csv"
    write_csv(path, ["SEASON", "TEAM", "PCT"], [
        {"SEASON": "25-26", "TEAM": "PHX", "PCT": "0.78"},
        {"SEASON": "26-27", "TEAM": "PHX", "PCT": "0.33"},
    ])
    orig = w.STANDINGS_CSV
    w.STANDINGS_CSV = path
    try:
        check("reads the row for the requested season, not another one",
              w._team_record_pct("PHX", "25-26") == 0.78)
        check("in-progress current season reads live too",
              w._team_record_pct("PHX", "26-27") == 0.33)
        check("unknown team/season -> None, not a crash",
              w._team_record_pct("PHX", "27-28") is None)
    finally:
        w.STANDINGS_CSV = orig


# ── ledger-derived enumeration (mirrors _open_offer_sheets' guarantees) ──────

def release_txn(tid, player="jones-marcus", team="PHX", created_at=None, snapshot=True):
    return {
        "id": tid, "type": "release", "date": "2026-11-15",
        "created_at": created_at or "2026-11-15T12:00:00Z",
        "details": {
            "player": player, "team": team,
            "terminated_salary": "$20,000,000",
            "_snapshot": ({"type": "player", "salaries": {"26-27": "$20,000,000"},
                          "cap_holds": {}, "guaranteed": {}, "guarantee_dates": {}}
                         if snapshot else None),
        },
    }


def claim_txn(tid, released_txn_id, team, created_at="2026-11-15T13:00:00Z"):
    return {"id": tid, "type": "waiver_claim", "created_at": created_at,
            "details": {"released_txn_id": released_txn_id, "team": team,
                       "signing_method": "cap_space"}}


def withdraw_txn(tid, claim_txn_id, released_txn_id, team):
    return {"id": tid, "type": "waiver_claim_withdraw",
            "details": {"claim_txn_id": claim_txn_id, "released_txn_id": released_txn_id, "team": team}}


def clear_txn(tid, released_txn_id, outcome="unclaimed"):
    return {"id": tid, "type": "waiver_clear", "details": {"released_txn_id": released_txn_id, "outcome": outcome}}


def test_waiver_deadline():
    print("\n_waiver_deadline")
    t = release_txn("r1", created_at="2026-11-15T12:00:00Z")
    d = w._waiver_deadline(t)
    check("deadline is created_at + 48h", d == datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc), d)
    check("no created_at -> None", w._waiver_deadline({"details": {}}) is None)


def test_claims_and_resolution_enumeration():
    print("\n_waiver_claims_for / _is_waiver_resolved / _open_waivers")

    r1 = release_txn("r1")
    c1 = claim_txn("c1", "r1", "SAC")
    c2 = claim_txn("c2", "r1", "DEN")
    wd = withdraw_txn("wd1", "c2", "r1", "DEN")
    ledger = [r1, c1, c2, wd]

    claims = w._waiver_claims_for("r1", ledger)
    check("live claims only — withdrawn one excluded", [c["team"] for c in claims] == ["SAC"])

    check("not resolved without a waiver_clear", w._is_waiver_resolved("r1", ledger) is False)
    ledger2 = ledger + [clear_txn("cl1", "r1", "claimed")]
    check("resolved once a waiver_clear names it", w._is_waiver_resolved("r1", ledger2) is True)

    # _open_waivers: a release from a very old timestamp should still surface
    # (this function itself doesn't filter by deadline — the sweep does that
    # separately; _open_waivers is a pure "not yet resolved" enumeration).
    orig_load = w._load_transactions
    w._load_transactions = lambda: ledger
    try:
        open_list = w._open_waivers()
        check("one open waiver listed", len(open_list) == 1 and open_list[0]["txn_id"] == "r1")
        check("player/released_by carried through", open_list[0]["player"] == "jones-marcus"
              and open_list[0]["released_by"] == "PHX")

        # A release with no _snapshot (predates this feature) is never offered.
        r_old = release_txn("r_old", snapshot=False)
        w._load_transactions = lambda: [r_old]
        check("pre-snapshot release is skipped, not shown broken", w._open_waivers() == [])

        # Resolved releases drop out.
        w._load_transactions = lambda: ledger2
        check("resolved release no longer listed as open", w._open_waivers() == [])
    finally:
        w._load_transactions = orig_load


def main():
    test_h2h_record()
    test_relevant_record_season()
    test_priority_order()
    test_team_record_pct_reads_real_csv()
    test_waiver_deadline()
    test_claims_and_resolution_enumeration()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()

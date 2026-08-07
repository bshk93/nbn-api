"""Regression tests for routers.transactions._bird_tenure and
_check_bird_rights_declaration — the § 3.8 gate that checks a declared Bird
tier against continuous service derived from the transaction ledger.

Written 2026-08-07. Before this, `bird_rights_type` was pure self-declaration
and `signing_method="bird_rights"` bypassed validation entirely:
_check_signing_method_funding returns early for any method outside the
cap-space/MLE family, so Bird was an unchecked path to sign over the cap while
also unlocking § 3.13's 8% raise ceiling instead of 5%.

The tenure model, and why each piece is the way it is:

  * A trade CARRIES the clock rather than resetting it — § 6.2 recognises
    holding "Bird Rights with the team via trade".
  * Re-signing with your own team CONTINUES the clock. § 3.8 is explicitly
    about "a team re-signing their own free agents"; if re-signing reset the
    clock, no player could ever use Bird rights twice.
  * Signing with a DIFFERENT team resets it, as does a release.
  * The draft seeds the timeline, so a drafted-then-traded player resolves to
    whoever holds them now (not their drafting team).
  * A trade with no earlier record on file gives a *lower bound* only
    ("trade_floor"), since the acquiring team inherits unseen accrual.

Only over-declaration errors, and only from a definite basis. That asymmetry
is the safety property: a gap in the backfilled ledger can only make derived
tenure look LONGER (the most recent signing we can see is an older one), so
"declared above derived" can't be manufactured by missing data. Verified
against all 14 real Bird signings in the production ledger: 12 pass, 2 warn,
0 false-positive errors.

    venv/bin/python -m tests.test_bird_rights_tenure
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []
SEASON = "26-27"


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def with_events(events, bio=None):
    """Pin the ledger index to a synthetic timeline for one player."""
    st = T.TRANSACTIONS_FILE.stat()
    T._BIRD_LEDGER_CACHE.update({"key": (st.st_mtime, st.st_size), "index": {"p": list(events)}})
    return bio or {}


def tenure(events, team, bio=None, season=SEASON):
    return T._bird_tenure("p", team, season, with_events(events, bio))


def main():
    print("tenure derivation")

    # Straight signing: 23-24 through 25-26 is three prior seasons.
    t = tenure([("2023-08-01", "sign", "BOS")], "BOS")
    check("3 prior seasons after a signing -> QVFA", t["tier"] == "QVFA" and t["seasons"] == 3)
    check("...basis is the ledger", t["basis"] == "ledger")

    check("2 prior seasons -> EQVFA",
          tenure([("2024-08-01", "sign", "BOS")], "BOS")["tier"] == "EQVFA")
    check("1 prior season -> Non-QVFA",
          tenure([("2025-08-01", "sign", "BOS")], "BOS")["tier"] == "Non-QVFA")
    check("signed this league year -> 0 seasons, Non-QVFA",
          tenure([("2026-07-05", "sign", "BOS")], "BOS")["seasons"] == 0)

    # § 6.2: rights travel with the player in a trade.
    t = tenure([("2023-08-01", "sign", "BOS"), ("2025-02-01", "trade", "LAL")], "LAL")
    check("a trade carries the clock to the acquiring team (QVFA)", t["tier"] == "QVFA")
    check("...and the origin team no longer holds the tenure",
          tenure([("2023-08-01", "sign", "BOS"), ("2025-02-01", "trade", "LAL")], "BOS")["tier"] is None)

    # § 3.8: re-signing your own free agent keeps accruing.
    t = tenure([("2023-08-01", "sign", "BOS"), ("2025-07-01", "sign", "BOS")], "BOS")
    check("re-signing with the same team CONTINUES the clock", t["tier"] == "QVFA" and t["seasons"] == 3)

    # Signing elsewhere is the disqualifier the rulebook names.
    t = tenure([("2020-08-01", "sign", "BOS"), ("2025-07-01", "sign", "LAL")], "LAL")
    check("signing with a different team RESETS the clock", t["tier"] == "Non-QVFA" and t["seasons"] == 1)

    t = tenure([("2020-08-01", "sign", "BOS"), ("2025-07-01", "release", "BOS")], "BOS")
    check("a release breaks the chain entirely", t["tier"] is None)

    # Draft seeds the timeline rather than acting as a late fallback.
    bio = {"draft_team": "CHA", "draft_year": 2022}
    t = tenure([("2024-11-03", "trade", "DAL")], "DAL", bio)
    check("drafted-then-traded resolves to the current holder", t["tier"] == "QVFA" and t["basis"] == "draft")
    check("...and not to the drafting team", tenure([("2024-11-03", "trade", "DAL")], "CHA", bio)["tier"] is None)

    # Trade with no origin on file: lower bound only.
    t = tenure([("2024-08-22", "trade", "MIL")], "MIL")
    check("a trade with no earlier record gives a trade_floor basis", t["basis"] == "trade_floor")
    check("...with tenure measured from the trade", t["seasons"] == 2)

    # The league year turns over on July 1, so an acquisition in June belongs
    # to the season that is ending, not the one about to start — which counts
    # that (partial) season toward tenure. Deliberately the generous reading:
    # over-counting inflates derived tenure, and inflation can only make the
    # over-declaration error FIRE LESS, never produce a false positive.
    check("a June acquisition counts the league year that is ending",
          tenure([("2024-06-22", "trade", "MIL")], "MIL")["seasons"] == 3)
    check("...while a July acquisition starts the next one",
          tenure([("2024-07-02", "trade", "MIL")], "MIL")["seasons"] == 2)

    check("no records at all -> unknown, not Non-QVFA",
          tenure([], "BOS")["tier"] is None)

    # Committee-confirmed 2026-08-07: an extension does NOT reset Bird tenure.
    # The player never reaches free agency, so service accrues uninterrupted.
    # Pinned by building the index from a ledger that contains an extension:
    # if someone ever adds "extension" to _player_acquisition_index as an
    # acquisition event, every extended player's clock resets and their tier
    # silently downgrades. See docs/extensions.md.
    orig_load = T._load_transactions
    T._load_transactions = lambda: [
        {"type": "sign",      "date": "2022-08-01", "details": {"player": "p", "team": "BOS"}},
        {"type": "extension", "date": "2025-09-01", "details": {"player": "p", "team": "BOS"}},
        {"type": "option",    "date": "2025-10-01", "details": {"player": "p", "team": "BOS"}},
    ]
    try:
        T._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})
        events = T._player_acquisition_index().get("p", [])
        check("an extension is not recorded as an acquisition event",
              all(kind != "extension" for _d, kind, _t in events))
        check("...so tenure still runs from the original signing (QVFA)",
              T._bird_tenure("p", "BOS", SEASON, {})["tier"] == "QVFA")
        check("neither is an option exercise", len(events) == 1)
    finally:
        T._load_transactions = orig_load
        T._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})

    print("\ndeclaration checks")
    bios = {"p": {}}

    def decl(events, team, declared, method=None, bio=None):
        with_events(events, bio)
        return T._check_bird_rights_declaration("p", team, declared, SEASON,
                                                {"p": bio or {}}, method=method)

    r = decl([("2025-08-01", "sign", "BOS")], "BOS", "QVFA")
    check("over-declaring QVFA on 1 season errors", r is not None and not r.passed and r.level == "error")

    r = decl([("2023-08-01", "sign", "BOS")], "BOS", "QVFA")
    check("a correct QVFA declaration passes", r is not None and r.passed)

    r = decl([("2023-08-01", "sign", "BOS")], "BOS", "Non-QVFA")
    check("under-declaring is allowed (a team may claim less)", r is not None and r.passed)

    r = decl([], "BOS", "QVFA")
    check("unverifiable tenure warns, never errors", r is not None and not r.passed and r.level == "warning")

    r = decl([("2025-06-01", "trade", "MIL")], "MIL", "QVFA")
    check("over-declaring against a trade FLOOR warns, not errors",
          r is not None and not r.passed and r.level == "warning")

    check("no tier and no Bird funding -> no check at all",
          decl([("2023-08-01", "sign", "BOS")], "BOS", None) is None)

    r = decl([("2023-08-01", "sign", "BOS")], "BOS", None, method="bird_rights")
    check("Bird-funded with no tier still gets checked", r is not None)

    r = decl([("2023-08-01", "sign", "BOS")], "LAL", None, method="bird_rights")
    check("Bird Rights for another team's player errors",
          r is not None and not r.passed and r.level == "error")

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

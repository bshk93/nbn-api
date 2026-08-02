"""Regression tests for § 4.2a per-player exception absorption.

§ 4.2a is per-player — "a team uses exactly one of the four for a given
incoming player" — so a trade can legitimately match some incoming players
against outgoing salary while funding another out of an exception. The
validator used to model this per-*team*, testing the exception against the
team's entire incoming total, which made a hybrid trade inexpressible: a real
NOP/TOR deal (Kennard + Jackson-Davis matched against Tre Mann, Hawkins
absorbed into an exception) failed with the full $20,652,828 tested against
the exception instead of Hawkins's $6,563,925 alone.

`_exception_absorption_split` is the pure helper both the validate path and
`_apply_trade` now use, so the amount that gets checked is always the amount
billed to the exception's balance. It takes plain dicts — nothing in this
suite touches production data.

    venv/bin/python -m tests.test_exception_absorption_split
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []

SEASON = "26-27"

# The real trade this was built for, with round numbers.
BIOS = {
    "kennard-luke":         {"salaries": {SEASON: "$7,184,700"}},
    "jackson-davis-trayce": {"salaries": {SEASON: "$6,904,203"}},
    "hawkins-jordan":       {"salaries": {SEASON: "$6,563,925"}},
    "mann-tre":             {"salaries": {SEASON: "$8,000,000"}},
}
IN_PLAYERS = {"NOP": ["kennard-luke", "jackson-davis-trayce", "hawkins-jordan"],
              "TOR": ["mann-tre"]}
NOP_INCOMING = 7_184_700 + 6_904_203 + 6_563_925  # 20,652,828


class _Details:
    """Stands in for TradeIn/TradeValidateInput — the helper only reads
    `exception_players`, via getattr, so a bare object is enough."""
    def __init__(self, exception_players=None):
        if exception_players is not None:
            self.exception_players = exception_players


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def split(details, team="NOP", incoming=NOP_INCOMING):
    return T._exception_absorption_split(details, team, incoming, IN_PLAYERS, BIOS, SEASON)


# ── Backward compatibility: no exception_players means all-or-nothing ────────

def test_legacy_behavior():
    print("\nlegacy (no exception_players) — unchanged all-or-nothing")

    absorbed, matched, err = split(_Details())
    check("absent field absorbs the whole incoming total",
          (absorbed, matched, err) == (NOP_INCOMING, 0, None))

    absorbed, matched, err = split(_Details({}))
    check("empty dict absorbs the whole incoming total",
          (absorbed, matched, err) == (NOP_INCOMING, 0, None))

    absorbed, matched, err = split(_Details({"NOP": []}))
    check("empty list for the team absorbs the whole incoming total",
          (absorbed, matched, err) == (NOP_INCOMING, 0, None))

    absorbed, matched, err = split(_Details({"TOR": ["mann-tre"]}))
    check("another team's entry doesn't affect this team",
          (absorbed, matched, err) == (NOP_INCOMING, 0, None))


# ── The hybrid split itself ─────────────────────────────────────────────────

def test_hybrid_split():
    print("\nhybrid split (§ 4.2a per-player)")

    absorbed, matched, err = split(_Details({"NOP": ["hawkins-jordan"]}))
    check("one named player is absorbed alone", absorbed == 6_563_925)
    check("the rest still has to match", matched == 14_088_903)
    check("no error on a clean split", err is None)
    check("split is exhaustive", absorbed + matched == NOP_INCOMING)

    absorbed, matched, err = split(
        _Details({"NOP": ["hawkins-jordan", "kennard-luke"]}))
    check("two named players sum", absorbed == 6_563_925 + 7_184_700)
    check("remainder is the third", matched == 6_904_203)

    absorbed, matched, err = split(
        _Details({"NOP": ["kennard-luke", "jackson-davis-trayce", "hawkins-jordan"]}))
    check("naming every incoming player leaves nothing to match",
          (absorbed, matched) == (NOP_INCOMING, 0))

    # A duplicate slug must not be billed twice — the helper de-dupes.
    absorbed, matched, err = split(
        _Details({"NOP": ["hawkins-jordan", "hawkins-jordan"]}))
    check("a repeated slug is only counted once", absorbed == 6_563_925)


# ── Guard: named players must actually be incoming to that team ─────────────

def test_rejects_bad_slugs():
    print("\nguard — named slugs must be incoming to that team")

    absorbed, matched, err = split(_Details({"NOP": ["mann-tre"]}))
    check("a player leaving the team is rejected", err is not None)
    check("rejection zeroes the split, it doesn't guess",
          (absorbed, matched) == (0, 0))
    check("error names the offending slug", err and "mann-tre" in err)

    _, _, err = split(_Details({"NOP": ["nobody-here"]}))
    check("an unknown slug is rejected", err is not None)

    _, _, err = split(_Details({"NOP": ["hawkins-jordan", "mann-tre"]}))
    check("one bad slug among good ones still rejects", err is not None)

    # Singular/plural wording, so the message reads correctly either way.
    _, _, err1 = split(_Details({"NOP": ["mann-tre"]}))
    _, _, err2 = split(_Details({"NOP": ["mann-tre", "nobody-here"]}))
    check("message is singular for one bad slug", err1 and " is not incoming" in err1)
    check("message is plural for several", err2 and " are not incoming" in err2)


# ── A player with no salary on record contributes nothing ───────────────────

def test_missing_salary():
    print("\nedge — incoming player with no salary for the season")

    bios = dict(BIOS)
    bios["freebie"] = {"salaries": {}}
    in_players = {"NOP": IN_PLAYERS["NOP"] + ["freebie"]}
    absorbed, matched, err = T._exception_absorption_split(
        _Details({"NOP": ["freebie"]}), "NOP", NOP_INCOMING, in_players, bios, SEASON)
    check("absorbs nothing", absorbed == 0)
    check("whole incoming total still has to match", matched == NOP_INCOMING)
    check("not treated as an error", err is None)


# ── § 3.2: the Room Exception assignment is locked on July 1 ────────────────

ROOM_CAP_LEVELS = {SEASON: {"cap": 164_961_000, "apron1": 209_015_000,
                            "apron2": 221_686_000, "ntmle_amount": 15_044_000,
                            "tmle_amount": 6_064_000, "room_amount": 9_366_000}}
ROOM_CEILING = 164_961_000 - 15_044_000   # 149,917,000
OVER_CEILING = 168_475_703                # NOP's real figure
UNDER_CEILING = 118_429_069               # MIN's real figure


def absorb(exception_type, salary, mle_type=None, incoming=6_563_925, used=0):
    state = {"NOP": {SEASON: {"mle_type": mle_type, "mle_used": used}}} if mle_type or used else {}
    return T._check_exception_absorption(
        "NOP", incoming, exception_type, salary, salary,
        ROOM_CAP_LEVELS[SEASON], state, SEASON)


def test_room_exception_locked_july_1():
    print("\n§ 3.2 — Room Exception assignment locked on July 1")

    # The bug: a team assigned the Room Exception that later rose above the
    # cap was refused, even though § 3.2 says it "cannot move out of it during
    # that year, even if they later exceed the cap."
    r = absorb("room_exception", OVER_CEILING, mle_type="room")
    check("assigned team over the ceiling is still eligible", r.passed)
    check("message reports the absorption, not a refusal",
          r.passed and "absorbed via the Room Exception" in r.message)

    r = absorb("room_exception", UNDER_CEILING, mle_type="room")
    check("assigned team under the ceiling is eligible", r.passed)

    # No assignment on record: fall back to the July-1 zone test.
    r = absorb("room_exception", UNDER_CEILING)
    check("unassigned team under the ceiling is eligible", r.passed)
    r = absorb("room_exception", OVER_CEILING)
    check("unassigned team over the ceiling is refused", not r.passed)
    check("refusal cites § 3.2", (not r.passed) and "§ 3.2" in r.message)

    # team_state stores the short form "room" (PUT /api/team-state rejects
    # "room_exception" outright), but _apply_trade briefly wrote the long key.
    # Both must read as an assignment so an older record isn't silently refused.
    r = absorb("room_exception", OVER_CEILING, mle_type="room_exception")
    check("long-form assignment is honored too", r.passed)

    # The balance still binds — the lock is about eligibility, not amount.
    r = absorb("room_exception", OVER_CEILING, mle_type="room", incoming=9_366_001)
    check("assignment does not waive the balance limit", not r.passed)
    r = absorb("room_exception", OVER_CEILING, mle_type="room",
               incoming=5_000_000, used=9_000_000)
    check("already-used balance still binds", not r.passed)


def test_apron_bars_are_still_live():
    print("\n§ 1.5/§ 1.6 — apron bars are standing restrictions, not locked")

    # These must NOT pick up the July-1 lock: § 1.5/§ 1.6 bar them outright
    # "regardless of transaction type" whenever the team is over the apron.
    r = absorb("ntmle", 210_000_000, mle_type="ntmle")
    check("NTMLE refused at/above the First Apron even if assigned", not r.passed)
    r = absorb("tmle", 222_000_000, mle_type="tmle")
    check("TMLE refused at/above the Second Apron even if assigned", not r.passed)
    r = absorb("ntmle", 180_000_000, mle_type="ntmle")
    check("NTMLE fine below the First Apron", r.passed)


def main():
    test_legacy_behavior()
    test_hybrid_split()
    test_rejects_bad_slugs()
    test_missing_salary()
    test_room_exception_locked_july_1()
    test_apron_bars_are_still_live()
    print("\n" + "=" * 40)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Regression tests for routers.transactions._check_signing_method_funding —
the § 3.1–§ 3.6 gate that checks a declared `signing_method` against real cap
room and the remaining exception balance.

Written 2026-08-01 after two signings were logged as `cap_space` by teams with
no cap room. `signing_method` had been pure self-declaration, and because
`_apply_sign` stamps `mle_type = "room"` off any cap-space signing, one wrong
declaration silently downgraded the team's exception for the rest of the
league year — the OKC case below charged a later $11,000,000 signing against
the $9,366,000 Room Exception instead of the $15,044,000 NTMLE.

Every scenario uses the real production figures from those transactions.

    venv/bin/python -m tests.test_signing_method_funding
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.transactions import _check_signing_method_funding  # noqa: E402

FAILS = []

# Real 26-27 levels from cap-levels.json.
CAP_LEVELS = {"26-27": {
    "cap": 164961000, "apron1": 209015000, "apron2": 221686000,
    "ntmle_amount": 15044000, "tmle_amount": 6064000,
    "bae_amount": 5477000, "room_amount": 9366000,
}}
SEASON = "26-27"


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def run(method, new_sal, salary, ex_holds=None, state=None, team="OKC", holds=0):
    """Returns the CheckResult, or None when the signing is allowed."""
    return _check_signing_method_funding(
        team, method, new_sal, salary,
        salary if ex_holds is None else ex_holds,
        SEASON, CAP_LEVELS, {team: {SEASON: state}} if state else {},
        unrenounced_holds=holds,
    )


def main():
    print("cap space — the two real misdeclarations")
    # OKC/Embiid 2026-07-20: ~$220M team salary incl. his own $71,002,962 UFA
    # hold, which is netted out before the comparison. $42M signing, no room.
    r = run("cap_space", 42000000, 149206863)
    check("OKC/Embiid $42M cap-space signing is rejected", r is not None)
    check("...cites the Salary Cap", r is not None and "Salary Cap" in r.message)
    check("...is a blocking error", r is not None and r.level == "error")
    # DET/Gordon 2026-07-26: $200,509,212 before the signing, $35.5M over.
    r = run("cap_space", 20000000, 200509212, team="DET")
    check("DET/Gordon $20M cap-space signing is rejected on ledger state", r is not None)
    check("...reports no room rather than a negative figure",
          r is not None and "no room" in r.message)

    print("\nunrenounced holds — the DET signing was actually legal")
    # DET had really renounced $64,405,760 of holds on the league sheet; none of
    # it had been entered here, so a legal signing read as unfundable. The gate
    # still rejects (the ledger genuinely shows no room) but must point at the
    # renounces rather than implying no method exists. $68,677,901 is DET's full
    # FA-hold book at the time.
    r = run("cap_space", 20000000, 200509212, team="DET", holds=68677901)
    check("still rejected — the ledger really does show no room", r is not None)
    check("...names the unrenounced hold total",
          r is not None and "$68,677,901" in r.message)
    check("...tells the submitter to enter the renounces",
          r is not None and "renounce transactions first" in r.message)
    check("...says the holds alone would cover it",
          r is not None and "enough on its own to fund this signing" in r.message)
    # When clearing every hold still wouldn't cover the signing, don't promise it would.
    r = run("cap_space", 20000000, 200509212, team="DET", holds=5000000)
    check("holds too small to help are reported without the promise",
          r is not None and "$5,000,000" in r.message
          and "enough on its own" not in r.message)
    check("no holds on the books leaves the message unchanged",
          r is not None and "unrenounced" not in run("cap_space", 20000000, 200509212).message)

    print("\ncap space — legitimate signings still pass")
    # MIA was genuinely under the cap all offseason.
    check("MIA under the cap passes", run("cap_space", 6000000, 158593570) is None)
    check("signing exactly to the cap passes",
          run("cap_space", 5754137, 159206863) is None)
    check("$1 over the cap is rejected",
          run("cap_space", 5754138, 159206863) is not None)

    print("\nexception balance")
    # Hachimura 2026-07-29: $11M against OKC's wrongly-assigned Room Exception.
    room_state = {"mle_used": 0, "mle_type": "room", "hard_cap": None}
    r = run("mle", 11000000, 202219825, ex_holds=191219825, state=room_state)
    check("$11M against a $9,366,000 Room Exception is rejected", r is not None)
    check("...names the Room Exception", r is not None and "Room Exception" in r.message)
    # Same signing once the bucket is corrected to the NTMLE — what the fix did.
    ntmle_state = {"mle_used": 0, "mle_type": "ntmle", "hard_cap": None}
    check("$11M against the $15,044,000 NTMLE passes",
          run("mle", 11000000, 202219825, ex_holds=191219825, state=ntmle_state) is None)
    # Partial use: CHI had already spent $8M of its NTMLE.
    part = {"mle_used": 8000000, "mle_type": "ntmle", "hard_cap": None}
    check("$7,044,000 of a $7,044,000 remainder passes",
          run("mle", 7044000, 100000000, state=part) is None)
    check("$7,044,001 exceeds the remainder", run("mle", 7044001, 100000000, state=part) is not None)
    check("a fully-spent exception rejects any amount",
          run("ntmle", 1, 100000000, state={"mle_used": 15044000, "mle_type": "ntmle"}) is not None)

    print("\n§ 3.3 — NTMLE unavailable at/above the First Apron")
    r = run("ntmle", 5000000, 210000000, ex_holds=210000000,
            state={"mle_used": 0, "mle_type": None})
    check("at/above the First Apron the NTMLE is rejected", r is not None)
    check("...points at the Taxpayer MLE", r is not None and "Taxpayer MLE" in r.message)
    check("just below the First Apron the NTMLE is allowed",
          run("ntmle", 5000000, 209014999, ex_holds=209014999,
              state={"mle_used": 0, "mle_type": None}) is None)
    # Holds don't push a team over an apron (§ 1.3) — a big hold with a low
    # ex-holds figure must not trip the apron gate.
    check("apron test ignores cap holds",
          run("ntmle", 5000000, 240000000, ex_holds=190000000,
              state={"mle_used": 0, "mle_type": None}) is None)

    print("\n§ 3.2 — a team with no locked mle_type derives its zone from live Team Salary")
    # Zone lines for CAP_LEVELS: Room ceiling = cap - ntmle = $149,917,000;
    # NTMLE ceiling = apron1 - ntmle = $193,971,000. This must match
    # computeMleType in teams/team.js exactly, or the team page and the
    # signing form disagree about which exception a team holds.
    check("deep under the cap resolves to the Room Exception",
          run("mle", 9366000, 100000000) is None)
    check("...and is bounded by the Room Exception amount",
          run("mle", 9366001, 100000000) is not None)
    check("mid-zone resolves to the NTMLE",
          run("mle", 15044000, 160000000) is None)
    check("...and is bounded by the NTMLE amount",
          run("mle", 15044001, 160000000) is not None)
    # SAC's real 26-27 position (2026-08-12): $201,308,319 Team Salary, no
    # holds. cap - salary is not > ntmle (not Room); apron1 - salary is
    # $7,706,681 < $15,044,000 (not NTMLE) — the team page correctly shows
    # the Taxpayer MLE here. A generic 'mle' signing_method used to default
    # to the NTMLE regardless, showing SAC an exception it didn't have.
    check("SAC's real position resolves to the Taxpayer MLE, not the NTMLE",
          run("mle", 6064000, 201308319) is None)
    check("...and is bounded by the Taxpayer MLE amount, not the (larger) NTMLE",
          run("mle", 6064001, 201308319) is not None)

    print("\nout of scope / defensive")
    check("bird_rights is not amount-checked", run("bird_rights", 42000000, 202219825) is None)
    check("minimum is not amount-checked", run("minimum", 3876529, 202219825) is None)
    check("bae defers to _check_bae_eligibility", run("bae", 5477000, 202219825) is None)
    check("sign_and_trade is out of scope", run("sign_and_trade", 30000000, 202219825) is None)
    check("no declared method is not second-guessed", run(None, 42000000, 202219825) is None)
    check("an unconfigured season is skipped, not failed",
          _check_signing_method_funding("OKC", "cap_space", 42000000, 202219825,
                                        202219825, "99-00", CAP_LEVELS, {}) is None)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

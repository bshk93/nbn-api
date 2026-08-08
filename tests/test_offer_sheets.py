"""Regression tests for the § 3.15 offer-sheet split.

An offer sheet used to be one transaction carrying its own `outcome`, entered
after the fact. It is now two: `offer_sheet` records the offer, and
`offer_sheet_decision` records the incumbent's answer and does the signing.

**That split is a deliberate return to a design that previously broke.** The
combined form exists because an earlier two-step version let an offer be
submitted with no follow-up, silently leaving the player on nothing but their old
RFA hold — it bit Dyson Daniels' matched sheet in production. What makes it safe
this time is that "pending" is now a state the system can see and price:

  * `_open_offer_sheets` enumerates every unresolved offer, derived from the
    ledger so it can't drift from the transactions it describes;
  * the offering team carries a § 3.15 cap hold the entire time it's open, so an
    unanswered offer has a cost rather than being free and invisible;
  * a legacy combined entry is never mistaken for an open one.

Also pins the § 3.8 fix: offer sheets were absent from the acquisition index, so
a player who changed teams on an unmatched offer kept their tenure attributed to
the team they left.

    venv/bin/python -m tests.test_offer_sheets
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as txn  # noqa: E402
from routers.transactions import (  # noqa: E402
    _offer_hold_amount, _offer_deadline, _offer_sheet_outcome_team,
    _validate_offer_sheet_decision, OfferSheetDecisionDetails,
)

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


SEASON = "26-27"
CAP_LEVELS = {SEASON: {"cap": 164961000, "apron1": 209015000, "apron2": 221686000,
                       "hard_cap": 252390330, "min_salary_scale": {"0": 1357763}}}
C2 = {"type": "player", "salaries": {"26-27": "$20,000,000", "27-28": "$21,000,000"}}


def fake_ledger(entries):
    """Point the ledger-derived helpers at a synthetic transaction list."""
    txn._load_transactions = lambda: entries
    txn._OPEN_OFFERS_CACHE.update({"key": None, "offers": []})
    txn._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})


def offer(tid, player="curry-stephen", offering="LAL", retaining="GSW", contract=None, date="2026-08-08"):
    return {"id": tid, "type": "offer_sheet", "date": date, "created_by": "t",
            "created_at": f"{date}T00:00:00Z", "description": "",
            "details": {"player": player, "offering_team": offering,
                        "retaining_team": retaining, "teams": [offering, retaining],
                        "contract": contract or C2}}


def decision(tid, offer_id, outcome, date="2026-08-09"):
    return {"id": tid, "type": "offer_sheet_decision", "date": date, "created_by": "t",
            "created_at": f"{date}T00:00:00Z", "description": "",
            "details": {"offer_id": offer_id, "outcome": outcome}}


# ── Open-offer derivation ─────────────────────────────────────────────────────
print("\nopen offers — derived from the ledger, never a second store")

fake_ledger([offer("o1")])
o = txn._open_offer_sheets()
check("an unanswered offer is open", len(o) == 1 and o[0]["id"] == "o1", o)
check("...and carries the offering and incumbent teams",
      o[0]["offering_team"] == "LAL" and o[0]["retaining_team"] == "GSW")
check("...and a 48-hour deadline", o[0]["deadline"] == "2026-08-10", o[0]["deadline"])

fake_ledger([offer("o1"), decision("d1", "o1", "matched")])
check("a resolved offer is no longer open", txn._open_offer_sheets() == [])

fake_ledger([offer("o1"), decision("d1", "o1", "not_matched")])
check("...whichever way it resolved", txn._open_offer_sheets() == [])

fake_ledger([offer("o1"), decision("d1", "SOMETHING-ELSE", "matched")])
check("a decision pointing elsewhere doesn't close it", len(txn._open_offer_sheets()) == 1)

# The 3 live ledger entries predate the split and were applied on submission.
# Reading one as "open" would charge a phantom cap hold years after the fact.
legacy = offer("legacy1")
legacy["details"]["outcome"] = "not_matched"
fake_ledger([legacy])
check("a legacy combined entry is never open", txn._open_offer_sheets() == [])

fake_ledger([offer("o1", player="a"), offer("o2", player="b"), decision("d", "o1", "matched")])
open_ids = [x["id"] for x in txn._open_offer_sheets()]
check("multiple offers resolve independently", open_ids == ["o2"], open_ids)


# ── § 3.15 cap hold ───────────────────────────────────────────────────────────
print("\n§ 3.15 hold — the offering team pays for an open offer")

check("the hold is the offer's Year 1 salary, not its total",
      _offer_hold_amount(C2) == 20000000, _offer_hold_amount(C2))
check("...an empty contract holds nothing", _offer_hold_amount({}) == 0)
check("...a two-way offer holds nothing", _offer_hold_amount({"type": "two-way", "salaries": {}}) == 0)

txn._current_league_year = lambda: SEASON
fake_ledger([offer("o1")])
check("the offering team carries the hold", txn._pending_offer_hold("LAL", SEASON) == 20000000)
check("the incumbent does not", txn._pending_offer_hold("GSW", SEASON) == 0)
check("an uninvolved team does not", txn._pending_offer_hold("BOS", SEASON) == 0)
check("the hold doesn't leak into a future season", txn._pending_offer_hold("LAL", "27-28") == 0)

# The rule this exists to enforce: a team can't float offers it couldn't
# collectively fund, because each open one occupies room.
fake_ledger([offer("o1", player="a"), offer("o2", player="b", contract={
    "type": "player", "salaries": {"26-27": "$15,000,000", "27-28": "$16,000,000"}})])
check("simultaneous offers stack on the offering team's books",
      txn._pending_offer_hold("LAL", SEASON) == 35000000, txn._pending_offer_hold("LAL", SEASON))

fake_ledger([offer("o1"), decision("d1", "o1", "matched")])
check("the hold clears the moment the offer resolves", txn._pending_offer_hold("LAL", SEASON) == 0)


# ── Outcome resolution ────────────────────────────────────────────────────────
print("\nwho ends up with the player")

check("matched leaves the player with the incumbent",
      _offer_sheet_outcome_team({"outcome": "matched", "offering_team": "LAL",
                                 "teams": ["LAL", "GSW"]}) == "GSW")
check("not matched sends them to the offering team",
      _offer_sheet_outcome_team({"outcome": "not_matched", "offering_team": "LAL",
                                 "teams": ["LAL", "GSW"]}) == "LAL")
check("a pending offer conveys nobody",
      _offer_sheet_outcome_team({"offering_team": "LAL", "teams": ["LAL", "GSW"]}) is None)
check("an unknown team resolves to nothing, not a guess",
      _offer_sheet_outcome_team({"outcome": "matched", "offering_team": "LAL",
                                 "teams": ["LAL", "XXX"]}) is None)


# ── § 3.8: an offer sheet is an acquisition ───────────────────────────────────
# It wasn't indexed at all before 2026-08-08, so a player who moved on an
# unmatched offer kept their tenure attributed to the team they left.
print("\n§ 3.8 — resolved offer sheets move the Bird clock")

BIO = {"draft_team": None, "draft_year": None}

fake_ledger([
    {"id": "s1", "type": "sign", "date": "2022-07-01", "details": {"player": "p", "team": "MIN"}},
    offer("o1", player="p", offering="TOR", retaining="MIN", date="2026-08-04"),
    decision("d1", "o1", "not_matched", date="2026-08-04"),
])
b = txn._bird_tenure("p", "TOR", SEASON, BIO)
check("an unmatched offer starts a clock with the new team",
      b["terminal_team"] == "TOR", b["terminal_team"])
check("...as a fresh Non-QVFA tenure", b["tier"] == "Non-QVFA" and b["seasons"] == 0, b)
b_old = txn._bird_tenure("p", "MIN", SEASON, BIO)
check("...and the old team no longer holds rights", b_old["tier"] is None, b_old["tier"])

fake_ledger([
    {"id": "s1", "type": "sign", "date": "2022-07-01", "details": {"player": "p", "team": "MIN"}},
    offer("o1", player="p", offering="TOR", retaining="MIN", date="2026-08-04"),
    decision("d1", "o1", "matched", date="2026-08-04"),
])
b = txn._bird_tenure("p", "MIN", SEASON, BIO)
check("a matched offer keeps the player with the incumbent", b["terminal_team"] == "MIN")
# Matching is how a team retains its OWN free agent, so the clock continues —
# resetting it would punish a team for exercising the right § 3.15 gives them.
check("...and continues the tenure rather than resetting it",
      b["tier"] == "QVFA" and b["seasons"] == 4, b)

legacy = offer("legacy1", player="p", offering="TOR", retaining="MIN", date="2026-08-04")
legacy["details"]["outcome"] = "not_matched"
fake_ledger([{"id": "s1", "type": "sign", "date": "2022-07-01",
              "details": {"player": "p", "team": "MIN"}}, legacy])
check("a legacy combined entry moves the clock too",
      txn._bird_tenure("p", "TOR", SEASON, BIO)["terminal_team"] == "TOR")

fake_ledger([
    {"id": "s1", "type": "sign", "date": "2022-07-01", "details": {"player": "p", "team": "MIN"}},
    offer("o1", player="p", offering="TOR", retaining="MIN", date="2026-08-04"),
])
check("an unresolved offer conveys nothing yet",
      txn._bird_tenure("p", "MIN", SEASON, BIO)["terminal_team"] == "MIN")


# ── Who can receive an offer sheet ────────────────────────────────────────────
# The eligibility rule has to agree with what `_apply_sign` will actually
# execute: it refuses a cross-team signing unless the player carries a UFA/RFA
# hold for the CURRENT season. An "earliest hold" reading looks reasonable and
# is wrong — it accepts a player whose deal runs through this season, so the
# offer validates, sits on the books holding real cap room, and then fails at
# the decision with "already on ATL". That happened in testing.
print("\nRFA eligibility — must match what a signing can actually do")

from routers.transactions import _rfa_eligibility  # noqa: E402


def rfa(holds):
    return _rfa_eligibility("p", {"p": {"cap_holds": holds}}, SEASON)


check("a current-season RFA hold is eligible", rfa({SEASON: "RFA"})[0])
check("a UFA is not — no match right to sell", not rfa({SEASON: "UFA"})[0])
check("...and says which hold it found", "not RFA" in rfa({SEASON: "UFA"})[1], rfa({SEASON: "UFA"})[1])
check("a player under contract now, RFA later, is NOT eligible yet",
      not rfa({"27-28": "RFA"})[0])
check("...and explains they have to wait for that league year",
      "has to wait" in rfa({"27-28": "RFA"})[1], rfa({"27-28": "RFA"})[1])
check("an option year is not an RFA hold", not rfa({SEASON: "PLAYER_OPT"})[0])
check("no holds at all is not eligible", not rfa({})[0])


# ── Decision-time validation ──────────────────────────────────────────────────
# New with the split: matching is allowed over the cap, but a hard cap is still
# a hard cap (§ 1.3). Nothing checked the incumbent's side before.
print("\ndecision-time checks — the incumbent's side")


def validate(outcome, ledger, salary_by_team, hard_cap_team_state=None):
    fake_ledger(ledger)
    txn._compute_team_salary_ex_holds = lambda t, b, s: salary_by_team.get(t, 0)
    txn._compute_team_salary = lambda t, b, s: salary_by_team.get(t, 0)
    txn._signee_existing_hold = lambda t, p, b, s: (0, False)
    txn._count_standard_roster = lambda t: 14
    ctx = {"bios": {"curry-stephen": {"name": "CURRY, STEPHEN"}}, "cur_season": SEASON,
           "cap_levels": CAP_LEVELS, "team_state": hard_cap_team_state or {},
           "txn_date": "2026-08-09", "trade_exceptions": {}}
    return _validate_offer_sheet_decision(
        OfferSheetDecisionDetails(offer_id="o1", outcome=outcome), ctx)


r = validate("matched", [offer("o1")], {"GSW": 100_000_000})
check("a comfortable match passes", all(c.passed for c in r), [c.message for c in r if not c.passed])

r = validate("not_matched", [offer("o1")], {"LAL": 100_000_000})
check("a comfortable non-match passes", all(c.passed for c in r))

# A match that would breach a hard cap must fail — this is the check the
# combined transaction never ran against the incumbent.
r = validate("matched", [offer("o1")], {"GSW": 250_000_000})
check("a match breaching the league hard cap is caught",
      any(not c.passed and c.level == "error" for c in r),
      [c.message for c in r])

r = validate("matched", [{"id": "o1", "type": "offer_sheet", "date": "2026-08-08",
                          "details": {"outcome": "matched", "player": "x"}}], {})
check("an already-resolved offer scores nothing rather than passing vacuously", r == [])

r = validate("bogus", [offer("o1")], {"GSW": 100_000_000})
check("an invalid outcome scores nothing", r == [])


print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

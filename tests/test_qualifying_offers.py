"""§ 3.1 qualifying offers — the amount, the RFA/UFA decision, the deadline,
withdrawal, acceptance and the lapse sweep.

Built 2026-10-02, when the league adopted QOs (an owner wanted to offer his
RFA less than the QO without renouncing his Bird Rights). Pins:

  * the amount: NBA rookie-scale slot raises over the 4th-year salary (+10
    points from the 2023 class), two-ways at no cap figure, everyone else the
    greater of the minimum and 125% of the *prior salary* — never the hold;
  * that an RFA tag alone no longer makes an RFA once the July 1 deadline has
    passed: the QO decision does;
  * that withdrawing keeps the hold (and puts back any hold the QO raised).

Pure functions plus patched loaders; nothing is written to live data.

    venv/bin/python -m tests.test_qualifying_offers
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as tx  # noqa: E402
from routers.transactions import (  # noqa: E402
    AcceptQualifyingOfferDetails, QualifyingOfferDetails,
    _finishes_rookie_scale, _qo_amount, _qo_status, _rfa_eligibility,
    _validate_accept_qo, _validate_qualifying_offer,
)

FAILS = []


def check(name, cond, got=None):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + ("" if cond or got is None else f"  (got {got!r})"))
    if not cond:
        FAILS.append(name)


CAP_LEVELS = {"26-27": {"min_salary_scale": {"0": 1357763, "1": 2185116, "2": 2449421,
                                             "3": 2537526, "4": 2625627}},
              "27-28": {"min_salary_scale": {"0": 1425651, "1": 2294372, "2": 2571892,
                                             "3": 2664402, "4": 2756908}}}

# Jaden Hardy's real shape: 2022 class, pick 21, a 300% rookie hold.
HARDY = {"name": "HARDY, JADEN", "type": "player", "draft_year": 2022, "draft_round": 1,
         "draft_pick": 21, "cap_holds": {"26-27": "RFA"},
         "salaries": {"25-26": "$4,101,338", "26-27": "$12,304,014"}}


def bios_fixture():
    return {
        "hardy-jaden": copy.deepcopy(HARDY),
        # 2nd-rounder in his final year (25-26 -> hold 26-27), 3 years entering it.
        "second-rounder": {"name": "ROUNDER, SECOND", "type": "player", "draft_year": 2023,
                           "draft_round": 2, "draft_pick": 44, "cap_holds": {"26-27": "RFA"},
                           "salaries": {"25-26": "$2,301,587", "26-27": "$4,373,015"}},
        "two-way-guy": {"name": "GUY, TWO-WAY", "type": "two-way", "draft_year": 2024,
                        "draft_round": 2, "draft_pick": 35, "cap_holds": {"26-27": "RFA"},
                        "salaries": {"25-26": "$0", "26-27": "$1"}},
        # Still under contract for 26-27; becomes RFA-eligible in 27-28.
        "next-year-rfa": {"name": "RFA, NEXT", "type": "player", "draft_year": 2024,
                          "draft_round": 2, "draft_pick": 50, "cap_holds": {"27-28": "RFA"},
                          "salaries": {"26-27": "$2,000,000", "27-28": "$2,400,000"}},
        # Six years in, tagged RFA by mistake.
        "veteran": {"name": "VET, OLD", "type": "player", "draft_year": 2020, "draft_round": 2,
                    "draft_pick": 40, "cap_holds": {"27-28": "RFA"},
                    "salaries": {"26-27": "$3,000,000", "27-28": "$3,600,000"}},
    }


def patch_world(bios, team="MIL", round_opened=None):
    """Point the module at an in-memory league. Returns the store `save` writes to."""
    store = {"bios": bios}
    tx.load_player_bios = lambda: copy.deepcopy(store["bios"])
    tx.save_player_bios = lambda b: store.__setitem__("bios", copy.deepcopy(b))
    tx._build_team_map = lambda: {slug: team for slug in store["bios"]}
    tx.log_write = lambda *a, **k: None
    tx._qo_round_opened = lambda slug, season: round_opened
    return store


def ctx(bios, season):
    return {"bios": bios, "cur_season": season, "cap_levels": CAP_LEVELS, "team_state": {},
            "txn_date": "2026-10-02", "trade_exceptions": {}}


def failed(checks, cid):
    return any(c.check == cid and not c.passed and c.level == "error" for c in checks)


def passed(checks, cid):
    return any(c.check == cid and c.passed for c in checks)


def main():
    print("Amount")
    check("Hardy (2022 class, pick 21): 4th-year salary + 44.1%",
          _qo_amount(HARDY, "26-27", CAP_LEVELS) == round(4_101_338 * 1.441),
          _qo_amount(HARDY, "26-27", CAP_LEVELS))
    check("…priced off the salary, nowhere near the $12.3M hold",
          _qo_amount(HARDY, "26-27", CAP_LEVELS) < 6_000_000)
    rookie_2023 = {**HARDY, "draft_year": 2023, "draft_pick": 1, "cap_holds": {"27-28": "RFA"},
                   "salaries": {"26-27": "$10,000,000", "27-28": "$30,000,000"}}
    check("2023 class onward gets the extra 10 points (pick 1: +40%)",
          _qo_amount(rookie_2023, "27-28", CAP_LEVELS) == 14_000_000,
          _qo_amount(rookie_2023, "27-28", CAP_LEVELS))
    b = bios_fixture()
    check("2nd-rounder: 125% of prior salary when that beats the minimum",
          _qo_amount(b["second-rounder"], "26-27", CAP_LEVELS) == round(2_301_587 * 1.25))
    cheap = {**b["second-rounder"], "salaries": {"25-26": "$1,000,000", "26-27": "$1,200,000"}}
    check("…the minimum when that's higher (3 years entering 26-27)",
          _qo_amount(cheap, "26-27", CAP_LEVELS) == 2_537_526,
          _qo_amount(cheap, "26-27", CAP_LEVELS))
    check("two-way: a two-way QO, no cap figure", _qo_amount(b["two-way-guy"], "26-27", CAP_LEVELS) == 0)
    check("rookie with no 4th-year salary on file can't be priced",
          _qo_amount({**HARDY, "salaries": {}}, "26-27", CAP_LEVELS) is None)

    print("\nRookie-scale carve-out")
    check("Hardy finishes his rookie scale before 26-27", _finishes_rookie_scale(HARDY, "26-27"))
    check("…not before 27-28", not _finishes_rookie_scale(HARDY, "27-28"))
    check("a 2nd-rounder never does", not _finishes_rookie_scale(b["second-rounder"], "26-27"))

    print("\nStatus: the tag alone is eligibility, the QO is the decision")
    check("undecided, deadline ahead -> pending", _qo_status(b["next-year-rfa"], "27-28", "26-27") == "pending")
    check("undecided, deadline passed -> lapsed", _qo_status(b["second-rounder"], "26-27", "26-27") == "lapsed")
    check("not RFA-tagged -> None", _qo_status({"cap_holds": {"26-27": "UFA"}}, "26-27", "26-27") is None)
    ok, why = _rfa_eligibility("second-rounder", b, "26-27")
    check("lapsed -> not an RFA, and says why", not ok and "deadline" in why, why)
    b["second-rounder"]["qualifying_offers"] = {"26-27": {"status": "extended", "amount": 1}}
    check("extended -> an RFA", _rfa_eligibility("second-rounder", b, "26-27")[0])
    b["second-rounder"]["qualifying_offers"]["26-27"]["status"] = "withdrawn"
    check("withdrawn -> not an RFA", not _rfa_eligibility("second-rounder", b, "26-27")[0])
    check("the FA pool's future class reads pending as RFA (as_of = today)",
          _rfa_eligibility("next-year-rfa", b, "27-28", as_of="26-27")[0])

    print("\nExtend: validation")
    b = bios_fixture()
    patch_world(b)
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="next-year-rfa"), ctx(b, "26-27"))
    check("final contract year, under 4 years, undecided: clean",
          not any(not x.passed for x in c), [x.message for x in c if not x.passed])
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="veteran"), ctx(b, "26-27"))
    check("4+ years: refused even with an RFA tag", failed(c, "qo_rfa_eligible"))
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="second-rounder"), ctx(b, "26-27"))
    check("hold season already started: past the deadline", failed(c, "qo_deadline"))
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="next-year-rfa"), ctx(b, "25-26"))
    check("two years out: too early", failed(c, "qo_deadline"))
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="hardy-jaden", season="26-27"),
                                   ctx(b, "25-26"))
    check("Hardy has 4 years but the rookie carve-out makes him eligible",
          passed(c, "qo_rfa_eligible"))

    print("\nExtend and withdraw: apply")
    b = bios_fixture()
    # hold $2.4M; QO = the tier-3 minimum ($2,664,402), above 125% of $2.0M
    store = patch_world(b)
    team, season, rec = tx._apply_qualifying_offer(
        QualifyingOfferDetails(player="next-year-rfa"), "2027-03-01", {"name": "t"}, txn_id="x1")
    after = store["bios"]["next-year-rfa"]
    check("extend records it", rec["status"] == "extended" and season == "27-28" and team == "MIL")
    check("a QO above the hold raises the hold to it (RFA hold = the greater)",
          after["salaries"]["27-28"] == "$2,664,402" and rec["base_hold"] == 2_400_000,
          after["salaries"]["27-28"])
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="next-year-rfa"), ctx(store["bios"], "26-27"))
    check("can't extend twice", failed(c, "qo_undecided"))
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="next-year-rfa", action="withdraw"),
                                   ctx(store["bios"], "26-27"))
    check("withdraw before the round opens: clean", not any(not x.passed for x in c))
    tx._apply_qualifying_offer(QualifyingOfferDetails(player="next-year-rfa", action="withdraw"),
                               "2027-03-02", {"name": "t"}, txn_id="x2")
    after = store["bios"]["next-year-rfa"]
    check("withdraw makes him a UFA", after["cap_holds"]["27-28"] == "UFA")
    check("…and puts the hold back where it was", after["salaries"]["27-28"] == "$2,400,000",
          after["salaries"]["27-28"])
    check("…keeping a hold at all (Bird Rights stay)", "27-28" in after["salaries"])

    b = bios_fixture()
    b["hardy-jaden"]["qualifying_offers"] = {"26-27": {"status": "extended", "amount": 5_910_028}}
    patch_world(b, round_opened="2026-08-10")
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="hardy-jaden", action="withdraw"),
                                   ctx(b, "26-27"))
    check("withdraw after his round opened: refused", failed(c, "qo_withdraw_window"))
    patch_world(b)
    c = _validate_qualifying_offer(QualifyingOfferDetails(player="second-rounder", action="withdraw"),
                                   ctx(b, "26-27"))
    check("nothing extended: nothing to withdraw", failed(c, "qo_withdrawable"))

    print("\nAccept")
    b = bios_fixture()
    b["hardy-jaden"]["qualifying_offers"] = {"26-27": {"status": "extended", "amount": 5_910_028,
                                                       "two_way": False}}
    store = patch_world(b)
    c = _validate_accept_qo(AcceptQualifyingOfferDetails(player="hardy-jaden"), ctx(b, "26-27"))
    check("an extended QO in its own league year can be accepted", passed(c, "qo_acceptable"))
    c = _validate_accept_qo(AcceptQualifyingOfferDetails(player="second-rounder"), ctx(b, "26-27"))
    check("no QO, nothing to accept", failed(c, "qo_acceptable"))
    signed = {}
    tx._apply_sign = lambda d, date, info, txn_id=None: signed.update(details=d)
    tx._apply_accept_qo(AcceptQualifyingOfferDetails(player="hardy-jaden"), "2026-10-02",
                        {"name": "t"}, txn_id="x3")
    d = signed["details"]
    check("signs one year at the QO amount with his own team",
          d.team == "MIL" and d.contract.salaries == {"26-27": "$5,910,028"})
    check("…rolling into a UFA hold", d.contract.cap_holds == {"27-28": "UFA"})
    check("…and marks the QO accepted",
          store["bios"]["hardy-jaden"]["qualifying_offers"]["26-27"]["status"] == "accepted")

    print("\nLapse sweep")
    b = bios_fixture()
    b["hardy-jaden"]["qualifying_offers"] = {"26-27": {"status": "extended", "amount": 1}}
    store = patch_world(b)
    tx.league_today_str = lambda: "2026-10-02"
    lapsed = tx.sweep_lapsed_qualifying_offers(as_of="26-27")
    check("undecided current-season RFAs lapse", set(lapsed) == {"second-rounder", "two-way-guy"}, lapsed)
    check("…their tag becomes UFA", store["bios"]["second-rounder"]["cap_holds"]["26-27"] == "UFA")
    check("…with the lapse on record",
          store["bios"]["second-rounder"]["qualifying_offers"]["26-27"]["status"] == "lapsed")
    check("an extended QO is left alone", store["bios"]["hardy-jaden"]["cap_holds"]["26-27"] == "RFA")
    check("next year's RFA is still pending", store["bios"]["next-year-rfa"]["cap_holds"]["27-28"] == "RFA")
    check("running it again changes nothing", tx.sweep_lapsed_qualifying_offers(as_of="26-27") == [])

    print("\n" + "=" * 40)
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()

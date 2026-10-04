"""Tests for _validate_extension / _extension_frame / _final_guaranteed_year
(§ 6.2 / § 6.3) — Phase A of nbn-today/docs/poext-extension-pipeline.md.

Why this doesn't reuse _validate_sign's tests: an extension adds years to a
live contract rather than replacing a current-season figure. Measured against
production 2026-08-07, feeding an extension's shape to /api/validate/sign
reported a team getting $18.9M *cheaper* for extending a player — Year 1 read
as salaries[cur_season] (an extension has none), the live salary backed out
as a hold being replaced (it isn't), and a roster body added for a player
already on the roster. _validate_extension is a clean implementation built
around "first extended season", not "current season".

Most fixtures are synthetic bios pinned into _BIRD_LEDGER_CACHE (the same
trick test_bird_rights_tenure.py uses), since a synthetic timeline is what
lets a boundary case (exactly 3 years, exactly Year 4 of 5) be constructed on
purpose rather than searched for in production data. One case runs against a
real rostered player as an integration smoke check.

    venv/bin/python -m tests.test_extensions
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import transactions as T  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


_REAL_BUILD_TEAM_MAP = T._build_team_map


def pin_ledger(events):
    st = T.TRANSACTIONS_FILE.stat()
    T._BIRD_LEDGER_CACHE.update({"key": (st.st_mtime, st.st_size), "index": {"p": list(events)}})


def make_ctx(bio, cur_season="26-27", cap_levels=None, txn_date=None):
    return {
        "bios": {"p": bio},
        "cap_levels": cap_levels or {},
        "cur_season": cur_season,
        "txn_date": txn_date or f"20{cur_season[3:5]}-01-15",
        "team_state": {},
        "trade_exceptions": {},
    }


def extend(bio, contract, team="XXX", cur_season="26-27", cap_levels=None,
          kind="veteran", events=(("2020-08-01", "sign", "XXX"),), holder=None,
          txn_date=None, submitted_date=None):
    pin_ledger(events)
    # _validate_extension checks the player is actually on `team`'s roster
    # (§ 2.4) — "p" is a synthetic test player with no real roster entry, so
    # this has to be faked the same way the ledger above is. `holder`
    # defaults to `team` (the common case: the request names the real
    # holder) but can be overridden to deliberately construct a mismatch.
    T._build_team_map = lambda: {"p": holder if holder is not None else team}
    details = T.ExtensionDetails(player="p", team=team, contract=T.ContractIn(**contract), kind=kind,
                                 submitted_date=submitted_date)
    ctx = make_ctx(bio, cur_season=cur_season, cap_levels=cap_levels, txn_date=txn_date)
    return T._validate_extension(details, ctx), ctx


def named(checks, name):
    return next((c for c in checks if c.check == name), None)


def main():
    print("_final_guaranteed_year")

    # Rule 1: explicit `guaranteed` present — last fully-guaranteed season wins,
    # even though 26-27 is nominally the last salaried year.
    bio = {
        "salaries": {"24-25": "$5,000,000", "25-26": "$6,000,000", "26-27": "$7,000,000"},
        "guaranteed": {"24-25": "$5,000,000", "25-26": "$6,000,000", "26-27": "$2,000,000"},
        "cap_holds": {},
    }
    check("rule 1: last FULLY guaranteed season, not the last salaried one",
          T._final_guaranteed_year(bio) == "25-26")

    # Rule 2: no `guaranteed` data — every year counts except NON_GTD/option.
    bio2 = {
        "salaries": {"24-25": "$5,000,000", "25-26": "$6,000,000", "26-27": "$7,000,000"},
        "guaranteed": {},
        "cap_holds": {"26-27": "NON_GTD"},
    }
    check("rule 2: a trailing NON_GTD year is excluded from 'guaranteed'",
          T._final_guaranteed_year(bio2) == "25-26")

    bio3 = {
        "salaries": {"24-25": "$5,000,000", "25-26": "$6,000,000", "26-27": "$7,000,000"},
        "guaranteed": {},
        "cap_holds": {"26-27": "PLAYER_OPT"},
    }
    check("rule 2: a trailing player option is excluded the same way",
          T._final_guaranteed_year(bio3) == "25-26")

    # The trailing UFA/RFA hold season is never a contract year at all.
    bio4 = {
        "salaries": {"24-25": "$5,000,000", "25-26": "$6,000,000", "27-28": "$7,500,000"},
        "guaranteed": {},
        "cap_holds": {"27-28": "UFA"},
    }
    check("the trailing FA hold season is discarded before rule 2 even runs",
          T._final_guaranteed_year(bio4) == "25-26")

    def deal(start, end, cur, extra_cap_holds=None):
        salaries = {}
        y = start
        while True:
            salaries[y] = "$5,000,000"
            if y == end:
                break
            y = T._season_shift(y, 1)
        return {"salaries": salaries, "guaranteed": {}, "cap_holds": extra_cap_holds or {}}, cur

    print("\nextension_team_match (§ 2.4 — only the incumbent may extend)")
    bio, cur = deal("24-25", "26-27", "26-27")
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$6,000,000"}, "cap_holds": {}},
                       team="ZZZ", holder="XXX", cur_season=cur, events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_team_match")
    check("a team that doesn't hold the player is refused outright", c and not c.passed)
    check("...and it's the ONLY check that ran (no point scoring eligibility for the wrong team)",
          len(checks) == 1)

    print("\neligibility boundaries (§ 6.2 rules 1-2)")

    bio, cur = deal("25-26", "26-27", "26-27")  # 2-year contract, final year
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$6,000,000", "28-29": "$6,300,000"},
                             "cap_holds": {}}, cur_season=cur,
                       events=(("2025-08-01", "sign", "XXX"),))
    c = named(checks, "extension_eligibility")
    check("2-year prior contract rejected (< 3 years)", c and not c.passed)

    bio, cur = deal("24-25", "26-27", "26-27")  # 3-year, final year
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$6,000,000", "28-29": "$6,300,000"},
                             "cap_holds": {}}, cur_season=cur,
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_eligibility")
    check("3-year contract, final year -> accepted", c and c.passed)

    bio, cur = deal("23-24", "27-28", "25-26")  # 5-year deal, currently Year 3
    checks, _ = extend(bio, {"type": "player", "salaries": {"28-29": "$6,000,000", "29-30": "$6,300,000"},
                             "cap_holds": {}}, cur_season=cur,
                       events=(("2023-08-01", "sign", "XXX"),))
    c = named(checks, "extension_eligibility")
    check("5-year deal, Year 3 -> rejected (not final, not Year 4)", c and not c.passed)

    bio, cur = deal("23-24", "27-28", "26-27")  # 5-year deal, currently Year 4
    checks, _ = extend(bio, {"type": "player", "salaries": {"28-29": "$6,000,000", "29-30": "$6,300,000"},
                             "cap_holds": {}}, cur_season=cur,
                       events=(("2023-08-01", "sign", "XXX"),))
    c = named(checks, "extension_eligibility")
    check("5-year deal, Year 4 of 5 -> accepted", c and c.passed)

    print("\ntrade_floor basis must warn-and-allow, never confirm ineligibility "
          "(regression: Tyler Herro + 31 others read as ineligible off a 2-year "
          "floor whose real start was invisible, caught 2026-08-21)")
    # A trade with no earlier record on file: the ledger can see the player
    # arriving in 24-25, but the real tenure could easily predate that — the
    # acquiring team inherits accrual the ledger never recorded, same as § 3.8.
    bio, cur = deal("24-25", "25-26", "26-27")  # reads as a 2-year deal, already ended
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$6,000,000"}, "cap_holds": {}},
                       cur_season=cur, events=(("2024-08-01", "trade", "XXX"),))
    c = named(checks, "extension_eligibility")
    check("a short derived length on trade_floor basis still PASSES (can't confirm, not disproven)",
          c and c.passed)
    check("...but at warning severity, not silently green", c and c.level == "warning")

    print("\nextension_start_season (rule 10)")
    bio, cur = deal("24-25", "26-27", "26-27")
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$6,000,000", "28-29": "$6,300,000"},
                             "cap_holds": {}}, cur_season=cur,
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_start_season")
    check("new money starting exactly the season after the final gtd year -> passes",
          c and c.passed)

    checks, _ = extend(bio, {"type": "player", "salaries": {"28-29": "$6,000,000", "29-30": "$6,300,000"},
                             "cap_holds": {}}, cur_season=cur,
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_start_season")
    check("new money starting a season late -> error naming the mismatch",
          c and not c.passed and c.level == "error")

    print("\nextension_max_year1 (rule 7: <= 140% of prior salary)")
    bio, cur = deal("24-25", "26-27", "26-27")  # 26-27 salary = $5,000,000
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$7,000,000"}, "cap_holds": {}},
                       cur_season=cur, events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_max_year1")
    check("140% of $5,000,000 = $7,000,000 exactly -> passes", c and c.passed)

    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$7,000,001"}, "cap_holds": {}},
                       cur_season=cur, events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_max_year1")
    check("$1 over 140% of prior salary, EAPS unset -> warns (the EAPS half might be greater)",
          c and not c.passed and c.level == "warning")

    over = {"type": "player", "salaries": {"27-28": "$7,000,001"}, "cap_holds": {}}
    checks, _ = extend(bio, over, cur_season=cur, events=(("2024-08-01", "sign", "XXX"),),
                       cap_levels={"27-28": {"eaps": 4_000_000}})
    c = named(checks, "extension_max_year1")
    check("$1 over, EAPS set and lower -> error against the greater (prior-salary) half",
          c and not c.passed and c.level == "error")

    checks, ctx = extend(bio, {"type": "player", "salaries": {"27-28": "$13,000,000"}, "cap_holds": {}},
                         cur_season=cur, events=(("2024-08-01", "sign", "XXX"),),
                         cap_levels={"27-28": {"eaps": 10_000_000}})
    c = named(checks, "extension_max_year1")
    check("whichever is greater: 140% of a $10M EAPS ($14M) beats 140% of $5M prior",
          c and c.passed and "EAPS" in c.message)
    sheet = T._extension_fact_sheet(T.ExtensionDetails(player="p", team="XXX",
        contract=T.ContractIn(type="player", salaries={"27-28": "$13,000,000"})), ctx)
    check("...and the fact sheet reports the same ceiling and basis",
          sheet["max_year1_ceiling"] == 14_000_000 and sheet["max_year1_basis"] == "eaps")

    # No prior-salary figure on file (fresh bio, no contract_end) -> EAPS-unset warn.
    bare_bio = {"salaries": {}, "guaranteed": {}, "cap_holds": {}}
    checks, _ = extend(bare_bio, {"type": "player", "salaries": {"27-28": "$7,000,000"}, "cap_holds": {}},
                       cur_season="26-27", events=())
    c = named(checks, "extension_max_year1")
    check("no prior salary and EAPS unset -> warns, doesn't block", c and c.passed and c.level == "warning")

    print("\nextension_raises (rule 8: 8% normal, 5% extend-and-trade)")
    bio, cur = deal("24-25", "26-27", "26-27")
    big_step = {"type": "player", "salaries": {"27-28": "$5,000,000", "28-29": "$5,450,000"}, "cap_holds": {}}
    checks, _ = extend(bio, big_step, cur_season=cur, kind="veteran",
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_raises")
    check("9% step under the normal 8% ceiling -> error", c and not c.passed)

    checks, _ = extend(bio, big_step, cur_season=cur, kind="extend_and_trade",
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_raises")
    check("same 9% step is ALSO over the tighter 5% extend-and-trade ceiling", c and not c.passed)

    ok_step = {"type": "player", "salaries": {"27-28": "$5,000,000", "28-29": "$5,400,000"}, "cap_holds": {}}
    checks, _ = extend(bio, ok_step, cur_season=cur, kind="veteran",
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_raises")
    check("8% step passes the normal (non-extend-and-trade) ceiling", c is None or c.passed)

    print("\nextension_min_length (rule 6: >= 2 guaranteed years)")
    checks, _ = extend(bio, {"type": "player", "salaries": {"27-28": "$5,000,000"}, "cap_holds": {}},
                       cur_season=cur, events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_min_length")
    check("a 1-year extension fails the 2-year minimum", c and not c.passed)

    print("\nextension_service (rule 3), reusing _bird_tenure's own asymmetry")
    bio, cur = deal("24-25", "26-27", "26-27")
    contract = {"type": "player", "salaries": {"27-28": "$6,000,000", "28-29": "$6,300,000"}, "cap_holds": {}}
    checks, _ = extend(bio, contract, cur_season=cur,
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_service")
    check("2+ ledger-basis seasons of service -> passes at error severity", c and c.passed)

    checks, _ = extend(bio, contract, cur_season=cur,
                       events=(("2024-08-01", "trade", "XXX"),))  # no earlier record -> trade_floor
    c = named(checks, "extension_service")
    check("trade_floor basis (no earlier record) still passes, but flagged as a warning",
          c and c.passed and c.level == "warning")

    print("\n_extension_fact_sheet's trailing_hold — the office form's EAPS field reads this")
    bio, cur = deal("24-25", "26-27", "26-27")
    pin_ledger((("2024-08-01", "sign", "XXX"),))
    details = T.ExtensionDetails(
        player="p", team="XXX",
        contract=T.ContractIn(type="player", salaries={"27-28": "$5,000,000", "28-29": "$5,300,000"},
                              cap_holds={"29-30": "UFA"}),
        bird_rights_type="QVFA",
    )
    ctx = make_ctx(bio, cur_season=cur)
    fs = T._extension_fact_sheet(details, ctx)
    th = fs.get("trailing_hold")
    check("a trailing UFA/RFA hold in the extension's own cap_holds is priced, not omitted",
          th is not None and th["season"] == "29-30")
    check("Full Bird with no EAPS on file -> needs_eaps, not a silent guess",
          th and th["needs_eaps"] is True)

    print("\nextension_cap_position (rule 5), first extended season not the current one")
    bio, cur = deal("24-25", "26-27", "26-27")
    contract = {"type": "player", "salaries": {"27-28": "$6,000,000"}, "cap_holds": {}}
    # 27-28 thresholds are all zero on the real cap-levels.json (unset) — D5:
    # must report "cannot evaluate", never silently pass.
    checks, _ = extend(bio, contract, cur_season=cur,
                       cap_levels={"27-28": {"cap": 0, "hard_cap": 0}},
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_cap_position")
    check("zero thresholds -> 'cannot evaluate', not a silent pass", c and c.passed and c.level == "warning")
    check("...and says so explicitly", "unset" in c.message or "cannot evaluate" in c.message.lower())

    checks, _ = extend(bio, contract, cur_season=cur,
                       cap_levels={"27-28": {"cap": 100_000_000, "hard_cap": 5_000_000}},
                       events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_cap_position")
    check("real thresholds set -> a real verdict, not a warning",
          c and c.level == "error" and not c.passed)

    print("\nextension_window: a PO-EXT proposal is judged as of its submission")
    # An expiring veteran (deal ends 26-27), proposed June 20 and voted on
    # July 3 — after the rollover, when he is no longer in his final year.
    bio, _ = deal("24-25", "26-27", "26-27")
    ext = {"type": "player", "salaries": {"27-28": "$6,000,000", "28-29": "$6,300,000"}, "cap_holds": {}}
    late = dict(cur_season="27-28", txn_date="2027-07-03", events=(("2024-08-01", "sign", "XXX"),))
    checks, _ = extend(bio, ext, **late)
    check("judged on the vote date, the deal has already run out",
          not T._validation_result(checks, {}).legal)
    checks, _ = extend(bio, ext, submitted_date="2027-06-20", **late)
    w = named(checks, "extension_window")
    check("judged on the submission date, it was on time",
          w and w.passed and "expiring-veteran" in w.message)
    check("...and it is legal", T._validation_result(checks, {}).legal)
    checks, _ = extend(bio, ext, submitted_date="2027-07-01", **late)
    check("a proposal first submitted after June 30 is late",
          not T._validation_result(checks, {}).legal)

    print("\nextension_kind / extension_rfa_hold / extension_supersedes_qo (§ 6.3, § 3.1)")
    # A 2023 first-rounder in Year 4 of his rookie deal, rolling into an RFA hold.
    rookie = {
        "draft_round": 1, "draft_year": 2023, "draft_pick": 5,
        "salaries": {"23-24": "$5,000,000", "24-25": "$5,200,000", "25-26": "$5,400,000",
                     "26-27": "$6,800,000", "27-28": "$20,400,000"},
        "cap_holds": {"27-28": "RFA"},
    }
    rookie_events = (("2023-08-01", "sign", "XXX"),)
    rs_contract = {"salaries": {"27-28": "$9,000,000", "28-29": "$9,500,000",
                                "29-30": "$10,000,000", "30-31": "$10,500,000"},
                   "cap_holds": {"31-32": "UFA"}}
    # txn_date defaults to 2027-01-15 — after opening night, before June 30.
    checks, _ = extend(rookie, rs_contract, kind="veteran", events=rookie_events)
    c = named(checks, "extension_kind")
    check("a rookie-scale player declared 'veteran' is an error", c and not c.passed and c.level == "error")
    w = named(checks, "extension_window")
    check("...and gets the rookie-scale window, not the June 30 one", w and not w.passed and "rookie-scale" in w.message)
    checks, ctx = extend(rookie, rs_contract, kind="rookie_scale", events=rookie_events)
    c = named(checks, "extension_kind")
    check("declared rookie_scale on a rookie-scale player passes", c and c.passed)
    c = named(checks, "extension_max_year1")
    check("a rookie-scale extension has no 140% ceiling ($9M on a $6.8M salary passes)",
          c and c.passed and c.level == "info")
    sheet = T._extension_fact_sheet(T.ExtensionDetails(player="p", team="XXX", kind="rookie_scale",
                                                       contract=T.ContractIn(**rs_contract)), ctx)
    check("fact sheet says rookie_scale so the forms can preselect it", sheet.get("rookie_scale") is True)
    check("...and shows no 140% ceiling", sheet["max_year1_ceiling"] is None)

    vet = {"draft_round": 2, "draft_year": 2018, "draft_pick": 40,
           "salaries": {"24-25": "$10,000,000", "25-26": "$10,000,000", "26-27": "$10,000,000"},
           "cap_holds": {}}
    checks, _ = extend(vet, {"salaries": {"27-28": "$12,000,000", "28-29": "$12,500,000"},
                             "cap_holds": {"29-30": "UFA"}},
                       kind="rookie_scale", events=(("2024-08-01", "sign", "XXX"),))
    c = named(checks, "extension_kind")
    check("rookie_scale declared on a non-rookie is an error", c and not c.passed and c.level == "error")

    rfa_tail = dict(rs_contract, cap_holds={"31-32": "RFA"})
    checks, _ = extend(rookie, rfa_tail, kind="rookie_scale", events=rookie_events)
    c = named(checks, "extension_rfa_hold")
    check("a trailing RFA hold after 8 years of experience is an error",
          c and not c.passed and c.level == "error" and "8 years" in c.message)
    checks, _ = extend(rookie, rs_contract, kind="rookie_scale", events=rookie_events)
    check("a trailing UFA hold isn't RFA-checked", named(checks, "extension_rfa_hold") is None)

    with_qo = dict(rookie, qualifying_offers={"27-28": {"status": "extended", "amount": 8000000}})
    checks, _ = extend(with_qo, rs_contract, kind="rookie_scale", events=rookie_events)
    c = named(checks, "extension_supersedes_qo")
    check("an extended QO on the replaced season is called out", c and c.passed and "superseded" in c.message)

    print("\nextension_max_salary (§ 3.11; § 6.2's rookie-scale 30% exception)")
    caps = {"26-27": {"cap": 160_000_000}, "27-28": {"cap": 170_000_000}}
    real_awards = T._player_awards
    T._player_awards = lambda slug: {}
    def rs(y1, cap_levels=caps):
        c = {"salaries": {"27-28": f"${y1:,}", "28-29": f"${y1:,}"}, "cap_holds": {"29-30": "UFA"}}
        checks, _ = extend(rookie, c, kind="rookie_scale", events=rookie_events, cap_levels=cap_levels)
        return named(checks, "extension_max_salary")
    c = rs(42_500_000)
    check("rookie at exactly 25% of the 27-28 cap passes", c and c.passed)
    c = rs(42_500_001)
    check("over 25% but under 30%, final rookie season's awards not out -> warning (Rose still open)",
          c and not c.passed and c.level == "warning" and "26-27" in c.message)
    c = rs(51_000_001)
    check("over 30% -> error", c and not c.passed and c.level == "error")
    T._player_awards = lambda slug: {"25-26": {"Most Valuable Player"}}
    c = rs(51_000_000)
    check("an MVP in the prior three seasons lifts the max to 30%", c and c.passed and "criteria" in c.message)
    T._player_awards = lambda slug: {}
    c = rs(42_000_000, cap_levels={"26-27": {"cap": 160_000_000}})
    check("27-28 cap unset: the 26-27 cap stands in, and says so",
          c and not c.passed and c.level == "warning" and "standing in" in c.message)

    rr = lambda awards: T._rose_rule("p", "27-28", "26-27", awards)
    check("Rose: All-NBN in the season just before -> met", rr({"26-27": {"All-NBN Third Team"}})["met"])
    check("Rose: All-NBN once, two seasons back -> not met",
          not rr({"25-26": {"All-NBN First Team"}})["met"])
    check("Rose: DPOY in two of three -> met",
          rr({"25-26": {"Defensive Player of the Year"}, "24-25": {"Defensive Player of the Year"}})["met"])
    check("Rose: still possible while 26-27's awards aren't out", rr({})["possible"])
    check("Rose: not possible once that season is past",
          not T._rose_rule("p", "27-28", "27-28", {})["possible"])

    checks, _ = extend(vet, {"salaries": {"27-28": "$60,000,000", "28-29": "$60,000,000"},
                             "cap_holds": {"29-30": "UFA"}},
                       events=(("2024-08-01", "sign", "XXX"),), cap_levels=caps)
    c = named(checks, "extension_max_salary")
    check("a veteran over his max warns (experience is inferred, as for signings)",
          c and not c.passed and c.level == "warning")
    T._player_awards = real_awards

    print("\nintegration smoke check against a real rostered player")
    # Every fixture above pinned _BIRD_LEDGER_CACHE to a synthetic one-player
    # index keyed to the real ledger file's (mtime, size) — since that key
    # hasn't changed, _player_acquisition_index would keep serving the stale
    # synthetic index instead of reloading. Force a real reload. Same story
    # for _build_team_map, patched by extend() to a synthetic {"p": team}.
    T._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})
    T._build_team_map = _REAL_BUILD_TEAM_MAP
    bios = T.load_player_bios()
    team_map = T._build_team_map()
    cur_season = T._current_league_year()
    subject = None
    for slug, team in sorted(team_map.items()):
        bio = bios.get(slug) or {}
        frame = T._extension_frame(slug, team, bio, cur_season)
        if frame["contract_start"] and frame["contract_length"] and frame["contract_length"] >= 3:
            is_final = cur_season == frame["contract_end"]
            is_y4of5 = frame["contract_length"] == 5 and frame["position_in_deal"] == 4
            if (is_final or is_y4of5) and frame["start_basis"] == "ledger":
                subject = (slug, team, frame)
                break
    if subject:
        slug, team, frame = subject
        details = T.ExtensionDetails(
            player=slug, team=team,
            contract=T.ContractIn(type="player", salaries={frame["first_extended_season"]: "$5,000,000"}),
        )
        ctx = T._validation_ctx()
        checks = T._validate_extension(details, ctx)
        fact_sheet = T._extension_fact_sheet(details, ctx)
        check(f"real subject {slug} ({team}): validator runs without raising", isinstance(checks, list))
        check("fact sheet is keyed on the first extended season, not the current one",
              fact_sheet["extended_term"]["first_season"] == frame["first_extended_season"])
        eligibility = named(checks, "extension_eligibility")
        check("real subject reads as eligible (real ledger data, real deal)",
              eligibility and eligibility.passed)
    else:
        print("  [skip] no real ledger-basis eligible player found to smoke-test against")

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
          kind="veteran", events=(("2020-08-01", "sign", "XXX"),)):
    pin_ledger(events)
    details = T.ExtensionDetails(player="p", team=team, contract=T.ContractIn(**contract), kind=kind)
    ctx = make_ctx(bio, cur_season=cur_season, cap_levels=cap_levels)
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

    print("\neligibility boundaries (§ 6.2 rules 1-2)")

    def deal(start, end, cur, extra_cap_holds=None):
        salaries = {}
        y = start
        while True:
            salaries[y] = "$5,000,000"
            if y == end:
                break
            y = T._season_shift(y, 1)
        return {"salaries": salaries, "guaranteed": {}, "cap_holds": extra_cap_holds or {}}, cur

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
    check("$1 over the 140% ceiling -> error", c and not c.passed)

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

    print("\nintegration smoke check against a real rostered player")
    # Every fixture above pinned _BIRD_LEDGER_CACHE to a synthetic one-player
    # index keyed to the real ledger file's (mtime, size) — since that key
    # hasn't changed, _player_acquisition_index would keep serving the stale
    # synthetic index instead of reloading. Force a real reload.
    T._BIRD_LEDGER_CACHE.update({"key": None, "index": {}})
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

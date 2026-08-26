"""Regression tests for stats_build/checks.py — the corpus-level sanity checks.

These were the Sheets-era R build's `check_allstats()`, lost in the move to
`allstats-*.csv` and restored 2026-08-26. The tests matter more than usual
because the checks run over 157k rows on a weekly timer: one that stops firing
goes unnoticed exactly as long as the data stays clean, which is the state it
was in when the checks were dropped in the first place.

Every case builds the smallest team-game that isolates one violation, so a
finding here can only be the thing the case injected.

    venv/bin/python -m tests.test_stats_checks
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stats_build import checks  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def row(player, m, fgm=2, fga=4, tpm=0, tpa=1, ftm=0, fta=0, reb=4, orb=1, pf=2, **kw):
    r = {"TEAM": "PHX", "DATE": "2026-10-20", "OPP": "@LAL", "OPP_TEAM": "LAL",
         "PLAYER": player, "M": str(m),
         "P": str(ftm + 2 * fgm + tpm),
         "R": str(reb), "OR": str(orb), "DR": str(reb - orb),
         "A": "2", "S": "1", "B": "0", "TO": "1", "PF": str(pf),
         "FGM": str(fgm), "FGA": str(fga), "3PM": str(tpm), "3PA": str(tpa),
         "FTM": str(ftm), "FTA": str(fta),
         "TEAM_PTS": "0", "OPP_TEAM_PTS": "90", "WL": "W"}
    r.update({k: str(v) for k, v in kw.items()})
    return r


def game(rows):
    """Five rows summing to 240 minutes, with TEAM_PTS made to agree."""
    total = sum(int(r["P"]) for r in rows)
    for r in rows:
        r["TEAM_PTS"] = str(total)
        r["WL"] = "W" if total > int(r["OPP_TEAM_PTS"]) else "L"
    return rows


def clean():
    return game([row(f"P{i}, ONE", 48) for i in range(5)])


def cats(findings):
    return sorted({f.category for f in findings})


def main():
    print("stats_build.checks")

    base = clean()
    check("a clean team-game produces nothing",
          checks.check_corpus([("f.csv", base)]) == [], checks.summarize(
              checks.check_corpus([("f.csv", base)])))
    check("summarize says so", checks.summarize([]) == "no violations")

    # --- the three the R build had ---------------------------------------
    r = clean()
    r[0]["P"] = str(int(r[0]["P"]) + 1)
    check("the points identity fires",
          cats(checks.check_rows("f.csv", r)) == ["bad_arithmetic"])

    for col, other, label in [("FGM", "FGA", "FGM>FGA"), ("3PM", "3PA", "3PM>3PA"),
                              ("FTM", "FTA", "FTM>FTA")]:
        r = clean()
        r[0][col] = str(int(r[0][other]) + 1)
        found = checks.check_rows("f.csv", r)
        check(f"{label} fires", any(f.category == "bad_arithmetic" for f in found))

    r = clean()
    r[0]["PF"] = "7"
    check("PF > 6 fires", cats(checks.check_rows("f.csv", r)) == ["bad_arithmetic"])

    r = clean()
    r[0]["OR"] = "9"           # R is 4
    check("OR > R fires", cats(checks.check_rows("f.csv", r)) == ["bad_arithmetic"])

    r = clean()
    r[0]["DR"] = "2"           # OR 1 + DR 2 != R 4
    check("OR + DR != R fires", cats(checks.check_rows("f.csv", r)) == ["bad_arithmetic"])

    r = clean()
    r[0]["M"] = "40"           # team now sums to 232
    check("illegal team minutes fire",
          cats(checks.check_games("f.csv", r)) == ["bad_minutes"])

    for minutes, ot in ((240, 0), (265, 1), (290, 2), (315, 3)):
        r = game([row(f"P{i}, ONE", minutes // 5) for i in range(5)])
        check(f"{minutes} minutes ({ot} OT) is legal",
              not any(f.category == "bad_minutes" for f in checks.check_games("f.csv", r)))

    r = clean()
    r[0]["P"] = ""
    r[0]["FGM"] = ""
    check("a blank required field fires",
          cats(checks.check_rows("f.csv", r)) == ["missing_data"])

    # --- the two that found real errors ----------------------------------
    # One player twice in a team-game, with different lines. Minutes and points
    # still reconcile, so nothing else in the file notices.
    r = game([row("HOLIDAY, JRUE", 45), row("HOLIDAY, JRUE", 3),
              row("B, TWO", 64), row("C, THREE", 64), row("D, FOUR", 64)])
    found = checks.check_games("f.csv", r)
    check("a duplicated player fires", cats(found) == ["duplicate_player"])
    check("it reports both stat lines",
          "45min" in found[0].detail and "3min" in found[0].detail, found[0].detail)
    check("and the arithmetic checks stay quiet", checks.check_rows("f.csv", r) == [])
    check("as do the team-total checks",
          not any(f.category in ("bad_minutes", "score_mismatch") for f in found))

    names = {"CURRY, STEPHEN"}
    r = game([row("CURRY, STEPHEN", 48)] + [row(f"P{i}, ONE", 48) for i in range(4)])
    found = checks.check_players("f.csv", r, names)
    check("an unknown player name fires", cats(found) == ["unknown_player"])
    check("a known one does not",
          checks.check_players("f.csv", [row("CURRY, STEPHEN", 48)], names) == [])
    # One misspelling across four games is one finding carrying the row count,
    # not four findings — the report has to be readable at 157k rows.
    repeated = [row("MISPELLED, NAME", 48) for _ in range(4)]
    found = checks.check_players("f.csv", repeated, names)
    check("a name repeated across rows is reported once", len(found) == 1, found)
    check("with its row count", "4 rows" in found[0].detail, found[0].detail)

    # PLAYER_FIXES is applied before the lookup — an alias in the table is not
    # an unknown player, which is the whole reason the table is imported rather
    # than restated here.
    alias, real = next(iter(checks.PLAYER_FIXES.items()))
    check("a name in PLAYER_FIXES is not reported",
          checks.check_players("f.csv", [row(alias, 48)], {real.upper()}) == [])
    check("Will Riley's alias is now in the table",
          checks.PLAYER_FIXES.get("RILEY, WENDELL") == "RILEY, WILL")

    # --- the rest --------------------------------------------------------
    r = clean()
    for x in r:
        x["TEAM_PTS"] = "999"
    check("a team score that disagrees with its players fires",
          any(f.category == "score_mismatch" for f in checks.check_games("f.csv", r)))

    # clean() scores 20 against OPP_TEAM_PTS 90, so the honest W/L is "L".
    r = clean()
    for x in r:
        x["WL"] = "W"
    check("a wrong W/L fires",
          any(f.category == "bad_wl" for f in checks.check_games("f.csv", r)))
    check("and the right one does not",
          not any(f.category == "bad_wl" for f in checks.check_games("f.csv", clean())))

    r = clean()
    for x in r:
        x["OPP_TEAM_PTS"] = x["TEAM_PTS"]
    check("a tied final score fires",
          any(f.category == "tied_game" for f in checks.check_games("f.csv", r)))

    r = clean()
    for x in r:
        x["TEAM"] = "XYZ"
    check("an invalid team abbreviation fires",
          any(f.category == "bad_team" for f in checks.check_games("f.csv", r)))

    # A run over several files must attribute each finding to its own file.
    a, b = clean(), clean()
    b[0]["PF"] = "7"
    found = checks.check_corpus([("good.csv", a), ("bad.csv", b)])
    check("findings name the file they came from",
          len(found) == 1 and found[0].where.startswith("bad.csv"), found)
    check("summarize counts by category",
          checks.summarize(found) == "1 bad_arithmetic", checks.summarize(found))

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

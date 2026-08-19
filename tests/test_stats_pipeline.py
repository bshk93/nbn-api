"""The ported aggregations (stats_build/pipeline.py), port spec Phase 2.

The real gate is `python3 -m stats_build.harness port`, which runs the R build
and diffs byte for byte — but that takes ~25s and needs R, so it isn't a test.
This pins the parts of each slice that are easy to get subtly wrong, plus a
direct comparison against the live R output for the files ported so far, which
is the same guarantee at test speed.

Slices ported: h2h-alltime, h2h-playoffs.
"""

import csv
import pathlib
import sys

from stats_build import pipeline

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("season labels come off the filename, as R derives them")
check("regular season reads the END year", pipeline._season_label("allstats-20-21.csv", False) == "20-21")
check("playoffs get their suffix", pipeline._season_label("allstats-playoffs-21.csv", True) == "20-21 Playoffs")
check("a file with no year is skipped, not guessed", pipeline._season_label("allstats.csv", False) is None)

print("\ngame rows are deduplicated from player rows")
rows = [
    {"SEASON": "25-26", "DATE": "2025-10-22", "TEAM": "ATL", "OPP": "TOR", "WL": "W"},
    {"SEASON": "25-26", "DATE": "2025-10-22", "TEAM": "ATL", "OPP": "TOR", "WL": "W"},
    {"SEASON": "25-26", "DATE": "2025-10-22", "TEAM": "TOR", "OPP": "@ATL", "WL": "L"},
    {"SEASON": "25-26", "DATE": "2025-10-24", "TEAM": "ATL", "OPP": "TOR", "WL": ""},
    {"SEASON": "25-26", "DATE": "2025-10-25", "TEAM": "ATL", "OPP": "", "WL": "W"},
]
games = pipeline._game_level(rows)
check("two players in one game count once", len(games) == 2)
check("the away @ is stripped, so both sides name the same matchup",
      {g[3] for g in games} == {"TOR", "ATL"})
check("a row with no result is dropped", not any(g[1] == "2025-10-24" for g in games))
check("a row with no opponent is dropped", not any(g[1] == "2025-10-25" for g in games))

print("\nthe matrix")
header, matrix = pipeline.h2h_matrix(games, ["ATL", "TOR"])
check("header is TEAM then every team", header == ["TEAM", "ATL", "TOR"])
check("ATL beat TOR once", matrix[0] == ["ATL", "", "1-0"])
check("and TOR's row is the mirror", matrix[1] == ["TOR", "0-1", ""])
check("the diagonal is EMPTY, not 0-0 — csvio keeps that distinct from NA",
      matrix[0][1] == "" and matrix[1][2] == "")
h2, m2 = pipeline.h2h_matrix(games, ["ATL", "BKN", "TOR"])
check("a team with no meetings still gets a 0-0 cell", m2[0][2] == "0-0")

print("\ncareer totals")
reg = [
    {"PLAYER": "WOOD, CHRISTIAN", "DATE": "2025-10-22", "P": "10"},
    {"PLAYER": "OLADIPO, VICTOR", "DATE": "2025-10-22", "P": "10"},
    {"PLAYER": "CURRY, STEPHEN", "DATE": "2025-10-22", "P": "40"},
    {"PLAYER": "CURRY, STEPHEN", "DATE": "2025-10-24", "P": ""},
]
header, totals = pipeline.career_totals(reg, "P")
check("header is RANK, PLAYER, stat", header == ["RANK", "PLAYER", "P"])
check("leader first", totals[0][1] == "CURRY, STEPHEN")
check("a blank stat is skipped, not zero-summed into a crash", totals[0][2] == 40)
check("a tie keeps alphabetical player order (R groups by player, then stable-sorts)",
      [r[1] for r in totals[1:]] == ["OLADIPO, VICTOR", "WOOD, CHRISTIAN"])
check("ranks are 1..n", [r[0] for r in totals] == [1, 2, 3])
check("the limit is applied", len(pipeline.career_totals(reg, "P", limit=1)[1]) == 1)

print("\nsingle-game highs")
games = [
    {"DATE": "2026-01-02", "SEASON": "25-26", "PLAYER": "B", "TEAM": "ATL", "OPP": "@TOR",
     "gametype": "REG", "ROUND": None, "GAME": None, "P": "50", "R": "1", "A": "1", "S": "1", "B": "1", "3PM": "1"},
    {"DATE": "2026-01-01", "SEASON": "25-26", "PLAYER": "A", "TEAM": "TOR", "OPP": "ATL",
     "gametype": "REG", "ROUND": None, "GAME": None, "P": "50", "R": "1", "A": "1", "S": "1", "B": "1", "3PM": "1"},
    {"DATE": "2026-01-03", "SEASON": "25-26", "PLAYER": "C", "TEAM": "BKN", "OPP": "NYK",
     "gametype": "REG", "ROUND": None, "GAME": None, "P": "", "R": "1", "A": "1", "S": "1", "B": "1", "3PM": "1"},
]
header, highs = pipeline.game_highs(games, "P")
check("RANK leads, then the game's columns", header[:4] == ["RANK", "DATE", "SEASON", "PLAYER"])
check("a tie goes to the EARLIER date", [r[1] for r in highs[:2]] == ["2026-01-01", "2026-01-02"])
check("a missing stat sorts last, as R puts NA last even under desc()",
      highs[-1][3] == "C")
check("the away @ is kept here — game highs show it, unlike h2h", highs[1][5] == "@TOR")
check("a regular-season row has no ROUND or GAME", highs[0][7] is None and highs[0][8] is None)

print("\nplayer name fixes")
check("an alias is corrected", pipeline.PLAYER_FIXES["KANTER, ENES"] == "FREEDOM, ENES")
check("a FIRST LAST stray is reformatted", pipeline.PLAYER_FIXES["KOBE BROWN"] == "BROWN, KOBE")

print("\nagainst the live R output")
derived = pathlib.Path("/var/lib/nothing-but-stats/derived")
data_dir = pathlib.Path("/var/lib/nothing-but-stats")
if not (derived / "data/h2h-alltime.csv").exists():
    print("  [skip] no derived/ on this box")
else:
    class _Args:
        season = "25-26"
    reg = pipeline.load_game_rows(data_dir, _Args.season, playoffs=False)
    po = pipeline.load_game_rows(data_dir, _Args.season, playoffs=True)
    teams = sorted({r["TEAM"] for r in reg if r.get("TEAM")})
    check("30 teams, from the regular season", len(teams) == 30)
    for name, games_ in (("h2h-alltime.csv", pipeline._game_level(reg) | pipeline._game_level(po)),
                         ("h2h-playoffs.csv", pipeline._game_level(po))):
        header, matrix = pipeline.h2h_matrix(games_, teams)
        expected = list(csv.reader((derived / "data" / name).read_text().splitlines()))
        check(f"{name} header matches R", header == expected[0])
        check(f"{name} every cell matches R", [[str(c) for c in r] for r in matrix] == expected[1:])

    from stats_build.csvio import render_csv
    reg_rows, po_rows = pipeline.prepare(data_dir, _Args.season)
    all_rows = reg_rows + po_rows
    for suffix, col in pipeline.STAT_FILES.items():
        for name, built in ((f"totals-{suffix}.csv", pipeline.career_totals(reg_rows, col)),
                            (f"game-highs-{suffix}.csv", pipeline.game_highs(all_rows, col))):
            check(f"{name} matches R byte for byte",
                  render_csv(*built) == (derived / "data" / name).read_text())

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

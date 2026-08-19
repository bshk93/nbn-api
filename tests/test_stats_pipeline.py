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

print("\nR-compatible arithmetic (stats_build/rmath.py)")
from stats_build.rmath import r_mean, r_round
for value, digits, expected in [(0.05, 1, 0.0), (0.15, 1, 0.1), (0.25, 1, 0.2), (0.35, 1, 0.3),
                                (0.45, 1, 0.4), (0.55, 1, 0.6), (2.925, 2, 2.92),
                                (10.185, 2, 10.19), (0.5, 0, 0.0), (1.5, 0, 2.0), (2.5, 0, 2.0)]:
    check(f"round({value}, {digits}) == {expected} as R does it", r_round(value, digits) == expected)
check("Python's own round disagrees on 0.05 — that is the whole point",
      round(0.05, 1) == 0.1 and r_round(0.05, 1) == 0.0)
check("mean makes R's second compensating pass",
      r_mean([1.0, 2.0, 3.0]) == 2.0 and repr(r_mean([10.19, 10.18] * 3)) != "")

print("\nper-team career lines")
games = [
    {"TEAM": "ATL", "PLAYER": "B", "SEASON": "24-25", "P": "10", "R": "5", "A": "1", "S": "1",
     "B": "0", "3PM": "1", "FGM": "4", "FGA": "8", "FTM": "2", "FTA": "2", "OR": "1", "DR": "4",
     "PF": "2", "TO": "1"},
    {"TEAM": "ATL", "PLAYER": "A", "SEASON": "25-26", "P": "30", "R": "5", "A": "1", "S": "1",
     "B": "0", "3PM": "1", "FGM": "10", "FGA": "18", "FTM": "4", "FTA": "4", "OR": "1", "DR": "4",
     "PF": "2", "TO": "1"},
    {"TEAM": "BKN", "PLAYER": "C", "SEASON": "25-26", "P": "40", "R": "5", "A": "1", "S": "1",
     "B": "0", "3PM": "1", "FGM": "14", "FGA": "20", "FTM": "4", "FTA": "4", "OR": "1", "DR": "4",
     "PF": "2", "TO": "1"},
]
header, built = pipeline.team_players(games, "ATL")
check("only that team's rows", [r[0] for r in built] == ["A", "B"])
check("ordered by total game score", built[0][0] == "A")
check("GP counts games", built[0][1] == 1)
check("seasons are listed, sorted and joined", built[0][10] == "25-26")
check("game score is rounded at the row, as R rounds it at load",
      pipeline.game_score(games[0]) == 7.8)

print("\nplayer awards")
check("name is title-cased", pipeline.title_case("CURRY, STEPHEN") == "Curry, Stephen")
check("hyphen segments capitalize (the gate caught this one)",
      pipeline.title_case("TOWNS, KARL-ANTHONY") == "Towns, Karl-Anthony")
check("apostrophes do NOT — R leaves them", pipeline.title_case("O'NEALE, ROYCE") == "O'neale, Royce")
check("every space-separated part capitalises", pipeline.title_case("DE LA CRUZ, JUAN") == "De La Cruz, Juan")
check("no book-title small-word list: R would write 'Smith, per'",
      pipeline.title_case("SMITH, PER") == "Smith, Per")
check("slug drops punctuation", pipeline.player_slug("Towns, Karl-Anthony") == "towns-karl-anthony")
check("slug of a suffix name", pipeline.player_slug("Smith Jr., Nolan") == "smith-jr-nolan")

po = [
    {"SEASON": "25-26 Playoffs", "TEAM": "ATL", "DATE": f"2026-05-{d:02d}", "WL": "W", "PLAYER": "A"}
    for d in range(1, 17)
] + [
    {"SEASON": "25-26 Playoffs", "TEAM": "ATL", "DATE": "2026-05-01", "WL": "W", "PLAYER": "B"},
    {"SEASON": "25-26 Playoffs", "TEAM": "BKN", "DATE": "2026-05-01", "WL": "L", "PLAYER": "C"},
]
champs = pipeline._champion_team_seasons(po)
check("16 playoff wins is the title — there is no bracket in the raw rows",
      champs == {("25-26 Playoffs", "ATL")})
check("a team's own duplicate player rows don't inflate its win count",
      pipeline._champion_team_seasons(po * 3) == champs)

print("\nstandings and seeding")
def _game(date, team, opp, pts):
    return {"DATE": date, "TEAM": team, "OPP": opp, "P": str(pts), "SEASON": "25-26"}
rows = []
for i, (a, b, pa, pb) in enumerate([("BOS", "NYK", 110, 100), ("BOS", "NYK", 105, 95),
                                    ("PHI", "TOR", 99, 120), ("PHI", "TOR", 90, 130)]):
    d = f"2025-11-{i + 1:02d}"
    rows += [_game(d, a, b, pa), _game(d, b, f"@{a}", pb)]
table = pipeline.compute_standings(rows)
seeds = {r["TEAM"]: r["SEED"] for r in table}
check("both 2-0 teams seed above both 0-2 teams",
      {seeds["BOS"], seeds["TOR"]} == {"East-1", "East-2"}
      and {seeds["NYK"], seeds["PHI"]} == {"East-3", "East-4"})
check("every team gets a conference-prefixed seed", all("-" in r["SEED"] for r in table))
check("games back is derived from the leader", any(r["GB"] == 0 for r in table))
check("PCT is rounded to three places", all(r["PCT"] is None or round(r["PCT"], 3) == r["PCT"]
                                            for r in table))
byteam = {r["TEAM"]: r for r in table}
check("record counted from points, not a result column",
      (byteam["BOS"]["W"], byteam["BOS"]["L"]) == (2, 0))
check("the loser's mirror row is counted too",
      (byteam["TOR"]["W"], byteam["TOR"]["L"]) == (2, 0))

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

    from stats_build import csvio
    from stats_build.csvio import render_csv
    check("player_awards.csv matches R byte for byte",
          render_csv(*pipeline.player_awards(data_dir, pipeline.prepare(data_dir, _Args.season)[1]))
          == (derived / "players" / "player_awards.csv").read_text())
    reg_rows, po_rows = pipeline.prepare(data_dir, _Args.season)
    from stats_build import csvio
    all_rows = reg_rows + po_rows
    # Standings, ratings and seeding against R's own file, allowing only the
    # accepted classes the harness allows.
    from stats_build.harness import rating_noise
    seasons_live = sorted({r["SEASON"] for r in reg_rows})
    standings = {sn: pipeline.compute_standings([r for r in reg_rows if r["SEASON"] == sn])
                 for sn in seasons_live}
    ratings = pipeline.team_ratings(reg_rows)
    results = pipeline.playoff_results(po_rows)
    header, built = pipeline.standings_history(standings, ratings, results, teams)
    expected = list(csv.reader((derived / "standings" / "standings-history.csv").read_text().splitlines()))
    check("standings-history header matches R", header == expected[0])
    bad = []
    for got_row, want_row in zip(built, expected[1:]):
        for j, want in enumerate(want_row):
            got = csvio.format_field(got_row[j])
            if got != want and not rating_noise(header[j], want, got):
                bad.append((want_row[-1], header[j], want, got))
    check("every seed, record and playoff result matches R exactly", not bad)

    # league-history is excluded from the byte gate because R's version is
    # broken (see harness.KNOWN_FIXED_FILES). That makes THIS the only thing
    # checking it, so it checks properly: every non-champion cell must still
    # match R, and the champion must match the finals winner in the bracket.
    hist_header, hist = pipeline.league_history(data_dir, reg_rows, po_rows, ratings)
    r_hist = list(csv.DictReader((derived / "data" / "league-history.csv").read_text().splitlines()))
    first_of = {}
    for row in r_hist:
        first_of.setdefault(row["SEASON"], row)
    check("one row per season, where R emitted one per playoff team",
          len(hist) == len(first_of) and len(r_hist) > len(hist))
    off_champion = [(row[0], hist_header[j], first_of[row[0]][hist_header[j]], v)
                    for row in hist for j, v in enumerate(row)
                    if hist_header[j] != "CHAMPION"
                    and csvio.format_field(v) != first_of[row[0]][hist_header[j]]]
    check("every non-champion cell still matches R exactly", not off_champion)
    _bcols, brackets = pipeline.playoff_brackets(po_rows, standings)
    finals = {b[0]: b[6] for b in brackets if str(b[1]) == "4"}
    champs = {row[0]: row[1] for row in hist if row[1]}
    check("every champion matches the finals winner in the bracket",
          champs and all(finals.get(season) == team for season, team in champs.items()))

    check("owner_stats matches R byte for byte",
          render_csv(*pipeline.owner_stats(data_dir, reg_rows, po_rows, ratings))
          == (derived / "data" / "owner_stats.csv").read_text())
    check("h2h-owners matches R byte for byte",
          render_csv(*pipeline.owner_h2h(data_dir, reg_rows, po_rows, teams))
          == (derived / "data" / "h2h-owners.csv").read_text())
    check("hof matches R byte for byte",
          render_csv(*pipeline.hall_of_fame(data_dir, reg_rows, po_rows))
          == (derived / "data" / "hof.csv").read_text())
    check("playoff brackets match R byte for byte",
          render_csv(*pipeline.playoff_brackets(po_rows, standings))
          == (derived / "standings" / "playoff-brackets.csv").read_text())
    check("playoff series margins match R byte for byte",
          render_csv(*pipeline.playoff_margins(po_rows, standings))
          == (derived / "nbntv-classics" / "playoff-series-margins.csv").read_text())

    from stats_build.harness import KNOWN_TIES
    ties = {(f, player, col): (rv, pv) for f, player, col, rv, pv in KNOWN_TIES}
    for team in teams:
        name = f"data/{team.lower()}-players.csv"
        header, built = pipeline.team_players(reg_rows, team)
        expected = list(csv.reader((derived / "data" / f"{team.lower()}-players.csv").read_text().splitlines()))
        mismatched = []
        for got_row, want_row in zip(built, expected[1:]):
            for j, want in enumerate(want_row):
                got = csvio.format_field(got_row[j])
                if got != want and ties.get((name, want_row[0], header[j])) != (want, got):
                    mismatched.append((want_row[0], header[j], want, got))
        check(f"{name} matches R except the listed .xx5 ties", not mismatched)

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

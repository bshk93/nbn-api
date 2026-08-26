"""Regression tests for allstats_files — how a season's raw file first appears.

The bug this closes broke every build for five days without anyone noticing: the
season clock rolled to 26-27 on July 1, `stats_build` found no
`allstats-26-27.csv` and exited 2, and `build.sh` runs under `set -e`. Nothing
triggers a build in the offseason, so it stayed latent until someone looked.

So the tests are about the distinction the module has to get right, in both
directions. Creating the file must happen on an ordinary rollover, and must
*not* happen in the case the old hard refusal was actually protecting: a data
directory that is not mounted, where aggregating zero rows would overwrite 86
good files with empty ones.

Nothing here touches the live data directory; every case runs in a tmp dir.

    venv/bin/python -m tests.test_allstats_files
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import allstats_files  # noqa: E402
from allstats_files import (  # noqa: E402
    RawFileMissing, ensure_season_file, previous_season, season_file_name,
)

FAILS = []

# A real header, with the two things that make copying it verbatim matter: the
# blank column name, and OPP_RAW (which the pre-24-25 seasons do not have).
HEADER = ("TEAM,DATE,OPP,OPP_RAW,PLAYER,M,P,R,OR,DR,A,S,B,TO,FGM,FGA,FGPCT,"
          "3PM,3PA,3PPCT,FTM,FTA,FTPCT,PF,OPP_TEAM,TD,BOX, ,TEAM_PTS,"
          "OPP_TEAM_PTS,AGE,WL,gametype\n")


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def refuses(name, fn, expect_fragment):
    try:
        fn()
    except RawFileMissing as exc:
        check(name, expect_fragment in str(exc), str(exc)[:140])
        return
    check(name, False, "no RawFileMissing raised")


def main():
    print("allstats_files")

    # --- naming and arithmetic -------------------------------------------
    check("regular season file name",
          season_file_name("26-27") == "allstats-26-27.csv")
    check("playoff file name",
          season_file_name("26-27", "PLAYOFF") == "allstats-playoffs-27.csv")
    check("previous season", previous_season("26-27") == "25-26")
    # The decade boundary is the case a naive int-to-str gets wrong: 2029-30
    # steps back to 28-29, and 2030-31 to 29-30, both needing the zero pad.
    check("previous season pads", previous_season("30-31") == "29-30")
    try:
        previous_season("2026-2027")
        check("a non-YY-YY season is rejected", False, "no ValueError")
    except ValueError:
        check("a non-YY-YY season is rejected", True)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # --- the case that used to break every build ---------------------
        (d / "allstats-25-26.csv").write_text(HEADER + "ATL,2026-01-01,BOS,BOS,X,1\n")
        path, created = ensure_season_file(d, "26-27")
        check("a rollover creates the new season's file", created)
        check("it is the file the build looks for", path.name == "allstats-26-27.csv")
        check("the header is copied verbatim from last season",
              path.read_text() == HEADER, repr(path.read_text()[:40]))
        check("no games are invented", path.read_text().count("\n") == 1)

        # Idempotent: the build runs on every box score commit, and must not
        # touch a file that already has games in it.
        path.write_text(HEADER + "ATL,2026-10-20,BOS,BOS,Y,2\n")
        before = path.read_text()
        path2, created2 = ensure_season_file(d, "26-27")
        check("an existing file is left alone", not created2)
        check("an existing file is not rewritten", path2.read_text() == before)

        # --- the refusal the old guard was really making -----------------
        # Neither this season nor the one before it: the data directory is not
        # mounted, or the season resolved to something that never existed.
        # Creating a file here is how you overwrite 86 good outputs with empty
        # ones, so it must still refuse.
        refuses("two missing seasons in a row refuses",
                lambda: ensure_season_file(d, "40-41"), "not mounted")
        check("and it creates nothing", not (d / "allstats-40-41.csv").exists())

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        refuses("an empty data directory refuses",
                lambda: ensure_season_file(d, "26-27"), "not mounted")

        # A previous-season file that exists but is empty has no header to copy,
        # and a file created without one could never take its first append —
        # allstats_guard refuses a write that drops a column.
        (d / "allstats-25-26.csv").write_text("")
        refuses("an empty previous season refuses",
                lambda: ensure_season_file(d, "26-27"), "no header row")

    # --- playoffs ---------------------------------------------------------
    # Same gap, six months later: the first playoff game of a season arrives
    # before allstats-playoffs-{YY}.csv exists.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "allstats-playoffs-26.csv").write_text(HEADER)
        path, created = ensure_season_file(d, "26-27", "PLAYOFF")
        check("the first playoff game creates the playoff file", created)
        check("under the playoff name", path.name == "allstats-playoffs-27.csv")
        # A regular-season file must not stand in for the playoff one: the two
        # have different headers (playoffs carry GAME and ROUND), so seeding one
        # from the other would produce a file whose first append is refused.
        (d / "allstats-29-30.csv").write_text(HEADER)
        refuses("a REG file does not satisfy a missing playoff predecessor",
                lambda: ensure_season_file(d, "30-31", "PLAYOFF"), "not mounted")
        check("and no playoff file appears",
              not (d / "allstats-playoffs-31.csv").exists())

    # --- dry run ----------------------------------------------------------
    # `stats_build --dry-run` promises to write nothing, so it asks whether a
    # file could be created rather than creating it.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "allstats-25-26.csv").write_text(HEADER)
        header = allstats_files.check_can_create(d, "26-27")
        check("check_can_create returns the header", header == HEADER)
        check("check_can_create writes nothing",
              not (d / "allstats-26-27.csv").exists())

    check_commit_path()

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


def check_commit_path():
    """The other half: `POST /api/boxscore/commit` for the season's first game.

    It used to 404 with `Allstats file not found`, which no amount of retrying
    on the caller's side could get past — the file simply did not exist and
    nothing created it.
    """
    from fastapi import HTTPException

    from routers import boxscores as bs

    def row(player, slug):
        return bs.BoxscorePlayerRow(
            player=player, slug=slug, min=48, pts=10, reb=5, oreb=1, dreb=4,
            ast=3, stl=1, blk=0, tov=2, pf=3, fgm=4, fga=9, tpm=1, tpa=3,
            ftm=1, fta=2)

    body = bs.BoxscoreCommitRequest(
        date="2026-10-20", home_team="PHX", away_team="LAL", season="26-27",
        game_type="REG", home_pts=10, away_pts=10,
        home_rows=[row("DURANT, KEVIN", "durant-kevin")],
        away_rows=[row("JAMES, LEBRON", "james-lebron")],
        skip_build=True, skip_reward=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        orig = (bs.DATA_DIR, bs.PLAYER_BIOS_FILE, bs.record_commit,
                bs._current_league_year, bs._remove_from_manual_queue)
        try:
            bs.DATA_DIR = tmp
            bs.PLAYER_BIOS_FILE = tmp / "player-bios.json"
            bs.record_commit = lambda **kw: None
            bs._remove_from_manual_queue = lambda *a: None
            bs._current_league_year = lambda: "26-27"

            (tmp / "allstats-25-26.csv").write_text(HEADER)

            result = bs.commit_boxscore(body, info={"name": "test"})
            check("the season's first game commits without a pre-made file",
                  result.get("ok") and result.get("rows_added") == 2, result)
            written = (tmp / "allstats-26-27.csv").read_text().splitlines()
            check("it created the file and appended both teams",
                  len(written) == 3, f"{len(written)} lines")
            check("with last season's header",
                  written[0] + "\n" == HEADER)

            # A commit against a season that is not the current one is a
            # mistake, not a rollover — an old 404 is the right answer, since
            # seeding it from a neighbour's header could silently drop a column
            # that season actually has.
            old = body.model_copy(update={"season": "21-22"})
            try:
                bs.commit_boxscore(old, info={"name": "test"})
                check("a past season with no file still 404s", False, "no HTTPException")
            except HTTPException as exc:
                check("a past season with no file still 404s", exc.status_code == 404, exc.detail)
            check("and no past-season file is created",
                  not (tmp / "allstats-21-22.csv").exists())
        finally:
            (bs.DATA_DIR, bs.PLAYER_BIOS_FILE, bs.record_commit,
             bs._current_league_year, bs._remove_from_manual_queue) = orig


if __name__ == "__main__":
    sys.exit(main())

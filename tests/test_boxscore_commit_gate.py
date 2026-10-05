"""`POST /api/boxscore/commit` refuses a bad game, and the build it triggers
can't get stuck or skip a game.

What is pinned here:

  * **The season must be the date's season.** A 26-27 game sent as "25-26"
    used to append to last season's file, because only a *missing* file was
    an error. Uploads are held to the same rule.
  * **The slug decides the name.** The slug must be a real bio and PLAYER is
    that bio's name, whatever the caller typed. No player twice in one game.
  * **The weekly integrity checks run at the door.** Points, minutes, OT,
    score, ties — a game they would flag is refused, and nothing is written,
    not even a new season file.
  * **A dead "running" build doesn't block every later one,** and a commit
    that lands mid-build gets a second pass instead of waiting for the next
    commit.

    venv/bin/python -m tests.test_boxscore_commit_gate
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

from routers import boxscores as bs  # noqa: E402

FAILS = []

HEADER = ("TEAM,DATE,OPP,OPP_RAW,PLAYER,M,P,R,OR,DR,A,S,B,TO,FGM,FGA,FGPCT,"
          "3PM,3PA,3PPCT,FTM,FTA,FTPCT,PF,OPP_TEAM,TD,BOX, ,TEAM_PTS,"
          "OPP_TEAM_PTS,AGE,WL,gametype\n")


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def refused(name, fn, status, fragment):
    try:
        fn()
        check(name, False, "no HTTPException")
    except HTTPException as exc:
        check(name, exc.status_code == status and fragment in str(exc.detail),
              f"{exc.status_code} {exc.detail}")


def row(slug, fgm=4, minutes=48, player=""):
    # (fgm-1)*2 + one three + one free throw
    return bs.BoxscorePlayerRow(
        player=player, slug=slug, min=minutes, pts=(fgm - 1) * 2 + 3 + 1, reb=5,
        oreb=1, dreb=4, ast=3, stl=1, blk=0, tov=2, pf=3, fgm=fgm, fga=9,
        tpm=1, tpa=3, ftm=1, fta=2)


HOME = [row(f"phx-{i}", 4) for i in range(5)]   # 10 each, 50
AWAY = [row(f"lal-{i}", 5) for i in range(5)]   # 12 each, 60
BIOS = {r.slug: {"name": f"{r.slug.upper()}, BIO"} for r in HOME + AWAY}


def game(**kw):
    base = dict(date="2026-10-20", home_team="PHX", away_team="LAL",
                season="26-27", game_type="REG", home_pts=50, away_pts=60,
                home_rows=HOME, away_rows=AWAY, skip_build=True, skip_reward=True)
    base.update(kw)
    return bs.BoxscoreCommitRequest(**base)


def commit(body):
    return bs.commit_boxscore(body, info={"name": "test"})


def check_commits(tmp: Path):
    target = tmp / "allstats-26-27.csv"

    print("season, date and game type")
    refused("a 26-27 date sent as 25-26 is refused",
            lambda: commit(game(season="25-26")), 422, "is in the 26-27 season")
    check("and last season's file is untouched",
          (tmp / "allstats-25-26.csv").read_text() == HEADER)
    refused("a date that is not YYYY-MM-DD is refused",
            lambda: commit(game(date="2026-10-5")), 422, "not YYYY-MM-DD")
    refused("an impossible date is refused",
            lambda: commit(game(date="2026-02-30")), 422, "not a real date")
    refused("an unknown game type is refused",
            lambda: commit(game(game_type="playoffs")), 422, "REG or PLAYOFF")
    refused("a team cannot play itself",
            lambda: commit(game(away_team="PHX")), 422, "cannot play itself")

    print("players")
    refused("a slug with no bio is refused, by name",
            lambda: commit(game(home_rows=HOME[:4] + [row("nobody-here")])),
            422, "'nobody-here' is not a player bio")
    refused("the same player on both teams is refused",
            lambda: commit(game(away_rows=AWAY[:4] + [row("phx-0", 5)])),
            422, "listed twice")

    print("the game's numbers")
    bad_pts = HOME[:4] + [row("phx-4").model_copy(update={"pts": 11})]
    refused("a line whose points don't add up is refused",
            lambda: commit(game(home_rows=bad_pts, home_pts=51)), 422, "PTS 11")
    refused("a team score that isn't the players' sum is refused",
            lambda: commit(game(home_pts=52)), 422, "score_mismatch")
    refused("a tie is refused",
            lambda: commit(game(home_pts=50, away_pts=50,
                                away_rows=[row(f"lal-{i}", 4) for i in range(5)])),
            422, "tied_game")
    short = HOME[:4] + [row("phx-4", minutes=47)]
    refused("239 team minutes is refused",
            lambda: commit(game(home_rows=short)), 422, "team minutes 239")
    ot_home = HOME + [row("phx-5", 2, minutes=25)]      # 265 minutes, 56 points
    BIOS["phx-5"] = {"name": "PHX-5, BIO"}
    refused("teams that disagree on overtime are refused",
            lambda: commit(game(home_rows=ot_home, home_pts=56)), 422, "overtime mismatch")
    one_ot_home = [row(f"phx-{i}", minutes=53) for i in range(5)]
    over_cap = ([row(f"lal-{i}", 5, minutes=53) for i in range(3)]
                + [row("lal-3", 5, minutes=54), row("lal-4", 5, minutes=52)])
    refused("a player past the game's length is refused",
            lambda: commit(game(home_rows=one_ot_home, away_rows=over_cap)),
            422, "played 54, the game was 53")
    check("nothing was written by any refused game", not target.exists())

    print("a good game")
    four_ot = [row(f"phx-{i}", minutes=68) for i in range(5)]
    four_ot_away = [row(f"lal-{i}", 5, minutes=68) for i in range(5)]
    named = [r.model_copy(update={"player": "typed by hand"}) for r in four_ot]
    result = commit(game(home_rows=named, away_rows=four_ot_away))
    check("a legal four-overtime game commits", result.get("rows_added") == 10, result)
    text = target.read_text()
    check("PLAYER is the bio's name", "PHX-0, BIO" in text)
    check("not what the caller typed", "typed by hand" not in text)


def check_upload_rule():
    print("uploads")
    refused("an upload for the wrong season is refused",
            lambda: bs._check_game_open("2026-10-21", "BOS", "NYK", "25-26", "REG"),
            422, "is in the 26-27 season")
    check("the game type comes back upper-cased",
          bs._check_game_open("2026-10-21", "BOS", "NYK", "26-27", "reg") == "REG")


def check_build(tmp: Path):
    status_file = tmp / "build-status.json"
    bs.BUILD_STATUS_FILE = status_file
    now = datetime.now(timezone.utc)
    started = []
    bs._build_loop = lambda s: started.append(s)

    print("build status")
    # A pid that cannot exist: the process the status names is gone.
    dead = {"status": "running", "started_at": now.isoformat(), "pid": 2 ** 22 + 1}
    status_file.write_text(json.dumps(dead))
    check("a running build whose process is gone reads as interrupted",
          bs._read_build_status()["status"] == "error")
    check("and doesn't block the next build", bs._trigger_build() and len(started) == 1)

    old = {"status": "running", "started_at": (now - timedelta(hours=2)).isoformat()}
    status_file.write_text(json.dumps(old))
    check("a two-hour-old running build is treated as dead",
          bs._trigger_build() and len(started) == 2)

    live = {"status": "running", "started_at": now.isoformat(), "pid": os.getpid()}
    status_file.write_text(json.dumps(live))
    check("a commit during a live build still reports a build coming",
          bs._trigger_build() is True)
    check("without starting a second one", len(started) == 2)
    check("but asks the running one to go again",
          json.loads(status_file.read_text()).get("rerun") is True)

    print("rerun")
    runs = []

    def fake_once(s):
        runs.append(s)
        if len(runs) == 1:      # a commit lands while the first pass runs
            st = json.loads(status_file.read_text())
            st["pid"] = os.getpid()
            status_file.write_text(json.dumps(st))
            bs._trigger_build()
        return {"status": "done", "started_at": s, "finished_at": "x"}

    real_loop = globals()["REAL_LOOP"]
    bs._run_build_once = fake_once
    status_file.write_text(json.dumps({"status": "running", "started_at": now.isoformat()}))
    real_loop(now.isoformat())
    check("the build runs a second pass for the mid-build commit", len(runs) == 2, runs)
    final = json.loads(status_file.read_text())
    check("and ends done, with no rerun left over",
          final.get("status") == "done" and not final.get("rerun"), final)


REAL_LOOP = bs._build_loop


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        saved = {k: getattr(bs, k) for k in (
            "DATA_DIR", "PLAYER_BIOS_FILE", "record_commit", "_remove_from_manual_queue",
            "_current_league_year", "_game_in_manual_queue", "BUILD_STATUS_FILE",
            "_build_loop", "_run_build_once")}
        saved_archive = bs.shots.archive_for_game
        try:
            bs.DATA_DIR = tmp
            bs.PLAYER_BIOS_FILE = tmp / "player-bios.json"
            bs.record_commit = lambda **kw: None
            bs._remove_from_manual_queue = lambda *a: None
            bs._game_in_manual_queue = lambda *a: False
            bs._current_league_year = lambda: "26-27"
            bs.shots.archive_for_game = lambda *a: None
            (tmp / "allstats-25-26.csv").write_text(HEADER)
            bs.PLAYER_BIOS_FILE.write_text(json.dumps(BIOS))
            # Resolve against the live BIOS dict, which gains a player mid-test.
            real_resolve = bs._resolve_players
            bs._resolve_players = lambda body, bios: real_resolve(body, BIOS)

            check_commits(tmp)
            check_upload_rule()
            check_build(tmp)
        finally:
            for k, v in saved.items():
                setattr(bs, k, v)
            bs.shots.archive_for_game = saved_archive
            bs._resolve_players = real_resolve

    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

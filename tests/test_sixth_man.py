"""6MOY eligibility (rulebook § 8.1) — `game_starters.sixth_man_status` and
the ballot save that enforces it. Pins:

  * a player who came off the bench in more of his known games than he
    started is eligible; exactly half is not
  * known games are his games played, capped by his teams' games with known
    starters, summed across a trade
  * with nothing known (no starters recorded) a player is eligible
  * saving a team ballot with an ineligible 6MOY pick is a 422 naming him,
    and nothing is written; an eligible ballot saves

Every file is a temp file; nothing here touches $DATA_DIR.

    venv/bin/python -m tests.test_sixth_man
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

import routers.game_starters as gs  # noqa: E402
import routers.misc as misc  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nbn-sixth-man-test-"))
gs.STARTERS_FILE = tmp / "game-starters.jsonl"
gs.PLAYER_SEASONS_CSV = tmp / "player_seasons.csv"


def game(date, starters):
    """One REG game line; `starters` is team → five slugs, or None for unknown."""
    return {"date": date, "season": "26-27", "game_type": "REG",
            "home_team": list(starters)[0], "away_team": list(starters)[-1],
            "teams": {t: {"starters": s, "flags": []} for t, s in starters.items()}}


A = ["a-star", "split", "a3", "a4", "a5"]
B = ["b1", "b2", "b3", "b4", "b5"]
with gs.STARTERS_FILE.open("w") as fh:
    for line in [
        game("2026-10-20", {"AAA": A, "BBB": B}),
        game("2026-10-21", {"AAA": ["a-star", "bench", "a3", "a4", "a5"], "BBB": None}),
        game("2026-10-22", {"AAA": ["a-star", "traded", "a3", "a4", "a5"], "BBB": B}),
        game("2026-10-23", {"AAA": ["a-star", "a2", "a3", "a4", "a5"], "BBB": B}),
    ]:
        fh.write(json.dumps(line) + "\n")

gs.PLAYER_SEASONS_CSV.write_text("\n".join([
    "PLAYER,SEASON,TEAM,G,SLUG",
    '"Star, A",26-27,AAA,4,a-star',
    '"Split, S",26-27,AAA,2,split',      # started 1 of 2: exactly half
    '"Bench, B",26-27,AAA,4,bench',      # started 1 of 4
    '"Traded, T",26-27,BBB,1,traded',    # BBB: 3 known games, he played 1
    '"Traded, T",26-27,AAA,2,traded',    # AAA: started 1 → 1 of 3
    '"Ghost, G",26-27,CCC,5,ghost',      # CCC never known
    '"Old, O",25-26,AAA,70,old',
]) + "\n")

print("eligibility")
st = gs.sixth_man_status("26-27")
check("a starter in every known game is out", st["a-star"] == {"gs": 4, "known": 4, "eligible": False})
check("exactly half is out", st["split"] == {"gs": 1, "known": 2, "eligible": False})
check("fewer than half is in", st["bench"]["eligible"] and st["bench"]["gs"] == 1)
check("known games sum across a trade, capped at his own", st["traded"]["known"] == 3)
check("a traded player with 1 start of 3 is in", st["traded"]["eligible"])
check("nothing known is eligible", st["ghost"] == {"gs": 0, "known": 0, "eligible": True})
check("another season's rows are not read", "old" not in st)
check("a season with no starters recorded is all eligible",
      all(p["eligible"] for p in gs.sixth_man_status("25-26").values()))
check("the endpoint serves the same", gs.get_sixth_man(season="26-27")["players"] == st)

print("\nballot save")
misc.AWARDS_CONFIG_FILE = tmp / "awards-config.json"
misc._awards_ballots_path = lambda season: tmp / f"awards-ballots-{season}.json"
misc._resolve_token = lambda authorization: {"name": "Owner", "roles": ["aaa"]}
misc.PLAYER_BIOS_FILE = tmp / "player-bios.json"
misc.PLAYER_BIOS_FILE.write_text(json.dumps({"split": {"name": "Split, Sam"}}))
misc.VALID_TEAMS = {"AAA"}
misc.log_write = lambda info, msg: None
misc.has_role = lambda info, role: False

try:
    misc.put_team_ballot("26-27", "AAA", {"6MOY": ["bench", "split", None]}, "Bearer x")
    check("an ineligible 6MOY pick is refused", False)
except HTTPException as e:
    check("an ineligible 6MOY pick is a 422", e.status_code == 422)
    check("it names him, with his starts", "Sam Split (started 1 of 2)" in e.detail["message"])
    check("and lists only him", [p["slug"] for p in e.detail["ineligible"]] == ["split"])
check("nothing is written", not misc._awards_ballots_path("26-27").exists())

r = misc.put_team_ballot("26-27", "AAA", {"6MOY": ["bench", "traded", "ghost"], "MVP": ["a-star"]}, "Bearer x")
check("an eligible ballot saves", r["ok"] and json.loads(
    misc._awards_ballots_path("26-27").read_text())["AAA"]["6MOY"] == ["bench", "traded", "ghost"])
check("a starter is still fine on other awards",
      json.loads(misc._awards_ballots_path("26-27").read_text())["AAA"]["MVP"] == ["a-star"])

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

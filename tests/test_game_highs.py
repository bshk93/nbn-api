"""`GET /api/game-highs` — single-game highs and feats for any slice.

Pins the ranking rules the page relies on (ties share a rank, the earlier
game first), that every filter narrows without renumbering into nonsense,
the true-shooting floor, the feat counts, and that Game Score and names are
the build's own. Synthetic rows only; nothing here reads the data dir.

    venv/bin/python -m tests.test_game_highs
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import game_highs as gh  # noqa: E402
from stats_build import pipeline  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def game(player, team, opp, date, season="24-25", playoff=False, **stats):
    r = {"PLAYER": player, "TEAM": team, "OPP": opp, "DATE": date, "SEASON": season,
         "gametype": "PLAYOFF" if playoff else "REG", "WL": "W",
         "TEAM_PTS": "110", "OPP_TEAM_PTS": "100", "ROUND": "2" if playoff else "",
         "GAME": "3" if playoff else ""}
    for k in gh.STATS:
        r[k] = str(stats.get(k, 0))
    return r


ROWS = [
    game("CURRY, STEPHEN", "GSW", "NOP", "2025-01-05", P=50, R=10, A=10, FGA=30, FGM=18, M=40),
    game("JOKIC, NIKOLA", "DEN", "@GSW", "2025-01-03", P=50, R=20, A=15, FGA=25, FGM=20, M=38),
    game("JOKIC, NIKOLA", "DEN", "LAL", "2025-02-01", P=12, R=11, A=10, FGA=9, M=30),
    game("KANTER, ENES", "BOS", "MIA", "2025-02-02", P=4, R=20, M=25),
    game("CURRY, STEPHEN", "GSW", "@DEN", "2025-05-01", playoff=True, P=60, FGA=19, M=44),
    game("DAVIS, ANTHONY", "LAL", "DEN", "2024-01-01", season="23-24", P=40, B=6, FGA=20, FTA=10, M=36),
]
idx = gh.GameIndex(iter(ROWS))
everything = idx.filter(gametype="all", season="", team="", slugs=None)

print("ranking")
top = idx.top_games("P", everything, 10)
check("the best game is first", top[0]["player"] == "CURRY, STEPHEN" and top[0]["value"] == 60)
check("a tie shares a rank", [r["rank"] for r in top[:3]] == [1, 2, 2])
check("the earlier game wins a tie", top[1]["date"] == "2025-01-03")
check("the next rank skips past the tie", top[3]["rank"] == 4)
check("limit is respected", len(idx.top_games("P", everything, 2)) == 2)

print("filters")
reg = idx.filter(gametype="reg", season="", team="", slugs=None)
po = idx.filter(gametype="po", season="", team="", slugs=None)
check("regular season drops the playoff game", idx.top_games("P", reg, 1)[0]["value"] == 50)
check("playoffs keep only it", [r["value"] for r in idx.top_games("P", po, 9)] == [60])
check("a playoff line carries round and game", idx.top_games("P", po, 1)[0]["round"] == 2)
den = idx.filter(gametype="all", season="", team="DEN", slugs=None)
check("a franchise keeps its own games", {r["team"] for r in idx.top_games("P", den, 9)} == {"DEN"})
s23 = idx.filter(gametype="all", season="23-24", team="", slugs=None)
check("a season keeps its own games", [r["player"] for r in idx.top_games("P", s23, 9)] == ["DAVIS, ANTHONY"])
act = idx.filter(gametype="all", season="", team="", slugs={"jokic-nikola"})
check("active keeps only those slugs", {r["slug"] for r in idx.top_games("P", act, 9)} == {"jokic-nikola"})
none = idx.filter(gametype="all", season="", team="TOR", slugs=None)
check("a slice with no games is empty, not an error", idx.top_games("P", none, 5) == [])
check("the road game is marked away", idx.top_games("P", den, 1)[0]["home"] is False)

print("derived categories")
check("PRA adds the three", idx.top_games("PRA", everything, 1)[0]["value"] == 85)
ts = idx.top_games("TS", everything, 9)
check("true shooting needs 20+ FGA", {r["player"] for r in ts} == {"CURRY, STEPHEN", "JOKIC, NIKOLA", "DAVIS, ANTHONY"})
check("a 19-FGA game is not ranked on TS", all(r["value"] != 60 / 38 for r in ts))
for r in ROWS:
    built = pipeline.game_score(r)
    mine = gh.game_score({k: int(r[k]) for k in gh.STATS})
    if abs(built - mine) > 0.006:
        check(f"Game Score matches the build for {r['PLAYER']} {r['DATE']}", False)
        break
else:
    check("Game Score matches the build's formula", True)

print("names")
bos = idx.top_games("R", idx.filter(gametype="all", season="", team="BOS", slugs=None), 1)[0]
check("the build's name fixes apply", bos["player"] == "FREEDOM, ENES")
check("slugs are the build's", bos["slug"] == "freedom-enes")

print("feats")
td = idx.top_feats("TD", everything, 10, True)
check("triple-doubles counted per player", [(r["player"], r["value"]) for r in td]
      == [("JOKIC, NIKOLA", 2), ("CURRY, STEPHEN", 1)])
check("games in the slice ride along", td[0]["games"] == 2 and td[1]["games"] == 2)
check("50-point games", [(r["slug"], r["value"]) for r in idx.top_feats("P50", everything, 9, False)]
      == [("curry-stephen", 2), ("jokic-nikola", 1)])
check("a feat respects the filter", idx.top_feats("P50", reg, 9, False)[0]["value"] == 1)
p40 = idx.top_feats("P40", everything, 9, False)
check("a feat tie shares a rank", [r["rank"] for r in p40] == [1, 2, 2])

print("payload")
p = gh.game_highs_payload(idx, gametype="all", season="", team="", slugs=None, stat="B5", limit=5)
check("a card per category", [c["key"] for c in p["categories"]] == [c["key"] for c in gh.CATEGORIES])
check("a card per feat", [f["key"] for f in p["feats"]] == [f["key"] for f in gh.FEATS])
check("the board can be a feat", p["board"]["kind"] == "feat" and p["board"]["rows"][0]["slug"] == "davis-anthony")
check("seasons listed", p["seasons"] == ["23-24", "24-25"])
check("the TS floor is stated", next(c for c in p["categories"] if c["key"] == "TS")["floor"] == "20+ FGA")

print("route")
from fastapi import HTTPException  # noqa: E402
gh.get_index = lambda: (idx, ("k",))
gh._active_slugs = lambda: {"jokic-nikola"}
gh._responses.clear()
for bad in ({"team": "XXX"}, {"stat": "nope"}, {"season": "99-00"}):
    try:
        gh.get_game_highs(**{"type": "all", "season": "", "team": "", "active": False,
                             "stat": "P", "limit": 10, **bad})
        check(f"{bad} is refused", False)
    except HTTPException as e:
        check(f"{bad} is refused", e.status_code == 422)
out = gh.get_game_highs(type="all", season="", team="den", active=True, stat="P", limit=10)
check("team is case-insensitive and active applies",
      {r["slug"] for r in out["board"]["rows"]} == {"jokic-nikola"})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all checks passed")

"""Career feats, streaks and teammates for one player — `GET /api/players/{slug}/insights`.

Three things a player page can't work out from its own game log, because each
needs every other player's games too:

  feats      career counts of games clearing a bar (the /stats/highs FEATS,
             same tests) with the player's all-time rank in each
  streaks    the longest run of consecutive regular-season games clearing a
             bar, with the dates, whether it is still running, and its rank
             among every player's longest
  teammates  who they shared the floor with most: games together and the
             team's record in them

Built on the game-highs index (`game_highs.get_index`), so it reads the raw box
scores the way the build does and is rebuilt when they change. The league-wide
tables (every player's feat counts, every player's longest streaks, the
team-and-date roster of each game) are computed once per index and cached.

    venv/bin/python -m tests.test_player_insights
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException

from . import game_highs as gh

router = APIRouter()

# Streaks run over regular-season games only, in date order, and carry across
# seasons (a run that ends one April and resumes in October is one run), the
# way the NBA counts them. A game the player didn't play is not in the log, so
# it neither extends nor breaks a run.
STREAKS: list[dict] = [
    {"key": "P20", "name": "20-point games"},
    {"key": "P30", "name": "30-point games"},
    {"key": "DD",  "name": "Double-doubles"},
    {"key": "R10", "name": "10-rebound games"},
    {"key": "A10", "name": "10-assist games"},
    {"key": "3PM", "name": "Games with a three"},
]
TEAMMATES_MAX = 15


def _streak_tests(c) -> dict:
    P, R, A, S, B = c["P"], c["R"], c["A"], c["S"], c["B"]
    tens = lambda i: (P[i] >= 10) + (R[i] >= 10) + (A[i] >= 10) + (S[i] >= 10) + (B[i] >= 10)
    return {
        "P20": lambda i: P[i] >= 20,
        "P30": lambda i: P[i] >= 30,
        "DD":  lambda i: tens(i) >= 2,
        "R10": lambda i: R[i] >= 10,
        "A10": lambda i: A[i] >= 10,
        "3PM": lambda i: c["3PM"][i] >= 1,
    }


def _rank(value: int, all_values: list[int]) -> dict:
    """Competition rank: 1 + how many are strictly ahead; `tied` if level with another."""
    ahead = sum(1 for v in all_values if v > value)
    level = sum(1 for v in all_values if v == value)
    return {"rank": ahead + 1, "tied": level > 1}


class Insights:
    def __init__(self, index: gh.GameIndex):
        self.index = index
        s, m = index.strings.values, index.meta
        slug, dates, team = m["slug"], m["date"], m["team"]

        # Every player's feat count, all games (the /stats/highs default).
        self.feat_counts: dict[str, Counter] = {
            f["key"]: Counter(slug[i] for i in index.feat_rows[f["key"]]) for f in gh.FEATS
        }

        # Each player's games: all of them, and the regular season in date order.
        self.all_games: dict[int, list[int]] = defaultdict(list)
        by_player: dict[int, list[int]] = defaultdict(list)
        for i in range(index.n):
            self.all_games[slug[i]].append(i)
            if not index.playoff[i]:
                by_player[slug[i]].append(i)
        for rows in by_player.values():
            rows.sort(key=lambda i: s[dates[i]])
        self.reg_games = by_player

        # Each player's longest run per streak: (length, first row, last row).
        tests = _streak_tests(index.cols)
        self.streaks: dict[str, dict[int, tuple[int, int, int]]] = {}
        for st in STREAKS:
            test = tests[st["key"]]
            best: dict[int, tuple[int, int, int]] = {}
            for pid, rows in by_player.items():
                top, run, start = (0, -1, -1), 0, -1
                for i in rows:
                    if test(i):
                        if run == 0:
                            start = i
                        run += 1
                        if run > top[0]:
                            top = (run, start, i)
                    else:
                        run = 0
                if top[0]:
                    best[pid] = top
            self.streaks[st["key"]] = best

        # Who played for which team on which date: the teammates of a game.
        games: dict[tuple[int, int, bool], list[int]] = defaultdict(list)
        for i in range(index.n):
            games[(team[i], dates[i], bool(index.playoff[i]))].append(i)
        self.games = games

    def payload(self, slug: str) -> Optional[dict]:
        index = self.index
        s, m = index.strings.values, index.meta
        pid = index.strings.ids.get(slug)
        if pid is None or pid not in self.all_games:
            return None

        feats = []
        for f in gh.FEATS:
            counts = self.feat_counts[f["key"]]
            n = counts.get(pid, 0)
            row = {"key": f["key"], "name": f["name"], "count": n}
            if n:
                row.update(_rank(n, list(counts.values())))
            feats.append(row)

        reg = self.reg_games.get(pid, [])
        last = reg[-1] if reg else None
        streaks = []
        for st in STREAKS:
            best = self.streaks[st["key"]]
            if pid not in best:
                streaks.append({"key": st["key"], "name": st["name"], "length": 0})
                continue
            length, first, end = best[pid]
            streaks.append({
                "key": st["key"], "name": st["name"], "length": length,
                "start": s[m["date"][first]], "end": s[m["date"][end]],
                "active": end == last,
                **_rank(length, [v[0] for v in best.values()]),
            })

        together: Counter = Counter()
        wins: Counter = Counter()
        names: dict[int, int] = {}
        pts, opp_pts = index.cols["team_pts"], index.cols["opp_pts"]
        for i in self.all_games[pid]:
            key = (m["team"][i], m["date"][i], bool(index.playoff[i]))
            # The score, not the raw WL column, which is NA in most seasons.
            won = pts[i] > opp_pts[i]
            for j in self.games[key]:
                other = m["slug"][j]
                if other == pid:
                    continue
                together[other] += 1
                wins[other] += won
                names.setdefault(other, m["player"][j])
        teammates = [
            {"slug": s[o], "player": s[names[o]], "games": g, "w": wins[o], "l": g - wins[o]}
            for o, g in sorted(together.items(), key=lambda kv: (-kv[1], s[names[kv[0]]]))[:TEAMMATES_MAX]
        ]
        return {"feats": feats, "streaks": streaks, "teammates": teammates}


_lock = threading.Lock()
_cache: tuple[Optional[tuple], Optional[Insights]] = (None, None)


def get_insights() -> Insights:
    global _cache
    index, key = gh.get_index()
    with _lock:
        if _cache[0] != key:
            _cache = (key, Insights(index))
        return _cache[1]


@router.get("/api/players/{slug}/insights")
def get_player_insights(slug: str):
    """Feats, streaks and teammates for one player. Public and read-only."""
    out = get_insights().payload(slug)
    if out is None:
        raise HTTPException(status_code=404, detail="No games for that player")
    return out

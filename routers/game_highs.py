"""Single-game highs and feats — `GET /api/game-highs`, behind /stats/highs.

The build's `game-highs-*.csv` are a fixed top 50 per stat across every game
ever played, so they cannot answer "in the playoffs", "for this franchise",
"in 23-24" or "active players only": a filtered top 5 can sit far below the
league-wide top 50. Those slices need every game, and the raw box scores are
~19MB — too much to send a browser, cheap to keep here.

The rows are read the way the build reads them: the same files, the same
season labels (`_season_label`), the same name fixes (`PLAYER_FIXES`), the
same slugs (`player_slug`) and the same Game Score formula. So a game that is
#1 here is #1 in `game-highs-*.csv` and `franchise-records.csv` too.

The index is ~160k games held as typed columns (~15MB, not the ~400MB the
same rows cost as dicts), and the files are streamed a line at a time so
building it never holds them as dicts either. It is rebuilt when any raw
file's mtime changes, which is every box score commit.

    venv/bin/python -m tests.test_game_highs
"""
from __future__ import annotations

import csv
import threading
from array import array
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Optional

from fastapi import APIRouter, HTTPException, Query

from stats_build.pipeline import PLAYER_FIXES, _season_label, player_slug

from .constants import DATA_DIR, VALID_TEAMS
from .storage import read_csv

router = APIRouter()

# Raw columns kept per game: everything a category or feat reads, plus the
# shooting lines the page prints beside it.
STATS = ("M", "P", "R", "OR", "DR", "A", "S", "B", "TO", "PF",
         "FGM", "FGA", "3PM", "3PA", "FTM", "FTA")

# What the page can rank single games by. `floor` is shown to the reader, so
# it must say exactly what `_category_values` tests.
CATEGORIES: list[dict] = [
    {"key": "P",    "name": "Points",             "abbr": "PTS"},
    {"key": "R",    "name": "Rebounds",           "abbr": "REB"},
    {"key": "A",    "name": "Assists",            "abbr": "AST"},
    {"key": "S",    "name": "Steals",             "abbr": "STL"},
    {"key": "B",    "name": "Blocks",             "abbr": "BLK"},
    {"key": "3PM",  "name": "Threes made",        "abbr": "3PM"},
    {"key": "PRA",  "name": "Pts + Reb + Ast",    "abbr": "PRA"},
    {"key": "STK",  "name": "Stocks (Stl + Blk)", "abbr": "STK"},
    {"key": "GMSC", "name": "Game Score",         "abbr": "GmSc"},
    {"key": "FGM",  "name": "Field goals made",   "abbr": "FGM"},
    {"key": "FTM",  "name": "Free throws made",   "abbr": "FTM"},
    {"key": "OR",   "name": "Offensive rebounds", "abbr": "OREB"},
    {"key": "TS",   "name": "True shooting",      "abbr": "TS%", "floor": "20+ FGA"},
    {"key": "M",    "name": "Minutes",            "abbr": "MIN"},
    {"key": "TO",   "name": "Turnovers",          "abbr": "TOV"},
]
TS_MIN_FGA = 20

# Career counts of games that clear a bar. (key, name, test over one game's
# columns by index.)
FEATS: list[dict] = [
    {"key": "TD",   "name": "Triple-doubles"},
    {"key": "DD",   "name": "Double-doubles"},
    {"key": "P50",  "name": "50-point games"},
    {"key": "P40",  "name": "40-point games"},
    {"key": "P30",  "name": "30-point games"},
    {"key": "R20",  "name": "20-rebound games"},
    {"key": "A15",  "name": "15-assist games"},
    {"key": "S5",   "name": "5-steal games"},
    {"key": "B5",   "name": "5-block games"},
    {"key": "3PM8", "name": "8-three games"},
]
CAT_BY_KEY = {c["key"]: c for c in CATEGORIES}
FEAT_BY_KEY = {f["key"]: f for f in FEATS}
SUMMARY_TOP = 5
MAX_LIMIT = 500


def game_score(s: dict[str, int]) -> float:
    """The build's Game Score (`stats_build.pipeline.game_score`), from ints.

    The build rounds it to 2 places R's way before summing; here it is only
    ranked and shown to 1 place, so the rounding is left to the display.
    """
    return (s["P"] + 0.4 * s["FGM"] - 0.7 * s["FGA"] - 0.4 * (s["FTA"] - s["FTM"])
            + 0.7 * s["OR"] + 0.3 * s["DR"] + s["S"] + 0.7 * s["A"] + 0.7 * s["B"]
            - 0.4 * s["PF"] - s["TO"])


def _int(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0


def iter_raw_files(data_dir: Path) -> Iterable[tuple[str, bool, Path]]:
    """(season, is_playoff, path) for every raw file the build reads."""
    for playoffs, pattern in ((False, "allstats-[0-9]*.csv"), (True, "allstats-playoffs-*.csv")):
        for path in sorted(data_dir.glob(pattern)):
            label = _season_label(path.name, playoffs)
            if label is not None:
                yield label.split(" ")[0], playoffs, path


def iter_games(data_dir: Path) -> Iterable[dict]:
    """One dict per raw line, streamed — never a whole file of them at once."""
    for season, playoffs, path in iter_raw_files(data_dir):
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r["SEASON"] = season
                r["gametype"] = "PLAYOFF" if playoffs else "REG"
                yield r


class _Strings:
    """Interns repeated text (names, dates, teams) as small ints."""
    def __init__(self):
        self.values: list[str] = []
        self.ids: dict[str, int] = {}

    def id(self, s: str) -> int:
        i = self.ids.get(s)
        if i is None:
            i = self.ids[s] = len(self.values)
            self.values.append(s)
        return i


class GameIndex:
    """Every game line, column-wise, with each category pre-sorted.

    A query walks a category's sorted order and keeps the first rows that pass
    its filter, so a top 5 for one franchise's playoffs stops after a few
    hundred rows instead of scoring all of them.
    """

    def __init__(self, games: Iterable[dict]):
        self.strings = st = _Strings()
        sid = st.id
        slug_of: dict[str, int] = {}
        cols = {k: array("h") for k in (*STATS, "round", "game", "team_pts", "opp_pts")}
        meta = {k: array("I") for k in ("player", "slug", "team", "opp", "date", "season", "wl")}
        home, playoff = array("b"), array("b")

        for r in games:
            name = PLAYER_FIXES.get(r["PLAYER"], r["PLAYER"])
            if name not in slug_of:
                slug_of[name] = sid(player_slug(name))
            opp = (r.get("OPP") or "").strip()
            is_po = r.get("gametype") == "PLAYOFF"
            meta["player"].append(sid(name))
            meta["slug"].append(slug_of[name])
            meta["team"].append(sid((r.get("TEAM") or "").strip()))
            meta["opp"].append(sid(opp.lstrip("@")))
            meta["date"].append(sid((r.get("DATE") or "").strip()))
            meta["season"].append(sid(r["SEASON"]))
            meta["wl"].append(sid((r.get("WL") or "").strip()))
            home.append(0 if opp.startswith("@") else 1)
            playoff.append(1 if is_po else 0)
            for k in STATS:
                cols[k].append(_int(r.get(k)))
            cols["round"].append(_int(r.get("ROUND")) if is_po else 0)
            cols["game"].append(_int(r.get("GAME")) if is_po else 0)
            cols["team_pts"].append(_int(r.get("TEAM_PTS")))
            cols["opp_pts"].append(_int(r.get("OPP_TEAM_PTS")))

        self.cols, self.meta, self.home, self.playoff = cols, meta, home, playoff
        self.n = n = len(home)
        self.values = self._category_values()

        # Best first, and the earlier game wins a tie, as in the build's
        # game-highs: sort by date once, then a stable sort on the value alone
        # (reverse=True keeps equal values in their date order).
        strs = st.values
        dates = meta["date"]
        by_date = sorted(range(n), key=lambda i: strs[dates[i]])
        self.order: dict[str, array] = {}
        for c in CATEGORIES:
            vals = self.values[c["key"]]
            pool = [i for i in by_date if vals[i] == vals[i]]   # NaN = not ranked
            pool.sort(key=vals.__getitem__, reverse=True)
            self.order[c["key"]] = array("I", pool)

        self.feat_rows = self._feat_rows()
        self.seasons = sorted({strs[s] for s in meta["season"]})

    def _category_values(self) -> dict[str, array]:
        c = self.cols
        P, R, A, S, B = c["P"], c["R"], c["A"], c["S"], c["B"]
        nan = float("nan")
        out = {k: c[k] for k in ("P", "R", "A", "S", "B", "3PM", "FGM", "FTM", "OR", "M", "TO")}
        out["PRA"] = array("h", (p + r + a for p, r, a in zip(P, R, A)))
        out["STK"] = array("h", (s + b for s, b in zip(S, B)))
        out["GMSC"] = array("f", (
            game_score({k: c[k][i] for k in STATS}) for i in range(len(P))))
        out["TS"] = array("f", (
            p / (2 * (fga + 0.44 * fta)) if fga >= TS_MIN_FGA else nan
            for p, fga, fta in zip(P, c["FGA"], c["FTA"])))
        return out

    def _feat_rows(self) -> dict[str, array]:
        c = self.cols
        P, R, A, S, B = c["P"], c["R"], c["A"], c["S"], c["B"]
        tens = [(p >= 10) + (r >= 10) + (a >= 10) + (s >= 10) + (b >= 10)
                for p, r, a, s, b in zip(P, R, A, S, B)]
        rng = range(self.n)
        pick = lambda test: array("I", (i for i in rng if test(i)))
        return {
            "TD":   pick(lambda i: tens[i] >= 3),
            "DD":   pick(lambda i: tens[i] >= 2),
            "P50":  pick(lambda i: P[i] >= 50),
            "P40":  pick(lambda i: P[i] >= 40),
            "P30":  pick(lambda i: P[i] >= 30),
            "R20":  pick(lambda i: R[i] >= 20),
            "A15":  pick(lambda i: A[i] >= 15),
            "S5":   pick(lambda i: S[i] >= 5),
            "B5":   pick(lambda i: B[i] >= 5),
            "3PM8": pick(lambda i: c["3PM"][i] >= 8),
        }

    def line(self, i: int) -> dict:
        s, m, c = self.strings.values, self.meta, self.cols
        box = {k: c[k][i] for k in STATS}
        box["GMSC"] = round(self.values["GMSC"][i], 1)
        return {
            "slug": s[m["slug"][i]],
            "player": s[m["player"][i]],
            "team": s[m["team"][i]],
            "opp": s[m["opp"][i]],
            "home": bool(self.home[i]),
            "date": s[m["date"][i]],
            "season": s[m["season"][i]],
            "playoff": bool(self.playoff[i]),
            "round": c["round"][i] or None,
            "game": c["game"][i] or None,
            "wl": s[m["wl"][i]],
            "team_pts": c["team_pts"][i],
            "opp_pts": c["opp_pts"][i],
            "line": box,
        }

    def filter(self, *, gametype: str, season: str, team: str,
               slugs: Optional[set[str]]) -> Callable[[int], bool]:
        ids, m, playoff = self.strings.ids, self.meta, self.playoff
        want_po = {"reg": 0, "po": 1}.get(gametype)
        season_id = ids.get(season, -1) if season else None
        team_id = ids.get(team, -1) if team else None
        slug_ids = {ids[s] for s in slugs if s in ids} if slugs is not None else None
        seasons, teams, slug = m["season"], m["team"], m["slug"]

        def ok(i: int) -> bool:
            return ((want_po is None or playoff[i] == want_po)
                    and (season_id is None or seasons[i] == season_id)
                    and (team_id is None or teams[i] == team_id)
                    and (slug_ids is None or slug[i] in slug_ids))
        return ok

    def top_games(self, key: str, ok: Callable[[int], bool], limit: int) -> list[dict]:
        """Best `limit` games by one category; ties share a rank."""
        vals = self.values[key]
        out, prev, rank = [], None, 0
        for i in self.order[key]:
            if len(out) >= limit:
                break
            if not ok(i):
                continue
            v = vals[i]
            rank = len(out) + 1 if v != prev else rank
            prev = v
            row = self.line(i)
            row["rank"] = rank
            row["value"] = round(v, 4) if isinstance(v, float) else v
            out.append(row)
        return out

    def top_feats(self, key: str, ok: Callable[[int], bool], limit: int,
                  with_games: bool) -> list[dict]:
        """Players with the most games clearing one bar; ties share a rank.

        With `with_games`, each row also carries the player's games in the
        same slice, so a count can be read against how often they played.
        """
        s, slug, player = self.strings.values, self.meta["slug"], self.meta["player"]
        counts: Counter = Counter()
        names: dict[int, int] = {}
        for i in self.feat_rows[key]:
            if ok(i):
                counts[slug[i]] += 1
                names.setdefault(slug[i], player[i])
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], s[names[kv[0]]]))[:limit]
        games: Counter = Counter()
        if with_games and ranked:
            wanted = {k for k, _ in ranked}
            for i in range(self.n):
                if slug[i] in wanted and ok(i):
                    games[slug[i]] += 1
        out, prev, rank = [], None, 0
        for n, (slug_id, count) in enumerate(ranked, 1):
            rank = n if count != prev else rank
            prev = count
            row = {"rank": rank, "slug": s[slug_id], "player": s[names[slug_id]], "value": count}
            if with_games:
                row["games"] = games[slug_id]
            out.append(row)
        return out


def game_highs_payload(index: GameIndex, *, gametype: str, season: str, team: str,
                       slugs: Optional[set[str]], stat: str, limit: int) -> dict:
    ok = index.filter(gametype=gametype, season=season, team=team, slugs=slugs)
    if stat in CAT_BY_KEY:
        board = {"key": stat, "kind": "game", "rows": index.top_games(stat, ok, limit)}
    else:
        board = {"key": stat, "kind": "feat", "rows": index.top_feats(stat, ok, limit, True)}
    return {
        "seasons": index.seasons,
        "categories": [{**c, "rows": index.top_games(c["key"], ok, SUMMARY_TOP)}
                       for c in CATEGORIES],
        "feats": [{**f, "rows": index.top_feats(f["key"], ok, SUMMARY_TOP, False)}
                  for f in FEATS],
        "board": board,
    }


# ── Loading and caching ──────────────────────────────────────────────────────

_lock = threading.Lock()
_index: Optional[GameIndex] = None
_index_key: Optional[tuple] = None
_responses: dict[tuple, dict] = {}


def _files_key() -> tuple:
    return tuple((p.name, p.stat().st_mtime_ns) for _s, _po, p in iter_raw_files(DATA_DIR))


def get_index() -> tuple[GameIndex, tuple]:
    global _index, _index_key
    key = _files_key()
    with _lock:
        if _index is None or _index_key != key:
            _index = GameIndex(iter_games(DATA_DIR))
            _index_key = key
            _responses.clear()
        return _index, _index_key


def _active_slugs() -> set[str]:
    """Slugs on a roster today — what "active" means everywhere on the site."""
    out = set()
    for team in VALID_TEAMS:
        path = DATA_DIR / f"{team.lower()}-roster.csv"
        if path.exists():
            _, rows = read_csv(path)
            out.update((r.get("SLUG") or "").strip() for r in rows)
    out.discard("")
    return out


@router.get("/api/game-highs")
def get_game_highs(
    type: str = Query(default="all", pattern="^(reg|po|all)$"),
    season: str = Query(default=""),
    team: str = Query(default=""),
    active: bool = Query(default=False),
    stat: str = Query(default="P"),
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
):
    """Top single games per category and top feat counts, for one slice.

    Public and read-only. One call returns everything the page shows: a top 5
    per category and per feat, and the full board for `stat`.
    """
    team = team.upper()
    if team and team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Unknown team")
    if stat not in CAT_BY_KEY and stat not in FEAT_BY_KEY:
        raise HTTPException(status_code=422, detail="Unknown stat")
    index, index_key = get_index()
    if season and season not in index.seasons:
        raise HTTPException(status_code=422, detail="Unknown season")
    slugs = _active_slugs() if active else None
    key = (index_key, type, season, team, frozenset(slugs) if slugs is not None else None, stat, limit)
    hit = _responses.get(key)
    if hit is not None:
        return hit
    payload = game_highs_payload(index, gametype=type, season=season, team=team,
                                 slugs=slugs, stat=stat, limit=limit)
    if len(_responses) > 128:
        _responses.clear()
    _responses[key] = payload
    return payload

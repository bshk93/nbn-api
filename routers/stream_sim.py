"""Streamed vs quick-simmed games — `GET /api/stream-vs-sim`, behind /stats/stream-sim.

Most regular-season games are quick-simmed; a few are played out in full on
stream. The schedule's `stream` flag (`schedule-{season}.json`) says which, and
it has only been kept since 26-27, so a season with no flagged game has nothing
to compare and is not offered.

A box score is matched to its scheduled game on date + its two teams,
unordered — the same match the stats committee's worklist makes. A game with no
schedule entry is counted as `unmatched` and left out of both sides, never
guessed into one. Regular season only: every playoff game is streamed, so the
playoffs have no sim side to compare.

Every measure is one entry in `METRICS`: a numerator and a denominator taken
from one team-game (or one game). The figure shown is pooled — the sum of
numerators over the sum of denominators, so FG% is makes over attempts, not an
average of nightly FG%s. Whether a gap is more than noise is a Welch t on the
per-unit values (`signal`): with a dozen or so streamed games a season, most
small gaps are noise, and the page says which.

Possessions are the usual estimate, FGA − OREB + TOV + 0.44·FTA, as in
`stats_build/advanced.py`.

    venv/bin/python -m tests.test_stream_sim
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

from fastapi import APIRouter, Query

from .boxscores import allstats_path
from .schedule import _known_seasons, _load, _resolve_season, _schedule_path
from .storage import read_csv

router = APIRouter()

TEAM_STATS = ("P", "OR", "DR", "R", "A", "S", "B", "TO", "PF",
              "FGM", "FGA", "3PM", "3PA", "FTM", "FTA", "M")
MODES = ("stream", "sim")
CLOSE, BLOWOUT = 5, 20
# |t| at or above these reads as a clear / likely difference.
CLEAR_T, LIKELY_T = 3.0, 2.0


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _games_from_rows(rows: Iterable[dict]) -> dict:
    """(date, frozenset(teams)) -> {date, teams: {abbr: {home, pts, opp_pts, tot, players}}}."""
    games: dict = {}
    for r in rows:
        date = (r.get("DATE") or "").strip()
        team = (r.get("TEAM") or "").strip()
        opp_field = (r.get("OPP") or "").strip()
        opp = (r.get("OPP_RAW") or opp_field).strip().lstrip("@")
        if not date or not team or not opp or team == opp:
            continue
        g = games.setdefault((date, frozenset((team, opp))), {"date": date, "teams": {}})
        t = g["teams"].setdefault(team, {
            "home": not opp_field.startswith("@"), "pts": _int(r.get("TEAM_PTS")),
            "opp_pts": _int(r.get("OPP_TEAM_PTS")),
            "tot": {k: 0 for k in TEAM_STATS}, "players": []})
        line = {k: _int(r.get(k)) for k in TEAM_STATS}
        for k in TEAM_STATS:
            t["tot"][k] += line[k]
        t["players"].append(line)
    return games


def _poss(t: dict) -> float:
    return t["FGA"] - t["OR"] + t["TO"] + 0.44 * t["FTA"]


def _team_unit(t: dict, opp: dict | None) -> dict:
    """One team-game, flattened to what the metrics read."""
    s = dict(t["tot"])
    o = opp["tot"] if opp else {k: 0 for k in TEAM_STATS}
    s.update({
        "PTS": t["pts"], "OPP_PTS": t["opp_pts"], "POSS": _poss(s), "OPP_POSS": _poss(o),
        "2PM": s["FGM"] - s["3PM"], "2PA": s["FGA"] - s["3PA"],
        "OPP_DR": o["DR"], "OPP_OR": o["OR"], "OPP_2PA": o["FGA"] - o["3PA"],
    })
    pl = [p for p in t["players"] if p["M"] > 0]
    by_min = sorted(pl, key=lambda p: p["M"], reverse=True)
    top5 = by_min[:5]
    cats = lambda p: sum(1 for k in ("P", "R", "A", "S", "B") if p[k] >= 10)
    s.update({
        "TOP_MIN": by_min[0]["M"] if by_min else 0,
        "USED": len(pl),
        "MIN30": sum(1 for p in pl if p["M"] >= 30),
        "TOP5_MIN": sum(p["M"] for p in top5),
        "BENCH_PTS": sum(p["P"] for p in by_min[5:]),
        "TOP_PTS": max((p["P"] for p in pl), default=0),
        "DBL_FIG": sum(1 for p in pl if p["P"] >= 10),
        "P20": sum(1 for p in pl if p["P"] >= 20),
        "P30": sum(1 for p in pl if p["P"] >= 30),
        "P40": sum(1 for p in pl if p["P"] >= 40),
        "DD": sum(1 for p in pl if cats(p) >= 2),
        "TD": sum(1 for p in pl if cats(p) >= 3),
    })
    return s


def _game_unit(g: dict, home: str) -> dict:
    ts = list(g["teams"].values())
    a = ts[0]
    margin = abs(a["pts"] - a["opp_pts"])
    total = a["pts"] + a["opp_pts"]
    h = g["teams"].get(home)
    return {
        "TOTAL": total, "WIN_PTS": max(a["pts"], a["opp_pts"]), "LOSE_PTS": min(a["pts"], a["opp_pts"]),
        "MARGIN": margin, "CLOSE": int(margin <= CLOSE), "BLOWOUT": int(margin >= BLOWOUT),
        # 240 player-minutes a side is regulation; anything over is overtime.
        "OT": int(any(t["tot"]["M"] > 245 for t in ts)),
        "HOME_WON": None if h is None else int(h["pts"] > h["opp_pts"]),
    }


ONE: Callable[[dict], float] = lambda u: 1


def _m(key, label, group, num, den=ONE, unit="team", fmt="n1", scale=1.0, note=None, family=None):
    return {"key": key, "label": label, "group": group, "num": num, "den": den,
            "unit": unit, "fmt": fmt, "scale": scale, "note": note, "family": family or key}


def _f(k):
    return lambda u: u[k]


# fmt: n1/n2/n3 = decimals, pct = a share shown as a percentage. `scale`
# multiplies the pooled ratio (100 for "per 100 possessions"). `family` ties
# measures that tell the same story (FTA, FTM, free throws per shot), so the
# page's headline tiles show one of each.
G_TEMPO, G_SHOOT, G_SRC, G_BALL, G_BOARDS, G_GAMES, G_ROT, G_BIG = (
    "Tempo and scoring", "Shooting", "Where the points come from", "Ball handling and defense",
    "Rebounding", "How games end", "Rotations", "Big individual games")
METRICS = [
    _m("pts", "Points per team", G_TEMPO, _f("PTS"), family="scoring"),
    _m("pace", "Possessions per team", G_TEMPO, _f("POSS")),
    _m("ortg", "Points per 100 possessions", G_TEMPO, _f("PTS"), _f("POSS"), scale=100, family="eff"),
    _m("total", "Combined score", G_TEMPO, _f("TOTAL"), unit="game", family="scoring"),
    _m("win_pts", "Winning score", G_TEMPO, _f("WIN_PTS"), unit="game", family="scoring"),
    _m("lose_pts", "Losing score", G_TEMPO, _f("LOSE_PTS"), unit="game", family="scoring"),

    _m("fgm", "Field goals made", G_SHOOT, _f("FGM"), family="fg"),
    _m("fga", "Field goals tried", G_SHOOT, _f("FGA"), family="fga"),
    _m("fg_pct", "FG%", G_SHOOT, _f("FGM"), _f("FGA"), fmt="pct", family="eff"),
    _m("2pm", "Twos made", G_SHOOT, _f("2PM"), family="fg"),
    _m("2pa", "Twos tried", G_SHOOT, _f("2PA"), family="fga"),
    _m("2p_pct", "2P%", G_SHOOT, _f("2PM"), _f("2PA"), fmt="pct"),
    _m("3pm", "Threes made", G_SHOOT, _f("3PM"), family="3p"),
    _m("3pa", "Threes tried", G_SHOOT, _f("3PA")),
    _m("3p_pct", "3P%", G_SHOOT, _f("3PM"), _f("3PA"), fmt="pct", family="3p"),
    _m("ftm", "Free throws made", G_SHOOT, _f("FTM"), family="ft"),
    _m("fta", "Free throws tried", G_SHOOT, _f("FTA"), family="ft"),
    _m("ft_pct", "FT%", G_SHOOT, _f("FTM"), _f("FTA"), fmt="pct"),
    _m("efg", "Effective FG%", G_SHOOT, lambda u: u["FGM"] + 0.5 * u["3PM"], _f("FGA"), fmt="pct",
       note="FG% with a three worth one and a half makes", family="eff"),
    _m("ts", "True shooting", G_SHOOT, _f("PTS"), lambda u: 2 * (u["FGA"] + 0.44 * u["FTA"]), fmt="pct", family="eff"),
    _m("3pa_rate", "Share of shots from three", G_SHOOT, _f("3PA"), _f("FGA"), fmt="pct"),
    _m("fta_rate", "Free throws per shot", G_SHOOT, _f("FTA"), _f("FGA"), fmt="n3", family="ft"),

    _m("pts_3", "Points from threes", G_SRC, lambda u: 3 * u["3PM"], _f("PTS"), fmt="pct", family="3p"),
    _m("pts_2", "Points from twos", G_SRC, lambda u: 2 * u["2PM"], _f("PTS"), fmt="pct"),
    _m("pts_ft", "Points from free throws", G_SRC, _f("FTM"), _f("PTS"), fmt="pct", family="ft"),

    _m("ast", "Assists", G_BALL, _f("A")),
    _m("ast_rate", "Share of makes assisted", G_BALL, _f("A"), _f("FGM"), fmt="pct"),
    _m("tov", "Turnovers", G_BALL, _f("TO"), family="tov"),
    _m("tov_pct", "Turnovers per 100 possessions", G_BALL, _f("TO"), _f("POSS"), scale=100, family="tov"),
    _m("ast_to", "Assists per turnover", G_BALL, _f("A"), _f("TO"), fmt="n2", family="tov"),
    _m("stl", "Steals", G_BALL, _f("S")),
    _m("stl_pct", "Steals per 100 opponent possessions", G_BALL, _f("S"), _f("OPP_POSS"), scale=100),
    _m("blk", "Blocks", G_BALL, _f("B")),
    _m("blk_pct", "Share of opponent twos blocked", G_BALL, _f("B"), _f("OPP_2PA"), fmt="pct"),
    _m("pf", "Fouls", G_BALL, _f("PF"), family="pf"),
    _m("pf_pct", "Fouls per 100 opponent possessions", G_BALL, _f("PF"), _f("OPP_POSS"), scale=100, family="pf"),

    _m("reb", "Rebounds", G_BOARDS, _f("R"), family="reb"),
    _m("oreb", "Offensive rebounds", G_BOARDS, _f("OR")),
    _m("dreb", "Defensive rebounds", G_BOARDS, _f("DR"), family="reb"),
    _m("oreb_pct", "Offensive rebound rate", G_BOARDS, _f("OR"), lambda u: u["OR"] + u["OPP_DR"], fmt="pct",
       note="share of a team's own misses it got back"),
    _m("dreb_pct", "Defensive rebound rate", G_BOARDS, _f("DR"), lambda u: u["DR"] + u["OPP_OR"], fmt="pct"),

    _m("margin", "Average margin", G_GAMES, _f("MARGIN"), unit="game"),
    _m("close", "Decided by 5 or fewer", G_GAMES, _f("CLOSE"), unit="game", fmt="pct"),
    _m("blowout", "Decided by 20 or more", G_GAMES, _f("BLOWOUT"), unit="game", fmt="pct"),
    _m("ot", "Went to overtime", G_GAMES, _f("OT"), unit="game", fmt="pct"),
    _m("home_won", "Home team won", G_GAMES, _f("HOME_WON"), unit="game", fmt="pct"),

    _m("top_min", "Most minutes by one player", G_ROT, _f("TOP_MIN"), family="stars"),
    _m("min30", "Players with 30+ minutes", G_ROT, _f("MIN30"), fmt="n2", family="stars"),
    _m("used", "Players who got minutes", G_ROT, _f("USED")),
    _m("top5_min", "Top five's share of minutes", G_ROT, _f("TOP5_MIN"), _f("M"), fmt="pct",
       note="the five who played the most that night", family="stars"),
    _m("bench_pts", "Bench share of points", G_ROT, _f("BENCH_PTS"), _f("PTS"), fmt="pct",
       note="everyone outside that night's top five in minutes", family="spread"),
    _m("top_pts", "Top scorer's share of points", G_ROT, _f("TOP_PTS"), _f("PTS"), fmt="pct", family="top_scorer"),
    _m("dbl_fig", "Players in double figures", G_ROT, _f("DBL_FIG"), fmt="n2", family="spread"),

    _m("p20", "20-point games", G_BIG, _f("P20"), fmt="n2", note="per team per game"),
    _m("p30", "30-point games", G_BIG, _f("P30"), fmt="n2", note="per team per game"),
    _m("p40", "40-point games", G_BIG, _f("P40"), fmt="n2", note="per team per game"),
    _m("dd", "Double-doubles", G_BIG, _f("DD"), fmt="n2", note="per team per game"),
    _m("td", "Triple-doubles", G_BIG, _f("TD"), fmt="n3", note="per team per game"),
]


def _mean_var(xs: list[float]) -> tuple[float, float, int]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    return m, v, n


def _side(metric: dict, units: list[dict]) -> tuple[float | None, list[float]]:
    """Pooled value, and the per-unit values the t-test reads."""
    num = den = 0.0
    per = []
    for u in units:
        try:
            a, b = metric["num"](u), metric["den"](u)
        except (TypeError, KeyError):
            continue
        if a is None or b is None or not b:
            continue
        num += a
        den += b
        per.append(a / b)
    return (num / den * metric["scale"] if den else None), per


def signal(t: float | None) -> str:
    if t is None:
        return "none"
    a = abs(t)
    return "clear" if a >= CLEAR_T else "likely" if a >= LIKELY_T else "noise"


def _metrics(units: dict[str, dict[str, list[dict]]]) -> list[dict]:
    out = []
    for m in METRICS:
        row = {k: m[k] for k in ("key", "label", "group", "unit", "fmt", "family")}
        if m["note"]:
            row["note"] = m["note"]
        per = {}
        for mode in MODES:
            row[mode], per[mode] = _side(m, units[mode][m["unit"]])
        a, b = row["stream"], row["sim"]
        row["diff"] = None if a is None or b is None else a - b
        row["rel"] = None if row["diff"] is None or not b else row["diff"] / abs(b)
        (ma, va, na), (mb, vb, nb) = _mean_var(per["stream"]), _mean_var(per["sim"])
        se = math.sqrt(va / na + vb / nb) if na > 1 and nb > 1 else 0
        t = None if not se else (ma - mb) / se
        row["t"] = None if t is None else round(t, 2)
        row["signal"] = signal(t)
        out.append(row)
    return out


def compare(rows: Iterable[dict], schedule_games: list[dict]) -> dict:
    """Split a season's regular-season box scores by the schedule's stream flag."""
    sched = {(g["date"], frozenset((g["home_team"], g["away_team"]))): g for g in schedule_games}
    units = {m: {"team": [], "game": []} for m in MODES}
    counts = {m: 0 for m in MODES}
    unmatched = 0
    stream_list = []
    for key, g in sorted(_games_from_rows(rows).items(), key=lambda kv: kv[0][0]):
        s = sched.get(key)
        if s is None:
            unmatched += 1
            continue
        mode = "stream" if s.get("stream") else "sim"
        counts[mode] += 1
        abbrs = list(g["teams"])
        for abbr in abbrs:
            opp = next((g["teams"][o] for o in abbrs if o != abbr), None)
            units[mode]["team"].append(_team_unit(g["teams"][abbr], opp))
        units[mode]["game"].append(_game_unit(g, s["home_team"]))
        if mode == "stream":
            h, a = g["teams"].get(s["home_team"]), g["teams"].get(s["away_team"])
            stream_list.append({
                "date": g["date"], "home_team": s["home_team"], "away_team": s["away_team"],
                "home_score": h["pts"] if h else (a["opp_pts"] if a else None),
                "away_score": a["pts"] if a else (h["opp_pts"] if h else None),
                "streamer": s.get("streamer") or None,
                "youtube_url": s.get("youtube_url") or None})
    return {
        "games": counts,
        "team_games": {m: len(units[m]["team"]) for m in MODES},
        "unmatched": unmatched,
        "metrics": _metrics(units),
        "stream_games": stream_list,
        "thresholds": {"clear": CLEAR_T, "likely": LIKELY_T},
    }


def _seasons_with_streams() -> list[str]:
    return sorted((s for s in _known_seasons()
                   if any(g.get("stream") for g in _load(s)["games"])), reverse=True)


_CACHE: dict = {}


@router.get("/api/stream-vs-sim")
def stream_vs_sim(season: str | None = Query(None)):
    seasons = _seasons_with_streams()
    season = _resolve_season(season or (seasons[0] if seasons else None))
    path = allstats_path(season, "REG")
    key = tuple(p.stat().st_mtime_ns if p.exists() else None for p in (path, _schedule_path(season)))
    hit = _CACHE.get(season)
    if hit and hit[0] == key:
        body = hit[1]
    else:
        rows = read_csv(path)[1] if path.exists() else []
        body = compare(rows, _load(season)["games"])
        _CACHE[season] = (key, body)
    return {"season": season, "seasons": seasons, **body}

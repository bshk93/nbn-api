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

Possessions are the usual estimate, FGA − OREB + TOV + 0.44·FTA, averaged over
the two teams, as in `stats_build/advanced.py`.

    venv/bin/python -m tests.test_stream_sim
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from fastapi import APIRouter, Query

from stats_build.pipeline import PLAYER_FIXES, player_slug

from .boxscores import allstats_path
from .schedule import _known_seasons, _load, _resolve_season, _schedule_path
from .storage import read_csv

router = APIRouter()

TEAM_STATS = ("P", "OR", "DR", "R", "A", "S", "B", "TO", "PF",
              "FGM", "FGA", "3PM", "3PA", "FTM", "FTA", "M")
MODES = ("stream", "sim")
CLOSE, BLOWOUT = 5, 20
# Who makes the player table: games in both modes, and enough minutes in each
# that a line means something.
MIN_MPG = 10.0


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _games_from_rows(rows: Iterable[dict]) -> dict:
    """(date, frozenset(teams)) -> {teams: {abbr: {home, pts, opp_pts, totals, players}}}."""
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
        name = (r.get("PLAYER") or "").strip()
        name = PLAYER_FIXES.get(name.upper(), name)
        t["players"].append({"player": name, "slug": player_slug(name), **line})
    return games


def _poss(t: dict) -> float:
    return t["FGA"] - t["OR"] + t["TO"] + 0.44 * t["FTA"]


def _summary(games: list[dict]) -> dict:
    """Per team-game averages and game-shape rates for one mode."""
    n = len(games)
    tg = [t for g in games for t in g["teams"].values()]
    out = {"games": n, "team_games": len(tg)}
    if not tg:
        return out
    tot = {k: sum(t["tot"][k] for t in tg) for k in TEAM_STATS}
    pts = sum(t["pts"] for t in tg)
    poss = sum(_poss(t["tot"]) for t in tg)
    oreb_chances = 0
    for g in games:
        sides = list(g["teams"].values())
        if len(sides) == 2:
            a, b = sides
            oreb_chances += a["tot"]["OR"] + b["tot"]["DR"] + b["tot"]["OR"] + a["tot"]["DR"]
    margins = [abs(g["teams_list"][0]["pts"] - g["teams_list"][0]["opp_pts"]) for g in games]
    home_wins = sum(1 for g in games if g["home_won"] is True)
    home_known = sum(1 for g in games if g["home_won"] is not None)
    # 240 player-minutes a side is regulation; anything over is overtime.
    ot = sum(1 for g in games if any(t["tot"]["M"] > 245 for t in g["teams_list"]))
    top_min = [max((p["M"] for p in t["players"]), default=0) for t in tg]
    used = [sum(1 for p in t["players"] if p["M"] > 0) for t in tg]
    top_pts_share = [max((p["P"] for p in t["players"]), default=0) / t["pts"]
                     for t in tg if t["pts"]]
    k = len(tg)
    out.update({
        "pts": pts / k,
        "pace": poss / k,
        "ortg": 100 * pts / poss if poss else None,
        "fg_pct": tot["FGM"] / tot["FGA"] if tot["FGA"] else None,
        "tp_pct": tot["3PM"] / tot["3PA"] if tot["3PA"] else None,
        "ft_pct": tot["FTM"] / tot["FTA"] if tot["FTA"] else None,
        "ts_pct": pts / (2 * (tot["FGA"] + 0.44 * tot["FTA"])) if tot["FGA"] else None,
        "tpa_rate": tot["3PA"] / tot["FGA"] if tot["FGA"] else None,
        "fta_rate": tot["FTA"] / tot["FGA"] if tot["FGA"] else None,
        "tov_pct": 100 * tot["TO"] / poss if poss else None,
        "oreb_pct": tot["OR"] / oreb_chances if oreb_chances else None,
        "ast_rate": tot["A"] / tot["FGM"] if tot["FGM"] else None,
        "fga": tot["FGA"] / k, "tpa": tot["3PA"] / k, "fta": tot["FTA"] / k,
        "reb": tot["R"] / k, "oreb": tot["OR"] / k, "ast": tot["A"] / k,
        "stl": tot["S"] / k, "blk": tot["B"] / k, "tov": tot["TO"] / k, "pf": tot["PF"] / k,
        "margin": sum(margins) / n if n else None,
        "close_pct": sum(1 for m in margins if m <= CLOSE) / n if n else None,
        "blowout_pct": sum(1 for m in margins if m >= BLOWOUT) / n if n else None,
        "ot_pct": ot / n if n else None,
        "home_win_pct": home_wins / home_known if home_known else None,
        "top_min": sum(top_min) / k,
        "players_used": sum(used) / k,
        "top_pts_share": sum(top_pts_share) / len(top_pts_share) if top_pts_share else None,
    })
    return out


def _players(by_mode: dict[str, list[dict]]) -> list[dict]:
    """Players with real minutes in both modes, their per-game line in each."""
    acc: dict = defaultdict(lambda: {m: None for m in MODES})
    names, teams = {}, defaultdict(set)
    for mode, games in by_mode.items():
        for g in games:
            for abbr, t in g["teams"].items():
                for p in t["players"]:
                    if p["M"] <= 0:
                        continue
                    a = acc[p["slug"]][mode]
                    if a is None:
                        a = acc[p["slug"]][mode] = {"G": 0, **{k: 0 for k in TEAM_STATS}}
                    a["G"] += 1
                    for k in TEAM_STATS:
                        a[k] += p[k]
                    names[p["slug"]] = p["player"]
                    teams[p["slug"]].add(abbr)
    out = []
    for slug, modes in acc.items():
        if any(modes[m] is None for m in MODES):
            continue
        row = {"slug": slug, "player": names[slug], "teams": sorted(teams[slug])}
        ok = True
        for m in MODES:
            a, gp = modes[m], modes[m]["G"]
            if a["M"] / gp < MIN_MPG:
                ok = False
            tsa = 2 * (a["FGA"] + 0.44 * a["FTA"])
            row[m] = {"g": gp, "min": a["M"] / gp, "pts": a["P"] / gp, "reb": a["R"] / gp,
                      "ast": a["A"] / gp, "stl": a["S"] / gp, "blk": a["B"] / gp,
                      "tov": a["TO"] / gp, "fga": a["FGA"] / gp, "tpa": a["3PA"] / gp,
                      "ts_pct": a["P"] / tsa if tsa else None,
                      "pts36": 36 * a["P"] / a["M"] if a["M"] else None}
        if ok:
            out.append(row)
    out.sort(key=lambda r: r["stream"]["pts"] - r["sim"]["pts"], reverse=True)
    return out


def compare(rows: Iterable[dict], schedule_games: list[dict]) -> dict:
    """Split a season's regular-season box scores by the schedule's stream flag."""
    flagged = {(g["date"], frozenset((g["home_team"], g["away_team"]))): g
               for g in schedule_games}
    by_mode: dict[str, list[dict]] = {m: [] for m in MODES}
    unmatched = 0
    stream_list = []
    for key, g in sorted(_games_from_rows(rows).items(), key=lambda kv: kv[0][0]):
        sched = flagged.get(key)
        if sched is None:
            unmatched += 1
            continue
        g["teams_list"] = list(g["teams"].values())
        home = sched["home_team"]
        h = g["teams"].get(home)
        g["home_won"] = (h["pts"] > h["opp_pts"]) if h else None
        mode = "stream" if sched.get("stream") else "sim"
        by_mode[mode].append(g)
        if mode == "stream":
            away = sched["away_team"]
            hp = h["pts"] if h else g["teams_list"][0]["opp_pts"]
            a = g["teams"].get(away)
            ap = a["pts"] if a else (h["opp_pts"] if h else None)
            stream_list.append({"date": g["date"], "home_team": home, "away_team": away,
                                "home_score": hp, "away_score": ap,
                                "streamer": sched.get("streamer") or None,
                                "youtube_url": sched.get("youtube_url") or None})
    return {
        "summary": {m: _summary(by_mode[m]) for m in MODES},
        "unmatched": unmatched,
        "players": _players(by_mode),
        "stream_games": stream_list,
        "min_mpg": MIN_MPG,
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
    sched_path = _schedule_path(season)
    key = tuple(p.stat().st_mtime_ns if p.exists() else None for p in (path, sched_path))
    hit = _CACHE.get(season)
    if hit and hit[0] == key:
        body = hit[1]
    else:
        rows = read_csv(path)[1] if path.exists() else []
        body = compare(rows, _load(season)["games"])
        _CACHE[season] = (key, body)
    return {"season": season, "seasons": seasons, **body}

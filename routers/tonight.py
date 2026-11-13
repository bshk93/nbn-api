"""`GET /api/tonight` — the homepage's "Tonight" graphic, and the 11am Discord card.

One answer for one game day: every game, a headline player on each side, the
night's featured game, a few storylines per game, and the previous night's
three best lines. The homepage poster and carousel (nbn-today/tonight-hero.js)
draw it; `routers/tonight_card.py` renders the same answer as a PNG and
`post_tonight.py` posts that to #leaguetalk at 11:00 ET. Both read this, so the
page and the Discord card can never disagree on who is featured or why.

The rules, each a pure function below so the tests can pin them:

* **Featured game** (`pick_featured`): a stream game first — the stream is
  the night's marquee, the one people will actually watch — the earliest tip
  if there are several. Otherwise the best sum of both teams' win% plus
  playoff stakes (`1 - |odds - 0.5| * 2` per team, so two bubble teams beat a
  lock and a lottery team). Ties go to the earlier tip, so the pick is the
  same every time it's asked that day.
* **Star** (`hot_hand`): each team's best mean Game Score over its last
  `HOT_GAMES` regular-season games this season, for players on its roster
  today, counting only games played *for this team* (a player just traded in
  is judged on his new team, or not at all yet). `HOT_MIN_GAMES` played is the
  floor; below it, the star is the top OVR in the team's starting five
  (`home._five`, the five the homepage slate shows).
* **Storylines** (`storylines`): the stream, the season series, streaks of
  `STREAK_MIN`+, the stars' last-5 lines, and playoff odds between 25% and
  75%. Career milestones are added on the page from milestones.js, which is
  the one home of that logic.
* **Line** (each game's `line`): `playoff_odds.game_line` on the stream
  planner's `ratings_now()` — the odds run's rating less each team's Out
  players — so the homepage slate quotes the same spread the planner scores
  closeness on. `spread` is to the half point, `fav` None for a pick'em.
* **Last night** (`last_night`): the newest date before this one with box
  scores, its three best lines by Game Score — the build's formula, the one
  `/stats/highs` ranks by.

Every number comes from the box score index (`game_highs.get_index`), the
schedule, the rosters, `playoff-odds.json` and the bios. Nothing is stored.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

from fastapi import APIRouter, HTTPException, Query

import season_clock

from . import game_highs as gh
from .constants import DATA_DIR, VALID_TEAMS
from .names import player_name
from .storage import read_csv

router = APIRouter()

HOT_GAMES = 5          # a star's recent form: his last this many games
HOT_MIN_GAMES = 3      # fewer than this for the team, and OVR picks instead
STREAK_MIN = 3         # a win or loss streak worth a line
ODDS_LO, ODDS_HI = 0.25, 0.75   # playoff odds this uncertain are a story
MAX_STORIES = 3
LAST_NIGHT_LOOKBACK = 7          # days back to look for the latest box scores

SITE_DIR = Path(os.environ.get("NBN_SITE_DIR", "/var/www/nbn.today"))
TEAM_COLORS_FILE = SITE_DIR / "build" / "team-colors.json"


# ── Small helpers ────────────────────────────────────────────────────────────

def display_name(raw: str) -> str:
    """`"BARRETT, RJ"` -> `"RJ Barrett"`, the way every page writes it
    (`names.player_name`, the mirror of names.js)."""
    return player_name(raw)


def last_name(display: str) -> str:
    parts = display.split(" ")
    suffix = {"Jr.", "Sr.", "II", "III", "IV"}
    while len(parts) > 2 and parts[-1] in suffix:
        parts.pop()
    return " ".join(parts[1:]) if len(parts) > 1 else display


def _rec(w: int, l: int) -> str:
    return f"{w}–{l}"


def _short_date(d: str) -> str:
    y, m, dd = d.split("-")
    return f"{int(m)}/{int(dd)}"


# ── Results, records, streaks, series ────────────────────────────────────────

def season_results(index: gh.GameIndex, season: str, before: Optional[str] = None) -> list[dict]:
    """Regular-season results, oldest first, optionally only before a date."""
    from .playoff_odds import season_results as _sr
    rows = _sr(index, season)
    if before:
        rows = [g for g in rows if g["date"] < before]
    return sorted(rows, key=lambda g: g["date"])


def records(results: Iterable[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for g in results:
        hw = g["home_pts"] > g["away_pts"]
        out.setdefault(g["home"], [0, 0])[0 if hw else 1] += 1
        out.setdefault(g["away"], [0, 0])[1 if hw else 0] += 1
    return out


def streak(results: list[dict], team: str) -> tuple[str, int]:
    """("W"|"L", length) of the team's current run, ("", 0) with no games."""
    kind, n = "", 0
    for g in reversed(results):
        if team not in (g["home"], g["away"]):
            continue
        won = (g["home_pts"] > g["away_pts"]) == (g["home"] == team)
        k = "W" if won else "L"
        if not kind:
            kind = k
        if k != kind:
            break
        n += 1
    return kind, n


def series(results: list[dict], a: str, b: str) -> tuple[int, int]:
    """(a's wins, b's wins) against each other this season."""
    wa = wb = 0
    for g in results:
        if {g["home"], g["away"]} != {a, b}:
            continue
        winner = g["home"] if g["home_pts"] > g["away_pts"] else g["away"]
        if winner == a:
            wa += 1
        else:
            wb += 1
    return wa, wb


# ── Stars ────────────────────────────────────────────────────────────────────

def hot_hand(index: gh.GameIndex, season: str, team: str, roster: set[str],
             before: str, n: int = HOT_GAMES, min_games: int = HOT_MIN_GAMES,
             resolve: Callable[[str, str], str] = lambda name, slug: slug) -> Optional[dict]:
    """The roster player with the best mean Game Score over his last `n`
    games for `team` this season, before `before`. None if nobody has
    `min_games`. `resolve(raw_name, index_slug)` maps a box score line to the
    bio slug the roster uses."""
    s, m, c = index.strings.values, index.meta, index.cols
    sid, tid = index.strings.ids.get(season), index.strings.ids.get(team)
    if sid is None or tid is None:
        return None
    games: dict[str, list[int]] = {}
    for i in range(index.n):
        if index.playoff[i] or m["season"][i] != sid or m["team"][i] != tid:
            continue
        if s[m["date"][i]] >= before:
            continue
        slug = resolve(s[m["player"][i]], s[m["slug"][i]])
        if slug in roster:
            games.setdefault(slug, []).append(i)
    best = None
    for slug, rows in games.items():
        if len(rows) < min_games:
            continue
        rows = sorted(rows, key=lambda i: s[m["date"][i]])[-n:]
        k = len(rows)
        gmsc = sum(index.values["GMSC"][i] for i in rows) / k
        line = {
            "slug": slug,
            "raw": s[m["player"][rows[-1]]],
            "basis": "hot",
            "last5": {"gp": k,
                      "pts": round(sum(c["P"][i] for i in rows) / k, 1),
                      "reb": round(sum(c["R"][i] for i in rows) / k, 1),
                      "ast": round(sum(c["A"][i] for i in rows) / k, 1),
                      "gmsc": round(gmsc, 1)},
        }
        # Best Game Score; a tie goes to the slug, so the pick is stable.
        if best is None or (gmsc, slug) > (best["last5"]["gmsc"], best["slug"]):
            best = line
    return best


def ovr_star(five: dict) -> Optional[dict]:
    players = [p for p in (five or {}).get("players") or [] if p and p.get("ovr") is not None]
    if not players:
        return None
    p = max(players, key=lambda p: (p["ovr"], p["slug"]))
    return {"slug": p["slug"], "raw": p.get("name") or p["slug"], "basis": "ovr", "last5": None}


# ── Featured game ────────────────────────────────────────────────────────────

def _time_key(g: dict) -> tuple:
    from .schedule import _time_key as tk
    return tk(g.get("time_et", ""))


def pick_featured(games: list[dict], recs: dict[str, list[int]],
                  odds: dict[str, float]) -> Optional[dict]:
    if not games:
        return None
    ordered = sorted(games, key=lambda g: (_time_key(g), g["home_team"]))
    streams = [g for g in ordered if g.get("stream")]
    if streams:
        return streams[0]

    def pct(t):
        w, l = recs.get(t, [0, 0])
        return w / (w + l) if w + l else 0.5

    def stakes(t):
        return 1 - abs(odds[t] - 0.5) * 2 if t in odds else 0.0

    def score(g):
        return sum(pct(t) + stakes(t) for t in (g["away_team"], g["home_team"]))

    # max() keeps the first of equals, and `ordered` is by tip time.
    return max(ordered, key=score)


# ── Storylines ───────────────────────────────────────────────────────────────

def storylines(g: dict, results: list[dict], odds: dict[str, float],
               stars: dict[str, Optional[dict]]) -> list[str]:
    a, h = g["away_team"], g["home_team"]
    out: list[str] = []
    if g.get("stream"):
        out.append("Live on Twitch" + (f", streamed by {g['streamer']}" if g.get("streamer") else ""))
    wa, wh = series(results, a, h)
    if wa or wh:
        if wa == wh:
            out.append(f"Season series tied {wa}–{wh}")
        else:
            lead, trail = (a, h) if wa > wh else (h, a)
            out.append(f"{lead} lead the season series {max(wa, wh)}–{min(wa, wh)}")
    for t in (a, h):
        kind, n = streak(results, t)
        if n >= STREAK_MIN:
            out.append(f"{t} have {'won' if kind == 'W' else 'lost'} {n} straight")
    for t in (a, h):
        s = stars.get(t)
        if s and s.get("last5"):
            l5 = s["last5"]
            out.append(f"{last_name(s['name'])}: {l5['pts']:.1f} PPG over his last {l5['gp']}")
    for t in (a, h):
        if t in odds and ODDS_LO <= odds[t] <= ODDS_HI:
            out.append(f"{t} playoff odds: {round(odds[t] * 100)}%")
    return out


# ── Last night ───────────────────────────────────────────────────────────────

def last_night(index: gh.GameIndex, before: str, resolve: Callable[[str, str], str],
               lookback: int = LAST_NIGHT_LOOKBACK, top: int = 3) -> Optional[dict]:
    s, m = index.strings.values, index.meta
    d = _date.fromisoformat(before)
    for back in range(1, lookback + 1):
        day = (d - timedelta(days=back)).isoformat()
        did = index.strings.ids.get(day)
        if did is None:
            continue
        rows = [i for i in range(index.n) if m["date"][i] == did and not index.playoff[i]] or \
               [i for i in range(index.n) if m["date"][i] == did]
        if not rows:
            continue
        rows.sort(key=lambda i: (-index.values["GMSC"][i], s[m["player"][i]]))
        lines = []
        for i in rows[:top]:
            ln = index.line(i)
            name = display_name(ln["player"])
            lines.append({
                "slug": resolve(ln["player"], ln["slug"]), "name": name, "team": ln["team"],
                "opp": ln["opp"], "home": ln["home"], "wl": ln["wl"],
                "score": f"{ln['team_pts']}–{ln['opp_pts']}",
                "pts": ln["line"]["P"], "reb": ln["line"]["R"], "ast": ln["line"]["A"],
                "stl": ln["line"]["S"], "blk": ln["line"]["B"], "min": ln["line"]["M"],
                "gmsc": ln["line"]["GMSC"],
            })
        return {"date": day, "top": lines}
    return None


# ── Assembly ─────────────────────────────────────────────────────────────────

def _team_colors() -> dict:
    try:
        data = json.loads(TEAM_COLORS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _rosters() -> dict[str, set[str]]:
    out = {}
    for t in VALID_TEAMS:
        path = DATA_DIR / f"{t.lower()}-roster.csv"
        if path.exists():
            _, rows = read_csv(path)
            out[t] = {(r.get("SLUG") or "").strip() for r in rows} - {""}
        else:
            out[t] = set()
    return out


def _odds() -> dict[str, float]:
    from .playoff_odds import CURRENT_FILE
    from .storage import _load_json
    run = _load_json(CURRENT_FILE, None) or {}
    return {r["team"]: float(r["playoffs"]) for r in run.get("teams", []) if "playoffs" in r}


def _star_out(star: Optional[dict], team: str, bios: dict, ovr: dict) -> Optional[dict]:
    if not star:
        return None
    bio = bios.get(star["slug"]) or {}
    raw = bio.get("name") or star["raw"]
    hist = ovr.get(star["slug"]) or []
    return {
        "slug": star["slug"], "name": display_name(raw), "raw": raw, "team": team,
        "headshot": f"/api/players/{star['slug']}/headshot",
        "basis": star["basis"], "last5": star["last5"],
        "ovr": hist[-1]["ovr"] if hist else None,
    }


def games_on(season: str, day: str) -> list[dict]:
    from .schedule import _load, _public
    games = [_public(g) for g in _load(season).get("games", []) if g.get("date") == day]
    return sorted(games, key=lambda g: (_time_key(g), g["home_team"]))


def next_slate(season: str, after: str) -> Optional[dict]:
    from .schedule import _load
    dates = sorted({g["date"] for g in _load(season).get("games", []) if g.get("date", "") > after})
    if not dates:
        return None
    n = sum(1 for g in _load(season).get("games", []) if g.get("date") == dates[0])
    return {"date": dates[0], "count": n}


def build(day: str) -> dict:
    from .boxscores import _bios_by_name
    from .home import _five
    from .players import ovr_shared, player_bios_shared

    season = season_clock.season_for_date(day)
    index, _ = gh.get_index()
    bios, ovr = player_bios_shared(), ovr_shared()
    by_name = _bios_by_name(bios)

    def resolve(raw: str, slug: str) -> str:
        if slug in bios:
            return slug
        hit = by_name.get((raw or "").strip().upper())
        return hit[0] if hit else slug

    results = season_results(index, season, before=day)
    recs = records(results)
    odds = _odds()
    rosters = _rosters()
    colors = _team_colors()
    games = games_on(season, day)

    stars: dict[str, Optional[dict]] = {}
    for t in {t for g in games for t in (g["away_team"], g["home_team"])}:
        s = hot_hand(index, season, t, rosters.get(t, set()), day, resolve=resolve)
        if s is None:
            try:
                s = ovr_star(_five(t, bios, ovr))
            except Exception:  # noqa: BLE001 — no five is no star, not an error page
                s = None
        stars[t] = _star_out(s, t, bios, ovr)

    feat = pick_featured(games, recs, odds)
    try:
        from .playoff_odds import game_line
        from .stream_planner import ratings_now
        ratings = ratings_now() if games else {}
    except Exception:  # noqa: BLE001 — no ratings is no line, not an error page
        ratings = {}
    out_games = []
    for g in games:
        a, h = g["away_team"], g["home_team"]
        out_games.append({
            "id": g.get("id"), "date": g["date"], "away": a, "home": h,
            "time_et": g.get("time_et") or "", "stream": g["stream"], "streamer": g["streamer"],
            # An NBA Cup group game: the schedule is the NBA's, and tags them.
            "cup": g.get("note") == "NBA Cup",
            "youtube_url": g.get("youtube_url"),
            "records": {"away": _rec(*recs.get(a, [0, 0])), "home": _rec(*recs.get(h, [0, 0]))},
            "odds": ({"away": odds.get(a), "home": odds.get(h)} if odds else None),
            "line": game_line(h, a, ratings) if ratings else None,
            "stars": {"away": stars.get(a), "home": stars.get(h)},
            "storylines": storylines(g, results, odds, stars)[:MAX_STORIES]
                          if g is not feat else storylines(g, results, odds, stars),
            "featured": g is feat,
        })

    theme = None
    if feat:
        a, h = feat["away_team"], feat["home_team"]
        theme = {"away": colors.get(a), "home": colors.get(h)}

    return {
        "date": day,
        "season": season,
        "count": len(games),
        "featured": feat.get("id") if feat else None,
        "theme": theme,
        "games": out_games,
        "last_night": last_night(index, day, resolve),
        "next": None if games else next_slate(season, day),
    }


_cache: dict[tuple, dict] = {}
_lock = threading.Lock()


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def _cache_key(day: str) -> tuple:
    from .playoff_odds import CURRENT_FILE
    from .schedule import _schedule_path
    season = season_clock.season_for_date(day)
    rosters = tuple(_mtime(DATA_DIR / f"{t.lower()}-roster.csv") for t in sorted(VALID_TEAMS))
    return (day, gh._files_key(), _mtime(_schedule_path(season)), rosters,
            _mtime(CURRENT_FILE), _mtime(DATA_DIR / "player-bios.json"),
            _mtime(DATA_DIR / "ovr-history.json"), _mtime(TEAM_COLORS_FILE),
            _mtime(DATA_DIR / "injuries.json"))


def tonight(day: Optional[str] = None) -> dict:
    """The answer for `day` (default: today's game day), cached until an
    input changes."""
    if not day:
        from .game_day import game_day
        try:
            day = game_day()
        except Exception:  # noqa: BLE001 — Twitch unreachable: the ET date is right
            from .league_time import league_today_str
            day = league_today_str()
    key = _cache_key(day)
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    data = build(day)
    with _lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = data
    return data


def _check_date(d: str) -> str:
    try:
        return _date.fromisoformat(d).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


@router.get("/api/tonight")
def get_tonight(date: str = Query(default="")):
    return tonight(_check_date(date) if date else None)

"""IRL feed — real-world game logs for the players on a fantasy roster.

Kept apart from NBN's own stats on purpose: its own data files, its own
routes, its own tab (the IRL tab on `/nbnfl`, drawn by `nbn-today/irl-feed.js`).
Nothing here reads or writes a box score, a roster CSV or a bio, and nothing in
the stats build reads this. The rosters are hand-kept in `irl-rosters.json`
rather than derived — the NBNFL has no roster model on the site at all.

Source: ESPN's public JSON (site.api.espn.com, site.web.api.espn.com). It is
unofficial: no key, no SLA. stats.nba.com and cdn.nba.com both 403 this
server's IP (checked 2026-10-03), so ESPN is the free source that works from
here. If it changes shape or blocks us, this page goes stale and nothing else
notices.

irl-rosters.json (hand-kept):
    {"nfl": {"CIN": {"league": "NBNFL", "label": "Bengals",
                     "players": [{"name": "Ja'Marr Chase", "espn_id": "4362628"}]}}}
`name` is a reminder for whoever edits the file; the page shows ESPN's name.
Matching a name to an id is done once, by hand, when a player is added — so a
second "Elijah Jones" can never be picked up by accident later.

irl/{sport}/{espn_id}.json (fetch_irl.py, one per player):
    {"espn_id", "sport", "fetched_at", "bio": {...},
     "seasons": {"2026": {"year", "label", "closed", "columns", "games", "totals"}}}
A season that was already over when it was fetched (`closed`) is kept as is;
the current season and the bio are refetched every run.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from .constants import IRL_DIR, IRL_ROSTERS_FILE, logger
from .storage import _atomic_write, _load_json

router = APIRouter()

# `seasons`: how many ESPN seasons to keep, current one included.
# `max_games`: the newest N games the endpoint returns across them (None = all).
SPORTS = {
    "nfl": {"espn": "football/nfl", "label": "NFL", "seasons": 2, "max_games": None},
    "nba": {"espn": "basketball/nba", "label": "NBA", "seasons": 2, "max_games": 50},
}

SITE_API = "https://site.api.espn.com/apis/site/v2/sports"
COMMON_API = "https://site.web.api.espn.com/apis/common/v3/sports"
# A plain product token. ESPN's WAF 403s a UA carrying a URL ("(+https://…)").
USER_AGENT = "nbn.today-irl/1.0"
REQUEST_GAP_SECONDS = 0.2


def season_label(sport: str, year: int) -> str:
    """ESPN names a basketball season by the year it ends (2026 = 2025-26)."""
    if sport == "nba":
        return f"{year - 1}-{year % 100:02d}"
    return str(year)


def _season_type(display_name: str):
    """'2025 Regular Season' → 'reg', '… Postseason' → 'post', '… Play In …' →
    'playin', preseason → None (not kept)."""
    s = display_name.lower()
    if "preseason" in s:
        return None
    if "play in" in s or "play-in" in s:
        return "playin"
    if "postseason" in s:
        return "post"
    return "reg"


_POST_NOTES = ("playoff", "wild card", "divisional", "conference", "super bowl",
               "finals", "semifinals", "quarterfinals")


def _type_from_note(note: str):
    """Season type of a game that has no stat line (an offensive lineman's,
    say). ESPN still lists the game, but only under `events`, which carries no
    season type — the note is the one hint, and it is set on postseason games."""
    s = (note or "").lower()
    if "preseason" in s:
        return None
    if "play-in" in s or "play in" in s:
        return "playin"
    if any(w in s for w in _POST_NOTES):
        return "post"
    return "reg"


def _abbr(team) -> str:
    return (team or {}).get("abbreviation") or ""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_gamelog(raw: dict, sport: str, year: int, closed: bool) -> dict:
    """ESPN's athlete game log → one season: its columns and every game,
    newest first. Stat values stay ESPN's strings ("15-22", "74.4")."""
    names = raw.get("names") or []
    labels = [str(x).strip() for x in raw.get("labels") or []]
    groups: list = []
    for c in raw.get("categories") or []:
        groups += [c.get("displayName") or c.get("name")] * int(c.get("count") or 0)
    columns = [{"key": k,
                "label": labels[i] if i < len(labels) else k,
                "group": groups[i] if i < len(groups) else None}
               for i, k in enumerate(names)]

    stats_by_event: dict = {}
    type_by_event: dict = {}
    totals = []
    for st in raw.get("seasonTypes") or []:
        stype = _season_type(st.get("displayName") or "")
        if stype is None:
            continue
        # Basketball splits a season type into one category per month or
        # playoff round; football has one. Either way each `event` category
        # is a list of games.
        for cat in st.get("categories") or []:
            if cat.get("type") != "event":
                continue
            for e in cat.get("events") or []:
                eid = str(e.get("eventId"))
                type_by_event[eid] = stype
                stats_by_event[eid] = dict(zip(names, e.get("stats") or []))
        for row in (st.get("summary") or {}).get("stats") or []:
            if row.get("stats"):
                totals.append({"type": stype, "label": row.get("displayName") or "",
                               "stats": dict(zip(names, row["stats"]))})

    games = []
    for eid, ev in (raw.get("events") or {}).items():
        stype = type_by_event.get(eid) or _type_from_note(ev.get("eventNote"))
        if stype is None:
            continue
        team_id = str((ev.get("team") or {}).get("id") or "")
        home, away = _int(ev.get("homeTeamScore")), _int(ev.get("awayTeamScore"))
        is_home = team_id and team_id == str(ev.get("homeTeamId"))
        games.append({
            "id": eid,
            "date": ev.get("gameDate"),
            "week": ev.get("week"),
            "type": stype,
            "at_vs": ev.get("atVs"),
            "team": _abbr(ev.get("team")),
            "opp": _abbr(ev.get("opponent")),
            "result": ev.get("gameResult"),
            "team_score": home if is_home else away,
            "opp_score": away if is_home else home,
            "note": ev.get("eventNote"),
            "stats": stats_by_event.get(eid, {}),
        })
    games.sort(key=lambda g: g["date"] or "", reverse=True)
    return {"year": year, "label": season_label(sport, year), "closed": closed,
            "columns": columns, "games": games, "totals": totals}


def normalize_bio(raw: dict) -> dict:
    a = raw.get("athlete") or {}
    injury = None
    injuries = a.get("injuries") or []
    if injuries:
        i = injuries[0]
        details = i.get("details") or {}
        injury = {
            "status": i.get("status"),
            "abbr": (i.get("type") or {}).get("abbreviation")
                    or (details.get("fantasyStatus") or {}).get("abbreviation"),
            # "Knee - PCL — Surgery"; ESPN often repeats one in the other
            # ("Concussion" / "Concussion"), and "Not Specified" says nothing.
            "detail": " — ".join(dict.fromkeys(
                x for x in (details.get("type"), details.get("detail"))
                if x and x != "Not Specified")) or None,
            "return_date": details.get("returnDate"),
            "comment": i.get("shortComment"),
            "date": i.get("date"),
        }
    team = a.get("team") or {}
    return {
        "name": a.get("displayName"),
        "pos": (a.get("position") or {}).get("abbreviation"),
        "team": team.get("abbreviation"),
        "team_name": team.get("displayName"),
        "jersey": a.get("jersey"),
        "headshot": (a.get("headshot") or {}).get("href"),
        "age": a.get("age"),
        "experience": a.get("displayExperience"),
        "status": (a.get("status") or {}).get("name"),
        "injury": injury,
    }


# ── fetching (fetch_irl.py, on a timer) ─────────────────────────────────────

def player_path(sport: str, espn_id: str) -> Path:
    return IRL_DIR / sport / f"{espn_id}.json"


def load_rosters() -> dict:
    return _load_json(IRL_ROSTERS_FILE, {})


def _get(client: httpx.Client, url: str, **params) -> dict:
    r = client.get(url, params=params or None)
    r.raise_for_status()
    time.sleep(REQUEST_GAP_SECONDS)
    return r.json()


def current_season(client: httpx.Client, sport: str) -> int:
    data = _get(client, f"{SITE_API}/{SPORTS[sport]['espn']}/scoreboard")
    return int(data["leagues"][0]["season"]["year"])


def fetch_player(client: httpx.Client, sport: str, espn_id: str, current: int,
                 force: bool = False) -> dict:
    """Refresh one player's cache file and return what was written."""
    cfg = SPORTS[sport]
    path = player_path(sport, espn_id)
    old = _load_json(path, {}).get("seasons") or {}
    seasons = {}
    for year in range(current, current - cfg["seasons"], -1):
        prev = old.get(str(year))
        if prev and prev.get("closed") and not force:
            seasons[str(year)] = prev
            continue
        url = f"{COMMON_API}/{cfg['espn']}/athletes/{espn_id}/gamelog"
        try:
            raw = _get(client, url, season=year)
        except httpx.HTTPStatusError as e:
            # A rookie has no log for last season. Anything else is a real
            # failure: keep what we had rather than blank it.
            if e.response.status_code != 404:
                raise
            raw = {}
        seasons[str(year)] = normalize_gamelog(raw, sport, year, closed=year < current)
    bio = normalize_bio(_get(client, f"{COMMON_API}/{cfg['espn']}/athletes/{espn_id}"))
    out = {"espn_id": espn_id, "sport": sport,
           "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "bio": bio, "seasons": seasons}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(out, separators=(",", ":")))
    return out


def fetch_all(sports=None, force: bool = False) -> dict:
    """Refresh every player on every roster. One player failing is logged and
    skipped, so a single bad id can't stall the rest."""
    rosters = load_rosters()
    summary = {}
    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        for sport, by_key in rosters.items():
            if sport not in SPORTS or (sports and sport not in sports):
                continue
            ids = []
            for r in by_key.values():
                ids += [str(p["espn_id"]) for p in r.get("players", []) if p.get("espn_id")]
            ids = list(dict.fromkeys(ids))
            current = current_season(client, sport)
            ok, failed = 0, []
            for espn_id in ids:
                try:
                    fetch_player(client, sport, espn_id, current, force=force)
                    ok += 1
                except Exception as e:  # noqa: BLE001 — one bad player, not the run
                    logger.warning("irl: %s %s failed: %s", sport, espn_id, e)
                    failed.append(espn_id)
            summary[sport] = {"season": current, "players": len(ids), "ok": ok, "failed": failed}
    return summary


# ── reads ───────────────────────────────────────────────────────────────────

def _trim(seasons: list, max_games) -> list:
    """Newest `max_games` games across seasons (already newest first)."""
    if max_games is None:
        return seasons
    left, out = max_games, []
    for s in seasons:
        if left <= 0:
            break
        games = s["games"][:left]
        left -= len(games)
        out.append({**s, "games": games})
    return out


@router.get("/api/irl/rosters")
def list_rosters():
    out = {}
    for sport, by_key in load_rosters().items():
        if sport not in SPORTS:
            continue
        out[sport] = {
            "label": SPORTS[sport]["label"],
            "rosters": [{"key": key, "league": r.get("league"), "label": r.get("label"),
                         "count": len(r.get("players", []))}
                        for key, r in by_key.items()],
        }
    return out


@router.get("/api/irl/{sport}/{roster}")
def get_roster(sport: str, roster: str):
    sport = sport.lower()
    by_key = load_rosters().get(sport)
    if sport not in SPORTS or not by_key:
        raise HTTPException(status_code=404, detail=f"No IRL rosters for {sport!r}")
    key = next((k for k in by_key if k.upper() == roster.upper()), None)
    if key is None:
        raise HTTPException(status_code=404, detail=f"No {sport} roster {roster!r}")
    r = by_key[key]
    players, fetched = [], []
    for p in r.get("players", []):
        espn_id = str(p.get("espn_id") or "")
        cached = _load_json(player_path(sport, espn_id), {}) if espn_id else {}
        seasons = sorted((cached.get("seasons") or {}).values(), key=lambda s: s["year"], reverse=True)
        if cached.get("fetched_at"):
            fetched.append(cached["fetched_at"])
        # A lineman or long snapper has games but never a stat column. The page
        # leaves them out; the roster file keeps them.
        has_stats = any(s.get("columns") for s in seasons)
        players.append({"espn_id": espn_id, "name": p.get("name"), "bio": cached.get("bio"),
                        "fetched_at": cached.get("fetched_at"), "has_stats": has_stats,
                        "seasons": _trim(seasons, SPORTS[sport]["max_games"])})
    return {"sport": sport, "roster": key, "league": r.get("league"), "label": r.get("label"),
            "max_games": SPORTS[sport]["max_games"],
            "updated_at": min(fetched) if fetched else None,
            "players": players}

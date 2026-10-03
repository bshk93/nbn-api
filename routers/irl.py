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

Rosters come from one of two places, per sport (`SPORTS[...]["rosters"]`):

- "file" (NFL): hand-kept in irl-rosters.json, since the NBNFL has no roster
  model on the site.
      {"nfl": {"CIN": {"league": "NBNFL", "label": "Bengals",
                       "players": [{"name": "Ja'Marr Chase", "espn_id": "4362628"}]}}}
  `name` is a reminder for whoever edits the file; the page shows ESPN's.
- "nbn" (NBA): NBN's own `{abbr}-roster.csv`, read live, so a trade or signing
  moves a player's IRL feed with them and nothing here needs editing. Slugs map
  to ESPN ids in irl-ids.json ({"nba": {slug: espn_id}}). A rostered player
  with no id is matched automatically on each fetch, but only on an exact name
  AND date of birth against ESPN's NBA rosters; anything looser (a nickname, a
  twin — Cody and Caleb Martin share a birthday) is added to the file by hand.

Either way an id is matched once and stored, never re-matched at read time, so
a second "Elijah Jones" can't be picked up by accident later.

irl/{sport}/{espn_id}.json (fetch_irl.py, one per player):
    {"espn_id", "sport", "fetched_at", "bio": {...},
     "seasons": {"2026": {"year", "label", "closed", "columns", "games", "totals"}}}
A season that was already over when it was fetched (`closed`) is kept as is;
the current season and the bio are refetched once the file is older than
the sport's `refresh_minutes`.
"""
import csv
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from .constants import (DATA_DIR, IRL_DIR, IRL_IDS_FILE, IRL_ROSTERS_FILE, PLAYER_BIOS_FILE,
                        VALID_TEAMS, logger)
from .storage import _atomic_write, _load_json

router = APIRouter()

# `rosters`: "file" (hand-kept irl-rosters.json) or "nbn" (NBN's roster CSVs).
# `seasons`: how many ESPN seasons to keep, current one included.
# `max_games`: the newest N games the endpoint returns across them (None = all).
# `refresh_minutes`: how stale a player's file may get before a run refetches
# it. The timer is hourly; NBA's ~510 players are two requests each, so they go
# every third run rather than every run.
SPORTS = {
    "nfl": {"espn": "football/nfl", "label": "NFL", "rosters": "file",
            "seasons": 2, "max_games": None, "refresh_minutes": 50},
    "nba": {"espn": "basketball/nba", "label": "NBA", "rosters": "nbn",
            "seasons": 2, "max_games": 50, "refresh_minutes": 170},
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


def load_ids() -> dict:
    return _load_json(IRL_IDS_FILE, {})


def _nbn_roster_slugs(team: str) -> list:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return [r["SLUG"] for r in csv.DictReader(f) if r.get("SLUG")]


def _nbn_display_name(bio: dict, slug: str) -> str:
    last, _, first = (bio.get("name") or slug).partition(", ")
    return f"{first.title()} {last.title()}".strip()


def roster_keys(sport: str) -> list:
    if SPORTS[sport]["rosters"] == "nbn":
        return sorted(VALID_TEAMS)
    return list(load_rosters().get(sport, {}))


def roster_players(sport: str, key: str):
    """(meta, [{name, espn_id, slug?}]) for one roster, or None if there is no
    such roster. `key` is matched case-insensitively."""
    if SPORTS[sport]["rosters"] == "nbn":
        team = key.upper()
        if team not in VALID_TEAMS:
            return None
        bios = _load_json(PLAYER_BIOS_FILE, {})
        ids = load_ids().get(sport, {})
        players = [{"slug": s, "name": _nbn_display_name(bios.get(s) or {}, s),
                    "espn_id": ids.get(s)} for s in _nbn_roster_slugs(team)]
        return {"key": team, "league": "NBN", "label": team}, players
    by_key = load_rosters().get(sport, {})
    found = next((k for k in by_key if k.upper() == key.upper()), None)
    if found is None:
        return None
    r = by_key[found]
    return {"key": found, "league": r.get("league"), "label": r.get("label")}, r.get("players", [])


def _norm_name(s: str) -> str:
    """'Kelly Oubre Jr.' and 'OUBRE, KELLY' → 'kellyoubre'."""
    if ", " in s:
        last, _, first = s.partition(", ")
        s = f"{first} {last}"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’\-]", " ", s)
    s = re.sub(r"[^a-z ]", "", s)
    return "".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def auto_map(client: httpx.Client, sport: str) -> dict:
    """Give an ESPN id to every rostered NBN player who lacks one, where ESPN's
    current team rosters have exactly one player with the same name and date
    of birth. Unsigned draft rights are skipped: they have no NBA games. Writes
    irl-ids.json only when something was added."""
    if SPORTS[sport]["rosters"] != "nbn":
        return {"added": {}, "unmapped": []}
    bios = _load_json(PLAYER_BIOS_FILE, {})
    all_ids = load_ids()
    ids = all_ids.setdefault(sport, {})
    want = [s for t in VALID_TEAMS for s in _nbn_roster_slugs(t)
            if s not in ids and (bios.get(s) or {}).get("type") != "draft-rights"]
    if not want:
        return {"added": {}, "unmapped": []}
    espn = SPORTS[sport]["espn"]
    index: dict = {}
    teams = _get(client, f"{SITE_API}/{espn}/teams", limit=40)
    for t in teams["sports"][0]["leagues"][0]["teams"]:
        roster = _get(client, f"{SITE_API}/{espn}/teams/{t['team']['id']}/roster")
        for a in roster.get("athletes") or []:
            key = (_norm_name(a.get("fullName") or ""), (a.get("dateOfBirth") or "")[:10])
            index.setdefault(key, []).append(str(a["id"]))
    added, unmapped = {}, []
    for slug in dict.fromkeys(want):
        bio = bios.get(slug) or {}
        hits = index.get((_norm_name(bio.get("name") or ""), bio.get("dob") or ""), []) if bio.get("dob") else []
        if len(hits) == 1:
            ids[slug] = added[slug] = hits[0]
        else:
            unmapped.append(slug)
    if added:
        _atomic_write(IRL_IDS_FILE, json.dumps(all_ids, indent=2, sort_keys=True) + "\n")
    return {"added": added, "unmapped": unmapped}


def sport_ids(sport: str) -> list:
    """Every ESPN id on any of the sport's rosters, once each."""
    ids = []
    for key in roster_keys(sport):
        found = roster_players(sport, key)
        if found:
            ids += [str(p["espn_id"]) for p in found[1] if p.get("espn_id")]
    return list(dict.fromkeys(ids))


def _fresh(sport: str, espn_id: str) -> bool:
    fetched = _load_json(player_path(sport, espn_id), {}).get("fetched_at")
    if not fetched:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched)
    return age.total_seconds() < SPORTS[sport]["refresh_minutes"] * 60


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
    try:
        bio = normalize_bio(_get(client, f"{COMMON_API}/{cfg['espn']}/athletes/{espn_id}"))
    except httpx.HTTPStatusError as e:
        # The id is real but has no record in this league yet: a G League or
        # college player on an NBN roster (ESPN ids span leagues). Stored with
        # no bio, so the page hides them until they reach the league.
        if e.response.status_code != 404:
            raise
        bio = None
    out = {"espn_id": espn_id, "sport": sport,
           "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "bio": bio, "seasons": seasons}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(out, separators=(",", ":")))
    return out


def fetch_all(sports=None, force: bool = False) -> dict:
    """Refresh every stale player on every roster (all of them with `force`).
    One player failing is logged and skipped, so a single bad id can't stall
    the rest."""
    summary = {}
    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        for sport in SPORTS:
            if sports and sport not in sports:
                continue
            mapped = auto_map(client, sport)
            ids = sport_ids(sport)
            if not ids:
                continue
            current = current_season(client, sport)
            ok, skipped, failed = 0, 0, []
            for espn_id in ids:
                if not force and _fresh(sport, espn_id):
                    skipped += 1
                    continue
                try:
                    fetch_player(client, sport, espn_id, current, force=force)
                    ok += 1
                except Exception as e:  # noqa: BLE001 — one bad player, not the run
                    logger.warning("irl: %s %s failed: %s", sport, espn_id, e)
                    failed.append(espn_id)
            summary[sport] = {"season": current, "players": len(ids), "ok": ok, "fresh": skipped,
                              "failed": failed, "mapped": mapped["added"], "unmapped": mapped["unmapped"]}
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
    for sport in SPORTS:
        rosters = []
        for key in roster_keys(sport):
            meta, players = roster_players(sport, key)
            if players:   # an NBN team with no roster file yet
                rosters.append({**meta, "count": len(players)})
        if rosters:
            out[sport] = {"label": SPORTS[sport]["label"], "rosters": rosters}
    return out


@router.get("/api/irl/{sport}/{roster}")
def get_roster(sport: str, roster: str):
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(status_code=404, detail=f"No IRL rosters for {sport!r}")
    found = roster_players(sport, roster)
    if found is None:
        raise HTTPException(status_code=404, detail=f"No {sport} roster {roster!r}")
    meta, roster_list = found
    players, fetched = [], []
    for p in roster_list:
        espn_id = str(p.get("espn_id") or "")
        cached = _load_json(player_path(sport, espn_id), {}) if espn_id else {}
        seasons = sorted((cached.get("seasons") or {}).values(), key=lambda s: s["year"], reverse=True)
        if cached.get("fetched_at"):
            fetched.append(cached["fetched_at"])
        # A lineman or long snapper has games but never a stat column, and an
        # unmapped or unfetched player has nothing. The page leaves them out.
        has_stats = any(s.get("columns") for s in seasons)
        players.append({"espn_id": espn_id, "slug": p.get("slug"), "name": p.get("name"), "bio": cached.get("bio"),
                        "fetched_at": cached.get("fetched_at"), "has_stats": has_stats,
                        "seasons": _trim(seasons, SPORTS[sport]["max_games"])})
    return {"sport": sport, "roster": meta["key"], "league": meta["league"], "label": meta["label"],
            "max_games": SPORTS[sport]["max_games"],
            "updated_at": min(fetched) if fetched else None,
            "players": players}

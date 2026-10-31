"""Who started each game — `game-starters.jsonl`, and `GET /api/game-starters`.

2K's box score screen lists players by points, not starters first, so the raw
box scores can't say who started. The coaching settings can: when a streamer
marks a team's settings entered (`POST /api/coaching-settings/{team}/enter`),
the record keeps the depth chart as entered (`entered_minutes`). Its PG–C slots
are the five 2K started. So when a box score is committed, `record_game` reads
each team's entered five and appends one line here.

What a line can and can't vouch for:

  * `source: "entered"` — the five as a streamer entered them. Trusted.
  * `source: "reported"` — a streamer (or the stats committee) read the five
    off 2K for this one game and recorded it: `POST /api/game-starters/report`.
    Beats the entered five, since it is what 2K actually started. This is how
    a team whose owner never saves a lineup still gets one on record, and how
    a game already played is filled in afterwards (from its VOD).
  * `source: null`, `starters: null` — nothing was ever entered for this team,
    so 2K used its own default lineup and nobody knows it. Never guessed from
    the team's saved-but-unentered order, which 2K never saw.
  * `flags` — a recorded starter who played no minutes in the box score (hurt,
    or 2K swapped him out), or who isn't in the box score at all. The line is
    kept as recorded; a reader decides what to do with a flagged starter.
  * `entered_after_game` — the snapshot was entered later than the game's
    date. Usually the same lineup (the box score is committed late, after
    tonight's settings went in), but not certainly.
  * `reconstructed: true` — written by `backfill_game_starters.py` for a game
    committed before this file existed, from the entered snapshot as it stood
    then. Opening week only.

Its own append-only file rather than a column on the raw box scores, which are
fixed-shape and guarded (`allstats_guard.py`). Re-recording a game appends a
new line; the last line for a game wins (`load`).

It is also where 6MOY eligibility lives (`sixth_man_status`, rulebook § 8.1):
a player who started at least half his games with known starters can't get
6MOY votes. `put_team_ballot` refuses him and the ballot page reads
`GET /api/game-starters/sixth-man` to leave him out of its search.

    venv/bin/python -m tests.test_game_starters
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import require_any_role
from .constants import DATA_DIR, DERIVED_DIR, PLAYER_BIOS_FILE, VALID_TEAMS, logger
from .coaching_settings import load_coaching_settings, _entered_snapshot
from .storage import _current_league_year, _load_json, _save_json, log_write, read_csv

router = APIRouter()

STARTERS_FILE = DATA_DIR / "game-starters.jsonl"
# game key -> team -> {starters, reported_by, reported_at, note}. Kept apart
# from the jsonl so a report made before the box score is committed is still
# there when `record_game` runs.
REPORTS_FILE = DATA_DIR / "game-starters-reports.json"
PLAYER_SEASONS_CSV = DERIVED_DIR / "players" / "player_seasons.csv"
SLOTS = ("PG", "SG", "SF", "PF", "C")
_ET = ZoneInfo("America/New_York")


def _slot_slug(v) -> Optional[str]:
    # A slot is {slug, minutes, res}; tolerate a bare slug too.
    if isinstance(v, dict):
        return v.get("slug") or None
    return v or None


def team_starters(rec: Optional[dict]) -> dict:
    """One team's entered five from its coaching record, PG→C, or unknown."""
    snap = _entered_snapshot(rec) if rec else None
    if snap is None:
        return {"starters": None, "source": None, "entered_at": None}
    _, minutes = snap
    five = [_slot_slug(minutes.get(s)) for s in SLOTS]
    if not all(five):
        # An entered depth chart with an empty starting slot: nothing to vouch for.
        return {"starters": None, "source": None, "entered_at": rec.get("entered_at")}
    return {"starters": five, "source": "entered", "entered_at": rec.get("entered_at")}


def _flags(starters: Optional[list], rows: list[dict]) -> list[dict]:
    if not starters:
        return []
    played = {r.get("slug"): r.get("min") or 0 for r in rows}
    out = []
    for slug in starters:
        if slug not in played:
            out.append({"slug": slug, "issue": "not in box score"})
        elif not played[slug]:
            out.append({"slug": slug, "issue": "no minutes"})
    return out


def _after_game(entered_at: Optional[str], game_date: str) -> bool:
    if not entered_at:
        return False
    try:
        when = datetime.fromisoformat(entered_at).astimezone(_ET).date().isoformat()
    except ValueError:
        return False
    return when > game_date


def game_key(date: str, game_type: str, home_team: str, away_team: str) -> str:
    a, b = sorted((home_team, away_team))
    return f"{date}|{game_type}|{a}|{b}"


def load_reports() -> dict:
    return _load_json(REPORTS_FILE, {})


def reported_starters(rep: dict) -> dict:
    return {"starters": list(rep["starters"]), "source": "reported", "entered_at": None,
            "reported_by": rep.get("reported_by"), "reported_at": rep.get("reported_at")}


def build_entry(*, date: str, season: str, game_type: str, home_team: str, away_team: str,
                home_rows: list[dict], away_rows: list[dict], coaching: dict,
                reports: Optional[dict] = None, reconstructed: bool = False) -> dict:
    """The line for one game. `*_rows` are `{slug, min}` per player.
    `reports` is team -> this game's reported five, which wins over the
    entered one."""
    reports = reports or {}
    teams = {}
    for team, rows in ((home_team, home_rows), (away_team, away_rows)):
        rep = reports.get(team)
        t = reported_starters(rep) if rep else team_starters(coaching.get(team))
        t["flags"] = _flags(t["starters"], rows)
        t["entered_after_game"] = _after_game(t["entered_at"], date)
        teams[team] = t
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": date, "season": season, "game_type": game_type,
        "home_team": home_team, "away_team": away_team, "teams": teams,
    }
    if reconstructed:
        entry["reconstructed"] = True
    return entry


def append(entry: dict) -> None:
    with STARTERS_FILE.open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def record_game(**kw) -> None:
    """Called from the box score commit. Never raises: a missing starters
    line is a gap in a nice-to-have, a failed commit is not."""
    try:
        reports = load_reports().get(game_key(kw["date"], kw["game_type"], kw["home_team"], kw["away_team"]))
        append(build_entry(coaching=load_coaching_settings(), reports=reports, **kw))
    except Exception as exc:
        logger.warning("game-starters: couldn't record %s %s@%s — %s",
                       kw.get("date"), kw.get("away_team"), kw.get("home_team"), exc)


def load(season: Optional[str] = None) -> list[dict]:
    """Every game's latest line, oldest first."""
    if not STARTERS_FILE.exists():
        return []
    latest: dict[tuple, dict] = {}
    for line in STARTERS_FILE.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if season and e.get("season") != season:
            continue
        key = (e.get("date"), e.get("game_type"), *sorted((e.get("home_team"), e.get("away_team"))))
        latest[key] = e
    return sorted(latest.values(), key=lambda e: e["date"])


def starts_by_player(entries: list[dict], game_type: str = "REG") -> dict:
    """slug → {gs, by_team: {TEAM: gs}}: games started. A flagged starter
    (no minutes, or not in the box) is not counted as a start."""
    out: dict[str, dict] = defaultdict(lambda: {"gs": 0, "by_team": defaultdict(int)})
    for e in entries:
        if e.get("game_type") != game_type:
            continue
        for team, t in (e.get("teams") or {}).items():
            flagged = {f["slug"] for f in t.get("flags") or []}
            for slug in t.get("starters") or []:
                if slug not in flagged:
                    out[slug]["gs"] += 1
                    out[slug]["by_team"][team] += 1
    return {k: {"gs": v["gs"], "by_team": dict(v["by_team"])} for k, v in out.items()}


def committed_rows(season: str, date: str, game_type: str, home_team: str, away_team: str) -> Optional[dict]:
    """team -> [{slug, min}] from the raw box score, or None if the game isn't
    committed. The raw rows carry a name, not a slug; the name is the bio's."""
    from .boxscores import allstats_path  # boxscores imports this module
    path = allstats_path(season, game_type)
    if not path.exists():
        return None
    bios = _load_json(PLAYER_BIOS_FILE, {})
    slug_of = {}
    for slug, b in bios.items():
        slug_of.setdefault((b.get("name") or "").strip().upper(), slug)
    _, rows = read_csv(path)
    out: dict[str, list] = {home_team: [], away_team: []}
    for r in rows:
        team = (r.get("TEAM") or "").strip()
        opp = (r.get("OPP_RAW") or r.get("OPP") or "").strip().lstrip("@")
        if r.get("DATE") != date or team not in out or opp not in out or opp == team:
            continue
        out[team].append({"slug": slug_of.get((r.get("PLAYER") or "").strip().upper()),
                          "min": int(float(r.get("M") or 0))})
    return out if out[home_team] or out[away_team] else None


class ReportBody(BaseModel):
    date: str
    home_team: str
    away_team: str
    team: str
    starters: list[str]          # PG, SG, SF, PF, C
    game_type: str = "REG"
    season: Optional[str] = None
    note: str = ""


@router.post("/api/game-starters/report")
def report_starters(body: ReportBody, info: dict = Depends(require_any_role("streamer", "stats"))):
    """Record the five 2K started for one team in one game, read off the game
    (its lineup screen at tip, or the VOD). For a team with no lineup entered,
    or one whose entered five isn't what 2K used. Before the box score is
    committed it waits for the commit; after, it rewrites the game's line now.
    Reporting again replaces the earlier report."""
    home, away, team = body.home_team.upper(), body.away_team.upper(), body.team.upper()
    gtype = body.game_type.upper()
    if gtype not in ("REG", "PLAYOFF"):
        raise HTTPException(422, "game_type must be REG or PLAYOFF")
    if home not in VALID_TEAMS or away not in VALID_TEAMS or home == away:
        raise HTTPException(422, "Unknown teams")
    if team not in (home, away):
        raise HTTPException(422, f"{team} isn't in this game")
    five = [s.strip() for s in body.starters]
    if len(five) != 5 or not all(five) or len(set(five)) != 5:
        raise HTTPException(422, "Need five different players, PG to C")
    bios = _load_json(PLAYER_BIOS_FILE, {})
    unknown = [s for s in five if s not in bios]
    if unknown:
        raise HTTPException(422, f"Unknown player: {', '.join(unknown)}")
    season = body.season or _current_league_year()

    rows = committed_rows(season, body.date, gtype, home, away)
    if rows is not None:
        played = {r["slug"] for r in rows.get(team, [])}
        missing = [bios[s].get("name", s) for s in five if s not in played]
        if missing:
            raise HTTPException(422, f"Not in {team}'s box score for this game: {', '.join(missing)}")
    else:
        roster_file = DATA_DIR / f"{team.lower()}-roster.csv"
        if roster_file.exists():
            _, roster = read_csv(roster_file)
            on = {r.get("SLUG") for r in roster}
            off = [bios[s].get("name", s) for s in five if s not in on]
            if off:
                raise HTTPException(422, f"Not on {team}'s roster: {', '.join(off)}")

    key = game_key(body.date, gtype, home, away)
    rep = {"starters": five, "reported_by": info.get("name"),
           "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "note": body.note.strip()}
    reports = load_reports()
    reports.setdefault(key, {})[team] = rep
    _save_json(REPORTS_FILE, reports)

    line = None
    if rows is not None:
        prior = next((e for e in load(season) if game_key(e["date"], e["game_type"], e["home_team"],
                                                          e["away_team"]) == key), None)
        if prior:
            # Keep the other team as recorded; replace this one.
            line = {**prior, "recorded_at": rep["reported_at"], "teams": dict(prior["teams"])}
            t = reported_starters(rep)
            t["flags"] = _flags(five, rows[team])
            t["entered_after_game"] = False
            line["teams"][team] = t
        else:
            # Committed before this file existed: the other side is unknown,
            # not guessed from today's settings.
            line = build_entry(date=body.date, season=season, game_type=gtype, home_team=home,
                               away_team=away, home_rows=rows[home], away_rows=rows[away],
                               coaching={}, reports=reports[key])
        append(line)
    log_write(info, f"POST game-starters/report {key} {team}")
    return {"key": key, "team": team, "report": rep, "recorded": line is not None, "line": line}


@router.get("/api/game-starters")
def get_game_starters(season: Optional[str] = Query(None), team: Optional[str] = Query(None)):
    season = season or _current_league_year()
    entries = load(season)
    if team:
        t = team.upper()
        entries = [e for e in entries if t in (e["home_team"], e["away_team"])]
    return {"season": season, "games": entries}


@router.get("/api/game-starters/reports")
def get_reports():
    """Every reported five, keyed like `game_key`. Public: the lineup a team
    started is public once played, and its depth order already is
    (`/api/rotations`)."""
    return load_reports()


@router.get("/api/game-starters/unknown")
def get_unknown(season: Optional[str] = Query(None)):
    """Played games where a team's five is unknown, newest first, each with
    the team's box score players to choose from (slug, name, minutes)."""
    season = season or _current_league_year()
    bios = _load_json(PLAYER_BIOS_FILE, {})
    out = []
    for e in reversed(load(season)):
        missing = [t for t, d in (e.get("teams") or {}).items() if not d.get("starters")]
        if not missing:
            continue
        rows = committed_rows(season, e["date"], e["game_type"], e["home_team"], e["away_team"]) or {}
        for team in missing:
            players = sorted(({"slug": r["slug"], "name": (bios.get(r["slug"]) or {}).get("name", ""),
                               "min": r["min"]} for r in rows.get(team, []) if r["slug"]),
                             key=lambda p: -p["min"])
            out.append({"date": e["date"], "game_type": e["game_type"], "home_team": e["home_team"],
                        "away_team": e["away_team"], "team": team, "players": players})
    return {"season": season, "games": out}


@router.get("/api/game-starters/counts")
def get_start_counts(season: Optional[str] = Query(None),
                     game_type: str = Query("REG", pattern="^(REG|PLAYOFF)$")):
    """Games started per player, and how many games of each team have known
    starters — a reader needs the second to tell "came off the bench" from
    "nobody knows"."""
    season = season or _current_league_year()
    entries = load(season)
    return {"season": season, "game_type": game_type,
            "players": starts_by_player(entries, game_type),
            "team_games_known": _team_games_known(entries, game_type)}


def _team_games_known(entries: list[dict], game_type: str = "REG") -> dict:
    known: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.get("game_type") != game_type:
            continue
        for team, t in (e.get("teams") or {}).items():
            if t.get("starters"):
                known[team] += 1
    return dict(known)


def _season_games(season: str) -> dict:
    """slug → (games played, [teams]) for a regular season, from the stats build."""
    out: dict[str, tuple[int, list]] = {}
    try:
        with PLAYER_SEASONS_CSV.open(newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("SEASON") != season or not r.get("SLUG"):
                    continue
                try:
                    g = int(r.get("G") or 0)
                except ValueError:
                    continue
                games, teams = out.get(r["SLUG"], (0, []))
                out[r["SLUG"]] = (games + g, teams + [r.get("TEAM")])
    except OSError:
        return {}
    return out


def sixth_man_status(season: str) -> dict:
    """slug → {gs, known, eligible} for 6MOY (rulebook § 8.1).

    `known` is his games with a known starting five: his games played, capped
    by how many of his teams' games have known starters — the same count the
    award race (`award-race.js` in nbn-today) uses. He is eligible unless he
    started at least half of those. With nothing known (seasons before 26-27,
    or a player with no recorded games) there is no evidence against him, so
    he is eligible: the rule only refuses what the record shows.
    """
    entries = load(season)
    known_by_team = _team_games_known(entries)
    starts = starts_by_player(entries)
    out = {}
    for slug, (games, teams) in _season_games(season).items():
        known = min(games, sum(known_by_team.get(t, 0) for t in teams))
        gs = (starts.get(slug) or {}).get("gs", 0)
        out[slug] = {"gs": gs, "known": known, "eligible": not (known and 2 * gs >= known)}
    return out


@router.get("/api/game-starters/sixth-man")
def get_sixth_man(season: Optional[str] = Query(None)):
    """Who can get 6MOY votes. A player not listed has no games this season and
    is eligible."""
    season = season or _current_league_year()
    return {"season": season, "players": sixth_man_status(season)}

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .constants import (
    DATA_DIR, PLAYER_BIOS_FILE, PENDING_BOXSCORES_DIR, MANUAL_QUEUE_FILE,
    BUILD_STATUS_FILE, BUILD_SCRIPT, VALID_TEAMS, _manual_queue_lock, logger,
)
from .storage import read_csv, write_csv, log_write, _current_season_str
from .auth import require_any_role
from .players import load_player_bios
from .bets import _award_submission_reward

router = APIRouter()

REG_ALLSTATS_HEADERS = [
    "TEAM","DATE","OPP","OPP_RAW","PLAYER","M","P","R","OR","DR","A","S","B","TO",
    "FGM","FGA","FGPCT","3PM","3PA","3PPCT","FTM","FTA","FTPCT","PF","OPP_TEAM",
    "TD","BOX"," ","TEAM_PTS","OPP_TEAM_PTS","AGE","WL","gametype",
]
PLAYOFF_ALLSTATS_HEADERS = [
    "TEAM","DATE","OPP","OPP_RAW","PLAYER","M","P","R","OR","DR","A","S","B","TO",
    "FGM","FGA","FGPCT","3PM","3PA","3PPCT","FTM","FTA","FTPCT","PF","OPP_TEAM",
    "TD","BOX"," ","TEAM_PTS","OPP_TEAM_PTS","AGE","WL","GAME","ROUND","gametype",
]


def allstats_path(season: str, game_type: str):
    if game_type.upper() == "PLAYOFF":
        year = season.split("-")[-1]
        return DATA_DIR / f"allstats-playoffs-{year}.csv"
    return DATA_DIR / f"allstats-{season}.csv"


def _safe_pct(made: int, att: int) -> str:
    return str(made / att) if att > 0 else "NA"


def _triple_double(pts, reb, ast, stl, blk) -> str:
    return "1" if sum(1 for v in [pts, reb, ast, stl, blk] if v >= 10) >= 3 else "0"


def _calc_age(dob_str: str, game_date_str: str) -> str:
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d")
        gd = datetime.strptime(game_date_str, "%Y-%m-%d")
        return f"{(gd - dob).days / 365.25:.5f}"
    except Exception:
        return "NA"


def _build_allstats_row(
    team: str, opp: str, date: str, player_name: str, slug: str,
    min_: int, pts: int, reb: int, oreb: int, dreb: int,
    ast: int, stl: int, blk: int, tov: int, pf: int,
    fgm: int, fga: int, tpm: int, tpa: int, ftm: int, fta: int,
    team_pts: int, opp_pts: int, wl: str, game_type: str,
    game_num: Optional[int], round_num: Optional[int],
    bios: dict,
) -> dict:
    age = _calc_age(bios.get(slug, {}).get("dob", ""), date)
    row = {
        "TEAM": team.upper(),
        "DATE": date,
        "OPP": opp.upper(),
        "OPP_RAW": opp.lstrip("@").upper(),
        "PLAYER": player_name,
        "M": str(min_),
        "P": str(pts),
        "R": str(reb),
        "OR": str(oreb),
        "DR": str(dreb),
        "A": str(ast),
        "S": str(stl),
        "B": str(blk),
        "TO": str(tov),
        "FGM": str(fgm),
        "FGA": str(fga),
        "FGPCT": _safe_pct(fgm, fga),
        "3PM": str(tpm),
        "3PA": str(tpa),
        "3PPCT": _safe_pct(tpm, tpa),
        "FTM": str(ftm),
        "FTA": str(fta),
        "FTPCT": _safe_pct(ftm, fta),
        "PF": str(pf),
        "OPP_TEAM": opp.lstrip("@").upper(),
        "TD": _triple_double(pts, reb, ast, stl, blk),
        "BOX": "0",
        " ": "",
        "TEAM_PTS": str(team_pts),
        "OPP_TEAM_PTS": str(opp_pts),
        "AGE": age,
        "WL": wl,
    }
    if game_type.upper() == "PLAYOFF":
        row["GAME"] = str(game_num) if game_num is not None else ""
        row["ROUND"] = str(round_num) if round_num is not None else ""
    row["gametype"] = "PLAYOFF" if game_type.upper() == "PLAYOFF" else "REG"
    return row


def _parse_one_screenshot(
    image_bytes: bytes, media_type: str,
    team: str, opp: str, date: str,
    roster_context: list[dict],
) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured on server")

    client = anthropic.Anthropic(api_key=api_key)
    roster_json = json.dumps(roster_context, indent=2)
    prompt = f"""You are parsing an NBA 2K simulation game box score screenshot.
This screenshot shows the {team} box score from a game against {opp} on {date}.

Here are the known {team} players with their slugs:
{roster_json}

Extract ALL players who played (skip DNP). For minutes shown as MM:SS, convert to integer minutes (round down).
Match each player name to the closest known player above. Set confidence:
- "high": obvious match
- "medium": somewhat uncertain
- "low": best guess only

Return ONLY valid JSON — no markdown, no explanation:
{{
  "team_pts": <integer or null if not visible>,
  "opp_pts": <integer or null if not visible>,
  "rows": [
    {{
      "player": "LAST, FIRST",
      "slug": "slug-from-roster",
      "confidence": "high|medium|low",
      "min": <integer>,
      "pts": <integer>,
      "reb": <integer total>,
      "oreb": <integer, 0 if not shown>,
      "dreb": <integer, equals reb if not split>,
      "ast": <integer>,
      "stl": <integer>,
      "blk": <integer>,
      "tov": <integer>,
      "pf": <integer>,
      "fgm": <integer>,
      "fga": <integer>,
      "tpm": <integer 3PM>,
      "tpa": <integer 3PA>,
      "ftm": <integer>,
      "fta": <integer>,
      "concern": "<string if uncertain, else null>"
    }}
  ],
  "concerns": ["<any general readability issues>"]
}}"""

    img_b64 = base64.standard_b64encode(image_bytes).decode()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _load_roster_context(team: str, bios: dict) -> list[dict]:
    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not roster_path.exists():
        return []
    _, rows = read_csv(roster_path)
    result = []
    for r in rows:
        slug = r.get("SLUG") or r.get("PLAYER", "")
        if not slug:
            continue
        bio = bios.get(slug, {})
        name = bio.get("name", slug)
        result.append({"slug": slug, "name": name})
    return result


# ── Build status/trigger ──────────────────────────────────────────────────────

def _read_build_status() -> dict:
    if BUILD_STATUS_FILE.exists():
        try:
            return json.loads(BUILD_STATUS_FILE.read_text())
        except Exception:
            pass
    return {"status": "idle"}


def _trigger_build():
    status = _read_build_status()
    if status.get("status") == "running":
        return False

    now = datetime.now(timezone.utc).isoformat()
    BUILD_STATUS_FILE.write_text(json.dumps({"status": "running", "started_at": now}))

    def _run():
        try:
            result = subprocess.run(
                ["bash", str(BUILD_SCRIPT), "build"],
                capture_output=True, text=True,
            )
            finished = datetime.now(timezone.utc).isoformat()
            if result.returncode == 0:
                BUILD_STATUS_FILE.write_text(json.dumps({
                    "status": "done",
                    "started_at": now,
                    "finished_at": finished,
                }))
            else:
                BUILD_STATUS_FILE.write_text(json.dumps({
                    "status": "error",
                    "started_at": now,
                    "finished_at": finished,
                    "error": (result.stderr or result.stdout or "unknown error")[-500:],
                }))
        except Exception as e:
            BUILD_STATUS_FILE.write_text(json.dumps({
                "status": "error",
                "started_at": now,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            }))

    threading.Thread(target=_run, daemon=True).start()
    return True


# ── Duplicate detection ───────────────────────────────────────────────────────

def _game_teams(home: str, away: str) -> frozenset:
    return frozenset({home.upper(), away.upper()})


def _game_in_data(date: str, home_team: str, away_team: str, season: str, game_type: str) -> bool:
    path = allstats_path(season, game_type)
    if not path.exists():
        return False
    _, rows = read_csv(path)
    teams = _game_teams(home_team, away_team)
    for r in rows:
        if r.get("DATE", "").strip() != date:
            continue
        t = r.get("TEAM", "").strip()
        o = r.get("OPP", "").strip().lstrip("@")
        if _game_teams(t, o) == teams:
            return True
    return False


def _game_in_pending_screenshots(date: str, home_team: str, away_team: str) -> bool:
    if not PENDING_BOXSCORES_DIR.exists():
        return False
    teams = _game_teams(home_team, away_team)
    for item_dir in PENDING_BOXSCORES_DIR.iterdir():
        meta_path = item_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("date") == date and _game_teams(meta.get("home_team", ""), meta.get("away_team", "")) == teams:
            return True
    return False


def _game_in_manual_queue(date: str, home_team: str, away_team: str) -> bool:
    with _manual_queue_lock:
        if not MANUAL_QUEUE_FILE.exists():
            return False
        try:
            queue = json.loads(MANUAL_QUEUE_FILE.read_text())
        except Exception:
            return False
    teams = _game_teams(home_team, away_team)
    return any(
        q.get("date") == date and _game_teams(q.get("home_team", ""), q.get("away_team", "")) == teams
        for q in queue
    )


def _remove_from_manual_queue(date: str, home_team: str, away_team: str):
    with _manual_queue_lock:
        if not MANUAL_QUEUE_FILE.exists():
            return
        try:
            queue = json.loads(MANUAL_QUEUE_FILE.read_text())
        except Exception:
            return
        teams = _game_teams(home_team, away_team)
        queue = [q for q in queue if not (
            q.get("date") == date and
            _game_teams(q.get("home_team", ""), q.get("away_team", "")) == teams
        )]
        MANUAL_QUEUE_FILE.write_text(json.dumps(queue))


# ── Pydantic models ───────────────────────────────────────────────────────────

class BoxscorePlayerRow(BaseModel):
    player: str
    slug: str
    min: int
    pts: int
    reb: int
    oreb: int
    dreb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int
    fgm: int
    fga: int
    tpm: int
    tpa: int
    ftm: int
    fta: int


class BoxscoreCommitRequest(BaseModel):
    date: str
    home_team: str
    away_team: str
    season: str
    game_type: str
    home_pts: int
    away_pts: int
    game_num: Optional[int] = None
    round_num: Optional[int] = None
    home_rows: list[BoxscorePlayerRow]
    away_rows: list[BoxscorePlayerRow]
    skip_build: bool = False
    skip_reward: bool = False


class BoxscoreQueueRequest(BaseModel):
    date: str
    home_team: str
    away_team: str
    season: str
    game_type: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/api/boxscore/parse")
async def parse_boxscore(
    home_image: UploadFile = File(...),
    away_image: UploadFile = File(...),
    date: str = Form(...),
    home_team: str = Form(...),
    away_team: str = Form(...),
    season: str = Form(...),
    game_type: str = Form(...),
    game_num: Optional[int] = Form(None),
    round_num: Optional[int] = Form(None),
    info: dict = Depends(require_any_role("rosters", "stats")),
):
    home_team = home_team.upper()
    away_team = away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}
    home_roster = _load_roster_context(home_team, bios)
    away_roster = _load_roster_context(away_team, bios)

    home_bytes = await home_image.read()
    away_bytes = await away_image.read()
    home_mt = home_image.content_type or "image/png"
    away_mt = away_image.content_type or "image/png"

    home_result = _parse_one_screenshot(home_bytes, home_mt, home_team, away_team, date, home_roster)
    away_result = _parse_one_screenshot(away_bytes, away_mt, away_team, home_team, date, away_roster)

    home_pts = home_result.get("team_pts") or away_result.get("opp_pts")
    away_pts = away_result.get("team_pts") or home_result.get("opp_pts")

    concerns = home_result.get("concerns", []) + away_result.get("concerns", [])
    logger.info("[%s] POST boxscore/parse — %s vs %s on %s", info.get("name"), home_team, away_team, date)
    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_pts": home_pts,
        "away_pts": away_pts,
        "home_rows": home_result.get("rows", []),
        "away_rows": away_result.get("rows", []),
        "concerns": concerns,
    }


@router.post("/api/boxscore/commit")
def commit_boxscore(body: BoxscoreCommitRequest, info: dict = Depends(require_any_role("rosters", "stats"))):
    home_team = body.home_team.upper()
    away_team = body.away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    if _game_in_data(body.date, home_team, away_team, body.season, body.game_type):
        raise HTTPException(status_code=409, detail=f"{home_team} vs {away_team} on {body.date} is already committed.")

    path = allstats_path(body.season, body.game_type)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Allstats file not found: {path.name}")

    headers, existing = read_csv(path)
    is_playoff = body.game_type.upper() == "PLAYOFF"
    expected_headers = PLAYOFF_ALLSTATS_HEADERS if is_playoff else REG_ALLSTATS_HEADERS

    bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}

    new_rows = []
    for r in body.home_rows:
        new_rows.append(_build_allstats_row(
            home_team, away_team, body.date, r.player, r.slug,
            r.min, r.pts, r.reb, r.oreb, r.dreb,
            r.ast, r.stl, r.blk, r.tov, r.pf,
            r.fgm, r.fga, r.tpm, r.tpa, r.ftm, r.fta,
            body.home_pts, body.away_pts,
            "W" if body.home_pts > body.away_pts else "L",
            body.game_type, body.game_num, body.round_num, bios,
        ))
    for r in body.away_rows:
        new_rows.append(_build_allstats_row(
            away_team, f"@{home_team}", body.date, r.player, r.slug,
            r.min, r.pts, r.reb, r.oreb, r.dreb,
            r.ast, r.stl, r.blk, r.tov, r.pf,
            r.fgm, r.fga, r.tpm, r.tpa, r.ftm, r.fta,
            body.away_pts, body.home_pts,
            "W" if body.away_pts > body.home_pts else "L",
            body.game_type, body.game_num, body.round_num, bios,
        ))

    write_csv(path, expected_headers, existing + new_rows)
    _remove_from_manual_queue(body.date, home_team, away_team)

    if body.skip_reward:
        reward, new_bal = 0.0, 0.0
    else:
        reward, new_bal = _award_submission_reward(info["name"])

    logger.info("[%s] POST boxscore/commit — %s vs %s on %s (%d rows, +NB¥%.2f)", info.get("name"), home_team, away_team, body.date, len(new_rows), reward)
    building = False if body.skip_build else _trigger_build()
    return {"ok": True, "rows_added": len(new_rows), "building": building, "nbyen_reward": reward, "nbyen_balance": new_bal}


@router.get("/api/boxscores/dates")
def get_boxscore_dates(season: str = Query(default=None)):
    if season is None:
        season = _current_season_str()
    reg_path = allstats_path(season, "REG")
    po_path = allstats_path(season, "PLAYOFF")
    dates: set[str] = set()
    for path in (reg_path, po_path):
        if not path.exists():
            continue
        _, rows = read_csv(path)
        for r in rows:
            d = r.get("DATE", "").strip()
            if d:
                dates.add(d)
    return sorted(dates, reverse=True)


def _all_allstats_paths() -> list:
    paths = []
    for p in sorted(DATA_DIR.glob("allstats-??-??.csv")):
        paths.append((p, "REG"))
    for p in sorted(DATA_DIR.glob("allstats-playoffs-??.csv")):
        paths.append((p, "PLAYOFF"))
    return paths


# Cache for the compact game index. Keyed by the set of (path, mtime) tuples
# for the allstats files in play, so it auto-invalidates whenever a box score
# is committed (which rewrites the underlying CSV and bumps its mtime).
_GAME_INDEX_CACHE: dict[tuple, list[dict]] = {}


def _build_game_index(paths: list) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for path, gtype in paths:
        if not path.exists():
            continue
        _, rows = read_csv(path)
        for r in rows:
            date = r.get("DATE", "").strip()
            team = r.get("TEAM", "").strip()
            opp_field = r.get("OPP", "").strip()
            opp_raw = r.get("OPP_RAW", opp_field).strip().lstrip("@")
            if not date or not team or not opp_raw:
                continue
            is_home = not opp_field.startswith("@")
            key = (date, team, opp_raw) if is_home else (date, opp_raw, team)
            if key in seen:
                continue
            try:
                team_pts = int(r.get("TEAM_PTS", 0) or 0)
                opp_pts = int(r.get("OPP_TEAM_PTS", 0) or 0)
            except ValueError:
                team_pts, opp_pts = 0, 0
            seen[key] = {
                "date": date,
                "home_team": key[1],
                "away_team": key[2],
                "home_score": team_pts if is_home else opp_pts,
                "away_score": opp_pts if is_home else team_pts,
                "gametype": r.get("gametype", gtype),
                "season": r.get("SEASON", "").strip(),
            }
    return sorted(seen.values(), key=lambda g: g["date"], reverse=True)


@router.get("/api/boxscores/games")
def get_boxscore_games(season: str = Query(default=None), team: str = Query(default=None)):
    """Compact game index. Cached by file mtime; optionally filtered by team."""
    if season:
        paths = [(allstats_path(season, "REG"), "REG"), (allstats_path(season, "PLAYOFF"), "PLAYOFF")]
    else:
        paths = _all_allstats_paths()

    cache_key = tuple(
        (str(p), p.stat().st_mtime_ns if p.exists() else None) for p, _ in paths
    )
    games = _GAME_INDEX_CACHE.get(cache_key)
    if games is None:
        games = _build_game_index(paths)
        _GAME_INDEX_CACHE.clear()  # only keep the most recent index set
        _GAME_INDEX_CACHE[cache_key] = games

    if team:
        team = team.upper()
        games = [g for g in games if g["home_team"] == team or g["away_team"] == team]
    return games


def _boxscore_player_row(r: dict) -> dict:
    def iv(k): return int(r.get(k, 0) or 0)
    def fv(k):
        v = r.get(k, "")
        try: return round(float(v), 3)
        except (ValueError, TypeError): return None
    return {
        "player": r.get("PLAYER", ""),
        "slug": r.get("SLUG", ""),
        "min": iv("M"),
        "pts": iv("P"),
        "reb": iv("R"),
        "oreb": iv("OR"),
        "dreb": iv("DR"),
        "ast": iv("A"),
        "stl": iv("S"),
        "blk": iv("B"),
        "tov": iv("TO"),
        "pf": iv("PF"),
        "fgm": iv("FGM"),
        "fga": iv("FGA"),
        "tpm": iv("3PM"),
        "tpa": iv("3PA"),
        "ftm": iv("FTM"),
        "fta": iv("FTA"),
        "fgpct": fv("FGPCT"),
        "tppct": fv("3PPCT"),
        "ftpct": fv("FTPCT"),
    }


@router.get("/api/boxscores")
def get_boxscores(date: str = Query(...), season: str = Query(default=None)):
    if season is None:
        season = _current_season_str()
    reg_path = allstats_path(season, "REG")
    po_path = allstats_path(season, "PLAYOFF")

    all_rows: list[dict] = []
    for path in (reg_path, po_path):
        if not path.exists():
            continue
        _, rows = read_csv(path)
        all_rows.extend(r for r in rows if r.get("DATE", "").strip() == date)

    games: dict[tuple, dict] = {}
    for r in all_rows:
        opp_raw = r.get("OPP_RAW", r.get("OPP", "")).strip().lstrip("@")
        opp_field = r.get("OPP", "").strip()
        team = r.get("TEAM", "").strip()
        if not team or not opp_raw:
            continue
        is_home = not opp_field.startswith("@")
        if is_home:
            key = (team, opp_raw)
        else:
            key = (opp_raw, team)
        if key not in games:
            games[key] = {
                "home_team": key[0],
                "away_team": key[1],
                "home_score": None,
                "away_score": None,
                "gametype": r.get("gametype", "REG"),
                "home_players": [],
                "away_players": [],
            }

    for r in all_rows:
        opp_raw = r.get("OPP_RAW", r.get("OPP", "")).strip().lstrip("@")
        opp_field = r.get("OPP", "").strip()
        team = r.get("TEAM", "").strip()
        if not team or not opp_raw:
            continue
        is_home = not opp_field.startswith("@")
        key = (team, opp_raw) if is_home else (opp_raw, team)
        if key not in games:
            continue
        g = games[key]
        try:
            team_pts = int(r.get("TEAM_PTS", 0) or 0)
            opp_pts = int(r.get("OPP_TEAM_PTS", 0) or 0)
        except ValueError:
            team_pts, opp_pts = 0, 0
        if is_home:
            if g["home_score"] is None:
                g["home_score"] = team_pts
                g["away_score"] = opp_pts
            g["home_players"].append(_boxscore_player_row(r))
        else:
            if g["away_score"] is None:
                g["away_score"] = team_pts
                g["home_score"] = opp_pts
            g["away_players"].append(_boxscore_player_row(r))

    return sorted(games.values(), key=lambda g: (g["home_team"], g["away_team"]))


def _gamelog_row(r: dict, season: str) -> dict:
    def iv(k): return int(r.get(k, 0) or 0)
    def fv(k):
        v = r.get(k, "")
        try: return round(float(v), 3)
        except (ValueError, TypeError): return None
    opp = r.get("OPP", "").strip()
    return {
        "date": r.get("DATE", ""),
        "season": season,
        "team": r.get("TEAM", "").strip(),
        "opp": opp,
        "opp_raw": r.get("OPP_RAW", opp.lstrip("@")).strip(),
        "home": not opp.startswith("@"),
        "wl": r.get("WL", "").strip(),
        "team_pts": iv("TEAM_PTS"),
        "opp_pts": iv("OPP_TEAM_PTS"),
        "min": iv("M"),
        "pts": iv("P"),
        "reb": iv("R"),
        "oreb": iv("OR"),
        "dreb": iv("DR"),
        "ast": iv("A"),
        "stl": iv("S"),
        "blk": iv("B"),
        "tov": iv("TO"),
        "pf": iv("PF"),
        "fgm": iv("FGM"),
        "fga": iv("FGA"),
        "tpm": iv("3PM"),
        "tpa": iv("3PA"),
        "ftm": iv("FTM"),
        "fta": iv("FTA"),
        "fgpct": fv("FGPCT"),
        "tppct": fv("3PPCT"),
        "ftpct": fv("FTPCT"),
        "td": r.get("TD", "0") == "1",
        "gametype": r.get("gametype", "REG"),
    }


@router.get("/api/players/{slug}/gamelog")
def get_player_gamelog(slug: str):
    bios = load_player_bios()
    bio = bios.get(slug)
    if not bio:
        raise HTTPException(status_code=404, detail="Player not found")
    player_name = (bio.get("name") or "").strip().upper()
    if not player_name:
        raise HTTPException(status_code=404, detail="Player has no name")

    results = []

    for path in sorted(DATA_DIR.glob("allstats-??-??.csv")):
        season = path.stem[len("allstats-"):]
        _, rows = read_csv(path)
        for r in rows:
            if r.get("PLAYER", "").strip().upper() == player_name:
                results.append(_gamelog_row(r, season))

    for path in sorted(DATA_DIR.glob("allstats-playoffs-??.csv")):
        end_yr = path.stem[len("allstats-playoffs-"):]
        start_yr = str(int(end_yr) - 1).zfill(2)[-2:]
        season = f"{start_yr}-{end_yr}"
        _, rows = read_csv(path)
        for r in rows:
            if r.get("PLAYER", "").strip().upper() == player_name:
                results.append(_gamelog_row(r, season))

    results.sort(key=lambda r: r["date"], reverse=True)
    return results


@router.get("/api/build/status")
def get_build_status():
    return _read_build_status()


@router.post("/api/build/trigger")
def trigger_build(info: dict = Depends(require_any_role("rosters", "stats"))):
    building = _trigger_build()
    logger.info("[%s] POST build/trigger (started=%s)", info.get("name"), building)
    return {"ok": True, "building": building}


@router.post("/api/boxscore/upload")
async def upload_boxscore(
    home_image: UploadFile = File(...),
    away_image: UploadFile = File(...),
    date: str = Form(...),
    home_team: str = Form(...),
    away_team: str = Form(...),
    season: str = Form(...),
    game_type: str = Form(...),
    game_num: Optional[int] = Form(None),
    round_num: Optional[int] = Form(None),
    info: dict = Depends(require_any_role("rosters", "stats")),
):
    home_team = home_team.upper()
    away_team = away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    if _game_in_data(date, home_team, away_team, season, game_type):
        raise HTTPException(status_code=409, detail=f"{home_team} vs {away_team} on {date} is already committed.")
    if _game_in_pending_screenshots(date, home_team, away_team):
        raise HTTPException(status_code=409, detail=f"A screenshot is already pending for {home_team} vs {away_team} on {date}.")
    if _game_in_manual_queue(date, home_team, away_team):
        raise HTTPException(status_code=409, detail=f"A manual entry is already queued for {home_team} vs {away_team} on {date}.")

    item_id = str(uuid.uuid4())[:8]
    item_dir = PENDING_BOXSCORES_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    home_bytes = await home_image.read()
    away_bytes = await away_image.read()
    home_ext = (home_image.filename or "home.png").rsplit(".", 1)[-1].lower()
    away_ext = (away_image.filename or "away.png").rsplit(".", 1)[-1].lower()
    (item_dir / f"home.{home_ext}").write_bytes(home_bytes)
    (item_dir / f"away.{away_ext}").write_bytes(away_bytes)

    meta = {
        "id": item_id,
        "date": date,
        "home_team": home_team,
        "away_team": away_team,
        "season": season,
        "game_type": game_type,
        "game_num": game_num,
        "round_num": round_num,
        "uploaded_by": info.get("name"),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "home_image": f"home.{home_ext}",
        "away_image": f"away.{away_ext}",
    }
    (item_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    reward, new_bal = _award_submission_reward(info["name"])
    logger.info("[%s] POST boxscore/upload — %s vs %s on %s (id=%s, +NB¥%.2f)", info.get("name"), home_team, away_team, date, item_id, reward)
    return {"ok": True, "id": item_id, "nbyen_reward": reward, "nbyen_balance": new_bal}


@router.post("/api/boxscore/queue")
def register_boxscore_queue(body: BoxscoreQueueRequest, info: dict = Depends(require_any_role("rosters", "stats"))):
    home_team = body.home_team.upper()
    away_team = body.away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    if _game_in_data(body.date, home_team, away_team, body.season, body.game_type):
        raise HTTPException(status_code=409, detail=f"{home_team} vs {away_team} on {body.date} is already committed.")
    if _game_in_pending_screenshots(body.date, home_team, away_team):
        raise HTTPException(status_code=409, detail=f"A screenshot is already pending for {home_team} vs {away_team} on {body.date}.")
    if _game_in_manual_queue(body.date, home_team, away_team):
        raise HTTPException(status_code=409, detail=f"{home_team} vs {away_team} on {body.date} is already in the queue.")

    item_id = str(uuid.uuid4())[:8]
    with _manual_queue_lock:
        queue = json.loads(MANUAL_QUEUE_FILE.read_text()) if MANUAL_QUEUE_FILE.exists() else []
        queue.append({
            "id": item_id,
            "date": body.date,
            "home_team": home_team,
            "away_team": away_team,
            "season": body.season,
            "game_type": body.game_type,
            "queued_by": info.get("name"),
            "queued_at": datetime.now(timezone.utc).isoformat(),
        })
        MANUAL_QUEUE_FILE.write_text(json.dumps(queue))

    reward, new_bal = _award_submission_reward(info["name"])
    logger.info("[%s] POST boxscore/queue — %s vs %s on %s (id=%s, +NB¥%.2f)", info.get("name"), home_team, away_team, body.date, item_id, reward)
    return {"ok": True, "id": item_id, "nbyen_reward": reward, "nbyen_balance": new_bal}


@router.get("/api/boxscore/queue")
def list_manual_queue(info: dict = Depends(require_any_role("rosters", "stats"))):
    with _manual_queue_lock:
        if not MANUAL_QUEUE_FILE.exists():
            return []
        return json.loads(MANUAL_QUEUE_FILE.read_text())


@router.delete("/api/boxscore/queue/{item_id}")
def delete_manual_queue_entry(item_id: str, info: dict = Depends(require_any_role("rosters", "stats"))):
    with _manual_queue_lock:
        if not MANUAL_QUEUE_FILE.exists():
            raise HTTPException(status_code=404, detail="Queue entry not found")
        queue = json.loads(MANUAL_QUEUE_FILE.read_text())
        new_queue = [q for q in queue if q["id"] != item_id]
        if len(new_queue) == len(queue):
            raise HTTPException(status_code=404, detail="Queue entry not found")
        MANUAL_QUEUE_FILE.write_text(json.dumps(new_queue))
    logger.info("[%s] DELETE boxscore/queue/%s", info.get("name"), item_id)
    return {"ok": True}


@router.get("/api/boxscore/pending")
def list_pending_boxscores(info: dict = Depends(require_any_role("rosters", "stats"))):
    if not PENDING_BOXSCORES_DIR.exists():
        return []
    items = []
    for item_dir in PENDING_BOXSCORES_DIR.iterdir():
        meta_path = item_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        items.append(meta)
    items.sort(key=lambda x: x.get("uploaded_at") or x.get("id", ""))
    return items


@router.delete("/api/boxscore/pending/{item_id}")
def delete_pending_boxscore(item_id: str, info: dict = Depends(require_any_role("rosters", "stats"))):
    item_dir = PENDING_BOXSCORES_DIR / item_id
    if not item_dir.exists():
        raise HTTPException(status_code=404, detail="Pending item not found")
    shutil.rmtree(item_dir)
    logger.info("[%s] DELETE boxscore/pending/%s", info.get("name"), item_id)
    return {"ok": True}

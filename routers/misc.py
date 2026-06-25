import asyncio
import csv
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import ptyprocess
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .constants import (
    DATA_DIR, CAP_LEVELS_FILE, ROOKIE_SCALE_FILE, AWARDS_CONFIG_FILE,
    AWARDS_HISTORY_FILE, PLAYER_BIOS_FILE, LEAGUE_STATE_FILE,
    CALENDAR_EVENTS_FILE, CALENDAR_GAMES_FILE, TRIVIA_SCORES_PATH,
    JOIN_SUBMISSIONS_FILE, VALID_TEAMS, logger,
)
from .storage import _load_json, _save_json, log_write, _current_season_str, _current_league_year
from .auth import (
    get_token_info, has_role, require_role, require_admin,
    load_tokens, _resolve_token,
)
from .bets import _load_balances, _save_balances, _init_bal, _append_ledger, _balances_lock

router = APIRouter()

CLAUDE_BIN = Path("/home/skim/.local/bin/claude")
NBN_TODAY_DIR = Path("/home/skim/projects/nbn-today")

DISCORD_STANDINGS_WEBHOOK = (
    "https://discord.com/api/webhooks/1508510341288689876/"
    "2_KbnDbQ5HqVfT9dvXUuR2oceOLuj4gGWrJZZfqYvH3nODExFoASqsrzZ24FtP2YUyCF"
)
NBN_STANDINGS_CSV = NBN_TODAY_DIR / "standings" / "standings-history.csv"


# ── Cap levels ────────────────────────────────────────────────────────────────

class CapLevel(BaseModel):
    cap: int
    apron1: int
    apron2: int
    hard_cap: int = 0
    ntmle_amount: int = 0
    tmle_amount: int = 0
    bae_amount: int = 0
    room_amount: int = 0
    eaps: int = 0          # Estimated Average Player Salary — cap-hold threshold (§ 3.10)


@router.get("/api/cap-levels")
def get_cap_levels():
    if not CAP_LEVELS_FILE.exists():
        return {}
    return json.loads(CAP_LEVELS_FILE.read_text())


@router.put("/api/cap-levels/{season}")
def put_cap_level(season: str, body: CapLevel, info: dict = Depends(require_role("rosters"))):
    levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    levels[season] = body.model_dump()
    CAP_LEVELS_FILE.write_text(json.dumps(levels, indent=2))
    log_write(info, f"PUT cap-levels/{season} — cap={body.cap} apron1={body.apron1} apron2={body.apron2} hard_cap={body.hard_cap} ntmle={body.ntmle_amount} tmle={body.tmle_amount} bae={body.bae_amount} room={body.room_amount} eaps={body.eaps}")
    return levels[season]


# ── League year (cap/contract clock) ───────────────────────────────────────────
# The league year is derived from today's date plus any rollover overrides stored
# in league-state.json. BOD sets a season's effective start date to roll the league
# year over on a date other than the default July 1 (e.g. start it early). The stats
# clock (box scores / R build) is unaffected — that stays on _current_season_str.

class LeagueRollover(BaseModel):
    effective: str  # YYYY-MM-DD


@router.get("/api/league-year")
def get_league_year():
    rollovers = _load_json(LEAGUE_STATE_FILE, {}).get("rollovers", {})
    return {"current_season": _current_league_year(), "rollovers": rollovers}


@router.put("/api/league-year/{season}")
def put_league_rollover(season: str, body: LeagueRollover, info: dict = Depends(require_role("bod"))):
    if not re.fullmatch(r"\d{2}-\d{2}", season):
        raise HTTPException(status_code=422, detail="season must be YY-YY format")
    try:
        datetime.strptime(body.effective, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="effective must be YYYY-MM-DD")
    state = _load_json(LEAGUE_STATE_FILE, {})
    state.setdefault("rollovers", {})[season] = body.effective
    _save_json(LEAGUE_STATE_FILE, state)
    log_write(info, f"PUT league-year/{season} — effective={body.effective}")
    return {"current_season": _current_league_year(), "rollovers": state["rollovers"]}


@router.delete("/api/league-year/{season}")
def delete_league_rollover(season: str, info: dict = Depends(require_role("bod"))):
    state = _load_json(LEAGUE_STATE_FILE, {})
    if state.get("rollovers", {}).pop(season, None) is not None:
        _save_json(LEAGUE_STATE_FILE, state)
        log_write(info, f"DELETE league-year/{season} — reset to default")
    return {"current_season": _current_league_year(), "rollovers": state.get("rollovers", {})}


# ── Awards config ─────────────────────────────────────────────────────────────

_INDIVIDUAL_AWARDS = ["MVP", "DPOY", "ROTY", "6MOY", "MIP"]
# (ballot_key, tier_size, num_tiers, scoring_per_tier)
_TEAM_AWARDS = [
    ("All-NBN",     5, 3, [5, 3, 1]),
    ("All-Defense", 5, 2, [5, 3]),
    ("All-Rookie",  5, 2, [5, 3]),
]

def _compute_season_awards(ballots: dict, bios: dict) -> dict:
    missing = []

    def to_name(slug):
        bio = bios.get(slug)
        if not bio:
            missing.append(slug)
            return f"UNKNOWN({slug})"
        return bio["name"]

    result = {}

    for key in _INDIVIDUAL_AWARDS:
        scores: dict[str, int] = {}
        for team_ballot in ballots.values():
            for i, slug in enumerate(team_ballot.get(key, [])[:3]):
                if slug:
                    scores[slug] = scores.get(slug, 0) + [5, 3, 1][i]
        if scores:
            winner = max(scores, key=lambda s: scores[s])
            result[key] = [to_name(winner)]
        else:
            result[key] = []

    for ballot_key, tier_size, num_tiers, scoring in _TEAM_AWARDS:
        scores = {}
        for team_ballot in ballots.values():
            for idx, slug in enumerate(team_ballot.get(ballot_key, [])):
                if not slug:
                    continue
                tier = idx // tier_size
                if tier < len(scoring):
                    scores[slug] = scores.get(slug, 0) + scoring[tier]
        ranked = sorted(scores, key=lambda s: scores[s], reverse=True)
        if ballot_key == "All-NBN":
            for t in range(num_tiers):
                tier_slugs = ranked[t * tier_size:(t + 1) * tier_size]
                result[f"All-NBN-{t + 1}"] = [to_name(s) for s in tier_slugs]
        else:
            result[ballot_key] = [to_name(s) for s in ranked[:tier_size * num_tiers]]

    if missing:
        raise ValueError(f"Unknown player slugs in ballots: {', '.join(sorted(set(missing)))}")

    return result


class AwardsSeasonConfig(BaseModel):
    revealed: bool = False


@router.get("/api/awards-config")
def get_awards_config():
    if not AWARDS_CONFIG_FILE.exists():
        return {}
    return json.loads(AWARDS_CONFIG_FILE.read_text())


@router.put("/api/awards-config/{season}")
def put_awards_config(season: str, body: AwardsSeasonConfig, info: dict = Depends(require_admin)):
    if body.revealed:
        ballots_path = _awards_ballots_path(season)
        if not ballots_path.exists():
            raise HTTPException(status_code=422, detail=f"No ballot data for season {season} — cannot reveal")
        ballots = json.loads(ballots_path.read_text())
        bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}
        try:
            season_awards = _compute_season_awards(ballots, bios)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        history = json.loads(AWARDS_HISTORY_FILE.read_text()) if AWARDS_HISTORY_FILE.exists() else {}
        history[season] = season_awards
        AWARDS_HISTORY_FILE.write_text(json.dumps(history, indent=2))

    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    config[season] = body.model_dump()
    AWARDS_CONFIG_FILE.write_text(json.dumps(config, indent=2))
    log_write(info, f"PUT awards-config/{season} — revealed={body.revealed}")
    return config[season]


# ── Awards ballots ────────────────────────────────────────────────────────────

def _awards_ballots_path(season: str) -> Path:
    if not re.fullmatch(r'\d{2}-\d{2}', season):
        raise HTTPException(status_code=422, detail="season must be YY-YY format")
    return DATA_DIR / f"awards-ballots-{season}.json"


@router.get("/api/awards-ballots/{season}")
def get_awards_ballots(season: str, info: Optional[dict] = None, authorization: Optional[str] = Header(None)):
    path = _awards_ballots_path(season)

    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    revealed = config.get(season, {}).get("revealed", False)
    if not revealed:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=403, detail="Awards not yet revealed")
        token = authorization[7:]
        tokens = load_tokens()
        token_info = tokens.get(token)
        if not token_info or not (has_role(token_info, "bod") or has_role(token_info, "admin")):
            raise HTTPException(status_code=403, detail="Awards not yet revealed")

    if not path.exists():
        raise HTTPException(status_code=404, detail="No ballot data for this season")
    return json.loads(path.read_text())


@router.put("/api/awards-ballots/{season}")
def put_awards_ballots(season: str, body: dict, info: dict = Depends(require_role("rosters"))):
    path = _awards_ballots_path(season)
    path.write_text(json.dumps(body, indent=2))
    log_write(info, f"PUT awards-ballots/{season}")
    return {"ok": True}


@router.get("/api/awards-ballots/{season}/{team}")
def get_team_ballot(season: str, team: str, authorization: Optional[str] = Header(None)):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = _awards_ballots_path(season)
    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    revealed = config.get(season, {}).get("revealed", False)
    if not revealed:
        info = _resolve_token(authorization)
        if not info:
            raise HTTPException(status_code=403, detail="Unauthorized")
        roles = set(info.get("roles", []))
        if not (has_role(info, "admin") or has_role(info, "bod") or team.lower() in roles):
            raise HTTPException(status_code=403, detail="Unauthorized")
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get(team, {})


@router.put("/api/awards-ballots/{season}/{team}")
def put_team_ballot(season: str, team: str, body: dict, authorization: Optional[str] = Header(None)):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    info = _resolve_token(authorization)
    if not info:
        raise HTTPException(status_code=403, detail="Unauthorized")
    roles = set(info.get("roles", []))
    if not (has_role(info, "admin") or has_role(info, "rosters") or team.lower() in roles):
        raise HTTPException(status_code=403, detail="Unauthorized")
    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    if config.get(season, {}).get("revealed", False):
        raise HTTPException(status_code=403, detail="Cannot edit a revealed season")
    path = _awards_ballots_path(season)
    ballots = json.loads(path.read_text()) if path.exists() else {}
    ballots[team] = body
    path.write_text(json.dumps(ballots, indent=2))
    log_write(info, f"PUT awards-ballots/{season}/{team}")
    return {"ok": True, "team": team}


@router.delete("/api/awards-ballots/{season}/{team}")
def delete_team_ballot(season: str, team: str, info: dict = Depends(require_role("rosters"))):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = _awards_ballots_path(season)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No ballot data for this season")
    ballots = json.loads(path.read_text())
    if team not in ballots:
        raise HTTPException(status_code=404, detail=f"No ballot for {team}")
    del ballots[team]
    path.write_text(json.dumps(ballots, indent=2))
    log_write(info, f"DELETE awards-ballots/{season}/{team}")
    return {"ok": True}


# ── Rookie scale ──────────────────────────────────────────────────────────────

@router.get("/api/rookie-scale")
def get_rookie_scale():
    if not ROOKIE_SCALE_FILE.exists():
        return {}
    return json.loads(ROOKIE_SCALE_FILE.read_text())


@router.put("/api/rookie-scale/{year}")
def put_rookie_scale(year: int, body: list[list[int]], info: dict = Depends(require_role("rosters"))):
    scale = json.loads(ROOKIE_SCALE_FILE.read_text()) if ROOKIE_SCALE_FILE.exists() else {}
    scale[str(year)] = body
    ROOKIE_SCALE_FILE.write_text(json.dumps(scale, indent=2))
    log_write(info, f"PUT rookie-scale/{year} — {len(body)} picks")
    return scale[str(year)]


# ── Trivia scores ─────────────────────────────────────────────────────────────

def load_trivia_scores() -> list:
    return _load_json(TRIVIA_SCORES_PATH, [])


def save_trivia_scores(scores: list):
    _save_json(TRIVIA_SCORES_PATH, scores)


class TriviaScoreSubmit(BaseModel):
    score: int


class TriviaAnswerSubmit(BaseModel):
    streak: int


@router.get("/api/trivia/scores")
def get_trivia_scores():
    scores = load_trivia_scores()
    best: dict[str, dict] = {}
    for s in scores:
        name = s.get("name", "?")
        if name not in best or s["score"] > best[name]["score"]:
            best[name] = s
    return sorted(best.values(), key=lambda s: s["score"], reverse=True)[:20]


@router.post("/api/trivia/scores")
def post_trivia_score(body: TriviaScoreSubmit, info: dict = Depends(get_token_info)):
    if not info.get("name"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if body.score < 0:
        raise HTTPException(status_code=422, detail="Score must be non-negative")
    scores = load_trivia_scores()
    scores.append({
        "name": info["name"],
        "score": body.score,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    save_trivia_scores(scores)
    log_write(info, f"POST trivia/scores — score={body.score}")
    return {"ok": True}


@router.post("/api/trivia/answer")
def post_trivia_answer(body: TriviaAnswerSubmit, info: dict = Depends(get_token_info)):
    """Award NB¥ for a correct trivia answer. Reward = 2^(streak-1), capped at 512;
    no reward past the 10th in a row (streak keeps going for the leaderboard only)."""
    if not info.get("name"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if body.streak < 1:
        raise HTTPException(status_code=422, detail="streak must be >= 1")
    reward = 0.0 if body.streak > 10 else float(min(2 ** (body.streak - 1), 512))
    if reward == 0.0:
        return {"ok": True, "reward": 0.0, "balance": _load_balances().get(info["name"], 0.0)}
    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, info["name"])
        balances[info["name"]] = round(balances[info["name"]] + reward, 2)
        _save_balances(balances)
    ts = datetime.now(timezone.utc).isoformat()
    _append_ledger([{
        "ts": ts, "member": info["name"], "delta": reward,
        "balance": balances[info["name"]],
        "reason": f"Trivia streak {body.streak} reward",
    }])
    log_write(info, f"POST trivia/answer — streak={body.streak} reward={reward}")
    return {"ok": True, "reward": reward, "balance": balances[info["name"]]}


# ── Calendar events ───────────────────────────────────────────────────────────

class CalendarEventIn(BaseModel):
    date: str
    label: str


def _load_calendar_events() -> list[dict]:
    return _load_json(CALENDAR_EVENTS_FILE, [])


def _save_calendar_events(events: list[dict]):
    _save_json(CALENDAR_EVENTS_FILE, events)


@router.get("/api/calendar/events")
def get_calendar_events():
    events = _load_calendar_events()
    return sorted(events, key=lambda e: e["date"])


@router.post("/api/calendar/events")
def create_calendar_event(body: CalendarEventIn, info: dict = Depends(require_role("bod"))):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if not body.label.strip():
        raise HTTPException(status_code=422, detail="label is required")
    events = _load_calendar_events()
    event_id = secrets.token_hex(8)
    event = {"id": event_id, "date": body.date, "label": body.label.strip()}
    events.append(event)
    _save_calendar_events(events)
    log_write(info, f"POST calendar/events — {body.date} {body.label!r}")
    return event


@router.delete("/api/calendar/events/{event_id}")
def delete_calendar_event(event_id: str, info: dict = Depends(require_role("bod"))):
    events = _load_calendar_events()
    remaining = [e for e in events if e["id"] != event_id]
    if len(remaining) == len(events):
        raise HTTPException(status_code=404, detail="Event not found")
    _save_calendar_events(remaining)
    log_write(info, f"DELETE calendar/events/{event_id}")
    return {"ok": True}


# ── Calendar games ────────────────────────────────────────────────────────────

class CalendarGameIn(BaseModel):
    date: str
    home_team: str
    away_team: str
    note: str = ""


def _load_calendar_games() -> list[dict]:
    return _load_json(CALENDAR_GAMES_FILE, [])


def _save_calendar_games(games: list[dict]):
    _save_json(CALENDAR_GAMES_FILE, games)


@router.get("/api/calendar/games")
def get_calendar_games():
    return sorted(_load_calendar_games(), key=lambda g: g["date"])


@router.post("/api/calendar/games")
def create_calendar_game(body: CalendarGameIn, info: dict = Depends(require_role("bod"))):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    ht = body.home_team.strip().upper()
    at = body.away_team.strip().upper()
    if ht not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown home team: {ht}")
    if at not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown away team: {at}")
    if ht == at:
        raise HTTPException(status_code=422, detail="home_team and away_team must differ")
    games = _load_calendar_games()
    game_id = secrets.token_hex(8)
    game = {"id": game_id, "date": body.date, "home_team": ht, "away_team": at, "note": body.note.strip()}
    games.append(game)
    _save_calendar_games(games)
    log_write(info, f"POST calendar/games — {body.date} {at}@{ht}" + (f" ({body.note})" if body.note else ""))
    return game


@router.delete("/api/calendar/games/{game_id}")
def delete_calendar_game(game_id: str, info: dict = Depends(require_role("bod"))):
    games = _load_calendar_games()
    remaining = [g for g in games if g["id"] != game_id]
    if len(remaining) == len(games):
        raise HTTPException(status_code=404, detail="Game not found")
    _save_calendar_games(remaining)
    log_write(info, f"DELETE calendar/games/{game_id}")
    return {"ok": True}


# ── Discord standings webhook ─────────────────────────────────────────────────

def _season_label(season: str) -> str:
    parts = season.split("-")
    if len(parts) == 2:
        return f"20{parts[0]}–{parts[1]}"
    return season


def _build_standings_payload(season: str) -> dict:
    if not NBN_STANDINGS_CSV.exists():
        raise FileNotFoundError("standings-history.csv not found")

    with open(NBN_STANDINGS_CSV, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("SEASON", "").strip() == season]

    if not rows:
        raise ValueError(f"No standings data found for season {season!r}")

    east = sorted(
        [r for r in rows if r["SEED"].startswith("East")],
        key=lambda r: int(r["SEED_NUM"]),
    )
    west = sorted(
        [r for r in rows if r["SEED"].startswith("West")],
        key=lambda r: int(r["SEED_NUM"]),
    )

    has_results = any(
        r.get("PLAYOFF_RESULT", "").strip() not in ("", "Missed")
        for r in rows
    )

    RESULT_CODE = {
        "Champion":    "CHAMP",
        "Runner-Up":   "FINAL",
        "Conf Finals": "CF   ",
        "First Round": "R1   ",
        "Missed":      "",
        "":            "",
    }

    def fmt_conf(teams: list[dict]) -> str:
        hdr = f" #  Team    W    L    PCT"
        if has_results:
            hdr += "   Result"
        lines = [hdr, "─" * len(hdr)]
        for t in teams:
            seed = int(t["SEED_NUM"])
            w    = int(t["W"])
            l    = int(t["L"])
            pct  = float(t["PCT"])
            res  = t.get("PLAYOFF_RESULT", "").strip()
            pct_str = f"{pct:.3f}"[1:]
            row  = f"{seed:2}  {t['TEAM']:<4}  {w:3}  {l:3}  {pct_str}"
            if has_results:
                row += f"  {RESULT_CODE.get(res, ''):5}"
            lines.append(row)
            if seed == 8 and seed < len(teams):
                lines.append("·" * 26)
        return "```\n" + "\n".join(lines) + "\n```"

    now = datetime.now(timezone.utc).isoformat()
    return {
        "username": "NBN League Office",
        "avatar_url": "https://nbn.today/logo.png",
        "embeds": [{
            "title": f"\U0001f4ca NBN Standings — {_season_label(season)}",
            "color": 0x3b82f6,
            "fields": [
                {"name": "\U0001f535 Eastern Conference", "value": fmt_conf(east)},
                {"name": "\U0001f534 Western Conference", "value": fmt_conf(west)},
            ],
            "footer": {"text": "Nothing But Net · nbn.today/standings"},
            "timestamp": now,
        }],
    }


@router.post("/api/admin/discord/post-standings")
def post_standings_to_discord(
    season: str = Query(..., description="Season code, e.g. 25-26"),
    info: dict = Depends(require_role("admin")),
):
    if not re.match(r"^\d{2}-\d{2}$", season):
        raise HTTPException(status_code=422, detail="season must be YY-YY format, e.g. 25-26")
    try:
        payload = _build_standings_payload(season)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        resp = httpx.post(DISCORD_STANDINGS_WEBHOOK, json=payload, timeout=10)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Discord request failed: {e}")

    if not resp.is_success:
        raise HTTPException(
            status_code=502,
            detail=f"Discord returned {resp.status_code}: {resp.text[:300]}",
        )

    log_write(info, f"POST discord/standings — season={season}")
    return {"ok": True, "season": season}


# ── Join interest form ────────────────────────────────────────────────────────

class JoinSubmission(BaseModel):
    discord: str


@router.post("/api/join")
def post_join(body: JoinSubmission, request: Request):
    discord = body.discord.strip()
    if not discord:
        raise HTTPException(status_code=422, detail="discord is required")
    if len(discord) > 100:
        raise HTTPException(status_code=422, detail="discord name too long")
    forwarded_for = request.headers.get("x-forwarded-for", "")
    ip = forwarded_for.split(",")[0].strip() if forwarded_for else (request.client.host if request.client else None)
    submissions = _load_json(JOIN_SUBMISSIONS_FILE, [])
    submissions.append({
        "id": secrets.token_hex(8),
        "discord": discord,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ip": ip,
        "user_agent": request.headers.get("user-agent"),
        "referrer": request.headers.get("referer"),
    })
    _save_json(JOIN_SUBMISSIONS_FILE, submissions)
    return {"ok": True}


@router.get("/api/join")
def get_join_submissions(info: dict = Depends(require_admin)):
    return _load_json(JOIN_SUBMISSIONS_FILE, [])


# ── WebSocket: claude session ─────────────────────────────────────────────────

@router.websocket("/api/ws/claude")
async def ws_claude(websocket: WebSocket, token: str = Query(...)):
    tokens = load_tokens()
    info = tokens.get(token)
    if not info or not has_role(info, "admin"):
        await websocket.close(code=4003)
        return

    await websocket.accept()
    log_write(info, "WS claude session started")

    proc = ptyprocess.PtyProcess.spawn(
        [str(CLAUDE_BIN), "/parse-boxscores"],
        cwd=str(NBN_TODAY_DIR),
        env={**os.environ, "TERM": "xterm-256color", "COLUMNS": "220", "LINES": "50"},
        dimensions=(50, 220),
    )

    loop = asyncio.get_event_loop()

    async def pty_to_ws():
        while proc.isalive():
            try:
                data = await loop.run_in_executor(None, proc.read, 4096)
                await websocket.send_bytes(data)
            except (EOFError, Exception):
                break
        try:
            await websocket.close()
        except Exception:
            pass

    async def ws_to_pty():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"]:
                    proc.write(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        ctrl = json.loads(msg["text"])
                        if ctrl.get("type") == "resize":
                            proc.setwinsize(ctrl["rows"], ctrl["cols"])
                    except Exception:
                        proc.write(msg["text"].encode())
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            if proc.isalive():
                proc.terminate(force=True)
            log_write(info, "WS claude session ended")

    await asyncio.gather(pty_to_ws(), ws_to_pty())

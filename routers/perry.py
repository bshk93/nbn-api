import csv
import json
import random
import threading
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .constants import DATA_DIR, PLAYER_BIOS_FILE, logger
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, require_admin, load_members
from .bets import (
    _load_balances, _save_balances, _init_bal, _append_ledger,
    _balances_lock, DISCORD_BETS_WEBHOOK,
)

router = APIRouter()

PERRY_STATE_FILE   = DATA_DIR / "perry-state.json"
PERRY_ARCHIVE_FILE = DATA_DIR / "perry-archive.json"
NBN_TODAY_DIR    = Path("/home/skim/projects/nbn-today")
PLAYER_SEASONS_CSV = NBN_TODAY_DIR / "players" / "player_seasons.csv"

_perry_lock = threading.Lock()

SLOTS = ["PG", "SG", "SF", "PF", "C", "6MAN"]
PRIZES = [100.0, 50.0, 25.0]

TEAM_NAMES = {
    "ATL": "Atlanta Hawks",    "BKN": "Brooklyn Nets",       "BOS": "Boston Celtics",
    "CHA": "Charlotte Hornets","CHI": "Chicago Bulls",        "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",       "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers",      "LAL": "Los Angeles Lakers",   "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",       "MIL": "Milwaukee Bucks",      "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",  "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",    "PHI": "Philadelphia 76ers",   "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors",  "UTA": "Utah Jazz",            "WAS": "Washington Wizards",
}

ALL_TEAMS = list(TEAM_NAMES.keys())


# ── Data helpers ──────────────────────────────────────────────────────────────

def _load_perry() -> dict:
    return _load_json(PERRY_STATE_FILE, {})


def _save_perry(state: dict):
    _save_json(PERRY_STATE_FILE, state)


def _today_et() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _extract_stats(row: dict) -> dict:
    try:
        g = int(row.get("G", 0) or 0)
    except ValueError:
        g = 0

    def avg(col):
        try:
            return round(float(row.get(col, 0) or 0) / g, 1) if g > 0 else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    def total(col):
        try:
            return int(row.get(col, 0) or 0)
        except ValueError:
            return 0

    return {"G": g, "PTS": avg("PTS"), "REB": avg("REB"), "AST": avg("AST"),
            "STL": avg("STL"), "BLK": avg("BLK"), "3PM": total("3PM")}


def _build_team_players(teams: list[str]) -> dict:
    bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}
    team_set = set(teams)

    # For each (team, slug): keep the row with the highest GMSC
    best: dict[tuple, dict] = {}
    with open(PLAYER_SEASONS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            team = row.get("TEAM", "").strip()
            if team not in team_set:
                continue
            slug = row.get("SLUG", "").strip()
            if not slug:
                continue
            try:
                gmsc = float(row.get("GMSC", 0) or 0)
            except ValueError:
                gmsc = 0.0
            key = (team, slug)
            if key not in best or gmsc > best[key]["gmsc"]:
                best[key] = {
                    "slug": slug,
                    "name": row.get("PLAYER", "").strip(),
                    "season": row.get("SEASON", "").strip(),
                    "gmsc": round(gmsc, 1),
                    "stats": _extract_stats(row),
                }

    team_players: dict[str, list] = {t: [] for t in teams}
    for (team, slug), entry in best.items():
        bio = bios.get(slug, {})
        entry["pos"] = bio.get("pos", [])
        team_players[team].append(entry)

    for team in teams:
        team_players[team].sort(key=lambda p: p["gmsc"], reverse=True)

    return team_players


def _compute_solution(teams: list[str], team_players: dict) -> tuple[dict | None, float]:
    best_score = -1.0
    best_assignment: dict | None = None

    for perm in permutations(range(6)):
        score = 0.0
        valid = True
        assignment: dict = {}
        used_slugs: set = set()

        for slot_idx, team_idx in enumerate(perm):
            slot = SLOTS[slot_idx]
            team = teams[team_idx]
            players = team_players[team]

            if slot == "6MAN":
                eligible = [p for p in players if p["slug"] not in used_slugs]
            else:
                eligible = [p for p in players if slot in p["pos"] and p["slug"] not in used_slugs]

            if not eligible:
                valid = False
                break

            best_p = max(eligible, key=lambda p: p["gmsc"])
            score += best_p["gmsc"]
            assignment[slot] = {**best_p, "team": team}
            used_slugs.add(best_p["slug"])

        if valid and score > best_score:
            best_score = score
            best_assignment = assignment

    return best_assignment, round(best_score, 1)


def _generate_puzzle(date_str: str) -> dict:
    rng = random.Random(date_str)
    teams = rng.sample(ALL_TEAMS, 6)
    team_players = _build_team_players(teams)
    solution, solution_score = _compute_solution(teams, team_players)
    return {
        "date": date_str,
        "teams": teams,
        "team_names": {t: TEAM_NAMES[t] for t in teams},
        "team_players": team_players,
        "solution": solution,
        "solution_score": solution_score,
        "entries": [],
    }


def _get_or_create_state() -> dict:
    today = _today_et()
    state = _load_perry()
    if state.get("date") != today:
        if state.get("date"):
            _archive_state(state)
        state = _generate_puzzle(today)
        _save_perry(state)
    return state


def _leaderboard(entries: list[dict]) -> list[dict]:
    ranked = sorted(entries, key=lambda e: (-e["score"], e["submitted_at"]))
    return [
        {"rank": i + 1, "member": e["member"], "score": e["score"], "submitted_at": e["submitted_at"]}
        for i, e in enumerate(ranked)
    ]


def _score_lineup(lineup_raw: dict, team_players: dict) -> tuple[dict, float]:
    scored: dict = {}
    total = 0.0
    for slot, pick in lineup_raw.items():
        team = pick["team"]
        slug = pick["slug"]
        player = next((p for p in team_players.get(team, []) if p["slug"] == slug), None)
        if player is None:
            raise HTTPException(status_code=422, detail=f"Player '{slug}' not found for team '{team}'")
        scored[slot] = {**player, "team": team}
        total += player["gmsc"]
    return scored, round(total, 1)


# ── Discord ───────────────────────────────────────────────────────────────────

def _discord_post(embed: dict) -> None:
    try:
        httpx.post(DISCORD_BETS_WEBHOOK, json={"embeds": [embed]}, timeout=5)
    except Exception as exc:
        logger.warning("Perry Discord webhook failed: %s", exc)


def _discord_submission(member: str, score: float, solution_score: float) -> None:
    pct = round(score / solution_score * 100, 1) if solution_score > 0 else 0.0
    _discord_post({
        "color": 0x3b82f6,
        "description": (
            f"**{member}** just completed the **Daily Perry Game**!\n"
            f"Score: **{score:,.1f}** / {solution_score:,.1f} ({pct}% of optimal)\n\n"
            f"Think you can beat it? **[nbn.today/perry](https://nbn.today/perry)**"
        ),
    })


def _discord_daily_results(state: dict) -> None:
    lb = _leaderboard(state["entries"])[:3]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"**Perry Game — {state['date']} Results**\n"]
    for i, entry in enumerate(lb):
        prize = int(PRIZES[i])
        lines.append(f"{medals[i]} **{entry['member']}** — {entry['score']:,.1f} pts  (+NB¥{prize})")
    if not lb:
        lines.append("No entries today.")
    lines.append("\n**Optimal Solution:**")
    sol = state.get("solution") or {}
    for slot in SLOTS:
        p = sol.get(slot)
        if p:
            lines.append(f"**{slot}**: {p['name']} ({p['team']}, {p['season']}) — {p['gmsc']:,.1f}")
    lines.append(f"**Solution score**: {state['solution_score']:,.1f}")
    lines.append(f"\nNew game available now! **[nbn.today/perry](https://nbn.today/perry)**")
    _discord_post({"color": 0xf59e0b, "description": "\n".join(lines)})


def _award_prizes(state: dict) -> None:
    lb = _leaderboard(state["entries"])[:3]
    if not lb:
        return
    ts = datetime.now(timezone.utc).isoformat()
    ledger_entries = []
    with _balances_lock:
        balances = _load_balances()
        for i, entry in enumerate(lb):
            prize = PRIZES[i]
            name = entry["member"]
            _init_bal(balances, name)
            balances[name] = round(balances[name] + prize, 2)
            ledger_entries.append({
                "ts": ts, "member": name, "delta": prize,
                "balance": balances[name],
                "reason": f"Perry Game daily prize (#{i + 1}) — {state['date']}",
            })
        _save_balances(balances)
    _append_ledger(ledger_entries)


# ── Archive ───────────────────────────────────────────────────────────────────

def _archive_state(state: dict) -> None:
    """Save completed day's state to archive, stripping team_players to save space."""
    archive = _load_json(PERRY_ARCHIVE_FILE, {})
    archive[state["date"]] = {
        "date": state["date"],
        "teams": state["teams"],
        "team_names": state["team_names"],
        "solution": state["solution"],
        "solution_score": state["solution_score"],
        "entries": state["entries"],
    }
    _save_json(PERRY_ARCHIVE_FILE, archive)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/perry/today")
def get_perry_today(authorization: Optional[str] = Header(None)):
    with _perry_lock:
        state = _get_or_create_state()

    member = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        members = load_members()
        for name, data in members.items():
            if data.get("token") == token:
                member = name
                break

    my_entry = None
    if member:
        my_entry = next((e for e in state["entries"] if e["member"] == member), None)

    resp = {
        "date": state["date"],
        "teams": [
            {"abbr": t, "name": state["team_names"][t], "players": state["team_players"].get(t, [])}
            for t in state["teams"]
        ],
        "leaderboard": _leaderboard(state["entries"]),
        "solution_score": state["solution_score"],
        "your_entry": my_entry,
    }
    if my_entry:
        resp["solution"] = state["solution"]
    return resp


class PerryLineupPick(BaseModel):
    team: str
    slug: str


class PerrySubmit(BaseModel):
    lineup: dict[str, PerryLineupPick]


@router.post("/api/perry/submit")
def post_perry_submit(body: PerrySubmit, info: dict = Depends(get_token_info)):
    if not info.get("name"):
        raise HTTPException(status_code=401, detail="Authentication required")
    member = info["name"]

    with _perry_lock:
        state = _get_or_create_state()

        if any(e["member"] == member for e in state["entries"]):
            raise HTTPException(status_code=409, detail="You have already submitted a lineup today")

        lineup_raw = {k: v.model_dump() for k, v in body.lineup.items()}

        if set(lineup_raw.keys()) != set(SLOTS):
            raise HTTPException(status_code=422, detail=f"Must submit exactly these slots: {SLOTS}")

        today_teams = set(state["teams"])
        used_teams = [pick["team"] for pick in lineup_raw.values()]
        if set(used_teams) != today_teams or len(used_teams) != 6:
            raise HTTPException(status_code=422, detail="Each of today's 6 teams must be used exactly once")

        team_players = state["team_players"]
        for slot, pick in lineup_raw.items():
            team = pick["team"]
            slug = pick["slug"]
            player = next((p for p in team_players.get(team, []) if p["slug"] == slug), None)
            if player is None:
                raise HTTPException(status_code=422, detail=f"Player '{slug}' did not play for '{team}'")
            if slot != "6MAN" and slot not in player["pos"]:
                raise HTTPException(status_code=422, detail=f"Player '{slug}' is not eligible for {slot}")

        scored_lineup, total_score = _score_lineup(lineup_raw, team_players)

        ts = datetime.now(timezone.utc).isoformat()
        state["entries"].append({
            "member": member,
            "submitted_at": ts,
            "lineup": scored_lineup,
            "score": total_score,
        })
        _save_perry(state)

    pct = round(total_score / state["solution_score"] * 100, 1) if state["solution_score"] > 0 else 0.0
    _discord_submission(member, total_score, state["solution_score"])
    log_write(info, f"POST perry/submit — score={total_score}")

    return {
        "score": total_score,
        "solution_score": state["solution_score"],
        "pct_of_solution": pct,
        "lineup": scored_lineup,
        "solution": state["solution"],
        "leaderboard": _leaderboard(state["entries"]),
    }


@router.get("/api/perry/history")
def get_perry_history():
    archive = _load_json(PERRY_ARCHIVE_FILE, {})
    result = []
    for date_str, day in sorted(archive.items(), reverse=True):
        lb = _leaderboard(day["entries"])
        result.append({
            "date": date_str,
            "teams": day["teams"],
            "solution_score": day["solution_score"],
            "entries": len(day["entries"]),
            "winner": lb[0]["member"] if lb else None,
            "winner_score": lb[0]["score"] if lb else None,
        })
    return result


@router.get("/api/perry/history/{date}")
def get_perry_history_date(date: str):
    archive = _load_json(PERRY_ARCHIVE_FILE, {})
    day = archive.get(date)
    if not day:
        raise HTTPException(status_code=404, detail=f"No results for {date}")
    return {**day, "leaderboard": _leaderboard(day["entries"])}


@router.post("/api/perry/admin/reset")
def perry_admin_reset(info: dict = Depends(require_admin)):
    with _perry_lock:
        old = _load_perry()
        if old and old.get("date"):
            _archive_state(old)
            if old.get("entries"):
                _award_prizes(old)
                _discord_daily_results(old)
        today = _today_et()
        new_state = _generate_puzzle(today)
        _save_perry(new_state)
    log_write(info, f"POST perry/admin/reset — {today}, teams={new_state['teams']}")
    return {"ok": True, "date": today, "teams": new_state["teams"], "solution_score": new_state["solution_score"]}

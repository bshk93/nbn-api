import csv
import json
import random
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .league_time import league_today, league_today_str

from .constants import DERIVED_DIR, DATA_DIR, PLAYER_BIOS_FILE, logger
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, require_admin, load_members
from .bets import (
    _load_balances, _save_balances, _init_bal, _append_ledger,
    _balances_lock, DISCORD_BETS_WEBHOOK,
)

router = APIRouter()

# ── Config ─────────────────────────────────────────────────────────────────────
POELTL_STATE_FILE   = DATA_DIR / "poeltl-state.json"
POELTL_ARCHIVE_FILE = DATA_DIR / "poeltl-archive.json"
NBN_TODAY_DIR       = Path("/home/skim/projects/nbn-today")
PLAYER_SEASONS_CSV  = DERIVED_DIR / "players" / "player_seasons.csv"

POELTL_SEASON      = "25-26"   # update each season
POELTL_MIN_GAMES   = 40        # minimum games to be eligible
POELTL_NO_REPEAT_WINDOW = 50   # don't reuse an answer from the last N archived puzzles
POELTL_MAX_GUESSES = 7
POELTL_TIME_LIMIT  = 300       # seconds (5-min anti-cheat window)
NBY_POELTL_REWARD  = 50.0

# 🟨 closeness thresholds — within these values = yellow, outside = black
HEIGHT_CLOSE_IN = 2     # inches
AGE_CLOSE_YR    = 2     # years
PPG_CLOSE       = 3.0
RPG_CLOSE       = 2.0
APG_CLOSE       = 2.0
FG3_CLOSE       = 8.0   # percentage points

_poeltl_lock = threading.Lock()

# ── Team / conference / division map ──────────────────────────────────────────
CONF_DIV: dict[str, tuple[str, str]] = {
    "ATL": ("East", "Southeast"), "BKN": ("East", "Atlantic"),
    "BOS": ("East", "Atlantic"),  "CHA": ("East", "Southeast"),
    "CHI": ("East", "Central"),   "CLE": ("East", "Central"),
    "DET": ("East", "Central"),   "IND": ("East", "Central"),
    "MIA": ("East", "Southeast"), "MIL": ("East", "Central"),
    "NYK": ("East", "Atlantic"),  "ORL": ("East", "Southeast"),
    "PHI": ("East", "Atlantic"),  "TOR": ("East", "Atlantic"),
    "WAS": ("East", "Southeast"),
    "DAL": ("West", "Southwest"), "DEN": ("West", "Northwest"),
    "GSW": ("West", "Pacific"),   "HOU": ("West", "Southwest"),
    "LAC": ("West", "Pacific"),   "LAL": ("West", "Pacific"),
    "MEM": ("West", "Southwest"), "MIN": ("West", "Northwest"),
    "NOP": ("West", "Southwest"), "OKC": ("West", "Northwest"),
    "PHX": ("West", "Pacific"),   "POR": ("West", "Northwest"),
    "SAC": ("West", "Pacific"),   "SAS": ("West", "Southwest"),
    "UTA": ("West", "Northwest"),
}


# ── Data helpers ───────────────────────────────────────────────────────────────

def _height_to_inches(h: str) -> Optional[int]:
    m = re.match(r"(\d+)'(\d+)\"", h or "")
    return int(m.group(1)) * 12 + int(m.group(2)) if m else None


def _compute_age(dob_str: str) -> Optional[int]:
    if not dob_str:
        return None
    try:
        dob = date.fromisoformat(dob_str)
        today = league_today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except ValueError:
        return None


def _today_et() -> str:
    return league_today_str()


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def _build_player_pool() -> list[dict]:
    bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}

    # Aggregate stats across all teams for the target season (handles mid-season trades)
    agg: dict[str, dict] = {}
    with open(PLAYER_SEASONS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("SEASON", "").strip() != POELTL_SEASON:
                continue
            slug = row.get("SLUG", "").strip()
            if not slug:
                continue

            if slug not in agg:
                agg[slug] = {
                    "G": 0, "PTS": 0, "REB": 0, "AST": 0, "3PM": 0, "3PA": 0,
                    "last_date": "", "team": "",
                    "csv_name": row.get("PLAYER", "").strip(),
                }

            agg[slug]["G"]   += _safe_int(row.get("G"))
            agg[slug]["PTS"] += _safe_int(row.get("PTS"))
            agg[slug]["REB"] += _safe_int(row.get("REB"))
            agg[slug]["AST"] += _safe_int(row.get("AST"))
            agg[slug]["3PM"] += _safe_int(row.get("3PM"))
            agg[slug]["3PA"] += _safe_int(row.get("3PA"))

            last_date = row.get("LAST_DATE", "").strip()
            if last_date > agg[slug]["last_date"]:
                agg[slug]["last_date"] = last_date
                agg[slug]["team"] = row.get("TEAM", "").strip()

    pool = []
    for slug, data in agg.items():
        g = data["G"]
        if g < POELTL_MIN_GAMES:
            continue

        bio = bios.get(slug, {})
        height_str = bio.get("height", "")
        height_in = _height_to_inches(height_str)
        age = _compute_age(bio.get("dob", ""))

        if height_in is None or age is None:
            continue

        fg3pct = round(data["3PM"] / data["3PA"] * 100, 1) if data["3PA"] > 0 else 0.0

        pool.append({
            "slug":       slug,
            "name":       bio.get("name") or data["csv_name"],
            "team":       data["team"],
            "height_in":  height_in,
            "height_str": height_str,
            "age":        age,
            "ppg":        round(data["PTS"] / g, 1),
            "rpg":        round(data["REB"] / g, 1),
            "apg":        round(data["AST"] / g, 1),
            "fg3pct":     fg3pct,
            "g":          g,
            "pos":        bio.get("pos", []),
        })

    pool.sort(key=lambda p: p["name"])
    return pool


# ── Comparison ─────────────────────────────────────────────────────────────────

def _team_match(g: str, a: str) -> str:
    if g == a:
        return "exact"
    gc, gd = CONF_DIV.get(g, ("?", "?"))
    ac, ad = CONF_DIV.get(a, ("?", "?"))
    if gc == ac and gd == ad:
        return "division"
    if gc == ac:
        return "conference"
    return "none"


def _num_result(guess_val: float, answer_val: float, close: float, exact_thresh: float) -> dict:
    diff = abs(guess_val - answer_val)
    # Arrow points toward the answer: ↑ = answer is higher, ↓ = answer is lower
    direction = "↓" if guess_val > answer_val else ("↑" if guess_val < answer_val else "=")
    color = "green" if diff <= exact_thresh else ("yellow" if diff <= close else "grey")
    return {"direction": direction, "color": color}


def _compare(guess: dict, answer: dict) -> dict:
    return {
        "team": {
            "value": guess["team"],
            "match": _team_match(guess["team"], answer["team"]),
        },
        "height": {
            "value":  guess["height_str"],
            "inches": guess["height_in"],
            **_num_result(guess["height_in"], answer["height_in"], HEIGHT_CLOSE_IN, 0),
        },
        "age": {
            "value": guess["age"],
            **_num_result(float(guess["age"]), float(answer["age"]), AGE_CLOSE_YR, 0),
        },
        "ppg": {
            "value": guess["ppg"],
            **_num_result(guess["ppg"], answer["ppg"], PPG_CLOSE, 0.5),
        },
        "rpg": {
            "value": guess["rpg"],
            **_num_result(guess["rpg"], answer["rpg"], RPG_CLOSE, 0.5),
        },
        "apg": {
            "value": guess["apg"],
            **_num_result(guess["apg"], answer["apg"], APG_CLOSE, 0.5),
        },
        "fg3pct": {
            "value": guess["fg3pct"],
            **_num_result(guess["fg3pct"], answer["fg3pct"], FG3_CLOSE, 2.0),
        },
    }


# ── Grid ───────────────────────────────────────────────────────────────────────

_GRID_KEYS = ["team", "height", "age", "ppg", "rpg", "apg", "fg3pct"]


def _grid_emoji(result: dict, key: str) -> str:
    if key == "team":
        m = result["team"]["match"]
        return "🟩" if m == "exact" else ("🟨" if m == "division" else "⬛")
    c = result[key]["color"]
    return "🟩" if c == "green" else ("🟨" if c == "yellow" else "⬛")


def _build_grid(guesses: list[dict]) -> str:
    return "\n".join(
        "".join(_grid_emoji(g["result"], k) for k in _GRID_KEYS)
        for g in guesses
    )


# ── State helpers ──────────────────────────────────────────────────────────────

def _load_poeltl() -> dict:
    return _load_json(POELTL_STATE_FILE, {})


def _save_poeltl(state: dict):
    _save_json(POELTL_STATE_FILE, state)


def _recent_answer_slugs(window: int = POELTL_NO_REPEAT_WINDOW) -> set[str]:
    """Answer slugs from the most recent `window` archived puzzle dates."""
    archive = _load_json(POELTL_ARCHIVE_FILE, {})
    recent_dates = sorted(archive.keys(), reverse=True)[:window]
    slugs = set()
    for d in recent_dates:
        slug = (archive.get(d) or {}).get("answer_slug", "")
        if slug:
            slugs.add(slug)
    return slugs


def _generate_puzzle(date_str: str) -> dict:
    rng = random.Random(date_str)
    pool = _build_player_pool()
    recent = _recent_answer_slugs()
    eligible = [p for p in pool if p["slug"] not in recent]
    # Fall back to the full pool if exclusions somehow leave nothing.
    answer = rng.choice(eligible or pool)
    return {
        "date":             date_str,
        "answer_slug":      answer["slug"],
        "answer_data":      answer,
        "player_pool":      [{"slug": p["slug"], "name": p["name"]} for p in pool],
        "player_pool_full": {p["slug"]: p for p in pool},
        "in_progress":      {},
        "entries":          [],
    }


def _get_or_create_state() -> dict:
    today = _today_et()
    state = _load_poeltl()
    if state.get("date") != today:
        if state.get("date"):
            _archive_state(state)
            _discord_daily_results(state)
        state = _generate_puzzle(today)
        _save_poeltl(state)
    return state


def _leaderboard(entries: list[dict]) -> list[dict]:
    solved = sorted(
        [e for e in entries if e.get("solved")],
        key=lambda e: (e["guess_count"], e["submitted_at"]),
    )
    failed = sorted(
        [e for e in entries if not e.get("solved")],
        key=lambda e: e["submitted_at"],
    )
    result = []
    for i, e in enumerate(solved):
        result.append({
            "rank": i + 1, "member": e["member"], "solved": True,
            "guess_count": e["guess_count"], "submitted_at": e["submitted_at"],
        })
    for e in failed:
        result.append({
            "rank": None, "member": e["member"], "solved": False,
            "guess_count": e.get("guess_count", POELTL_MAX_GUESSES),
            "submitted_at": e["submitted_at"],
        })
    return result


# ── Discord ────────────────────────────────────────────────────────────────────

def _discord_post(embed: dict) -> None:
    try:
        httpx.post(DISCORD_BETS_WEBHOOK, json={"embeds": [embed]}, timeout=5)
    except Exception as exc:
        logger.warning("Poeltl Discord webhook failed: %s", exc)


def _discord_result(member: str, solved: bool, guess_count: int, grid: str) -> None:
    if solved:
        desc = f"**{member}** solved the **Daily NBN Poeltl** in **{guess_count}/7**!\n\n{grid}"
        color = 0x34d399
    else:
        desc = f"**{member}** did not solve today's **Daily NBN Poeltl**.\n\n{grid}"
        color = 0xf87171
    desc += "\n\nPlay at **[nbn.today/poeltl](https://nbn.today/poeltl)**"
    _discord_post({"color": color, "description": desc})


def _discord_daily_results(state: dict) -> None:
    lb = _leaderboard(state["entries"])
    answer = state.get("answer_data", {})
    solved_lb = [e for e in lb if e.get("solved")]
    lines = [f"**NBN Poeltl — {state['date']} Results**\n"]
    if solved_lb:
        for e in solved_lb[:5]:
            lines.append(f"**{e['member']}** — {e['guess_count']}/7")
        unsolved = sum(1 for e in lb if not e.get("solved"))
        if unsolved:
            lines.append(f"_{unsolved} player(s) did not solve it._")
    else:
        lines.append("No one solved it today.")
    lines.append(f"\n**Answer:** {answer.get('name', '?')} ({answer.get('team', '?')})")
    lines.append("\nNew game available now! **[nbn.today/poeltl](https://nbn.today/poeltl)**")
    _discord_post({"color": 0xf59e0b, "description": "\n".join(lines)})


# ── Archive ────────────────────────────────────────────────────────────────────

def _archive_state(state: dict) -> None:
    archive = _load_json(POELTL_ARCHIVE_FILE, {})
    archive[state["date"]] = {
        "date":         state["date"],
        "answer_name":  state.get("answer_data", {}).get("name", ""),
        "answer_team":  state.get("answer_data", {}).get("team", ""),
        "answer_slug":  state.get("answer_slug", ""),
        "entries":      state["entries"],
    }
    _save_json(POELTL_ARCHIVE_FILE, archive)


# ── NBYen reward ───────────────────────────────────────────────────────────────

def _award_poeltl_reward(name: str) -> float:
    ts = datetime.now(timezone.utc).isoformat()
    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, name)
        balances[name] = round(balances[name] + NBY_POELTL_REWARD, 2)
        new_bal = balances[name]
        _save_balances(balances)
    _append_ledger([{
        "ts": ts, "member": name, "delta": NBY_POELTL_REWARD,
        "balance": new_bal, "reason": "NBN Poeltl daily solve",
    }])
    return NBY_POELTL_REWARD


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/api/poeltl/today")
def get_poeltl_today(authorization: Optional[str] = Header(None)):
    with _poeltl_lock:
        state = _get_or_create_state()

    member = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        members = load_members()
        for name, data in members.items():
            if data.get("token") == token:
                member = name
                break

    your_entry = None
    your_progress = None
    solution = None

    if member:
        your_entry = next((e for e in state["entries"] if e["member"] == member), None)
        if your_entry:
            solution = state.get("answer_data")
        else:
            prog = state["in_progress"].get(member)
            if prog:
                started = datetime.fromisoformat(prog["started_at"])
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                your_progress = {
                    "started_at":    prog["started_at"],
                    "guesses":       prog["guesses"],
                    "remaining_sec": round(max(0.0, POELTL_TIME_LIMIT - elapsed)),
                }

    return {
        "date":          state["date"],
        "player_pool":   state["player_pool"],
        "leaderboard":   _leaderboard(state["entries"]),
        "time_limit":    POELTL_TIME_LIMIT,
        "your_progress": your_progress,
        "your_entry":    your_entry,
        "solution":      solution,
    }


class PoeltlGuess(BaseModel):
    slug: str


@router.post("/api/poeltl/guess")
def post_poeltl_guess(body: PoeltlGuess, info: dict = Depends(get_token_info)):
    member = info["name"]

    with _poeltl_lock:
        state = _get_or_create_state()

        # Already completed
        if any(e["member"] == member for e in state["entries"]):
            raise HTTPException(status_code=409, detail="You have already completed today's Poeltl")

        pool_full: dict[str, dict] = state.get("player_pool_full", {})
        if body.slug not in pool_full:
            raise HTTPException(status_code=422, detail=f"Player '{body.slug}' is not in today's pool")

        # Init in-progress entry on first guess
        now = datetime.now(timezone.utc)
        if member not in state["in_progress"]:
            state["in_progress"][member] = {
                "started_at": now.isoformat(),
                "guesses": [],
            }

        prog = state["in_progress"][member]

        # Check time limit
        started = datetime.fromisoformat(prog["started_at"])
        elapsed = (now - started).total_seconds()
        if elapsed > POELTL_TIME_LIMIT:
            # Time expired — mark as failed if not already
            if not any(e["member"] == member for e in state["entries"]):
                grid = _build_grid(prog["guesses"])
                state["entries"].append({
                    "member":       member,
                    "solved":       False,
                    "guess_count":  len(prog["guesses"]),
                    "submitted_at": now.isoformat(),
                    "grid":         grid,
                    "timed_out":    True,
                })
                del state["in_progress"][member]
                _save_poeltl(state)
                _discord_result(member, False, len(prog["guesses"]), grid)
            raise HTTPException(status_code=410, detail="Time's up! Game over.")

        # Check guess limit (shouldn't normally be exceeded but guard it)
        if len(prog["guesses"]) >= POELTL_MAX_GUESSES:
            raise HTTPException(status_code=409, detail="No guesses remaining")

        guess_player = pool_full[body.slug]
        answer = state["answer_data"]
        result = _compare(guess_player, answer)
        solved = body.slug == state["answer_slug"]

        prog["guesses"].append({
            "slug":   body.slug,
            "name":   guess_player["name"],
            "result": result,
        })

        guesses_used = len(prog["guesses"])
        done = solved or guesses_used >= POELTL_MAX_GUESSES
        nbyen_reward = None

        if done:
            grid = _build_grid(prog["guesses"])
            state["entries"].append({
                "member":       member,
                "solved":       solved,
                "guess_count":  guesses_used,
                "submitted_at": now.isoformat(),
                "grid":         grid,
            })
            del state["in_progress"][member]
            if solved:
                nbyen_reward = _award_poeltl_reward(member)
            _discord_result(member, solved, guesses_used, grid)

        _save_poeltl(state)

    log_write(info, f"POST poeltl/guess — slug={body.slug} solved={solved} guesses={guesses_used}")

    return {
        "result":            result,
        "solved":            solved,
        "guesses_used":      guesses_used,
        "guesses_remaining": POELTL_MAX_GUESSES - guesses_used,
        "done":              done,
        "solution":          answer if done else None,
        "leaderboard":       _leaderboard(state["entries"]),
        "nbyen_reward":      nbyen_reward,
    }


@router.get("/api/poeltl/history")
def get_poeltl_history():
    archive = _load_json(POELTL_ARCHIVE_FILE, {})
    result = []
    for date_str, day in sorted(archive.items(), reverse=True):
        lb = _leaderboard(day["entries"])
        solved_count = sum(1 for e in day["entries"] if e.get("solved"))
        result.append({
            "date":         date_str,
            "answer_name":  day.get("answer_name", ""),
            "answer_team":  day.get("answer_team", ""),
            "entries":      len(day["entries"]),
            "solved":       solved_count,
            "top_solver":   lb[0]["member"] if lb else None,
            "top_guesses":  lb[0]["guess_count"] if lb else None,
        })
    return result


@router.get("/api/poeltl/history/{date_str}")
def get_poeltl_history_date(date_str: str):
    archive = _load_json(POELTL_ARCHIVE_FILE, {})
    day = archive.get(date_str)
    if not day:
        raise HTTPException(status_code=404, detail=f"No results for {date_str}")
    return {**day, "leaderboard": _leaderboard(day["entries"])}


@router.post("/api/poeltl/admin/reset")
def poeltl_admin_reset(info: dict = Depends(require_admin)):
    with _poeltl_lock:
        old = _load_poeltl()
        if old and old.get("date"):
            _archive_state(old)
            _discord_daily_results(old)
        today = _today_et()
        new_state = _generate_puzzle(today)
        _save_poeltl(new_state)
    log_write(info, f"POST poeltl/admin/reset — {today}, answer={new_state['answer_slug']}")
    return {
        "ok":     True,
        "date":   today,
        "answer": new_state["answer_slug"],
    }

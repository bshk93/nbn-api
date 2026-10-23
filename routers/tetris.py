"""Tetris high scores — the leaderboard on the homepage's Tetris card.

The game runs entirely in the browser (nbn-today `index.html`, the Tetris
script block); this only keeps each member's best finished game.

| Route | Auth | What |
|---|---|---|
| `GET /api/tetris/leaderboard` | public | the top `LEADERBOARD_SIZE`, one row per member |
| `POST /api/tetris/scores` | member token | `{score, lines}` from a finished game |

Stored in `tetris-scores.json` in NBS_DATA_DIR, keyed by member name:
`{best: {score, lines, level, at}, games}`. Only the best game is kept.

**A score is the browser's word for it**, so a member with devtools can post
any number that passes `_plausible`. That check is a ceiling, not proof: it
refuses a score no game with that many lines could reach under the page's own
scoring, which stops a typed-in 9,999,999, not a careful forgery. Nothing
rides on it — no NB¥ is paid for a score, and keep it that way unless the
game moves server-side.
"""
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import get_token_info
from .constants import DATA_DIR
from .storage import _load_json, _save_json, log_write

router = APIRouter()

TETRIS_SCORES_FILE = DATA_DIR / "tetris-scores.json"
LEADERBOARD_SIZE = 20

# Mirrors the page's rules: a 10×20 board, 800 for four lines at once (the
# most per line, 200), times the level, which is lines // 10 + 1; a hard drop
# pays 2 a row, more than a soft drop's 1.
COLS, ROWS = 10, 20
BEST_POINTS_PER_LINE = 200
DROP_POINTS_PER_ROW = 2

_tetris_lock = threading.Lock()


class TetrisScore(BaseModel):
    score: int = Field(ge=0, le=100_000_000)
    lines: int = Field(ge=0, le=100_000)


def _level(lines: int) -> int:
    return lines // 10 + 1


def _plausible(score: int, lines: int) -> bool:
    """Is `score` reachable in a game that cleared `lines`? Every cleared line
    at the final level, all as tetrises, plus every piece that could have been
    placed (the cleared cells and a full board's worth) hard-dropped from the
    top."""
    pieces = (lines * COLS + ROWS * COLS) // 4 + 1
    ceiling = (BEST_POINTS_PER_LINE * lines * _level(lines)
               + pieces * ROWS * DROP_POINTS_PER_ROW)
    return score <= ceiling


def _leaderboard(scores: dict) -> list[dict]:
    rows = sorted(
        ({"member": name, **entry["best"]} for name, entry in scores.items() if entry.get("best")),
        key=lambda r: (-r["score"], r["at"]),
    )[:LEADERBOARD_SIZE]
    return [{"rank": i + 1, **r} for i, r in enumerate(rows)]


@router.get("/api/tetris/leaderboard")
def get_tetris_leaderboard():
    return {"leaderboard": _leaderboard(_load_json(TETRIS_SCORES_FILE, {}))}


@router.post("/api/tetris/scores")
def post_tetris_score(body: TetrisScore, info: dict = Depends(get_token_info)):
    member = info["name"]
    if not _plausible(body.score, body.lines):
        raise HTTPException(status_code=422, detail="That score isn't reachable with that many lines")

    with _tetris_lock:
        scores = _load_json(TETRIS_SCORES_FILE, {})
        entry = scores.setdefault(member, {"best": None, "games": 0})
        entry["games"] = entry.get("games", 0) + 1
        best = entry.get("best")
        new_best = body.score > 0 and (best is None or body.score > best["score"])
        if new_best:
            entry["best"] = {
                "score": body.score,
                "lines": body.lines,
                "level": _level(body.lines),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            log_write(info, f"tetris best {body.score}")
        _save_json(TETRIS_SCORES_FILE, scores)
        board = _leaderboard(scores)

    rank = next((r["rank"] for r in board if r["member"] == member), None)
    return {"new_best": new_best, "best": entry["best"], "rank": rank, "leaderboard": board}

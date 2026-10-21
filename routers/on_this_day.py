"""On this day — `GET /api/on-this-day`, behind the homepage card.

What happened on today's month and day in earlier seasons, from three
sources the site already keeps:

  * **Games** — the best line that day in each earlier season, by Game Score,
    from the single-game highs index (`game_highs.get_index`), so the same
    game ranks the same way here as on /stats/highs. A line that also clears a
    feat bar (50 points, a triple-double, …) says so.
  * **Clinches** — a playoff series that ended that day: its last game in the
    raw box scores, its result from the build's `playoff-brackets.csv`. A
    Finals clincher is a title.
  * **Trades** — `trade` entries in the ledger dated that day, voided ones
    left out. Returned as the ledger's own entries, so the homepage builds the
    headline the way its feed does (`FeedTypes.headline`).

Only days before `date` count, so tonight's games never show up as history.
Nothing here is stored: the index and ledger are read per request.

    venv/bin/python -m tests.test_on_this_day
"""
from __future__ import annotations

import csv
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from . import game_highs as gh
from .constants import DERIVED_DIR
from .league_time import league_today_str

router = APIRouter()

BRACKETS_FILE = DERIVED_DIR / "standings" / "playoff-brackets.csv"
MAX_TRADES = 3

# A feat a best-of-the-day line is called out for, strongest first.
_FEAT_LABELS = [("P50", "50-point game"), ("TD", "triple-double"), ("R20", "20-rebound game"),
                ("A15", "15-assist game"), ("3PM8", "8-three game"), ("P40", "40-point game")]


def _load_trades() -> list[dict]:
    from .transactions import _load_transactions
    return [t for t in _load_transactions() if t.get("type") == "trade"]


def _load_brackets() -> list[dict]:
    try:
        with BRACKETS_FILE.open(newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def best_games(index: gh.GameIndex, date: str) -> list[dict]:
    """The best line by Game Score on this month-day in each earlier season."""
    md = date[5:]
    s, dates = index.strings.values, index.meta["date"]
    feats = {k: set(index.feat_rows[k]) for k, _ in _FEAT_LABELS}
    best: dict[str, int] = {}
    gmsc = index.values["GMSC"]
    for i in index.order["GMSC"]:        # best first, so the first per day wins
        d = s[dates[i]]
        if d[5:] != md or d >= date or d in best:
            continue
        best[d] = i
    out = []
    for d, i in sorted(best.items(), reverse=True):
        line = index.line(i)
        line["feat"] = next((label for k, label in _FEAT_LABELS if i in feats[k]), None)
        line["gmsc"] = round(gmsc[i], 1)
        out.append(line)
    return out


def clinches(index: gh.GameIndex, date: str, brackets: list[dict]) -> list[dict]:
    """Playoff series whose last game fell on this month-day, before `date`."""
    md = date[5:]
    s, m, po = index.strings.values, index.meta, index.playoff
    last: dict[tuple, str] = defaultdict(str)
    for i in range(index.n):
        if not po[i]:
            continue
        key = (s[m["season"][i]], frozenset((s[m["team"][i]], s[m["opp"][i]])))
        d = s[m["date"][i]]
        if d > last[key]:
            last[key] = d
    out = []
    for b in brackets:
        key = (b.get("SEASON"), frozenset((b.get("T1"), b.get("T2"))))
        d = last.get(key)
        if not d or d[5:] != md or d >= date or not b.get("WINNER"):
            continue
        win = b["WINNER"]
        lose = b["T2"] if win == b["T1"] else b["T1"]
        try:
            w = max(int(b["T1_W"]), int(b["T2_W"]))
            l = min(int(b["T1_W"]), int(b["T2_W"]))
        except (KeyError, ValueError):
            w = l = None
        rnd = int(b.get("ROUND") or 0)
        out.append({"date": d, "season": b["SEASON"], "round": rnd, "title": rnd == 4,
                    "winner": win, "loser": lose, "wins": w, "losses": l})
    out.sort(key=lambda c: (c["date"], c["round"]), reverse=True)
    return out


def trades(date: str, all_trades: list[dict]) -> list[dict]:
    md = date[5:]
    hits = [t for t in all_trades
            if not t.get("voided") and (t.get("date") or "")[5:] == md and (t.get("date") or "") < date]
    hits.sort(key=lambda t: t["date"], reverse=True)
    return hits[:MAX_TRADES]


@router.get("/api/on-this-day")
def get_on_this_day(date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today")):
    date = date or league_today_str()
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    index, _ = gh.get_index()
    return {
        "date": date,
        "games": best_games(index, date),
        "clinches": clinches(index, date, _load_brackets()),
        "trades": trades(date, _load_trades()),
    }

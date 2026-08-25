"""A daily record of where every team sat, so the question can be asked later.

Every cap figure the site shows is *now*. There is no history of where a team
sat, which costs three things at once:

- § 7.3's second-apron pick freeze needs a four-year lookback on apron position.
  Nothing was banking that time, so the freeze was not "blocked until 2029" —
  it was blocked until four years after someone started recording. This starts
  the clock.
- "When did this team cross the first apron" is unanswerable on a team page.
- It is the same forensic material the edit log (`routers/audit.py`) wants,
  from the other end: that one says who changed a figure, this one says what
  the figure was on a given day.

Consistent with the standing rule to snapshot state *at* the moment it is true
rather than reconstructing it later by replaying the ledger — a reconstruction
would inherit every gap in the backfilled ledger, and the ledger does not carry
the roster CSV edits that move these figures anyway.

Storage is `cap-history.jsonl` in NBS_DATA_DIR: one line per team per
day, append-only, never rewritten. It sits at the data-dir root rather than
under `derived/` because the build cannot regenerate it — it is an observation,
not an aggregate — so `nbs-snapshot.timer` backs it up with the rest of the
league state.

Written by the daily timer (`snapshot_cap_history.py`), readable at
`GET /api/cap-history`.

The routes are `/api/cap-history*` and not `/api/team-state/history` on purpose:
`roster_picks.py` already owns `GET /api/team-state/{team}`, which would capture
a `history` path segment as a team name depending on router include order. A
separate prefix cannot collide however the routers are ordered.
"""

import json
from datetime import date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import require_role
from .constants import CAP_LEVELS_FILE, DATA_DIR, VALID_TEAMS, logger
from .roster_picks import get_season_state, load_team_state
from .storage import _current_league_year, _load_json, read_csv
from .transactions import _compute_team_salary, _compute_team_salary_ex_holds

router = APIRouter()

HISTORY_FILE = DATA_DIR / "cap-history.jsonl"


def _cap_levels_for(season: str) -> dict:
    levels = _load_json(CAP_LEVELS_FILE, {}).get(season, {})
    # 27-28 onward are on file with cap/apron1/apron2 all literally 0 — the
    # committee figures have not been entered yet. A 0 threshold is "unknown",
    # not "every team is over it", so it is normalised to None here; the rest of
    # the codebase makes the same reading (§ 3.11 skips its max-salary check on
    # a 0 cap rather than miscalculating).
    return {k: (levels.get(k) or None) for k in ("cap", "apron1", "apron2")}


def _apron_position(salary_ex_holds: int, levels: dict) -> Optional[str]:
    """Which apron tier a team sits in, on the same basis the validators use:
    Team Salary excluding free-agent holds (§ 1.3). None when under the first
    apron *or* when the thresholds for that season aren't known yet."""
    if levels["apron2"] is not None and salary_ex_holds >= levels["apron2"]:
        return "second_apron"
    if levels["apron1"] is not None and salary_ex_holds >= levels["apron1"]:
        return "first_apron"
    return None


def _roster_counts(team: str, bios: dict) -> dict:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return {"roster": 0, "two_way": 0}
    _, rows = read_csv(path)
    standard = two_way = 0
    for row in rows:
        bio = bios.get((row.get("SLUG") or "").strip(), {})
        if bio.get("type") == "two-way":
            two_way += 1
        else:
            standard += 1
    return {"roster": standard, "two_way": two_way}


def build_rows(on_date: Optional[str] = None, season: Optional[str] = None) -> list[dict]:
    """One row per team for `on_date`. Pure — computes, writes nothing."""
    on_date = on_date or _date.today().isoformat()
    season = season or _current_league_year()
    bios = _load_json(DATA_DIR / "player-bios.json", {})
    state = load_team_state()
    levels = _cap_levels_for(season)

    rows = []
    for team in sorted(VALID_TEAMS):
        salary = _compute_team_salary(team, bios, season)
        ex_holds = _compute_team_salary_ex_holds(team, bios, season)
        ts = get_season_state(state, team, season)
        counts = _roster_counts(team, bios)
        rows.append({
            "date": on_date,
            "season": season,
            "team": team,
            "salary": salary,
            "salary_ex_holds": ex_holds,
            # The holds are the difference, and they are exactly what makes the
            # two figures diverge during free agency — worth storing resolved so
            # a reader never has to know which basis a chart is on.
            "holds": salary - ex_holds,
            "cap": levels["cap"],
            "apron1": levels["apron1"],
            "apron2": levels["apron2"],
            "apron_position": _apron_position(ex_holds, levels),
            "hard_cap": ts.get("hard_cap"),
            "mle_used": ts.get("mle_used", 0),
            "mle_type": ts.get("mle_type"),
            "bae_used": ts.get("bae_used", False),
            **counts,
        })
    return rows


def recorded_dates() -> set[str]:
    return {e["date"] for e in read_history() if "date" in e}


def read_history(team: Optional[str] = None, since: Optional[str] = None,
                 until: Optional[str] = None) -> list[dict]:
    """Every recorded row, oldest first, optionally narrowed."""
    if not HISTORY_FILE.exists():
        return []
    out = []
    with HISTORY_FILE.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if team and entry.get("team") != team.upper():
                continue
            if since and entry.get("date", "") < since:
                continue
            if until and entry.get("date", "") > until:
                continue
            out.append(entry)
    return out


def snapshot(on_date: Optional[str] = None, force: bool = False) -> dict:
    """Append today's rows. A second run on the same day is a no-op unless
    forced — the timer may fire twice after a reboot, and a duplicated day would
    quietly double-count in any chart built off this."""
    on_date = on_date or _date.today().isoformat()
    if not force and on_date in recorded_dates():
        return {"date": on_date, "written": 0, "skipped": "already recorded"}
    rows = build_rows(on_date)
    with HISTORY_FILE.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    logger.info("cap-history: recorded %d teams for %s", len(rows), on_date)
    return {"date": on_date, "written": len(rows)}


@router.get("/api/cap-history")
def get_cap_history(team: Optional[str] = None,
                           since: Optional[str] = None,
                           until: Optional[str] = None,
                           limit: int = Query(5000, ge=1, le=50000)):
    if team and team.upper() not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail=f"Unknown team '{team}'")
    entries = read_history(team=team, since=since, until=until)
    return {"count": len(entries), "entries": entries[-limit:]}


@router.get("/api/cap-history/current")
def get_cap_history_current(season: Optional[str] = None):
    """What today's snapshot would record, computed live. Lets a page show the
    current point on the same basis as the history without waiting for the
    timer, and makes the timer's output checkable by eye."""
    return {"entries": build_rows(season=season)}


@router.post("/api/cap-history/snapshot")
def post_cap_history_snapshot(force: bool = False,
                             _: dict = Depends(require_role("rosters"))):
    return snapshot(force=force)

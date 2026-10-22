"""Pick'em — a daily fantasy lineup game on NBN's own box scores.

Each game day, a member sets six players from the teams playing that day: one
per position (PG, SG, SF, PF, C) plus a sixth man of any position. There is no
salary cap; the skill is guessing who has the big night.

  * **Eligibility.** A player fits a slot if it is his 2K primary position or
    his effective secondary (`players.effective_secondary`, the same § 2.3
    reading team pages use). A player 2K doesn't carry falls back to his bio's
    `pos` list. Only `player` and `two-way` types are in the pool.
  * **Lock.** The whole lineup locks at the day's first tip-off (ET). Other
    members' lineups stay hidden until then.
  * **Scoring.** DraftKings classic: PTS 1, 3PM +0.5, REB 1.25, AST 1.5,
    STL 2, BLK 2, TO -0.5, plus +1.5 for a double-double and +3 for a
    triple-double (a triple-double earns both). Scored straight from the raw
    box scores, so a day fills in as its games are uploaded; a player with no
    line that day scores 0.
  * **Standings.** Daily, weekly (Monday to Sunday) and season totals. NB¥
    prizes are `PRIZES` below, all 0 until the league sets them; nothing is
    paid from here yet.

Storage: `pickem-{season}.json` in DATA_DIR, `{date: {member: {lineup, teams,
saved_at}}}`.

    venv/bin/python -m tests.test_pickem
"""
from __future__ import annotations

import csv
import threading
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .auth import _resolve_token, get_token_info
from .boxscores import allstats_path
from .coaching_settings import _roster_slugs
from .constants import DATA_DIR
from .league_time import LEAGUE_TZ, league_now
from .players import _primary_pos, effective_secondary, load_attributes, load_ovr, load_player_bios
from .schedule import _load as load_schedule, _time_key
from .storage import _load_json, _save_json, log_write
import season_clock

router = APIRouter()

POSITIONS = ["PG", "SG", "SF", "PF", "C"]
SLOTS = [*POSITIONS, "6TH"]
POOL_TYPES = {"player", "two-way"}

SCORING = {"P": 1.0, "3PM": 0.5, "R": 1.25, "A": 1.5, "S": 2.0, "B": 2.0, "TO": -0.5}
DOUBLE_DOUBLE_BONUS = 1.5
TRIPLE_DOUBLE_BONUS = 3.0
DD_CATS = ("P", "R", "A", "S", "B")

# NB¥ for first place. Set by the league; 0 means no prize.
PRIZES = {"day": 0, "week": 0, "season": 0}

# Used when no game that day has a tip-off time.
DEFAULT_LOCK = (19, 0)

_lock = threading.Lock()


def _file(season: str):
    return DATA_DIR / f"pickem-{season}.json"


def _load(season: str) -> dict:
    return _load_json(_file(season), {})


def _save(season: str, data: dict):
    _save_json(_file(season), data)


def _check_date(date: str) -> str:
    try:
        return Date.fromisoformat(date).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"date must be YYYY-MM-DD, got {date!r}")


# ── Slate ────────────────────────────────────────────────────────────────────

def slate_games(date: str) -> tuple[str, list[dict]]:
    season = season_clock.season_for_date(date)
    games = [g for g in load_schedule(season).get("games", []) if g.get("date") == date]
    games.sort(key=lambda g: _time_key(g.get("time_et", "")))
    return season, games


def lock_at(date: str, games: list[dict]) -> datetime:
    hh, mm = DEFAULT_LOCK
    times = [_time_key(g.get("time_et", "")) for g in games]
    known = [t for t in times if t != (99, 99)]
    if known:
        hh, mm = min(known)
    d = Date.fromisoformat(date)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=LEAGUE_TZ)


def positions_of(slug: str, bio: dict, attrs: dict) -> list[str]:
    primary = _primary_pos(slug, attrs)
    if not primary:
        return [p for p in POSITIONS if p in (bio.get("pos") or [])]
    sec = effective_secondary(slug, bio, attrs)
    return [primary] + ([sec] if sec and sec != primary else [])


def season_averages(rows: list[dict], bios: dict, before: str) -> dict[str, tuple[float, int]]:
    """slug -> (fantasy points per game, games) over box scores before `before`."""
    by_name = _slug_by_name(bios)
    tot: dict[str, list] = {}
    for r in rows:
        if (r.get("DATE") or "") >= before or not _int(r.get("M")):
            continue
        slug = by_name.get((r.get("PLAYER") or "").strip().upper())
        if slug:
            t = tot.setdefault(slug, [0.0, 0])
            t[0] += fantasy_points(r)
            t[1] += 1
    return {k: (round(v[0] / v[1], 1), v[1]) for k, v in tot.items()}


def player_pool(games: list[dict], averages: Optional[dict] = None) -> dict[str, dict]:
    bios = load_player_bios()
    ovr = load_ovr()
    attrs = load_attributes()
    averages = averages or {}
    pool = {}
    for g in games:
        for team, opp, home in ((g["away_team"], g["home_team"], False),
                                (g["home_team"], g["away_team"], True)):
            for slug in _roster_slugs(team):
                bio = bios.get(slug) or {}
                if bio.get("type") not in POOL_TYPES:
                    continue
                hist = ovr.get(slug) or []
                pool[slug] = {
                    "slug": slug,
                    "name": bio.get("name", slug),
                    "team": team,
                    "opp": opp,
                    "home": home,
                    "positions": positions_of(slug, bio, attrs),
                    "ovr": hist[-1]["ovr"] if hist else None,
                    "photo_url": bio.get("photo_url") or "",
                    "type": bio.get("type"),
                    "fp_avg": averages.get(slug, (None, 0))[0],
                    "gp": averages.get(slug, (None, 0))[1],
                }
    return pool


def check_lineup(lineup: dict, pool: dict[str, dict]) -> list[str]:
    """Every reason this lineup can't be saved; empty when it can."""
    errs = []
    missing = [s for s in SLOTS if not lineup.get(s)]
    if missing:
        errs.append(f"Fill every slot (missing {', '.join(missing)}).")
    extra = [s for s in lineup if s not in SLOTS]
    if extra:
        errs.append(f"Unknown slot(s): {', '.join(extra)}.")
    picked = [lineup[s] for s in SLOTS if lineup.get(s)]
    if len(set(picked)) != len(picked):
        errs.append("A player can only fill one slot.")
    for s in SLOTS:
        slug = lineup.get(s)
        if not slug:
            continue
        p = pool.get(slug)
        if not p:
            errs.append(f"{slug} isn't playing today.")
        elif s != "6TH" and s not in p["positions"]:
            errs.append(f"{p['name']} can't play {s} (he plays {'/'.join(p['positions']) or 'no listed position'}).")
    return errs


# ── Scoring ──────────────────────────────────────────────────────────────────

def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def fantasy_points(row: dict) -> float:
    stats = {k: _int(row.get(k)) for k in SCORING}
    pts = sum(SCORING[k] * stats[k] for k in SCORING)
    tens = sum(1 for k in DD_CATS if stats[k] >= 10)
    if tens >= 2:
        pts += DOUBLE_DOUBLE_BONUS
    if tens >= 3:
        pts += TRIPLE_DOUBLE_BONUS
    return round(pts, 2)


def box_rows(season: str) -> list[dict]:
    rows = []
    for gt in ("REG", "PLAYOFF"):
        path = allstats_path(season, gt)
        if path.exists():
            with open(path, newline="") as f:
                rows.extend(csv.DictReader(f))
    return rows


def _slug_by_name(bios: dict) -> dict[str, str]:
    """Box score rows carry a name, not a slug: the same match as
    `boxscores._bios_by_name`."""
    out = {}
    for slug, bio in bios.items():
        n = (bio.get("name") or "").strip().upper()
        if n:
            out.setdefault(n, slug)
    return out


def day_lines(rows: list[dict], date: str, bios: dict) -> tuple[dict[str, dict], set[str]]:
    """slug -> {fp, line} for every box score line that date, and the teams
    that have a box score in."""
    by_name = _slug_by_name(bios)
    out, teams = {}, set()
    for r in rows:
        if (r.get("DATE") or "").strip() != date:
            continue
        teams.add((r.get("TEAM") or "").strip())
        slug = by_name.get((r.get("PLAYER") or "").strip().upper())
        if not slug:
            continue
        out[slug] = {
            "fp": fantasy_points(r),
            "line": {k.lower(): _int(r.get(k)) for k in ("M", "P", "R", "A", "S", "B", "3PM", "TO")},
        }
    return out, teams


def score_entry(entry: dict, lines: dict[str, dict]) -> dict:
    slots = {}
    for s in SLOTS:
        slug = entry["lineup"].get(s)
        got = lines.get(slug)
        slots[s] = {"slug": slug, "fp": got["fp"] if got else 0.0,
                    "line": got["line"] if got else None}
    return {"total": round(sum(v["fp"] for v in slots.values()), 2), "slots": slots}


def week_of(date: str) -> str:
    """The Monday that starts this date's week."""
    d = Date.fromisoformat(date)
    return (d - timedelta(days=d.weekday())).isoformat()


# ── Routes ───────────────────────────────────────────────────────────────────

class EntryIn(BaseModel):
    lineup: dict[str, str]


@router.get("/api/pickem/{date}")
def get_day(date: str, authorization: Optional[str] = Header(None)):
    date = _check_date(date)
    season, games = slate_games(date)
    lock = lock_at(date, games)
    locked = league_now() >= lock
    me = (_resolve_token(authorization) or {}).get("name")

    bios = load_player_bios()
    rows = box_rows(season)
    lines, box_teams = day_lines(rows, date, bios)
    slate_teams = {t for g in games for t in (g["away_team"], g["home_team"])}
    entries = _load(season).get(date, {})

    out_entries = []
    for member, e in entries.items():
        if not locked and member != me:
            continue
        scored = score_entry(e, lines) if locked else None
        out_entries.append({"member": member, "lineup": e["lineup"], "saved_at": e.get("saved_at"),
                            "score": scored})
    out_entries.sort(key=lambda x: -(x["score"]["total"] if x["score"] else 0))

    # Locked days keep their names even after a player is traded away.
    pool = player_pool(games, season_averages(rows, bios, date)) if games else {}
    names = {s: (bios.get(s) or {}).get("name", s) for e in entries.values() for s in e["lineup"].values()}

    return {
        "date": date,
        "season": season,
        "games": [{"away": g["away_team"], "home": g["home_team"], "time_et": g.get("time_et", ""),
                   "final": g["away_team"] in box_teams and g["home_team"] in box_teams} for g in games],
        "lock_at": lock.isoformat(),
        "locked": locked,
        "final": bool(games) and slate_teams <= box_teams,
        "players": sorted(pool.values(), key=lambda p: (-(p["ovr"] or 0), p["name"])),
        "names": names,
        "entries": out_entries,
        "entry_count": len(entries),
        "scoring": {**SCORING, "DD": DOUBLE_DOUBLE_BONUS, "TD": TRIPLE_DOUBLE_BONUS},
    }


@router.put("/api/pickem/{date}/entry")
def put_entry(date: str, body: EntryIn, info: dict = Depends(get_token_info)):
    date = _check_date(date)
    season, games = slate_games(date)
    if not games:
        raise HTTPException(status_code=404, detail="No games that day.")
    if league_now() >= lock_at(date, games):
        raise HTTPException(status_code=409, detail="Lineups for that day are locked.")
    lineup = {k.upper(): v for k, v in body.lineup.items() if v}
    pool = player_pool(games)
    errs = check_lineup(lineup, pool)
    if errs:
        raise HTTPException(status_code=422, detail=" ".join(errs))
    with _lock:
        data = _load(season)
        data.setdefault(date, {})[info["name"]] = {
            "lineup": {s: lineup[s] for s in SLOTS},
            "teams": {s: pool[lineup[s]]["team"] for s in SLOTS},
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save(season, data)
    log_write(info, f"pickem lineup {date}")
    return {"ok": True, "lineup": data[date][info["name"]]["lineup"]}


@router.delete("/api/pickem/{date}/entry")
def delete_entry(date: str, info: dict = Depends(get_token_info)):
    date = _check_date(date)
    season, games = slate_games(date)
    if games and league_now() >= lock_at(date, games):
        raise HTTPException(status_code=409, detail="Lineups for that day are locked.")
    with _lock:
        data = _load(season)
        if data.get(date, {}).pop(info["name"], None) is not None:
            _save(season, data)
    return {"ok": True}


def standings(season: str, today: Optional[str] = None) -> dict:
    """Daily results, weekly and season totals, over every locked day."""
    data = _load(season)
    bios = load_player_bios()
    rows = box_rows(season)
    now = league_now()
    days, weeks, totals = [], {}, {}
    for date in sorted(data):
        _, games = slate_games(date)
        if not data[date] or now < lock_at(date, games):
            continue
        lines, box_teams = day_lines(rows, date, bios)
        slate_teams = {t for g in games for t in (g["away_team"], g["home_team"])}
        scores = {m: score_entry(e, lines)["total"] for m, e in data[date].items()}
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        top = ranked[0][1] if ranked else 0
        days.append({"date": date, "final": slate_teams <= box_teams,
                     "winners": [m for m, s in ranked if s == top and top > 0],
                     "scores": [{"member": m, "total": s} for m, s in ranked]})
        wk = weeks.setdefault(week_of(date), {})
        for m, s in scores.items():
            t = totals.setdefault(m, {"member": m, "total": 0.0, "days": 0, "day_wins": 0, "best": 0.0})
            t["total"] = round(t["total"] + s, 2)
            t["days"] += 1
            t["best"] = max(t["best"], s)
            if m in days[-1]["winners"]:
                t["day_wins"] += 1
            wk[m] = round(wk.get(m, 0) + s, 2)
    return {
        "season": season,
        "prizes": PRIZES,
        "season_totals": sorted(totals.values(), key=lambda t: -t["total"]),
        "weeks": [{"week_of": w, "scores": [{"member": m, "total": s}
                                            for m, s in sorted(v.items(), key=lambda x: -x[1])]}
                  for w, v in sorted(weeks.items(), reverse=True)],
        "days": list(reversed(days)),
    }


@router.get("/api/pickem")
def get_standings(season: Optional[str] = None):
    return standings(season or season_clock.current_season())

"""Playoff odds — `GET /api/playoff-odds`, behind the Playoff Odds tab on /standings.

Plays out the rest of the regular season and the postseason many times and
counts how often each team lands where. Run by `nbn-playoff-odds.timer`
(`snapshot_playoff_odds.py`); the endpoint serves the last run, because one
run is a few seconds of pure Python (no numpy in this venv) and a page load
should not pay for it.

The model, in full:

  Team strength is a rating in points per game against an average team.
  Before any games it is the **roster prior**: the team's OVRs (top eight,
  weighted roughly by minutes), placed on the spread NBN point differentials
  have actually had (PRIOR_SD). As games are played the rating moves to the
  team's real point differential, weighting the prior as PRIOR_GAMES games:

      rating = (prior * PRIOR_GAMES + total margin) / (PRIOR_GAMES + games)

  Each simulation also draws every team's *true* strength around that rating
  (the uncertainty shrinks as games are played), so a hot 5–0 start isn't
  treated as certain.

  One game: home margin ~ Normal(home rating − away rating + HOME_EDGE, GAME_SD).
  HOME_EDGE and GAME_SD were measured from every NBN regular-season game
  20-21 to 25-26 (+2.0 home margin; 20 points spread, about 18 of it noise).

  Format, as NBN plays it: seeds 1–6 go straight in; 7–10 play in (7 v 8, the
  winner is the 7 seed; 9 v 10, and its winner plays 7 v 8's loser for the 8
  seed). Then best-of-seven rounds, 2-2-1-1-1, home court to the better seed
  (in the Finals, the better record). Ties in the standings are broken by coin
  flip — the build's tiebreakers (head-to-head, division, conference) are not
  modelled.

Results come from the raw box scores (via the game-highs index), so a game
counts the moment it is committed. The schedule is `schedule-{season}.json`;
a scheduled game with no result yet is simulated, and a played game is
matched to it on date + the two teams.

    venv/bin/python -m tests.test_playoff_odds
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from . import game_highs as gh
from .constants import DATA_DIR, VALID_TEAMS
from .players import load_ovr, load_player_bios
from .storage import _atomic_write, _load_json, read_csv

router = APIRouter()

EAST = {"ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DET", "IND",
        "MIA", "MIL", "NYK", "ORL", "PHI", "TOR", "WAS"}
WEST = set(VALID_TEAMS) - EAST

HOME_EDGE = 2.0
GAME_SD = 18.0
PRIOR_SD = 5.5          # spread of the roster prior, in points per game
PRIOR_GAMES = 13.0      # GAME_SD² / PRIOR_SD², rounded: the prior is worth this many games
SIMS = 5000
MINUTES = [36, 34, 32, 30, 28, 24, 18, 14]   # weights for a roster's top eight OVRs

CURRENT_FILE = DATA_DIR / "playoff-odds.json"
HISTORY_FILE = DATA_DIR / "playoff-odds-history.jsonl"


# ── Inputs ───────────────────────────────────────────────────────────────────

def roster_strength(team: str, bios: dict, ovr: dict) -> Optional[float]:
    """Minutes-weighted mean OVR of a roster's top eight players."""
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return None
    _, rows = read_csv(path)
    vals = []
    for r in rows:
        slug = (r.get("SLUG") or "").strip()
        if (bios.get(slug) or {}).get("type") in ("draft-rights", "dead"):
            continue
        hist = ovr.get(slug) or []
        if hist and hist[-1].get("ovr") is not None:
            vals.append(float(hist[-1]["ovr"]))
    vals.sort(reverse=True)
    top = vals[:len(MINUTES)]
    if not top:
        return None
    w = MINUTES[:len(top)]
    return sum(v * x for v, x in zip(top, w)) / sum(w)


def roster_priors(strengths: dict[str, Optional[float]]) -> dict[str, float]:
    """Roster strength → expected margin per game, centred on the league."""
    known = [v for v in strengths.values() if v is not None]
    if len(known) < 2:
        return {t: 0.0 for t in strengths}
    mean = sum(known) / len(known)
    sd = math.sqrt(sum((v - mean) ** 2 for v in known) / len(known)) or 1.0
    return {t: (0.0 if v is None else (v - mean) / sd * PRIOR_SD) for t, v in strengths.items()}


def season_results(index: gh.GameIndex, season: str) -> list[dict]:
    """One row per regular-season game played: date, home, away, both scores."""
    s, m, c = index.strings.values, index.meta, index.cols
    sid = index.strings.ids.get(season)
    if sid is None:
        return []
    seen = {}
    for i in range(index.n):
        if index.playoff[i] or m["season"][i] != sid:
            continue
        team, opp, date = s[m["team"][i]], s[m["opp"][i]], s[m["date"][i]]
        home, away = (team, opp) if index.home[i] else (opp, team)
        key = (date, home, away)
        if key in seen:
            continue
        hp, ap = (c["team_pts"][i], c["opp_pts"][i]) if index.home[i] else (c["opp_pts"][i], c["team_pts"][i])
        seen[key] = {"date": date, "home": home, "away": away, "home_pts": hp, "away_pts": ap}
    return [g for g in seen.values() if g["home_pts"] != g["away_pts"]]


def load_schedule(season: str) -> list[dict]:
    data = _load_json(DATA_DIR / f"schedule-{season}.json", {})
    return [g for g in data.get("games", []) if g.get("home_team") and g.get("away_team")]


# ── The simulation ───────────────────────────────────────────────────────────

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def simulate(results: list[dict], schedule: list[dict], priors: dict[str, float],
             sims: int = SIMS, seed: int = 0) -> dict:
    teams = sorted(priors)
    rng = random.Random(seed)

    # The record so far, and each team's margin for the rating.
    w0, l0, margin, played = (defaultdict(int) for _ in range(4))
    for g in results:
        hw = g["home_pts"] > g["away_pts"]
        w0[g["home"] if hw else g["away"]] += 1
        l0[g["away"] if hw else g["home"]] += 1
        d = g["home_pts"] - g["away_pts"]
        margin[g["home"]] += d
        margin[g["away"]] -= d
        played[g["home"]] += 1
        played[g["away"]] += 1

    rating = {t: (priors[t] * PRIOR_GAMES + margin[t]) / (PRIOR_GAMES + played[t]) for t in teams}
    spread = {t: math.sqrt(1.0 / (1.0 / PRIOR_SD ** 2 + played[t] / GAME_SD ** 2)) for t in teams}

    done = defaultdict(int)
    for g in results:
        done[(g["date"], frozenset((g["home"], g["away"])))] += 1
    remaining = []
    for g in schedule:
        key = (g["date"], frozenset((g["home_team"], g["away_team"])))
        if done[key]:
            done[key] -= 1
            continue
        if g["home_team"] in rating and g["away_team"] in rating:
            remaining.append((g["home_team"], g["away_team"]))

    conf_of = {t: ("East" if t in EAST else "West") for t in teams}
    keys = ("playoffs", "top6", "play_in", "seed1", "conf_finals", "finals", "title")
    count = {t: dict.fromkeys(keys, 0) for t in teams}
    wins_total = defaultdict(float)
    seed_count = {t: defaultdict(int) for t in teams}
    gauss, rand = rng.gauss, rng.random

    for _ in range(sims):
        true = {t: rating[t] + gauss(0.0, spread[t]) for t in teams}
        wins = dict(w0)
        for h, a in remaining:
            if true[h] - true[a] + HOME_EDGE + gauss(0.0, GAME_SD) > 0:
                wins[h] = wins.get(h, 0) + 1
            else:
                wins[a] = wins.get(a, 0) + 1
        for t in teams:
            wins_total[t] += wins.get(t, 0)

        def p_home(h, a):
            return _phi((true[h] - true[a] + HOME_EDGE) / GAME_SD)

        def series(hi, lo):
            # 2-2-1-1-1. Playing all seven decides the same winner as stopping at four.
            won = 0
            for at_hi in (1, 1, 0, 0, 1, 0, 1):
                p = p_home(hi, lo) if at_hi else 1.0 - p_home(lo, hi)
                won += rand() < p
            return hi if won >= 4 else lo

        champs = {}
        for conf in ("East", "West"):
            ranked = sorted((t for t in teams if conf_of[t] == conf),
                            key=lambda t: (-wins.get(t, 0), rand()))
            for i, t in enumerate(ranked, 1):
                seed_count[t][i] += 1
            if len(ranked) < 10:
                continue
            count[ranked[0]]["seed1"] += 1
            for t in ranked[:6]:
                count[t]["top6"] += 1
                count[t]["playoffs"] += 1
            for t in ranked[6:10]:
                count[t]["play_in"] += 1
            s7, s8, s9, s10 = ranked[6:10]
            win78 = s7 if rand() < p_home(s7, s8) else s8
            lose78 = s8 if win78 == s7 else s7
            win910 = s9 if rand() < p_home(s9, s10) else s10
            last = lose78 if rand() < p_home(lose78, win910) else win910
            count[win78]["playoffs"] += 1
            count[last]["playoffs"] += 1
            seeds = ranked[:6] + [win78, last]           # seeds 1..8
            r1 = [series(seeds[0], seeds[7]), series(seeds[3], seeds[4]),
                  series(seeds[2], seeds[5]), series(seeds[1], seeds[6])]
            order = {t: i for i, t in enumerate(seeds)}
            better = lambda x, y: (x, y) if order[x] < order[y] else (y, x)
            r2 = [series(*better(r1[0], r1[1])), series(*better(r1[2], r1[3]))]
            for t in r2:
                count[t]["conf_finals"] += 1
            champ = series(*better(r2[0], r2[1]))
            count[champ]["finals"] += 1
            champs[conf] = champ
        if len(champs) == 2:
            e, w = champs["East"], champs["West"]
            hi, lo = (e, w) if (wins.get(e, 0), rand()) >= (wins.get(w, 0), rand()) else (w, e)
            count[series(hi, lo)]["title"] += 1

    rows = []
    for t in teams:
        n = played[t]
        rows.append({
            "team": t, "conf": conf_of[t],
            "w": w0.get(t, 0), "l": l0.get(t, 0),
            "proj_w": round(wins_total[t] / sims, 1),
            "games": n + sum(1 for h, a in remaining if t in (h, a)),
            "rating": round(rating[t], 1), "prior": round(priors[t], 1),
            **{k: round(count[t][k] / sims, 4) for k in keys},
            "seeds": {str(k): round(v / sims, 4) for k, v in sorted(seed_count[t].items())},
        })
    return {"sims": sims, "games_played": len(results), "games_left": len(remaining), "teams": rows}


# ── Runs and history ─────────────────────────────────────────────────────────

def compute(season: str, today: str) -> dict:
    index, _ = gh.get_index()
    bios, ovr = load_player_bios(), load_ovr()
    priors = roster_priors({t: roster_strength(t, bios, ovr) for t in sorted(VALID_TEAMS)})
    out = simulate(season_results(index, season), load_schedule(season), priors,
                   seed=int(today.replace("-", "")))
    return {"season": season, "date": today,
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **out}


def read_history(season: Optional[str] = None) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if season is None or row.get("season") == season:
                out.append(row)
    return out


def _history_row(run: dict) -> dict:
    return {"date": run["date"], "season": run["season"],
            "teams": {r["team"]: {k: r[k] for k in ("playoffs", "top6", "seed1", "title", "proj_w")}
                      for r in run["teams"]}}


def save_run(run: dict) -> None:
    """Write the current run, and make it the day's row in the history.

    One row per date: a later run the same day replaces that day's row, so the
    history reads as each day's final odds.
    """
    _atomic_write(CURRENT_FILE, json.dumps(run, indent=1))
    rows = [r for r in read_history() if not (r["date"] == run["date"] and r["season"] == run["season"])]
    rows.append(_history_row(run))
    rows.sort(key=lambda r: (r["season"], r["date"]))
    _atomic_write(HISTORY_FILE, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


@router.get("/api/playoff-odds")
def get_playoff_odds():
    """The latest run, plus each team's playoff odds from the last earlier day."""
    run = _load_json(CURRENT_FILE, None)
    if not run:
        raise HTTPException(status_code=404, detail="No playoff odds have been computed yet")
    earlier = [r for r in read_history(run["season"]) if r["date"] < run["date"]]
    prev = earlier[-1] if earlier else None
    return {**run, "prev_date": prev["date"] if prev else None,
            "prev": {t: v["playoffs"] for t, v in prev["teams"].items()} if prev else {}}


@router.get("/api/playoff-odds/history")
def get_playoff_odds_history(team: Optional[str] = Query(default=None)):
    """Each day's odds this season, for the chart. `team` narrows to one."""
    run = _load_json(CURRENT_FILE, None)
    rows = read_history(run["season"] if run else None)
    if team:
        t = team.upper()
        if t not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail="Unknown team")
        return [{"date": r["date"], **r["teams"].get(t, {})} for r in rows if t in r["teams"]]
    return rows

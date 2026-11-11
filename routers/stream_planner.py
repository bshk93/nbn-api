"""Stream planner — `GET /api/stream-planner`, the Planner tab on /committees/stream.

One scheduled stream game a day (bought streams are extra). This ranks the
games on each day in a range by how much they're worth putting on stream, and
says why. It never writes: the page flags a pick through the existing
`POST /api/schedule/{id}/stream`, and a person always makes that call.

The score, in points (WEIGHTS):

  quality            both teams good — the hourly playoff-odds `rating`
                     (points per game vs an average team), adjusted for players
                     currently Out, ranked against the league
  closeness          small projected spread: home − away + HOME_EDGE
  owner_rivalry      the two *current owners'* head-to-head, across every team
                     either has run (owners.csv tenures, attributed by game
                     day): how often they've met, how even it is, playoff series
  franchise_rivalry  the two franchises' head-to-head, playoff series, same
                     division
  stakes             playoff uncertainty 4·p·(1−p) for each side, plus two
                     same-conference teams projected close together; it grows
                     as the season runs out
  stars              each side's best healthy OVR (Out players excluded)
  fairness           the even share of *scheduled* streams (bought ones don't
                     count against a team) minus what each team has had or is
                     already flagged for; under-exposed teams gain, over-exposed lose
  repeat             a team on stream close to the date, either side: more off
                     within REPEAT_NEAR_DAYS than within COOLDOWN_DAYS (bought
                     streams count here: it's the same team on stream)

Milestones are the page's (+5, from `milestones.js`), so there is one
milestone implementation, not a Python mirror.

Days: a day with a flagged game that nobody bought is **locked** — its stream
is chosen — and is not ranked. A day whose only stream is bought still needs
its pick.

**The cooldown is a rule, not a weight.** A game with a team on stream within
COOLDOWN_DAYS either side (set, bought, or picked earlier in this plan) is
still ranked and shown, marked `cooldown`, but can't be the day's pick unless
every game that day is on cooldown — then the one whose clash is furthest away
is picked, marked `forced`. Without it, fair share kept dragging the same
under-exposed team back every few days (SAS, Nov 2026).

Days are scored in order. Each open day's pick is the caller's choice when it
sent one (`picks`, by game id; `skips`, by date, for a day it leaves empty),
otherwise the best game off cooldown (`projected`). Either way the pick counts
toward fairness and the cooldown for every day after, so a swap re-ranks the
rest of the range.

**Priorities** (`priority=`, PRIORITIES): a preset that scales each factor's
weight — `balanced` (the weights above), `coverage` (fair share
and repeats count for more), `rivalry` (rivalries and close games), `quality`
(good teams and star players) and `stakes` (the playoff race). Each factor's `pts` and
`max` are multiplied after it's scored, and `weights` in the response is the
effective set. The cooldown stays a rule under every preset.

**Open days** (`open_days=N`): instead of a fixed `to`, walk from `from` until
N open days (games, and no locked stream) have been planned, at most MAX_DAYS
out. Locked and empty days on the way are returned as usual, and `to` is the
last day walked; `open_days_short` says the window ran out first. It's the
Planner's "7 days" with Hide saved days on.

Everything below the loaders is pure (inputs in, plan out) so the tests run
without a data dir. Pinned by tests/test_stream_planner.py.
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import game_highs as gh
from . import injuries as injuries_mod
from . import playoff_odds as po
from .auth import require_any_role
from .constants import DATA_DIR, VALID_TEAMS
from .league_time import league_today_str
from .names import player_name
from .players import ovr_shared, player_bios_shared
from .storage import _current_league_year, _load_json, read_csv

router = APIRouter()

WEIGHTS = {
    "quality": 25,
    "closeness": 15,
    "owner_rivalry": 15,
    "franchise_rivalry": 10,
    "stakes": 15,
    "stars": 10,
    "fairness": 25,     # ± — an over-exposed pairing loses up to this much
    "repeat": 15,       # penalty only
}
LABELS = {
    "quality": "Quality", "closeness": "Closeness", "owner_rivalry": "Owner rivalry",
    "franchise_rivalry": "Franchise rivalry", "stakes": "Stakes", "stars": "Star power",
    "fairness": "Fair share", "repeat": "Recent repeat",
}
# Multipliers on WEIGHTS per preset; a factor not named stays at ×1.
PRIORITIES = {
    "balanced": {"label": "Balanced", "desc": "The standard weights", "mult": {}},
    "coverage": {"label": "Team coverage", "desc": "Fair share and recent repeats count for much more, so every team gets on stream",
                 "mult": {"fairness": 2.5, "repeat": 2.0, "quality": 0.5, "stars": 0.5}},
    "rivalry": {"label": "Rivalries", "desc": "Owner and franchise rivalries and close games first",
              "mult": {"owner_rivalry": 2.5, "franchise_rivalry": 2.0, "closeness": 1.5, "fairness": 0.5}},
    "quality": {"label": "Team quality", "desc": "Good teams and star players first",
                "mult": {"quality": 2.0, "stars": 2.0, "closeness": 1.5, "fairness": 0.5}},
    "stakes": {"label": "Playoff race", "desc": "Games with playoff seeding on the line first",
               "mult": {"stakes": 2.5, "closeness": 1.5, "quality": 1.25}},
}
DEFAULT_PRIORITY = "balanced"


def effective_weights(priority: str) -> dict[str, float]:
    mult = PRIORITIES[priority]["mult"]
    return {k: round(w * mult.get(k, 1.0), 2) for k, w in WEIGHTS.items()}


MAX_DAYS = 31
DEFAULT_DAYS = 7
COOLDOWN_DAYS = 7          # a team on stream this close (either side) can't be the pick
REPEAT_NEAR_DAYS = 3       # and this close costs the most
REPEAT_NEAR_PTS = 10       # points off per team within REPEAT_NEAR_DAYS ...
REPEAT_FAR_PTS = 5         # ... and within COOLDOWN_DAYS, capped at WEIGHTS["repeat"]
SPREAD_FOR_ZERO = 12.0     # a projected spread this wide scores no closeness
OWNER_GAMES_FULL = 30      # meetings for a full volume score
FRANCHISE_GAMES_FULL = 24
STAR_FLOOR, STAR_SPAN = 78.0, 18.0   # OVR 78 scores nothing, 96 scores full
FAIR_FULL = 1.0            # streams under the even share for the full fairness bonus

DIVISIONS = {
    "Atlantic": {"NYK", "TOR", "BOS", "PHI", "BKN"},
    "Central": {"DET", "CLE", "MIL", "CHI", "IND"},
    "Southeast": {"ORL", "ATL", "MIA", "CHA", "WAS"},
    "Northwest": {"OKC", "DEN", "MIN", "UTA", "POR"},
    "Pacific": {"LAL", "PHX", "GSW", "SAC", "LAC"},
    "Southwest": {"SAS", "HOU", "MEM", "DAL", "NOP"},
}
_DIVISION_OF = {t: d for d, ts in DIVISIONS.items() for t in ts}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pts(key: str, frac: float, detail: str) -> dict:
    """A factor's contribution: `frac` of its weight (negative allowed for the
    two that can cost points), rounded for display."""
    return {"pts": round(WEIGHTS[key] * frac, 1), "max": WEIGHTS[key],
            "label": LABELS[key], "detail": detail}


def _rec(w: int, l: int) -> str:
    return f"{w}–{l}"


def _even(wins: int, games: int) -> float:
    """1 for a dead-even record, 0 for a sweep."""
    return 1.0 - abs(2 * wins / games - 1) if games else 0.0


# ── Head-to-head, from the game index ────────────────────────────────────────

def all_games(index: gh.GameIndex) -> list[dict]:
    """One row per game ever played (both kinds): season, date, home, away,
    winner, playoff. Like `playoff_odds.season_results`, for every season."""
    s, m, c = index.strings.values, index.meta, index.cols
    seen: dict[tuple, dict] = {}
    for i in range(index.n):
        team, opp, d = s[m["team"][i]], s[m["opp"][i]], s[m["date"][i]]
        home, away = (team, opp) if index.home[i] else (opp, team)
        key = (d, home, away)
        if key in seen:
            continue
        hp, ap = (c["team_pts"][i], c["opp_pts"][i]) if index.home[i] else (c["opp_pts"][i], c["team_pts"][i])
        if hp == ap:
            continue
        seen[key] = {"date": d, "season": s[m["season"][i]], "home": home, "away": away,
                     "winner": home if hp > ap else away, "playoff": bool(index.playoff[i])}
    return list(seen.values())


def franchise_h2h(games: list[dict]) -> dict[frozenset, dict]:
    """{frozenset({A, B}): {games, wins: {team: n}, series}} — series counts
    distinct playoff (season, pair) meetings."""
    out: dict[frozenset, dict] = defaultdict(lambda: {"games": 0, "wins": defaultdict(int), "series": set()})
    for g in games:
        k = frozenset((g["home"], g["away"]))
        rec = out[k]
        rec["games"] += 1
        rec["wins"][g["winner"]] += 1
        if g["playoff"]:
            rec["series"].add(g["season"])
    return out


def owner_on(tenures: list[dict], team: str, day: date) -> Optional[str]:
    for t in tenures:
        if t["TEAM"] == team and t["start"] <= day <= t["end"]:
            return t["owner"]
    return None


def owner_h2h(games: list[dict], tenures: list[dict]) -> dict[frozenset, dict]:
    """The same shape as `franchise_h2h`, keyed by the two owners on game day
    (`load_owner_tenures` attribution, as `owner_stats` does). A game whose
    side has no owner on record isn't counted; nor is one owner vs himself."""
    by_team: dict[str, list[dict]] = defaultdict(list)
    for t in tenures:
        by_team[t["TEAM"]].append(t)
    out: dict[frozenset, dict] = defaultdict(lambda: {"games": 0, "wins": defaultdict(int), "series": set()})
    for g in games:
        d = date.fromisoformat(g["date"])
        oh = owner_on(by_team.get(g["home"], []), g["home"], d)
        oa = owner_on(by_team.get(g["away"], []), g["away"], d)
        if not oh or not oa or oh == oa:
            continue
        rec = out[frozenset((oh, oa))]
        rec["games"] += 1
        rec["wins"][oh if g["winner"] == g["home"] else oa] += 1
        if g["playoff"]:
            rec["series"].add((g["season"], frozenset((g["home"], g["away"]))))
    return out


# ── Team strength with injuries ──────────────────────────────────────────────

def weighted_top8(ovrs: list[float]) -> Optional[float]:
    """`playoff_odds.roster_strength`'s weighting, on a list of OVRs."""
    top = sorted(ovrs, reverse=True)[:len(po.MINUTES)]
    if not top:
        return None
    w = po.MINUTES[:len(top)]
    return sum(v * x for v, x in zip(top, w)) / sum(w)


def injury_adjustments(rosters: dict[str, list[dict]], out_slugs: set[str]) -> dict[str, float]:
    """Rating change, in points per game, from playing without the Out players:
    the roster prior healthy minus the prior in full (`roster_priors`, so the
    two are on the playoff-odds scale)."""
    full = {t: weighted_top8([p["ovr"] for p in ps]) for t, ps in rosters.items()}
    healthy = {t: weighted_top8([p["ovr"] for p in ps if p["slug"] not in out_slugs]) for t, ps in rosters.items()}
    pf, ph = po.roster_priors(full), po.roster_priors(healthy)
    # roster_priors re-centres each set on its own mean; put both on the full
    # set's scale so a team's adjustment is only its own missing players.
    known = [v for v in full.values() if v is not None]
    if len(known) < 2:
        return {t: 0.0 for t in rosters}
    mean = sum(known) / len(known)
    sd = math.sqrt(sum((v - mean) ** 2 for v in known) / len(known)) or 1.0
    return {t: ((healthy[t] - full[t]) / sd * po.PRIOR_SD) if full[t] is not None and healthy[t] is not None else 0.0
            for t in rosters}


# ── Scoring one game ─────────────────────────────────────────────────────────

def _quality(away, home, ratings: dict[str, float]) -> dict:
    vals = list(ratings.values())
    n = max(len(vals) - 1, 1)
    # Tied teams share a percentile (the mean of their places), so equal
    # ratings — every team at 0.0 before the first game — don't rank by name.
    def pct_of(t):
        v = ratings[t]
        below = sum(1 for x in vals if x < v)
        ties = sum(1 for x in vals if x == v)
        return (below + (ties - 1) / 2) / n
    a = pct_of(away) if away in ratings else 0.5
    h = pct_of(home) if home in ratings else 0.5
    frac = 0.6 * (a + h) / 2 + 0.4 * min(a, h)
    def rk(t):
        return 1 + sum(1 for x in vals if x > ratings[t]) if t in ratings else "?"
    return _pts("quality", frac, f"Rating rank {away} #{rk(away)}, {home} #{rk(home)} of {len(vals)}")


def _closeness(away, home, ratings) -> dict:
    line = po.game_line(home, away, ratings)
    margin, p_home = line["margin"], line["p_home"]
    fav, spread = (home, margin) if margin >= 0 else (away, -margin)
    frac = _clamp(1 - spread / SPREAD_FOR_ZERO)
    return _pts("closeness", frac, f"{fav} by {spread:.1f} projected · {home} {round(p_home * 100)}% to win")


def _rivalry(key, rec: Optional[dict], full: int, names: tuple[str, str], extra: float = 0.0,
             extra_note: str = "") -> dict:
    a, b = names
    if not rec or not rec["games"]:
        frac = _clamp(extra)
        note = f"{a} and {b} have never met" + (f" · {extra_note}" if extra_note else "")
        return _pts(key, frac, note)
    g = rec["games"]
    wa, wb = rec["wins"].get(a, 0), rec["wins"].get(b, 0)
    series = len(rec["series"])
    frac = _clamp(min(g / full, 1) * (0.4 + 0.6 * _even(wa, g)) + 0.2 * min(series, 3) + extra)
    note = f"{a} {wa}–{wb} {b} in {g} games"
    if series:
        note += f" · {series} playoff series"
    if extra_note:
        note += f" · {extra_note}"
    return _pts(key, frac, note)


def _stakes(away, home, odds: dict[str, dict], season_frac: float) -> dict:
    oa, oh = odds.get(away) or {}, odds.get(home) or {}
    if not oa or not oh:
        return _pts("stakes", 0, "No playoff odds yet")
    ua = 4 * oa["playoffs"] * (1 - oa["playoffs"])
    uh = 4 * oh["playoffs"] * (1 - oh["playoffs"])
    race = 0.0
    if oa.get("conf") == oh.get("conf"):
        gap = abs(oa["proj_w"] - oh["proj_w"])
        race = _clamp(1 - gap / 6)
    frac = _clamp(((ua + uh) / 2 * 0.7 + race * 0.3) * (0.6 + 0.4 * season_frac))
    note = f"Playoff odds {away} {round(oa['playoffs'] * 100)}%, {home} {round(oh['playoffs'] * 100)}%"
    if race >= 0.5:
        note += f" · same-conference race ({oa['proj_w']:.0f} vs {oh['proj_w']:.0f} proj. wins)"
    return _pts("stakes", frac, note)


def _stars(away, home, rosters, out_slugs, names) -> dict:
    def best(t):
        healthy = sorted((p for p in rosters.get(t, []) if p["slug"] not in out_slugs),
                         key=lambda p: -p["ovr"])
        return healthy[0] if healthy else None
    def hurt(t):
        return [p for p in rosters.get(t, []) if p["slug"] in out_slugs and p["ovr"] >= STAR_FLOOR]
    ba, bh = best(away), best(home)
    sa = _clamp((ba["ovr"] - STAR_FLOOR) / STAR_SPAN) if ba else 0
    sh = _clamp((bh["ovr"] - STAR_FLOOR) / STAR_SPAN) if bh else 0
    frac = 0.6 * max(sa, sh) + 0.4 * min(sa, sh)
    parts = [f"{names.get(p['slug'], p['slug'])} {p['ovr']:.0f}" for p in (ba, bh) if p]
    note = "Best healthy: " + " · ".join(parts) if parts else "No ratings"
    out = hurt(away) + hurt(home)
    if out:
        note += " · out: " + ", ".join(f"{names.get(p['slug'], p['slug'])} ({p['ovr']:.0f})" for p in out)
    return _pts("stars", frac, note)


def _fairness(away, home, counts: dict[str, int], share: float) -> dict:
    da, dh = share - counts.get(away, 0), share - counts.get(home, 0)
    frac = _clamp((da + dh) / 2 / FAIR_FULL, -1, 1)
    note = f"Scheduled streams {away} {counts.get(away, 0)}, {home} {counts.get(home, 0)} · even share {share:.1f}"
    return _pts("fairness", frac, note)


def nearest_stream(team: str, day: date, on_stream: dict[str, list[date]]) -> Optional[date]:
    """The team's stream closest to `day` (either side, not `day` itself)."""
    near = [d for d in on_stream.get(team, []) if d != day]
    return min(near, key=lambda d: (abs((d - day).days), d)) if near else None


def cooldown(away: str, home: str, day: date, on_stream: dict[str, list[date]]) -> Optional[dict]:
    """{team, date, days} for the closer clash within COOLDOWN_DAYS, or None."""
    hits = []
    for t in (away, home):
        d = nearest_stream(t, day, on_stream)
        if d and abs((d - day).days) <= COOLDOWN_DAYS:
            hits.append({"team": t, "date": d.isoformat(), "days": abs((d - day).days)})
    return min(hits, key=lambda h: h["days"]) if hits else None


def _repeat(away, home, day: date, on_stream: dict[str, list[date]]) -> dict:
    hits, pen = [], 0
    for t in (away, home):
        d = nearest_stream(t, day, on_stream)
        if d is None:
            continue
        gap = abs((d - day).days)
        if gap <= COOLDOWN_DAYS:
            pen += REPEAT_NEAR_PTS if gap <= REPEAT_NEAR_DAYS else REPEAT_FAR_PTS
            hits.append(f"{t} on stream {d.strftime('%b %-d')}")
    pen = min(pen, WEIGHTS["repeat"])
    return {"pts": -float(pen), "max": WEIGHTS["repeat"], "label": LABELS["repeat"],
            "detail": " · ".join(hits) if hits else f"Neither team on stream within {COOLDOWN_DAYS} days"}


def score_game(g: dict, day: date, ctx: dict) -> dict:
    away, home = g["away_team"], g["home_team"]
    oa, oh = ctx["owners"].get(away), ctx["owners"].get(home)
    same_div = _DIVISION_OF.get(away) and _DIVISION_OF.get(away) == _DIVISION_OF.get(home)
    if oa and oh and oa != oh:
        orec = ctx["owner_h2h"].get(frozenset((oa, oh)))
        owner_f = _rivalry("owner_rivalry", orec, OWNER_GAMES_FULL, (oa, oh))
    else:
        owner_f = _pts("owner_rivalry", 0, "Owner not on record" if not (oa and oh) else "Same owner")
    factors = {
        "quality": _quality(away, home, ctx["ratings"]),
        "closeness": _closeness(away, home, ctx["ratings"]),
        "owner_rivalry": owner_f,
        "franchise_rivalry": _rivalry("franchise_rivalry", ctx["franchise_h2h"].get(frozenset((away, home))),
                                      FRANCHISE_GAMES_FULL, (away, home), 0.15 if same_div else 0.0,
                                      f"{_DIVISION_OF[away]} division" if same_div else ""),
        "stakes": _stakes(away, home, ctx["odds"], ctx["season_frac"]),
        "stars": _stars(away, home, ctx["rosters"], ctx["out"], ctx["names"]),
        "fairness": _fairness(away, home, ctx["counts"], ctx["share"]),
        "repeat": _repeat(away, home, day, ctx["on_stream"]),
    }
    for k, m in (ctx.get("mult") or {}).items():
        if m != 1.0 and k in factors:
            f = factors[k]
            f["pts"], f["max"] = round(f["pts"] * m, 1), round(f["max"] * m, 2)
    oda, odh = ctx["odds"].get(away) or {}, ctx["odds"].get(home) or {}
    return {
        "id": g["id"], "date": g["date"], "time_et": g.get("time_et") or "",
        "away": away, "home": home,
        "away_record": _rec(oda["w"], oda["l"]) if oda else None,
        "home_record": _rec(odh["w"], odh["l"]) if odh else None,
        "away_owner": oa, "home_owner": oh,
        "streamer": g.get("streamer") or None, "note": g.get("note") or "",
        "score": round(sum(f["pts"] for f in factors.values()), 1),
        "factors": factors,
    }


# ── The plan ─────────────────────────────────────────────────────────────────

def _is_scheduled_stream(g: dict) -> bool:
    return bool(g.get("stream")) and not g.get("stream_buyer")


def _is_bought(g: dict) -> bool:
    return bool(g.get("stream")) and bool(g.get("stream_buyer"))


def _conference(team: str) -> str:
    return "East" if team in po.EAST else "West"


def meetings_index(schedule: list[dict], results: list[dict]) -> dict[frozenset, list[dict]]:
    """This season's games between each pair: every scheduled meeting with its
    score once played (`season_results`, matched on date + home + away) and
    whether it was on stream. Date order."""
    scores = {(r["date"], r["home"], r["away"]): r for r in results}
    out: dict[frozenset, list[dict]] = defaultdict(list)
    for g in schedule:
        r = scores.get((g["date"], g["home_team"], g["away_team"]))
        out[frozenset((g["away_team"], g["home_team"]))].append({
            "id": g["id"], "date": g["date"], "away": g["away_team"], "home": g["home_team"],
            "away_pts": r["away_pts"] if r else None, "home_pts": r["home_pts"] if r else None,
            "streamed": bool(g.get("stream"))})
    for v in out.values():
        v.sort(key=lambda m: m["date"])
    return out


def plan(start: date, end: date, schedule: list[dict], *, ratings: dict[str, float],
         odds: dict[str, dict], season_frac: float, rosters: dict[str, list[dict]],
         out: set[str], names: dict[str, str], owners: dict[str, str],
         owner_h2h: dict, franchise_h2h: dict,
         picks: Optional[set[str]] = None, skips: Optional[set[str]] = None,
         results: Optional[list[dict]] = None, today: Optional[date] = None,
         priority: str = DEFAULT_PRIORITY, open_days: Optional[int] = None) -> dict:
    """The whole plan for [start, end]. `schedule` is the season's games
    (schedule-{season}.json rows); `picks` are game ids the caller has chosen,
    `skips` dates it leaves empty; `results` are the season's played games
    (`playoff_odds.season_results`) for the meetings. The rest is described in
    the module docstring. Pure."""
    picks, skips = set(picks or ()), set(skips or ())
    if priority not in PRIORITIES:
        raise ValueError(f"unknown priority {priority!r}")
    mult = PRIORITIES[priority]["mult"]
    open_seen = 0
    today = today or start
    counts: dict[str, int] = defaultdict(int)          # scheduled streams, had + flagged
    bought: dict[str, int] = defaultdict(int)
    on_stream: dict[str, list[date]] = defaultdict(list)
    last: dict[str, Optional[str]] = {}
    for g in schedule:
        d = date.fromisoformat(g["date"])
        if not g.get("stream"):
            continue
        for t in (g["away_team"], g["home_team"]):
            (counts if _is_scheduled_stream(g) else bought)[t] += 1
            on_stream[t].append(d)
            if d <= today and (last.get(t) or "") < g["date"]:
                last[t] = g["date"]
    stream_dates = {g["date"] for g in schedule if _is_scheduled_stream(g)}
    days_with_games = sorted({g["date"] for g in schedule})
    teams = sorted(VALID_TEAMS)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for g in schedule:
        by_date[g["date"]].append(g)
    meets = meetings_index(schedule, results or [])

    picked: dict[str, int] = defaultdict(int)
    used_picks: set[str] = set()
    out_days = []
    day = start
    while day <= end:
        ds = day.isoformat()
        games = by_date.get(ds, [])
        locked = next((g for g in games if _is_scheduled_stream(g)), None)
        bought_games = [g for g in games if _is_bought(g)]
        entry = {"date": ds, "games_count": len(games),
                 "locked": _brief(locked) if locked else None,
                 "bought": [_brief(g) for g in bought_games],
                 "games": [], "projected_pick": None, "pick_source": None,
                 "forced": False, "skipped": False}
        if games and not locked:
            # The even share: every stream day so far, every pick in this plan
            # and this one give two teams a scheduled stream, over thirty teams.
            stream_days = len(stream_dates | {d["date"] for d in out_days if d["projected_pick"]}) + 1
            share = 2 * stream_days / len(teams)
            ctx = {"ratings": ratings, "odds": odds, "season_frac": season_frac, "rosters": rosters,
                   "out": out, "names": names, "owners": owners, "owner_h2h": owner_h2h,
                   "franchise_h2h": franchise_h2h,
                   "counts": {t: counts[t] + picked[t] for t in teams},
                   "share": share, "on_stream": on_stream, "mult": mult}
            ranked = []
            for g in games:
                if _is_bought(g):
                    continue
                r = score_game(g, day, ctx)
                r["cooldown"] = cooldown(r["away"], r["home"], day, on_stream)
                r["meetings"] = [m for m in meets.get(frozenset((r["away"], r["home"])), []) if m["id"] != r["id"]]
                r["last_stream"] = {"away": last.get(r["away"]), "home": last.get(r["home"])}
                ranked.append(r)
            ranked.sort(key=lambda r: (-r["score"], r["time_et"], r["home"]))
            for i, r in enumerate(ranked):
                r["rank"] = i + 1
            entry["games"] = ranked
            chosen = next((r for r in ranked if r["id"] in picks), None)
            pick = None
            if chosen:
                pick, entry["pick_source"] = chosen, "chosen"
                used_picks.add(chosen["id"])
            elif ds in skips:
                entry["skipped"] = True
            elif ranked:
                fresh = [r for r in ranked if not r["cooldown"]]
                if fresh:
                    pick = fresh[0]
                else:   # every game clashes: the one whose clash is furthest away
                    pick = max(ranked, key=lambda r: (r["cooldown"]["days"], r["score"]))
                    entry["forced"] = True
                entry["pick_source"] = "projected"
            if pick:
                entry["projected_pick"] = pick["id"]
                for t in (pick["away"], pick["home"]):
                    picked[t] += 1
                    on_stream[t].append(day)
        out_days.append(entry)
        if games and not locked:
            open_seen += 1
        if open_days and open_seen >= open_days:
            break
        day += timedelta(days=1)

    total_streams = len(stream_dates) + sum(1 for d in out_days if d["projected_pick"])
    share = 2 * total_streams / len(teams) if teams else 0
    exposure = sorted(({"team": t, "conference": _conference(t), "division": _DIVISION_OF.get(t),
                        "scheduled": counts[t], "picked": picked[t], "bought": bought[t],
                        "last_stream": last.get(t),
                        "vs_share": round(counts[t] + picked[t] - share, 2)} for t in teams),
                      key=lambda r: (r["scheduled"] + r["picked"], r["team"]))

    def group(name: str, members: list[str]) -> dict:
        return {"name": name, "teams": len(members),
                "appearances": sum(counts[t] + picked[t] for t in members),
                "share": round(share * len(members), 2)}
    balance = {
        "conferences": [group(c, [t for t in teams if _conference(t) == c]) for c in ("East", "West")],
        "divisions": [group(dv, sorted(ts)) for dv, ts in DIVISIONS.items()],
    }
    return {"from": start.isoformat(), "to": out_days[-1]["date"] if out_days else end.isoformat(),
            "weights": effective_weights(priority), "labels": LABELS, "priority": priority,
            "priorities": [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in PRIORITIES.items()],
            "open_days": open_days, "open_days_found": open_seen,
            "open_days_short": bool(open_days) and open_seen < open_days,
            "cooldown_days": COOLDOWN_DAYS, "share": round(share, 2), "exposure": exposure,
            "balance": balance, "days": out_days, "season_days": len(days_with_games),
            "ignored_picks": sorted(picks - used_picks)}


def _brief(g: dict) -> dict:
    return {"id": g["id"], "away": g["away_team"], "home": g["home_team"], "time_et": g.get("time_et") or "",
            "streamer": g.get("streamer") or None, "buyer": g.get("stream_buyer") or None}


# ── Loaders ──────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_h2h_cache: dict = {"key": None, "franchise": None, "owner": None}


def _h2h(today: date) -> tuple[dict, dict, list[dict]]:
    """Franchise and owner head-to-head, rebuilt when the box scores or the
    tenures change (the game index's own key + owners.csv's mtime)."""
    from stats_build.pipeline import load_owner_tenures
    index, key = gh.get_index()
    owners_path = DATA_DIR / "owners.csv"
    full_key = (key, owners_path.stat().st_mtime if owners_path.exists() else None, today)
    with _cache_lock:
        if _h2h_cache["key"] != full_key:
            tenures = load_owner_tenures(DATA_DIR, today)
            games = all_games(index)
            _h2h_cache.update(key=full_key, franchise=franchise_h2h(games),
                              owner=owner_h2h(games, tenures), tenures=tenures)
        return _h2h_cache["franchise"], _h2h_cache["owner"], _h2h_cache["tenures"]


def _rosters(bios: dict, ovr: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in sorted(VALID_TEAMS):
        path = DATA_DIR / f"{t.lower()}-roster.csv"
        rows = read_csv(path)[1] if path.exists() else []
        ps = []
        for r in rows:
            slug = (r.get("SLUG") or "").strip()
            if not slug or (bios.get(slug) or {}).get("type") in ("draft-rights", "dead"):
                continue
            hist = ovr.get(slug) or []
            if hist and hist[-1].get("ovr") is not None:
                ps.append({"slug": slug, "ovr": float(hist[-1]["ovr"])})
        out[t] = ps
    return out


def out_slugs(injuries: dict) -> set[str]:
    players = injuries.get("players") or {}
    rows = players.values() if isinstance(players, dict) else players
    return {r["slug"] for r in rows if (r.get("status") or "").lower() == "out" and r.get("slug")}


def odds_inputs(run: Optional[dict]) -> tuple[dict, dict, float, Optional[str]]:
    """(ratings, odds by team, how far through the season 0..1, as-of date)."""
    if not run:
        return {}, {}, 0.0, None
    odds = {r["team"]: r for r in run.get("teams", [])}
    played, left = run.get("games_played") or 0, run.get("games_left") or 0
    frac = played / (played + left) if played + left else 0.0
    return {t: r["rating"] for t, r in odds.items()}, odds, frac, run.get("date")


def current_ratings(rosters: dict[str, list[dict]], out: set[str], run: Optional[dict]) -> dict[str, float]:
    """Each team's rating tonight: the playoff-odds run's (the roster prior
    alone before the first run), less what its Out players are worth. The one
    strength both the planner and the homepage's lines (`tonight.py`) use."""
    ratings = odds_inputs(run)[0]
    if not ratings:   # before the first odds run: the roster prior alone
        ratings = po.roster_priors({t: weighted_top8([p["ovr"] for p in ps]) for t, ps in rosters.items()})
    adj = injury_adjustments(rosters, out)
    return {t: r + adj.get(t, 0.0) for t, r in ratings.items()}


def ratings_now() -> dict[str, float]:
    """`current_ratings` from the files on disk."""
    rosters = _rosters(player_bios_shared(), ovr_shared())
    return current_ratings(rosters, out_slugs(injuries_mod.load_injuries()),
                           _load_json(po.CURRENT_FILE, None))


@router.get("/api/stream-planner")
def get_stream_planner(from_: Optional[str] = Query(default=None, alias="from"),
                       to: Optional[str] = Query(default=None),
                       picks: Optional[str] = Query(default=None),
                       skip: Optional[str] = Query(default=None),
                       priority: str = Query(default=DEFAULT_PRIORITY),
                       open_days: Optional[int] = Query(default=None),
                       info: dict = Depends(require_any_role("streamer"))):
    """`picks`: comma-separated game ids the caller has chosen; `skip`:
    comma-separated dates it leaves empty. Both steer the days after them.
    `priority`: a PRIORITIES key. `open_days`: plan until this many open
    days instead of to `to` (see the module docstring)."""
    if priority not in PRIORITIES:
        raise HTTPException(status_code=422, detail=f"priority must be one of {', '.join(PRIORITIES)}")
    if open_days is not None and not 1 <= open_days <= MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"open_days must be 1 to {MAX_DAYS}")
    today = date.fromisoformat(league_today_str())
    pick_ids = {x.strip() for x in (picks or "").split(",") if x.strip()}
    skip_dates = {x.strip() for x in (skip or "").split(",") if x.strip()}
    if len(pick_ids) > MAX_DAYS or len(skip_dates) > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_DAYS} picks or skips")
    for d in skip_dates:
        try:
            date.fromisoformat(d)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"skip dates must be YYYY-MM-DD, got {d!r}")
    try:
        start = date.fromisoformat(from_) if from_ else today
        end = date.fromisoformat(to) if to and not open_days else start + timedelta(
            days=(MAX_DAYS if open_days else DEFAULT_DAYS) - 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")
    if end < start:
        raise HTTPException(status_code=422, detail="'to' is before 'from'")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_DAYS} days at a time")

    season = _current_league_year()
    schedule = po.load_schedule(season)
    bios, ovr = player_bios_shared(), ovr_shared()
    rosters = _rosters(bios, ovr)
    out = out_slugs(injuries_mod.load_injuries())
    run = _load_json(po.CURRENT_FILE, None)
    _, odds, frac, as_of = odds_inputs(run)
    ratings = current_ratings(rosters, out, run)
    franchise, owner, tenures = _h2h(today)
    owners = {t: owner_on(tenures, t, today) for t in VALID_TEAMS}
    names = {slug: player_name((b or {}).get("name") or slug) for slug, b in bios.items()}
    result = plan(start, end, schedule, ratings=ratings, odds=odds, season_frac=frac, rosters=rosters,
                  out=out, names=names, owners={t: o for t, o in owners.items() if o},
                  owner_h2h=owner, franchise_h2h=franchise, picks=pick_ids, skips=skip_dates,
                  results=po.season_results(gh.get_index()[0], season), today=today,
                  priority=priority, open_days=open_days)
    return {"season": season, "today": today.isoformat(), "odds_as_of": as_of, **result}

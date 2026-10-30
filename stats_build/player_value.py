"""Win Shares, Box Plus/Minus and VORP, per player stint.

    python3 -m stats_build.player_value --dry-run   # print the leaders, write nothing

Written by `stats_build.advanced` (its `write`), so `build.sh` needs no step of
its own. One output, under `derived/`:

- `players/player-value.csv`: one row per player per season per team per
  `TYPE` (REG / PLAYOFF), keyed like `player_seasons.csv` (SLUG, SEASON, TEAM).
  For a traded player's combined line the page adds WS and VORP and takes
  BPM weighted by minutes, which is what these stats are built to allow.

Both follow Basketball-Reference's published methods, with one difference
that runs through everything: a player's "team" totals are summed over the
games he played (the game context in `advanced.py`), not the team's whole
season. That is more exact for a player who missed games or was traded, and
it is the same choice the rate stats make.

**Win Shares** (basketball-reference.com/about/ws.html): Dean Oliver's
individual points produced, possessions and Defensive Rating
(basketball-reference.com/about/ratings.html), then

    OWS = (PProd - 0.92 * lgPPP * TotPoss) / marginal points per win
    DWS = (MP/TmMP) * TmDefPoss * (1.08 * lgPPP - DRtg/100) / marginal points per win
    marginal points per win = 0.32 * lgPPG * (team pace / league pace)

with one recalibration: 0.32 is the NBA's, and NBN's wider margins make a
point worth fewer wins, so `PTS_PER_WIN` replaces it (and 0.92 / 1.08, which
are 1 -/+ 0.32/4, follow it). With 0.32, team Win Shares missed actual wins
by 4.5 on average and in proportion to margin; see the constant.

**BPM 2.0** (basketball-reference.com/about/bpm2.html): a regression on
per-100-possession box stats, its coefficients interpolated by an estimated
position (1 = PG .. 5 = C) and offensive role (1 = creator .. 5 = receiver),
plus a team adjustment so the team's minutes-weighted BPM equals its rating.
Two inputs the page describes without giving a number are set here:

- Points are adjusted to a baseline of 1.0 point per shot attempt
  (FGA + 0.44 FTA) at the team's level of shooting. 1.0 is the value that
  reproduces the page's worked example (2017 LeBron, 34.9 adjusted to 30.4).
- The team's average lead is estimated as half its average margin (a lead
  that grows evenly through the game); the page's own example estimates 2017
  Cleveland's at 1.4 on a 3.2 margin.

The team rating is the plain net rating per 100, not adjusted for schedule.

**VORP** = (BPM + 2.0) * (share of the team's minutes) * (team games / 82).

Possessions are estimated, as everywhere in `advanced.py`: there are no team
turnovers in the box scores.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from stats_build import advanced, csvio
from stats_build.buildargs import DATA_DIR, OUT_DIR, BuildArgs

OUTPUT = "players/player-value.csv"

HEADER = ["SLUG", "SEASON", "TEAM", "TYPE", "G", "MIN",
          "OWS", "DWS", "WS", "WS48", "OBPM", "DBPM", "BPM", "VORP", "POS_EST", "ROLE_EST"]

STATS = advanced.BOX            # own and context totals use the same columns
POS_NUM = {"PG": 1, "SG": 2, "SF": 3, "PF": 4, "C": 5}
PRIOR_MIN = 50                  # minutes of prior at the listed position / role 4
REPLACEMENT = -2.0

# (position-1 value, position-5 value); interpolated linearly in position.
BPM_POS = {"P": (0.860, 0.860), "3PM": (0.389, 0.389), "A": (0.580, 1.034),
           "TO": (-0.964, -0.964), "OR": (0.613, 0.181), "DR": (0.116, 0.181),
           "S": (1.369, 1.008), "B": (1.327, 0.703), "PF": (-0.367, -0.367)}
OBPM_POS = {"P": (0.605, 0.605), "3PM": (0.477, 0.477), "A": (0.476, 0.476),
            "TO": (-0.579, -0.882), "OR": (0.606, 0.422), "DR": (-0.112, 0.103),
            "S": (0.177, 0.294), "B": (0.725, 0.097), "PF": (-0.439, -0.439)}
# (role-1 value, role-5 value); interpolated linearly in offensive role.
BPM_ROLE = {"FGA": (-0.560, -0.780), "FTA": (-0.246, -0.343)}
OBPM_ROLE = {"FGA": (-0.330, -0.472), "FTA": (-0.145, -0.208)}
# Position constant at position 1 (zero from 3 up); role constant at role 5.
BPM_CONST = (-0.818, 2.774)
OBPM_CONST = (-1.698, 0.860)
PTS_BASELINE = 1.0              # points per shot attempt the regression assumes
THRESHOLD_GAP = 0.33            # threshold points: this far below team pts/TSA
LEAD_EFFECT = 0.35              # pts/100 a leading team gives back per point of lead
# Win Shares' points-per-win share: 0.32 in the NBA. NBN's margins run wider
# (24-25 HOU lost by 24 a game and still won 8), so a point of margin buys
# fewer wins; fitted to NBN's own records. The 0.92 / 1.08 baselines are
# 1 -/+ this over 4 in the original, which is what puts an average team at
# half its games, so they follow it. tests/test_player_value.py refits it.
PTS_PER_WIN = 0.42


def _div(n: float, d: float) -> float:
    return n / d if d else 0.0


def _lerp(pair: tuple[float, float], x: float) -> float:
    """Value at x on the 1..5 scale, from the values at 1 and at 5."""
    return pair[0] + (pair[1] - pair[0]) * (x - 1) / 4


def raw_bpm(per100: dict, pos: float, role: float, offense: bool = False) -> float:
    """The regression part of BPM (or OBPM): before the team adjustment.

    `per100` holds per-100-possession numbers with `P` already adjusted for
    the team's shooting. Pinned against the page's 2017 LeBron example.
    """
    by_pos, by_role, const = (OBPM_POS, OBPM_ROLE, OBPM_CONST) if offense \
        else (BPM_POS, BPM_ROLE, BPM_CONST)
    total = sum(_lerp(c, pos) * per100[k] for k, c in by_pos.items())
    total += sum(_lerp(c, role) * per100[k] for k, c in by_role.items())
    total += const[0] * (3 - pos) / 2 if pos < 3 else 0.0
    total += const[1] * (role - 3) / 2
    return total


def _center(values: list[float], weights: list[float]) -> list[float]:
    """Shift every value by one constant, then clip to [1, 5], so the weighted
    mean is 3.0. The shift is found before the clip, as the BPM page says,
    hence a search rather than one subtraction."""
    total = sum(weights)
    if not total:
        return [3.0] * len(values)
    def mean(c):
        return sum(w * min(5.0, max(1.0, v + c)) for v, w in zip(values, weights)) / total
    lo, hi = -10.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if mean(mid) < 3.0:
            lo = mid
        else:
            hi = mid
    c = (lo + hi) / 2
    return [min(5.0, max(1.0, v + c)) for v in values]


def _listed_positions(data_dir: Path) -> dict[str, int]:
    path = data_dir / "player-bios.json"
    if not path.exists():
        return {}
    out = {}
    for slug, bio in json.loads(path.read_text()).items():
        primary = (bio.get("position") or {}).get("primary") or (bio.get("pos") or [None])[0]
        if primary in POS_NUM:
            out[slug] = POS_NUM[primary]
    return out


def _stints(rows: list[dict]) -> tuple[dict, dict]:
    """Own totals and game context per (slug, team); and per-team-season sums."""
    games = {(g["SEASON"], g["DATE"], g["TEAM"], g["OPP"]): g for g in advanced.team_games(rows)}
    stints: dict = {}
    for r in rows:
        g = games.get((r["SEASON"], r["DATE"], r.get("TEAM"), (r.get("OPP") or "").replace("@", "")))
        if g is None:
            continue
        key = (advanced._slug(r["PLAYER"]), r["SEASON"], r["TEAM"])
        st = stints.setdefault(key, {"own": dict.fromkeys(STATS, 0.0), "tm": dict.fromkeys(STATS, 0.0),
                                     "opp": dict.fromkeys(STATS, 0.0), "POSS": 0.0, "G": 0})
        for col in STATS:
            v = advanced.pipeline._stat(r, col)
            if v is not None:
                st["own"][col] += v
            st["tm"][col] += g["own"][col]
            st["opp"][col] += g["opp"][col]
        st["POSS"] += g["POSS"]
        st["G"] += 1
    team: dict = defaultdict(lambda: {"own": dict.fromkeys(STATS, 0.0), "opp": dict.fromkeys(STATS, 0.0),
                                      "POSS": 0.0, "G": 0})
    for g in games.values():
        t = team[(g["SEASON"], g["TEAM"])]
        for col in STATS:
            t["own"][col] += g["own"][col]
            t["opp"][col] += g["opp"][col]
        t["POSS"] += g["POSS"]
        t["G"] += 1
    return stints, team


def win_shares(p: dict, t: dict, o: dict, poss: float, lg: dict) -> tuple[float, float]:
    """(OWS, DWS) for one stint: `p` own totals, `t`/`o` team and opponent
    totals over his games, `poss` their possessions. Oliver's formulas as
    basketball-reference.com/about/ratings.html writes them."""
    mp = p["M"]
    if mp <= 0 or t["M"] <= 0:
        return 0.0, 0.0
    t_trb, o_trb = t["OR"] + t["DR"], o["OR"] + o["DR"]
    share = mp / (t["M"] / 5)

    # Offense: points produced and total possessions.
    q_ast = (share * 1.14 * _div(t["A"] - p["A"], t["FGM"])
             + _div(_div(t["A"], t["M"]) * mp * 5 - p["A"], _div(t["FGM"], t["M"]) * mp * 5 - p["FGM"])
             * (1 - share))
    shot_pts = _div(p["P"] - p["FTM"], 2 * p["FGA"])
    fg_part = p["FGM"] * (1 - 0.5 * shot_pts * q_ast)
    ast_pts = _div((t["P"] - t["FTM"]) - (p["P"] - p["FTM"]), 2 * (t["FGA"] - p["FGA"]))
    ast_part = 0.5 * ast_pts * p["A"]
    ft_miss2 = (1 - _div(p["FTM"], p["FTA"])) ** 2 if p["FTA"] else 1.0
    ft_part = (1 - ft_miss2) * 0.4 * p["FTA"]
    t_ft_miss2 = (1 - _div(t["FTM"], t["FTA"])) ** 2 if t["FTA"] else 1.0
    t_sc_poss = t["FGM"] + (1 - t_ft_miss2) * t["FTA"] * 0.4
    t_orb_pct = _div(t["OR"], t["OR"] + (o_trb - o["OR"]))
    t_play = _div(t_sc_poss, t["FGA"] + t["FTA"] * 0.4 + t["TO"])
    t_orb_w = _div((1 - t_orb_pct) * t_play, (1 - t_orb_pct) * t_play + t_orb_pct * (1 - t_play))
    orb_part = p["OR"] * t_orb_w * t_play
    orb_scale = 1 - _div(t["OR"], t_sc_poss) * t_orb_w * t_play
    sc_poss = (fg_part + ast_part + ft_part) * orb_scale + orb_part
    fgx_poss = (p["FGA"] - p["FGM"]) * (1 - 1.07 * t_orb_pct)
    ftx_poss = ft_miss2 * 0.4 * p["FTA"] if p["FTA"] else 0.0
    tot_poss = sc_poss + fgx_poss + ftx_poss + p["TO"]
    pprod_fg = 2 * (p["FGM"] + 0.5 * p["3PM"]) * (1 - 0.5 * shot_pts * q_ast)
    pprod_ast = (2 * _div(t["FGM"] - p["FGM"] + 0.5 * (t["3PM"] - p["3PM"]), t["FGM"] - p["FGM"])
                 * 0.5 * ast_pts * p["A"])
    pprod_orb = p["OR"] * t_orb_w * t_play * _div(t["P"], t_sc_poss)
    pprod = (pprod_fg + pprod_ast + p["FTM"]) * orb_scale + pprod_orb

    # Defense: stops and Defensive Rating.
    dor = _div(o["OR"], o["OR"] + t["DR"])
    dfg = _div(o["FGM"], o["FGA"])
    fmwt = _div(dfg * (1 - dor), dfg * (1 - dor) + (1 - dfg) * dor)
    stops1 = p["S"] + p["B"] * fmwt * (1 - 1.07 * dor) + p["DR"] * (1 - fmwt)
    o_ft_miss2 = (1 - _div(o["FTM"], o["FTA"])) ** 2 if o["FTA"] else 1.0
    stops2 = ((_div(o["FGA"] - o["FGM"] - t["B"], t["M"]) * fmwt * (1 - 1.07 * dor)
               + _div(o["TO"] - t["S"], t["M"])) * mp
              + _div(p["PF"], t["PF"]) * 0.4 * o["FTA"] * o_ft_miss2)
    stop_pct = _div((stops1 + stops2) * o["M"], poss * mp)
    team_drtg = 100 * _div(o["P"], poss)
    d_pts_per_sc = _div(o["P"], o["FGM"] + (1 - o_ft_miss2) * o["FTA"] * 0.4)
    drtg = team_drtg + 0.2 * (100 * d_pts_per_sc * (1 - stop_pct) - team_drtg)

    pace = 48 * _div(poss, t["M"] / 5)
    per_win = PTS_PER_WIN * lg["PPG"] * _div(pace, lg["PACE"])
    ows = _div(pprod - (1 - PTS_PER_WIN / 4) * lg["PPP"] * tot_poss, per_win)
    dws = _div(_div(mp, t["M"]) * poss * ((1 + PTS_PER_WIN / 4) * lg["PPP"] - drtg / 100), per_win)
    return ows, dws


def _league(team: dict) -> dict:
    pts = sum(t["own"]["P"] for t in team.values())
    poss = sum(t["POSS"] for t in team.values())
    mins = sum(t["own"]["M"] for t in team.values())
    games = sum(t["G"] for t in team.values())
    return {"PPG": _div(pts, games), "PPP": _div(pts, poss),
            "PACE": 48 * _div(poss, mins / 5), "ORTG": 100 * _div(pts, poss)}


def season_values(rows: list[dict], kind: str, listed: dict[str, int]) -> list[list]:
    """Every stint of one season and type (all of `rows` share a SEASON)."""
    stints, team = _stints(rows)
    lg = _league(team)
    out = []
    by_team: dict = defaultdict(list)
    for key in stints:
        by_team[(key[1], key[2])].append(key)

    for (season, abbr), keys in sorted(by_team.items()):
        tm = team[(season, abbr)]
        team_min = tm["own"]["M"]
        pos_raw, role_raw, weights = [], [], []
        for key in keys:
            st = stints[key]
            p, t = st["own"], st["tm"]
            mp = p["M"]
            share = _div(mp, t["M"] / 5)
            def pct(col, team_total=None):
                return _div(p[col], (t[col] if team_total is None else team_total) * share)
            trb = _div(p["OR"] + p["DR"], (t["OR"] + t["DR"]) * share)
            est = 2.130 + 8.668 * trb - 2.486 * pct("S") + 0.992 * pct("PF") \
                - 3.536 * pct("A") + 1.667 * pct("B")
            listed_pos = listed.get(key[0], 3)
            pos_raw.append(_div(est * mp + listed_pos * PRIOR_MIN, mp + PRIOR_MIN))
            tsa, t_tsa = p["FGA"] + 0.44 * p["FTA"], t["FGA"] + 0.44 * t["FTA"]
            t_pptsa = _div(t["P"], t_tsa)
            thr = p["P"] - tsa * (t_pptsa - THRESHOLD_GAP)
            role = 6.00 - 6.642 * pct("A") - 8.544 * _div(thr, t_tsa * THRESHOLD_GAP * share)
            role_raw.append(_div(role * mp + 4.0 * PRIOR_MIN, mp + PRIOR_MIN))
            weights.append(mp)
        positions = _center(pos_raw, weights)
        roles = _center(role_raw, weights)

        raws, oraws, pcts = [], [], []
        for key, pos, role in zip(keys, positions, roles):
            st = stints[key]
            p, t = st["own"], st["tm"]
            on_poss = _div(p["M"], t["M"] / 5) * st["POSS"]
            per100 = {k: 100 * _div(v, on_poss) for k, v in p.items()}
            tsa = p["FGA"] + 0.44 * p["FTA"]
            t_pptsa = _div(t["P"], t["FGA"] + 0.44 * t["FTA"])
            per100["P"] = 100 * _div(p["P"] + tsa * (PTS_BASELINE - t_pptsa), on_poss)
            raws.append(raw_bpm(per100, pos, role))
            oraws.append(raw_bpm(per100, pos, role, offense=True))
            pcts.append(_div(p["M"], team_min / 5))

        poss = tm["POSS"]
        ortg, drtg = 100 * _div(tm["own"]["P"], poss), 100 * _div(tm["opp"]["P"], poss)
        lead = _div(tm["own"]["P"] - tm["opp"]["P"], tm["G"]) / 2
        lead_adj = LEAD_EFFECT / 2 * lead
        team_rating = ortg - drtg + lead_adj
        off_rating = ortg - lg["ORTG"] + lead_adj / 2
        weight = sum(pcts)
        adj = _div(team_rating - sum(r * w for r, w in zip(raws, pcts)), weight)
        oadj = _div(off_rating - sum(r * w for r, w in zip(oraws, pcts)), weight)

        for key, pos, role, raw, oraw, pc in zip(keys, positions, roles, raws, oraws, pcts):
            st = stints[key]
            p = st["own"]
            ows, dws = win_shares(p, st["tm"], st["opp"], st["POSS"], lg)
            bpm, obpm = raw + adj, oraw + oadj
            ws = ows + dws
            out.append([key[0], season, abbr, kind, st["G"], round(p["M"]),
                        round(ows, 2), round(dws, 2), round(ws, 2),
                        round(_div(ws * 48, p["M"]), 3) if p["M"] else None,
                        round(obpm, 1), round(bpm - obpm, 1), round(bpm, 1),
                        round((bpm - REPLACEMENT) * pc * tm["G"] / 82, 2),
                        round(pos, 2), round(role, 2)])
    return out


def player_values(data_dir: Path) -> list[list]:
    listed = _listed_positions(data_dir)
    out = []
    for kind, rows in advanced._labelled_rows(data_dir):
        out.extend(season_values(rows, kind, listed))
    order = {"REG": 0, "PLAYOFF": 1}
    out.sort(key=lambda r: (r[0], r[1], order[r[3]], r[2]))
    return out


def write(out_dir: Path, data_dir: Path) -> list[str]:
    csvio.write_csv(Path(out_dir) / OUTPUT, HEADER, player_values(Path(data_dir)))
    return [OUTPUT]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stats_build.player_value", description=__doc__.split("\n")[0])
    ap.add_argument("--season", help="season to list leaders for (default: today's)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    a = ap.parse_args(argv)
    season = BuildArgs.resolve(a.season, None).season
    col = {c: i for i, c in enumerate(HEADER)}
    rows = [r for r in player_values(a.data_dir) if r[col["SEASON"]] == season and r[col["TYPE"]] == "REG"]
    for stat in ("WS", "BPM", "VORP"):
        floor = [r for r in rows if r[col["MIN"]] >= 500] if stat == "BPM" else rows
        top = sorted(floor, key=lambda r: -r[col[stat]])[:5]
        print(f"{season} {stat}: " + ", ".join(f"{r[0]} ({r[2]}) {r[col[stat]]}" for r in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())

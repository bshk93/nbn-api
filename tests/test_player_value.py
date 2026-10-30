"""`stats_build/player_value.py` — Win Shares, BPM, VORP.

What would make it lie is pinned:

- **BPM's regression matches Basketball-Reference's worked example** (2017
  LeBron, raw BPM 15.6), so the coefficients and their interpolation are
  right.
- **Position and role are centred** on 3.0 per team, inside [1, 5].
- **BPM sums to the team**: minutes-weighted BPM is zero league-wide every
  season, and DBPM is exactly BPM minus OBPM.
- **Win Shares sum to wins**: within 3 a team-season on average, and
  `PTS_PER_WIN` is still what NBN's records fit (it was refitted for NBN's
  wider margins; a drift means the league changed and it needs refitting).
- **VORP is positive exactly when BPM beats replacement.**
- **Every stint joins `player_seasons*.csv`** with the same games.

Reads live data; writes nothing.

    venv/bin/python -m tests.test_player_value
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.constants import DATA_DIR  # noqa: E402
from stats_build import advanced  # noqa: E402
from stats_build import player_value as pv  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("BPM regression against the worked example")
lebron = {"P": 30.4, "3PM": 2.2, "A": 11.5, "TO": 5.4, "OR": 1.7, "DR": 9.7,
          "S": 1.6, "B": 0.8, "PF": 2.4, "FGA": 24.0, "FTA": 9.5}
raw = pv.raw_bpm(lebron, 2.3, 1.0)
# The page rounds each row before adding them up, hence the tolerance.
check(f"2017 LeBron raw BPM is 15.6 ({raw:.2f})", abs(raw - 15.6) < 0.2)
check("a center's assists are worth more than a point guard's",
      pv._lerp(pv.BPM_POS["A"], 5) > pv._lerp(pv.BPM_POS["A"], 1))
check("no position constant from small forward up", pv.raw_bpm(dict.fromkeys(lebron, 0.0), 4.0, 3.0) == 0)

print("position and role centring")
vals = pv._center([0.5, 2.0, 4.5, 6.0], [30, 20, 25, 25])
mean = sum(v * w for v, w in zip(vals, [30, 20, 25, 25])) / 100
check(f"weighted mean is 3.0 ({mean:.4f})", abs(mean - 3.0) < 1e-6)
check("clipped to [1, 5]", all(1 <= v <= 5 for v in vals))

print("live data")
rows = pv.player_values(DATA_DIR)
H = {c: i for i, c in enumerate(pv.HEADER)}
check("DBPM is BPM minus OBPM", all(
    abs(r[H["DBPM"]] - (r[H["BPM"]] - r[H["OBPM"]])) <= 0.11 for r in rows))
check("WS is OWS plus DWS", all(
    abs(r[H["WS"]] - (r[H["OWS"]] + r[H["DWS"]])) <= 0.011 for r in rows))
check("VORP is positive exactly when BPM beats replacement", all(
    (r[H["VORP"]] > 0) == (r[H["BPM"]] > pv.REPLACEMENT) for r in rows
    if r[H["VORP"]] != 0 and abs(r[H["BPM"]] - pv.REPLACEMENT) > 0.05))
for season in sorted({r[H["SEASON"]] for r in rows}):
    reg = [r for r in rows if r[H["SEASON"]] == season and r[H["TYPE"]] == "REG"]
    lg = sum(r[H["BPM"]] * r[H["MIN"]] for r in reg) / sum(r[H["MIN"]] for r in reg)
    check(f"{season}: league BPM, weighted by minutes, is zero ({lg:+.3f})", abs(lg) < 0.05)

wins, games = defaultdict(int), defaultdict(int)
for kind, season_rows in advanced._labelled_rows(DATA_DIR):
    for g in advanced.team_games(season_rows):
        k = (g["SEASON"], kind, g["TEAM"])
        games[k] += 1
        wins[k] += g["own"]["P"] > g["opp"]["P"]
ws = defaultdict(float)
for r in rows:
    ws[(r[H["SEASON"]], r[H["TYPE"]], r[H["TEAM"]])] += r[H["WS"]]
for kind, floor, limit in (("REG", 60, 3.0), ("PLAYOFF", 8, 1.5)):
    miss = [ws[k] - wins[k] for k in ws if k[1] == kind and games[k] >= floor]
    avg = statistics.mean(map(abs, miss))
    check(f"{kind}: team Win Shares within {limit} of wins on average ({avg:.2f} over {len(miss)})",
          avg <= limit)

# The constant, refitted: margin per game against wins above .500, in points.
num = den = 0.0
for kind, season_rows in advanced._labelled_rows(DATA_DIR):
    if kind != "REG":
        continue
    team = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for g in advanced.team_games(season_rows):
        t = team[g["TEAM"]]
        t[0] += 1
        t[1] += g["own"]["P"] > g["opp"]["P"]
        t[2] += g["own"]["P"] - g["opp"]["P"]
        t[3] += g["own"]["P"]
    lg_ppg = sum(t[3] for t in team.values()) / sum(t[0] for t in team.values())
    for t in team.values():
        if t[0] >= 60:
            x = (t[1] / t[0] - 0.5) * lg_ppg
            num += t[2] / t[0] * x
            den += x * x
fit = num / den
check(f"PTS_PER_WIN {pv.PTS_PER_WIN} still fits NBN's records ({fit:.3f})",
      abs(fit - pv.PTS_PER_WIN) <= 0.05)

print("joins")
by_key = {(r[0], r[1], r[2], r[3]): r for r in rows}
derived = DATA_DIR / "derived" / "players"
for fname, kind in (("player_seasons.csv", "REG"), ("player_seasons_playoffs.csv", "PLAYOFF")):
    with (derived / fname).open(newline="") as fh:
        stints = [r for r in csv.DictReader(fh) if r["G"] not in ("", "NA")]
    lost = [r for r in stints
            if (by_key.get((r["SLUG"], r["SEASON"][:5], r["TEAM"], kind)) or [None] * 5)[4] != int(r["G"])]
    check(f"{fname}: all {len(stints)} stints join with the same G ({len(lost)} don't)", not lost)

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall checks passed")

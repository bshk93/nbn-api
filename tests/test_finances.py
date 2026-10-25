"""`stats_build/finances.py` — the Front Office Ledger.

Flavor, but it is reconstructed over all of NBN history and read as a sanity
check, so the things that would make it lie are pinned:

- **Home is the side without the `@`.** A game that doesn't pick one out is
  split, not guessed.
- **Deterministic.** Two runs give the same file; noise is seeded on the game.
- **Payroll is money, never cap.** No free-agent hold, no Empty Roster Charge.
- **The spec's sanity checks** (`docs/franchise-finances-spec.md`), on live data:
  a champion rises, the spread stays plausible, no step where a source begins.

Reads live data; writes nothing.

    venv/bin/python -m tests.test_finances
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.constants import DATA_DIR  # noqa: E402
from stats_build import finances as f  # noqa: E402
from stats_build.buildargs import BuildArgs  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def row(team, opp, wl="W", season="25-26", date="2026-01-01", **extra):
    return {"TEAM": team, "OPP": opp, "WL": wl, "SEASON": season, "DATE": date,
            "PLAYER": f"{team} GUY", **extra}


print("home is the side without the @")
gs = f.games([row("BOS", "@NYK", "L"), row("BOS", "@NYK", "L"), row("NYK", "BOS", "W")],
             playoffs=False)
check("one game from three rows", len(gs) == 1)
check("NYK is home", gs[0]["home"] == "NYK" and gs[0]["away"] == "BOS")
check("and known", gs[0]["home_known"])
check("results are per team", gs[0]["wl"] == {"BOS": "L", "NYK": "W"})

neither = f.games([row("BOS", "NYK"), row("NYK", "BOS", "L")], playoffs=False)
both = f.games([row("BOS", "@NYK"), row("NYK", "@BOS", "L")], playoffs=False)
check("no @ on either side is not guessed", not neither[0]["home_known"])
check("an @ on both sides is not guessed", not both[0]["home_known"])

po = f.games([row("MIA", "@ORL", season="25-26 Playoffs", ROUND="3"),
              row("ORL", "MIA", "L", season="25-26 Playoffs", ROUND="3")], playoffs=True)
check("a playoff game files under its regular-season label", po[0]["season"] == "25-26")
check("and keeps its round", po[0]["round"] == 3)

print("noise is seeded on the game")
g = {"date": "2026-01-01", "home": "NYK", "away": "BOS"}
check("same game, same noise", f._noise(g) == f._noise(dict(g)))
check("noise stays inside its band", all(
    abs(f._noise({"date": f"2026-01-{d:02d}", "home": "NYK", "away": "BOS"})) <= f.FILL_NOISE
    for d in range(1, 29)))

print("luxury tax")
check("nothing under the first apron", f.luxury_tax(200, 209, 222) == 0)
check("over the first only", f.luxury_tax(219, 209, 222) == f.TAX_RATE_APRON1 * 10)
check("over both", f.luxury_tax(232, 209, 222) == f.TAX_RATE_APRON1 * 23 + f.TAX_RATE_APRON2 * 10)
check("an unknown apron charges nothing", f.luxury_tax(400, None, None) == 0)

print("payroll is money, never cap")
tmp = Path(tempfile.mkdtemp(prefix="nbn-fin-test-"))
base = {"season": "26-27", "team": "ZZZ", "in_season": True, "apron1": 1, "apron2": 2}
lines = [
    # A newer row carries `payroll`; it wins over everything else.
    {**base, "payroll": 100, "salary": 999, "salary_ex_holds": 500,
     "empty_roster_charge": 0, "dead_cap": 4},
    # An older row: the ex-holds basis, less the Empty Roster Charge. Never `salary`.
    {**base, "salary": 999, "salary_ex_holds": 130, "empty_roster_charge": 30},
    # Out of season is not a game day's books.
    {**base, "in_season": False, "payroll": 10_000},
]
(tmp / "cap-history.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
book = f.load_books(tmp)[("26-27", "ZZZ")]
check("mean of payroll, with the fallback excluding holds and the charge",
      book["payroll"] == 100)
check("dead money averages only the rows that carry it", book["dead"] == 4)

print("the ledger, on live data")
season = BuildArgs.resolve(None, None).season
fin, att = f.ledger(DATA_DIR, season)
fin2, att2 = f.ledger(DATA_DIR, season)
check("two runs are identical", fin == fin2 and att == att2)
rows = [dict(zip(f.FINANCE_COLS, r)) for r in fin]
seasons = sorted({r["SEASON"] for r in rows})
done = [s for s in seasons if not any(r["IN_PROGRESS"] for r in rows if r["SEASON"] == s)]
check("every finished season has 30 rows",
      all(sum(r["SEASON"] == s for r in rows) == 30 for s in done))

by = {(r["SEASON"], r["TEAM"]): r for r in rows}
champs = [r for r in rows if r["PLAYOFF_RESULT"] == "Champion"]
falling = [f"{r['SEASON']} {r['TEAM']}" for r in champs if r["VALUE_CHANGE"] <= 0]
check(f"a champion's value rises in its title season ({', '.join(falling) or 'all do'})",
      champs and not falling)

last_done = done[-1]
league = sum(r["VALUE"] for r in rows if r["SEASON"] == last_done) / 30
never = [t for t in {r["TEAM"] for r in rows}
         if all(by[(s, t)]["PLAYOFF_RESULT"] == "Missed" for s in done)]
above = [t for t in never if by[(last_done, t)]["VALUE"] >= league]
check(f"a team that never made the playoffs is below average ({', '.join(above) or 'all are'})",
      not above)

vals = sorted(r["VALUE"] for r in rows if r["SEASON"] == seasons[-1])
spread = vals[-1] / vals[0]
check(f"the spread stays plausible ({spread:.2f}x, want 1.3-2)", 1.3 <= spread <= 2.0)

# Valuation reads nothing that starts partway through history, so the league
# average moves by the multiple's growth and very little else, every season.
avg = [sum(r["VALUE"] for r in rows if r["SEASON"] == s) / 30 for s in seasons]
steps = [b / a - 1 for a, b in zip(avg, avg[1:])]
check(f"no step in the league average ({', '.join(f'{x:+.1%}' for x in steps)})",
      all(abs(x - f.VALUE_GROWTH) < 0.03 for x in steps))

reg = [a for a in att if a[2] == "REG"]
by_season = {}
for a in reg:
    by_season.setdefault(a[0], []).append(a)
dry = [s for s in done if not any(a[8] for a in by_season.get(s, []))]
check(f"every finished season has sellouts ({', '.join(dry) or 'all do'})", not dry)
floor = sum(a[7] <= round(f.SEATS * f.FILL_MIN) for a in reg) / len(reg)
check(f"few games at the floor ({floor:.1%})", floor < 0.02)
check("every finished season has stars", all(
    sum(by[(s, t)]["STARS"] for t in {r["TEAM"] for r in rows}) > 0 for s in done))

print("bad blood and the champ in town")
from stats_build import pipeline  # noqa: E402
_, po_rows = pipeline.prepare(DATA_DIR, season)
met = {(g["season"], frozenset((g["home"], g["away"]))) for g in f.games(po_rows, playoffs=True)}
champs_by = {s: t for (s, t), res in pipeline.playoff_results(po_rows).items() if res == "Champion"}
rival, other, visit = [], [], []
for a in reg:
    s, home, away, crowd = a[0], a[4], a[5], a[7]
    i = seasons.index(s)
    seen = any((seasons[j], frozenset((home, away))) in met for j in range(max(0, i - f.RIVAL_WINDOW), i))
    (rival if seen else other).append(crowd)
    if i and champs_by.get(seasons[i - 1]) == away:
        visit.append(crowd)
mean = lambda xs: sum(xs) / len(xs)
check(f"recent playoff opponents draw more ({mean(rival):.0f} vs {mean(other):.0f})",
      rival and mean(rival) > mean(other))
check(f"so does the defending champ on the road ({mean(visit):.0f})",
      visit and mean(visit) > mean(other))

print("owners")
tenures = pipeline.load_owner_tenures(DATA_DIR)
owners = [dict(zip(f.OWNER_COLS, r)) for r in f.owner_ledger(fin, tenures)]
check("every team-season is credited to exactly one owner",
      sum(o["SEASONS"] for o in owners) == len(rows))
check("beating the league is zero-sum", abs(sum(o["VS_LEAGUE"] for o in owners)) < 30)
check("every team has an owner for the season in progress",
      len({o["TEAM"] for o in owners if o["IN_PROGRESS"]}) == len({r["TEAM"] for r in rows}))
# A summer takeover gets the season, the outgoing owner doesn't: one owner per
# team at mid-season, unlike the playoff stats' overlap rule.
dup = [(s, t) for s in seasons for t in {r["TEAM"] for r in rows}
       if sum(1 for ten in tenures if ten["TEAM"] == t
              and ten["start"] <= f._midseason(s) <= ten["end"]) > 1]
check(f"no team has two owners at any mid-season ({dup[:3] or 'none'})", not dup)

profit = [r for r in rows if r["PROFIT"] is not None]
check("profit only where payroll is on record", all(r["PAYROLL"] is not None for r in profit))
check("no profit before payroll records begin", all(r["SEASON"] >= "26-27" for r in profit))

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall checks passed")

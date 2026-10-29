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

print("luxury tax: brackets over the first apron")
w = f.TAX_BRACKET * 100          # bracket width on a cap of 100
check("nothing under the first apron", f.luxury_tax(200, 209, 100) == 0)
check("inside the first bracket", abs(f.luxury_tax(211, 209, 100) - 1.5 * 2) < 1e-9)
check("two brackets in, the second rate applies",
      abs(f.luxury_tax(209 + w + 1, 209, 100) - (1.5 * w + 1.75 * 1)) < 1e-9)
check("past the listed rates each bracket adds a step",
      abs(f.luxury_tax(209 + 5 * w, 209, 100) - w * (1.5 + 1.75 + 2.5 + 3.25 + 3.75)) < 1e-6)
check("an unknown apron or cap charges nothing",
      f.luxury_tax(400, None, 100) == 0 and f.luxury_tax(400, 209, None) == 0)

print("prices grow with the cap after PRICE_BASE")
caps = {"25-26": 150.0, "26-27": 160.0, "28-29": 180.0}
check("a cap on file is used", f.season_cap("26-27", caps) == 160.0)
check("an unset cap is the last one grown by CAP_GROWTH",
      abs(f.season_cap("27-28", caps) - 160 * (1 + f.CAP_GROWTH)) < 1e-9
      and abs(f.season_cap("30-31", caps) - 180 * (1 + f.CAP_GROWTH) ** 2) < 1e-9)
check("no cap before the first on file", f.season_cap("24-25", caps) is None)
check("1.0 up to PRICE_BASE", f.price_level("26-27", 160, 160) == 1.0 and f.price_level("22-23", 100, 160) == 1.0)
check("the cap's ratio after it", abs(f.price_level("28-29", 180, 160) - 1.125) < 1e-9)
check("1.0 when a cap is unknown", f.price_level("28-29", None, 160) == 1.0)
m = [f.value_multiple(s_, 100.0) for s_ in ("25-26", "26-27", "27-28", "30-31")]
check("the multiple grows to PRICE_BASE, then holds",
      abs(m[1] / m[0] - 1 - f.VALUE_GROWTH) < 1e-9 and m[1] == m[2] == m[3])

print("jersey patches")
teams30 = [f"T{i:02d}" for i in range(30)]
seas = [f"{y}-{y + 1}" for y in range(20, 30)]
pp = f.jersey_patches(teams30, seas)
check("no sponsor on two teams in one season",
      all(len({pp[(s, t)] for t in teams30}) == 30 for s in seas))
check("deals still renew", any(pp[(seas[i], t)] == pp[(seas[i - 1], t)]
                               for i in range(1, len(seas)) for t in teams30))
check("and are the same every run", pp == f.jersey_patches(teams30, seas))

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
check(f"the spread stays plausible ({spread:.2f}x, want 1.8-3; the NBA's is about 3)", 1.8 <= spread <= 3.0)

print("buzz follows form, against who it came against")
def game(d, h, a, hp, ap, season="25-26"):
    return {"season": season, "date": f"2026-01-{d:02d}", "home": h, "away": a, "pts": {h: hp, a: ap}, "wl": {}}
check("one blowout can't max it", f.buzz_from([40.0]) < f.BUZZ_MAX / 2)
check("ten big wins can", f.buzz_from([25.0] * 10) == f.BUZZ_MAX)
check("only the last ten count", f.buzz_from([-30.0] * 10 + [10.0] * 10) == f.buzz_from([10.0] * 10))
check("and it stays in its band", f.buzz_from([-50.0] * 10) == f.BUZZ_MIN)
strong = [game(1, "GGG", "XXX", 130, 100), game(2, "GGG", "YYY", 130, 100)]
weak = [game(1, "BBB", "XXX", 100, 130), game(2, "BBB", "YYY", 100, 130)]
_, _, last_s = f.buzz_series(strong + [game(3, "AAA", "GGG", 110, 100)])
_, _, last_w = f.buzz_series(weak + [game(3, "AAA", "BBB", 110, 100)])
check("beating a good team by 10 beats beating a bad one by 10",
      last_s[("25-26", "AAA")] > last_w[("25-26", "AAA")])
before, _, _ = f.buzz_series([game(1, "AAA", "BBB", 120, 100), game(2, "AAA", "CCC", 100, 90)])
check("a game is priced on the buzz going into it",
      before[("25-26", "2026-01-01", "AAA")] == 0 and before[("25-26", "2026-01-02", "AAA")] > 0)
check("a missing score moves nothing",
      all(v == 0 for v in f.buzz_series([{**game(1, "AAA", "BBB", 0, 0), "pts": {}}])[2].values()))
check("buzz feeds the market", f.market_next(1.0, {"First Round": 1.0}, 0.5, 0.1)
      > f.market_next(1.0, {"First Round": 1.0}, 0.5, 0.0))

print("market follows success")
check("a title grows it", f.market_next(1.0, {"Champion": 1.0}, 0.7) > 1.15)
check("missing the playoffs shrinks it", f.market_next(1.0, {"Missed": 1.0}, 0.35) < 1.0)
check("it fades back toward 1.0", 1.0 < f.market_next(1.5, {"First Round": 1.0}, 0.5) < 1.5)
check("and stays in its band", f.market_next(1.99, {"Champion": 1.0}, 0.9) == f.MARKET_MAX
      and f.market_next(0.51, {"Missed": 1.0}, 0.1) == f.MARKET_MIN)
check("odds are an expectation", abs(f.market_next(1.0, {"Champion": 0.5, "Missed": 0.5}, 0.5)
      - (f.market_next(1.0, {"Champion": 1.0}, 0.5) + f.market_next(1.0, {"Missed": 1.0}, 0.5)) / 2) < 1e-9)
for s_ in seasons:
    mean = sum(by[(s_, t)]["MARKET"] for t in {r["TEAM"] for r in rows}) / 30
    if abs(mean - 1) > 0.002:
        check(f"the league's mean market is 1.0 in {s_} ({mean:.3f})", False)
check("everyone opens at 1.0", all(by[(seasons[0], t)]["MARKET"] == 1.0 for t in {r["TEAM"] for r in rows}))
grew = [r for r in champs if r["SEASON"] in done[:-1]
        and by[(seasons[seasons.index(r["SEASON"]) + 1], r["TEAM"])]["MARKET"] <= r["MARKET"]]
check(f"a champion's market grows the next season ({', '.join(r['SEASON'] + ' ' + r['TEAM'] for r in grew) or 'all do'})",
      not grew)

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

print("the tax goes to the teams that didn't pay it")
for s in sorted({r["SEASON"] for r in rows if r["TAX_SHARE"] is not None}):
    rs = [r for r in rows if r["SEASON"] == s]
    paid = sum(r["LUXURY_TAX"] for r in rs)
    got = sum(r["TAX_SHARE"] for r in rs)
    check(f"{s}: paid out what came in (¥{paid / 1e6:.0f}M)", abs(paid - got) <= 30)
    check(f"{s}: no payer gets a share",
          all(r["TAX_SHARE"] == 0 for r in rs if r["LUXURY_TAX"]))

print("concessions follow the crowd")
fin_done = [r for r in rows if r["SEASON"] in done and r["HOME_GAMES"] == 41]
hi = max(fin_done, key=lambda r: r["AVG_ATTENDANCE"])
lo = min(fin_done, key=lambda r: r["AVG_ATTENDANCE"])
check(f"a fuller arena sells more hot dogs ({hi['CONCESSIONS'] / 1e6:.0f}M vs {lo['CONCESSIONS'] / 1e6:.0f}M)",
      hi["CONCESSIONS"] > lo["CONCESSIONS"])

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall checks passed")

"""Seeding: the NBA tie-break procedure in stats_build/pipeline.py.

`nba_seed_order`'s docstring is the rule set. The port gate checks that R
and Python agree on every real season; this pins the rules themselves on
small made-up seasons, where each case has exactly one step that decides it.
"""

import sys

from stats_build import pipeline

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def season(games, label="99-00"):
    """Box-score rows, one per team per game: (date, winner, loser, w_pts, l_pts)."""
    rows = []
    for date, w, l, wp, lp in games:
        rows.append({"SEASON": label, "DATE": date, "TEAM": w, "OPP": l, "P": str(wp)})
        rows.append({"SEASON": label, "DATE": date, "TEAM": l, "OPP": "@" + w, "P": str(lp)})
    return rows


def order(rows, conf):
    out = [r for r in pipeline.compute_standings(rows) if r["SEED"].startswith(conf)]
    return [r["TEAM"] for r in sorted(out, key=lambda r: r["SEED_NUM"])]


def before(seq, a, b):
    return seq.index(a) < seq.index(b)


print("two teams: head-to-head comes first")
east = order(season([("d1", "BOS", "NYK", 100, 90), ("d2", "LAL", "BOS", 100, 90),
                     ("d3", "NYK", "LAC", 100, 90)]), "East")
check("BOS beat NYK, so BOS is ahead at 1-1 each", before(east, "BOS", "NYK"))

print("\na step with no games in it can't separate teams")
# The 26-27 BOS/BKN case: same division, BOS 0-1 in it, BKN yet to play one.
# Division % is skipped, conference records are level, net points decide.
east = order(season([
    ("d1", "NYK", "BOS", 110, 100), ("d2", "BOS", "DET", 100, 99), ("d3", "CHI", "BOS", 100, 99),
    ("d1", "CHA", "BKN", 100, 99), ("d2", "CLE", "BKN", 100, 99), ("d3", "BKN", "IND", 100, 99),
]), "East")
check("BKN (-1 net) ahead of BOS (-10 net), not behind it for 0-0 in the division",
      before(east, "BKN", "BOS"))

print("\nmore than two: division winner comes before head-to-head")
east = order(season([
    ("d1", "BOS", "NYK", 100, 90), ("d2", "BOS", "LAL", 100, 90),
    ("d3", "NYK", "DET", 100, 90), ("d4", "DET", "ORL", 100, 90), ("d5", "ORL", "LAC", 100, 90),
]), "East")
check("NYK, 1-0 against the group but not a division winner, is last of the three",
      east[:4] == ["BOS", "DET", "ORL", "NYK"])
check("and the two that are left go back to head-to-head: DET over ORL",
      before(east, "DET", "ORL"))

print("\na seed 2K gave that the rules don't reproduce is pinned by season")
games = [("d1", "NOP", "UTA", 100, 90), ("d2", "BOS", "NOP", 100, 90), ("d3", "UTA", "LAC", 100, 90)]
check("24-25: UTA ahead of NOP, as the first-round bracket shows",
      before(order(season(games, "24-25"), "West"), "UTA", "NOP"))
check("any other season: NOP ahead, on head-to-head",
      before(order(season(games, "23-24"), "West"), "NOP", "UTA"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

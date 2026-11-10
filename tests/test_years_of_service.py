"""Years of experience, the NBN way (league ruling 2026-11-10): NBA years
before 2020-21, plus each NBN regular season with a day on a roster.

    venv/bin/python -m tests.test_years_of_service
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routers.years_of_service as y  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


BOUNDS = {"24-25": ("2024-10-22", "2025-04-13"), "25-26": ("2025-10-21", "2026-04-12"),
          "26-27": ("2026-10-20", "2027-04-11")}


def season_for(d):
    yy, mm = int(d[2:4]), int(d[5:7])
    return f"{yy - 1:02d}-{yy:02d}" if mm < 7 else f"{yy:02d}-{yy + 1:02d}"


def build(games=(), txns=(), rostered=(), today="2026-11-10", contracted=None):
    return y.build_service(list(games), list(txns), set(rostered), today,
                           bounds_for=BOUNDS.get, season_for_date=season_for,
                           contracted=contracted)


def sign(player, date, salaries=None, holds=None, kind="sign"):
    return {"type": kind, "date": date,
            "details": {"player": player, "contract": {"salaries": salaries or {},
                                                       "cap_holds": holds or {}}}}


print("build_service")
s = build(games=[("a", "24-25", "2024-11-01")])
check("a regular-season game counts that season", s["a"] == {"24-25"}, s["a"])

s = build(txns=[sign("b", "2025-08-01")])
check("an offseason signing counts the season it leads into", s["b"] == {"25-26"}, s["b"])

s = build(txns=[sign("c", "2025-08-01"), {"type": "release", "date": "2025-09-01",
                                          "details": {"player": "c"}}])
check("released before opening night: no season", "25-26" not in s["c"], s["c"])

s = build(txns=[sign("d", "2025-11-01"), {"type": "release", "date": "2025-12-01",
                                          "details": {"player": "d"}}])
check("released mid-season still counts", s["d"] == {"25-26"}, s["d"])

s = build(txns=[sign("e", "2025-05-01")])
check("signed after the regular season ended: not that season", "24-25" not in s["e"], s["e"])

s = build(txns=[sign("f", "2024-08-01", {"24-25": "$1", "25-26": "$1", "26-27": "$1"},
                     {"26-27": "UFA"})])
check("later contract years count; a trailing hold does not",
      s["f"] == {"24-25", "25-26"}, s["f"])

s = build(txns=[sign("g", "2025-08-01", {"25-26": "$1", "26-27": "$1", "27-28": "$1"})])
check("a season that hasn't started doesn't count", "27-28" not in s["g"], s["g"])

s = build(rostered={"h"})
check("on a roster after opening night counts this season", s["h"] == {"26-27"}, s["h"])
s = build(rostered={"h"}, today="2026-09-01")
check("before opening night it doesn't", "26-27" not in s["h"], s["h"])

s = build(txns=[sign("i", "2024-08-01", holds="25-26:RFA", salaries={"24-25": "$1", "25-26": "$1"})])
check("old string-form cap holds are read", s["i"] == {"24-25"}, s["i"])

s = build(contracted={"j": {"25-26", "27-28"}})
check("a salary on the bio counts a season that has started", s["j"] == {"25-26"}, s["j"])

print("years_of_service")
SERVICE = {"vet": {"20-21", "21-22", "22-23", "23-24", "24-25", "25-26"},
           "gap": {"24-25"}, "rookie": set()}
real = y.service_seasons
y.service_seasons = lambda slug: SERVICE.get(slug, set())
try:
    check("2007 draftee who played every NBN season: NBA years",
          y.years_of_service({"draft_year": 2007}, "26-27", "vet") == 19)
    check("2016 draftee with one NBN season: 4 before 2020 + 1",
          y.years_of_service({"draft_year": 2016}, "26-27", "gap") == 5)
    check("2024 draftee never on a roster: 0",
          y.years_of_service({"draft_year": 2024}, "26-27", "rookie") == 0)
    check("only seasons before the one asked about count",
          y.years_of_service({"draft_year": 2007}, "23-24", "vet") == 16)
    check("no draft year: None", y.years_of_service({}, "26-27", "vet") is None)
finally:
    y.service_seasons = real

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

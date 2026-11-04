"""`routers/stream_planner.py` — ranking each day's games for the stream.

- **Read-only, streamer/admin only.** A member without the role gets a 403;
  the endpoint writes nothing (it has no write path to test).
- **A flagged, unbought game locks its day**, which is then not ranked. A day
  whose only stream is bought is still open, and the bought game isn't a
  candidate (it's streaming anyway).
- **Each factor points the right way**: better teams, a closer spread, a
  longer and more even rivalry (owners and franchises), more at stake, better
  healthy stars, fewer streams so far, and no recent repeat all score higher.
- **Out players count against a team**: excluded from stars, and lowering the
  team's rating.
- **The projection spreads picks**: an under-exposed team that tops day one
  isn't automatically the pick on day two.
- **Range limits**: at most MAX_DAYS, `to` not before `from`.

Pure functions on in-memory inputs; nothing reads the data dir.

    venv/bin/python -m tests.test_stream_planner
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.stream_planner as sp  # noqa: E402
from routers.constants import VALID_TEAMS  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TEAMS = sorted(VALID_TEAMS)


def game(gid, d, away, home, **kw):
    return {"id": gid, "date": d, "time_et": kw.pop("time_et", "7:00p"), "away_team": away, "home_team": home, **kw}


def base_inputs(**over):
    ratings = {t: 0.0 for t in TEAMS}
    odds = {t: {"team": t, "conf": "East" if t in sp._DIVISION_OF and t in
                {"ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NYK", "ORL", "PHI", "TOR", "WAS"}
                else "West", "w": 2, "l": 2, "proj_w": 41.0, "playoffs": 0.5, "rating": 0.0} for t in TEAMS}
    rosters = {t: [{"slug": f"{t.lower()}-star", "ovr": 85.0}] + [{"slug": f"{t.lower()}-{i}", "ovr": 75.0}
                                                                  for i in range(9)] for t in TEAMS}
    names = {p["slug"]: p["slug"] for ps in rosters.values() for p in ps}
    kw = dict(ratings=ratings, odds=odds, season_frac=0.1, rosters=rosters, out=set(), names=names,
              owners={t: f"owner-{t}" for t in TEAMS}, owner_h2h={}, franchise_h2h={})
    kw.update(over)
    return kw


def factor(day, gid, key):
    g = next(x for x in day["games"] if x["id"] == gid)
    return g["factors"][key]["pts"]


D1, D2 = date(2026, 11, 20), date(2026, 11, 21)

# ── locking ──────────────────────────────────────────────────────────────────

print("locked and bought days")
sched = [game("a", "2026-11-20", "BOS", "NYK", stream=True, streamer="chuck"),
         game("b", "2026-11-20", "LAL", "GSW"),
         game("c", "2026-11-21", "MIA", "ORL", stream=True, stream_buyer="someone"),
         game("d", "2026-11-21", "DEN", "UTA")]
p = sp.plan(D1, D2, sched, **base_inputs())
check("a flagged unbought game locks the day", p["days"][0]["locked"]["id"] == "a" and p["days"][0]["games"] == [])
check("a bought-only day stays open", p["days"][1]["locked"] is None and len(p["days"][1]["games"]) == 1)
check("the bought game is listed, not ranked",
      p["days"][1]["bought"][0]["id"] == "c" and all(g["id"] != "c" for g in p["days"][1]["games"]))
check("an open day projects its top pick", p["days"][1]["projected_pick"] == "d")
check("a day with no games is empty, not an error",
      sp.plan(date(2026, 12, 25), date(2026, 12, 25), sched, **base_inputs())["days"][0]["games"] == [])

# ── each factor's direction ──────────────────────────────────────────────────

print("factor directions")
one = [game("x", "2026-11-20", "BOS", "NYK"), game("y", "2026-11-20", "LAL", "GSW")]

r = {**{t: 0.0 for t in TEAMS}, "BOS": 6.0, "NYK": 5.0, "LAL": -6.0, "GSW": -5.0}
d = sp.plan(D1, D1, one, **base_inputs(ratings=r))["days"][0]
check("quality: two good teams beat two bad ones", factor(d, "x", "quality") > factor(d, "y", "quality"))

r = {**{t: 0.0 for t in TEAMS}, "BOS": 0.0, "NYK": -2.0, "LAL": 10.0, "GSW": -10.0}
d = sp.plan(D1, D1, one, **base_inputs(ratings=r))["days"][0]
check("closeness: an even game beats a mismatch", factor(d, "x", "closeness") > factor(d, "y", "closeness"))

oh = {frozenset(("owner-BOS", "owner-NYK")): {"games": 30, "wins": {"owner-BOS": 15, "owner-NYK": 15}, "series": {1}},
      frozenset(("owner-LAL", "owner-GSW")): {"games": 30, "wins": {"owner-LAL": 30}, "series": set()}}
d = sp.plan(D1, D1, one, **base_inputs(owner_h2h=oh))["days"][0]
check("owner rivalry: an even, long one with a series beats a one-sided one",
      factor(d, "x", "owner_rivalry") > factor(d, "y", "owner_rivalry"))
check("owner rivalry names the owners and the record",
      "owner-BOS 15–15 owner-NYK" in d["games"][[g["id"] for g in d["games"]].index("x")]["factors"]["owner_rivalry"]["detail"])

fh = {frozenset(("BOS", "NYK")): {"games": 24, "wins": {"BOS": 12, "NYK": 12}, "series": {"24-25"}}}
d = sp.plan(D1, D1, one, **base_inputs(franchise_h2h=fh))["days"][0]
check("franchise rivalry: history beats none", factor(d, "x", "franchise_rivalry") > factor(d, "y", "franchise_rivalry"))
d = sp.plan(D1, D1, [game("x", "2026-11-20", "BOS", "NYK"), game("y", "2026-11-20", "BOS", "LAL")],
            **base_inputs())["days"][0]
check("franchise rivalry: same division counts", factor(d, "x", "franchise_rivalry") > factor(d, "y", "franchise_rivalry"))

od = base_inputs()["odds"]
od["BOS"]["playoffs"] = od["NYK"]["playoffs"] = 0.5
od["LAL"]["playoffs"] = od["GSW"]["playoffs"] = 0.99
d = sp.plan(D1, D1, one, **base_inputs(odds=od))["days"][0]
check("stakes: bubble teams beat locks", factor(d, "x", "stakes") > factor(d, "y", "stakes"))
late = sp.plan(D1, D1, one, **base_inputs(odds=od, season_frac=0.95))["days"][0]
check("stakes grow as the season runs out", factor(late, "x", "stakes") > factor(d, "x", "stakes"))

ros = base_inputs()["rosters"]
ros["BOS"][0]["ovr"] = 96.0
d = sp.plan(D1, D1, one, **base_inputs(rosters=ros))["days"][0]
check("stars: a 96 beats an 85", factor(d, "x", "stars") > factor(d, "y", "stars"))
d2 = sp.plan(D1, D1, one, **base_inputs(rosters=ros, out={"bos-star"}))["days"][0]
check("stars: an Out star doesn't count", factor(d2, "x", "stars") < factor(d, "x", "stars"))
check("stars: the Out star is named", "out: bos-star (96)" in
      next(g for g in d2["games"] if g["id"] == "x")["factors"]["stars"]["detail"])

# ── injuries lower the rating ────────────────────────────────────────────────

print("injury adjustment")
adj = sp.injury_adjustments(ros, {"bos-star"})
check("a team missing its star loses rating", adj["BOS"] < 0)
check("a healthy team is untouched", abs(adj["NYK"]) < 1e-9)

# ── fairness and repeats ─────────────────────────────────────────────────────

print("fairness and repeats")
hist = [game(f"h{i}", f"2026-11-0{i}", "LAL", "GSW", stream=True) for i in range(1, 5)]
d = sp.plan(D1, D1, hist + one, **base_inputs())["days"][0]
check("fairness: never-streamed teams beat four-time ones", factor(d, "x", "fairness") > factor(d, "y", "fairness"))
check("fairness can go negative for an over-exposed pair", factor(d, "y", "fairness") < 0)
bought = [game(f"h{i}", f"2026-11-0{i}", "LAL", "GSW", stream=True, stream_buyer="m") for i in range(1, 5)]
d = sp.plan(D1, D1, bought + one, **base_inputs())["days"][0]
check("fairness: bought streams don't count against a team",
      factor(d, "x", "fairness") == factor(d, "y", "fairness"))

rec = [game("r", "2026-11-18", "LAL", "MIA", stream=True)]
d = sp.plan(D1, D1, rec + one, **base_inputs())["days"][0]
check("repeat: a team on stream two days ago is penalised", factor(d, "y", "repeat") < 0)
check("repeat: an unrelated game isn't", factor(d, "x", "repeat") == 0)
rec = [game("r", "2026-11-18", "LAL", "MIA", stream=True, stream_buyer="m")]
d = sp.plan(D1, D1, rec + one, **base_inputs())["days"][0]
check("repeat: a bought stream still counts as on stream", factor(d, "y", "repeat") < 0)

# ── the projection spreads picks ─────────────────────────────────────────────

print("projection")
r = {**{t: 0.0 for t in TEAMS}, "BOS": 3.0, "NYK": 3.0}
two = [game("x1", "2026-11-20", "BOS", "NYK"), game("y1", "2026-11-20", "LAL", "GSW"),
       game("x2", "2026-11-21", "NYK", "BOS"), game("y2", "2026-11-21", "GSW", "LAL")]
p = sp.plan(D1, D2, two, **base_inputs(ratings=r))
check("day one picks the better game", p["days"][0]["projected_pick"] == "x1")
check("day two doesn't repeat the same pair", p["days"][1]["projected_pick"] == "y2")
check("exposure counts projected picks", next(e for e in p["exposure"] if e["team"] == "BOS")["projected"] == 1)

# ── head-to-head from games ──────────────────────────────────────────────────

print("head-to-head")
games = [{"date": "2025-01-01", "season": "24-25", "home": "BOS", "away": "NYK", "winner": "BOS", "playoff": False},
         {"date": "2026-01-01", "season": "25-26", "home": "BOS", "away": "NYK", "winner": "NYK", "playoff": False},
         {"date": "2026-04-20", "season": "25-26", "home": "BOS", "away": "NYK", "winner": "NYK", "playoff": True}]
fh = sp.franchise_h2h(games)[frozenset(("BOS", "NYK"))]
check("franchise h2h counts games, wins and series",
      fh["games"] == 3 and fh["wins"]["NYK"] == 2 and len(fh["series"]) == 1)
ten = [{"owner": "Ann", "TEAM": "BOS", "start": date(2020, 1, 1), "end": date(2025, 6, 30)},
       {"owner": "Bob", "TEAM": "BOS", "start": date(2025, 7, 1), "end": date(2026, 12, 31)},
       {"owner": "Cy", "TEAM": "NYK", "start": date(2020, 1, 1), "end": date(2026, 12, 31)}]
oh = sp.owner_h2h(games, ten)
check("owner h2h follows the owner on game day",
      oh[frozenset(("Ann", "Cy"))]["games"] == 1 and oh[frozenset(("Bob", "Cy"))]["games"] == 2)
check("owner h2h credits wins to the owner", oh[frozenset(("Bob", "Cy"))]["wins"]["Cy"] == 2)

# ── the endpoint ─────────────────────────────────────────────────────────────

print("endpoint")
STREAM, PLAIN = "s" * 64, "n" * 64
auth.load_members = lambda: {"Streamy": {"token": STREAM, "roles": ["streamer"], "tenures": []},
                             "Nobody": {"token": PLAIN, "roles": [], "tenures": []}}
sp.league_today_str = lambda: "2026-11-20"
sp._current_league_year = lambda: "26-27"
sp.po.load_schedule = lambda season: one
sp.player_bios_shared = lambda: {}
sp.ovr_shared = lambda: {}
sp._rosters = lambda bios, ovr: base_inputs()["rosters"]
sp.injuries_mod.load_injuries = lambda: {"players": [{"slug": "bos-star", "status": "Out"}]}
sp._load_json = lambda path, default: None
sp._h2h = lambda today: ({}, {}, [])
app = FastAPI()
app.include_router(sp.router)
c = TestClient(app)
check("a non-streamer is refused",
      c.get("/api/stream-planner", headers={"Authorization": "Bearer " + PLAIN}).status_code == 403)
res = c.get("/api/stream-planner", headers={"Authorization": "Bearer " + STREAM})
check("a streamer gets a week by default", res.status_code == 200 and len(res.json()["days"]) == sp.DEFAULT_DAYS)
check("no odds yet still ranks (roster prior)", len(res.json()["days"][0]["games"]) == 2)
check("over MAX_DAYS is refused", c.get("/api/stream-planner?from=2026-11-01&to=2026-12-15",
                                        headers={"Authorization": "Bearer " + STREAM}).status_code == 422)
check("'to' before 'from' is refused", c.get("/api/stream-planner?from=2026-11-10&to=2026-11-01",
                                             headers={"Authorization": "Bearer " + STREAM}).status_code == 422)
check("a bad date is refused", c.get("/api/stream-planner?from=soon",
                                     headers={"Authorization": "Bearer " + STREAM}).status_code == 422)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

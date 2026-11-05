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
check("exposure counts projected picks", next(e for e in p["exposure"] if e["team"] == "BOS")["picked"] == 1)

# ── the cooldown (v2) ────────────────────────────────────────────────────────

print("cooldown")
# SAS, Nov 2026: an under-share team kept topping days a week apart on fair
# share alone. Here SAS is the only team never streamed, so fair share loves
# every SAS game; it may top one day, then must sit out 7 days either side.
hist = [game(f"h{i}", f"2026-11-0{i}", a, h, stream=True) for i, (a, h) in enumerate(
    [("LAL", "GSW"), ("BOS", "NYK"), ("MIA", "ORL"), ("DEN", "UTA"), ("PHX", "SAC"), ("DAL", "HOU")], start=1)]
sas = [game("s1", "2026-11-20", "CHA", "SAS"), game("o1", "2026-11-20", "LAL", "BOS"),
       game("s2", "2026-11-24", "ATL", "SAS"), game("o2", "2026-11-24", "MIA", "DEN"),
       game("s3", "2026-11-28", "SAS", "TOR"), game("o3", "2026-11-28", "PHX", "DAL")]
p = sp.plan(date(2026, 11, 20), date(2026, 11, 28), hist + sas, **base_inputs())
pk = {d["date"]: d["projected_pick"] for d in p["days"]}
check("the under-share team tops the first day", pk["2026-11-20"] == "s1")
check("but not again within 7 days", pk["2026-11-24"] == "o2")
sas_day2 = next(g for g in p["days"][4]["games"] if g["id"] == "s2")
check("the passed-over game is marked with its clash, and still outscores the pick",
      factor(p["days"][4], "s2", "fairness") > factor(p["days"][4], "o2", "fairness") and sas_day2["cooldown"] == {"team": "SAS", "date": "2026-11-20", "days": 4})
check("after 7 days it can come back", pk["2026-11-28"] == "s3")
check("no SAS pick within 7 days of another", all(
    abs((date.fromisoformat(a) - date.fromisoformat(b)).days) > 7
    for a in pk for b in pk if a < b and pk[a] in ("s1", "s2", "s3") and pk[b] in ("s1", "s2", "s3")))

only = [game("r", "2026-11-18", "LAL", "MIA", stream=True), game("q", "2026-11-20", "LAL", "GSW"),
        game("q2", "2026-11-20", "MIA", "BOS")]
d = sp.plan(D1, D1, only, **base_inputs())["days"][0]
check("every game on cooldown: the pick is forced", d["forced"] and d["projected_pick"] is not None)
only2 = [game("r", "2026-11-18", "LAL", "MIA", stream=True), game("r2", "2026-11-15", "BOS", "NYK", stream=True),
         game("q", "2026-11-20", "LAL", "GSW"), game("q2", "2026-11-20", "BOS", "CHA")]
d = sp.plan(D1, D1, only2, **base_inputs())["days"][0]
check("forced picks the furthest clash", d["projected_pick"] == "q2")

d = sp.plan(D1, D1, [game("r", "2026-11-14", "LAL", "MIA", stream=True)] + one, **base_inputs())["days"][0]
check("repeat: 6 days away costs less than 2 days away",
      factor(d, "y", "repeat") == -sp.REPEAT_FAR_PTS)

r = {t: 0.0 for t in TEAMS}
d = sp.plan(D1, D1, [game("t1", "2026-11-20", "ATL", "BKN"), game("t2", "2026-11-20", "TOR", "WAS")],
            **base_inputs(ratings=r))["days"][0]
check("quality: tied ratings score the same, whatever the team names", factor(d, "t1", "quality") == factor(d, "t2", "quality"))

# ── picks and skips drive later days ─────────────────────────────────────────

print("picks and skips")
r = {**{t: 0.0 for t in TEAMS}, "BOS": 3.0, "NYK": 3.0}
two = [game("x1", "2026-11-20", "BOS", "NYK"), game("y1", "2026-11-20", "LAL", "GSW"),
       game("x2", "2026-11-21", "NYK", "BOS"), game("y2", "2026-11-21", "GSW", "LAL")]
p = sp.plan(D1, D2, two, **base_inputs(ratings=r), picks={"y1"})
check("a chosen game is the day's pick", p["days"][0]["projected_pick"] == "y1" and p["days"][0]["pick_source"] == "chosen")
check("and the next day re-ranks around it", p["days"][1]["projected_pick"] == "x2")
p = sp.plan(D1, D2, two, **base_inputs(ratings=r), skips={"2026-11-20"})
check("a skipped day has no pick", p["days"][0]["projected_pick"] is None and p["days"][0]["skipped"])
check("and doesn't push the next day off its best game", p["days"][1]["projected_pick"] == "x2")
p = sp.plan(D1, D2, two, **base_inputs(ratings=r), picks={"nope"})
check("an unknown pick is reported, not used", p["ignored_picks"] == ["nope"] and p["days"][0]["projected_pick"] == "x1")

# ── meetings, last stream, balance ───────────────────────────────────────────

print("meetings and balance")
sch = [game("m1", "2026-10-25", "BOS", "NYK", stream=True), game("m2", "2026-11-01", "NYK", "BOS"),
       game("m3", "2026-11-20", "BOS", "NYK"), game("m4", "2026-12-20", "NYK", "BOS"),
       game("z", "2026-11-20", "LAL", "GSW")]
res = [{"date": "2026-10-25", "home": "NYK", "away": "BOS", "home_pts": 100, "away_pts": 110},
       {"date": "2026-11-01", "home": "BOS", "away": "NYK", "home_pts": 99, "away_pts": 101}]
d = sp.plan(D1, D1, sch, **base_inputs(), results=res, today=D1)["days"][0]
g = next(x for x in d["games"] if x["id"] == "m3")
check("meetings list the other games this season, in date order",
      [m["id"] for m in g["meetings"]] == ["m1", "m2", "m4"])
check("a played meeting carries its score and stream flag",
      g["meetings"][0]["away_pts"] == 110 and g["meetings"][0]["streamed"] and not g["meetings"][1]["streamed"])
check("a future meeting has no score", g["meetings"][2]["home_pts"] is None)
check("last stream is the latest on or before today", g["last_stream"] == {"away": "2026-10-25", "home": "2026-10-25"}
      and next(x for x in d["games"] if x["id"] == "z")["last_stream"] == {"away": None, "home": None})
p = sp.plan(D1, D1, sch, **base_inputs(), results=res, today=D1)
bal = {c["name"]: c for c in p["balance"]["conferences"]}
check("balance counts both teams of each stream (BOS and NYK, plus the day's pick)", bal["East"]["appearances"] + bal["West"]["appearances"] == 4)
check("balance has six divisions", len(p["balance"]["divisions"]) == 6)
check("exposure carries conference and division",
      next(e for e in p["exposure"] if e["team"] == "BOS")["division"] == "Atlantic")

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

# ── open days ────────────────────────────────────────────────────────────────

print("open days")
od = [game("l1", "2026-11-20", "BOS", "NYK", stream=True), game("l2", "2026-11-20", "LAL", "GSW"),
      # 11-21: no games
      game("o1", "2026-11-22", "MIA", "ORL"), game("o2", "2026-11-23", "DEN", "UTA"),
      game("o3", "2026-11-24", "PHX", "SAC")]
p = sp.plan(D1, date(2026, 12, 20), od, **base_inputs(), open_days=2)
check("open_days walks past a locked day and an empty one",
      [d["date"] for d in p["days"]] == ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"])
check("open_days: 'to' is the last day walked", p["to"] == "2026-11-23")
check("open_days: found two, not short", p["open_days_found"] == 2 and not p["open_days_short"])
p = sp.plan(D1, date(2026, 12, 20), od, **base_inputs(), open_days=5)
check("open_days past the schedule is short", p["open_days_short"] and p["open_days_found"] == 3)
p = sp.plan(D1, D2, od, **base_inputs())
check("no open_days keeps the plain range", p["to"] == "2026-11-21" and not p["open_days_short"])

# ── priorities ───────────────────────────────────────────────────────────────

print("priorities")
r = {**{t: 0.0 for t in TEAMS}, "BOS": 6.0, "NYK": 6.0}
oh = {frozenset(("owner-LAL", "owner-GSW")): {"games": 30, "wins": {"owner-LAL": 15, "owner-GSW": 15}, "series": {1, 2}}}
fh = {frozenset(("LAL", "GSW")): {"games": 24, "wins": {"LAL": 12, "GSW": 12}, "series": {"24-25"}}}
inp = base_inputs(ratings=r, owner_h2h=oh, franchise_h2h=fh)
bal = sp.plan(D1, D1, one, **inp)
spicy = sp.plan(D1, D1, one, **inp, priority="spicy")
qual = sp.plan(D1, D1, one, **inp, priority="quality")
check("quality preset picks the better teams", qual["days"][0]["projected_pick"] == "x")
check("spicy preset picks the rivalry", spicy["days"][0]["projected_pick"] == "y")
check("a preset scales a factor's pts and max",
      factor(spicy["days"][0], "y", "owner_rivalry") > factor(bal["days"][0], "y", "owner_rivalry")
      and spicy["weights"]["owner_rivalry"] == sp.WEIGHTS["owner_rivalry"] * 2.5)
check("balanced reports the plain weights", bal["weights"] == {k: float(v) for k, v in sp.WEIGHTS.items()}
      and bal["priority"] == "balanced")
check("the presets are listed", [x["key"] for x in bal["priorities"]] == list(sp.PRIORITIES))
cov_sched = [game("h1", "2026-11-10", "BOS", "NYK", stream=True), game("h2", "2026-11-01", "BOS", "NYK", stream=True),
             game("x", "2026-11-20", "BOS", "NYK"), game("y", "2026-11-20", "LAL", "GSW")]
cov = sp.plan(D1, D1, cov_sched, **base_inputs(ratings=r), priority="coverage")
y = next(g for g in cov["days"][0]["games"] if g["id"] == "y")
check("coverage: the unstreamed pair is the pick", cov["days"][0]["projected_pick"] == "y")
check("coverage scales fair share", y["factors"]["fairness"]["max"] == sp.WEIGHTS["fairness"] * 2.5)
try:
    sp.plan(D1, D1, one, **base_inputs(), priority="vibes")
    check("an unknown priority raises", False)
except ValueError:
    check("an unknown priority raises", True)

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
sp.gh.get_index = lambda: (None, None)
sp.po.season_results = lambda index, season: []
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
res = c.get("/api/stream-planner?picks=y&skip=2026-11-21", headers={"Authorization": "Bearer " + STREAM})
check("picks= reaches the plan", res.status_code == 200 and res.json()["days"][0]["projected_pick"] == "y")
check("an unknown priority is refused", c.get("/api/stream-planner?priority=vibes",
                                             headers={"Authorization": "Bearer " + STREAM}).status_code == 422)
check("open_days over MAX_DAYS is refused", c.get("/api/stream-planner?open_days=40",
                                                  headers={"Authorization": "Bearer " + STREAM}).status_code == 422)
res = c.get("/api/stream-planner?open_days=1&priority=spicy&to=2026-11-20", headers={"Authorization": "Bearer " + STREAM})
check("open_days and priority reach the plan", res.status_code == 200 and res.json()["priority"] == "spicy"
      and res.json()["open_days_found"] == 1 and len(res.json()["days"]) == 1)
check("a bad skip date is refused", c.get("/api/stream-planner?skip=tomorrow",
                                          headers={"Authorization": "Bearer " + STREAM}).status_code == 422)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

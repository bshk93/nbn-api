"""Fair value — the pieces of the DCF, on synthetic inputs.

Pins: the floored prior lands the simulator on the floored rating; who counts
as under contract or re-signed in a later season; re-signing stops at the
ceiling, best OVR first, a kept player stays and a bargain is kept past it;
who walks and who signs them in free agency; the staff playoff edge; ageing;
draft pick odds through the conveyance resolver; the crowd's mood from title
futures; the bandwagon and playoff gate on expected finishes; discounting.
Nothing reads the data dir.

    venv/bin/python -m tests.test_fair_value
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import fair_value as fv  # noqa: E402
from routers import playoff_odds as po  # noqa: E402
from stats_build import finances as fin_mod  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("seasons")
check("next season", fv.season_after("26-27") == "27-28")
check("two back", fv.season_after("26-27", -2) == "24-25")
check("20-21 is index 0", fv.season_index("20-21") == 0 and fv.season_index("26-27") == 6)

print("\nthe roster floor")
priors = {"AAA": 6.0, "BBB": -2.0, "CCC": 1.0}
margin = {"AAA": -100, "BBB": 300}
played = {"AAA": 60, "BBB": 60}
fp = fv.floored_priors(priors, margin, played)
K = po.PRIOR_GAMES
for t in ("AAA", "BBB"):
    rating = (fp[t] * K + margin[t]) / (K + played[t])
    want = fv.PRIOR_FLOOR * priors[t] + (1 - fv.PRIOR_FLOOR) * margin[t] / played[t]
    check(f"{t}: the simulator lands on the floored rating", abs(rating - want) < 1e-9)
check("a team with no games keeps its prior", fp["CCC"] == 1.0)
early = fv.floored_priors({"AAA": 6.0}, {"AAA": 10}, {"AAA": 2})
check("early on, the odds model's own fade is stronger, and stands",
      abs(early["AAA"] - 6.0) < 1e-9)

print("\nwho is on the books")
bio_contract = {"type": "player", "salaries": {"26-27": "$10,000,000", "27-28": "$11,000,000"}}
bio_ending = {"type": "player", "salaries": {"26-27": "$20,000,000", "27-28": "$30,000,000"},
              "cap_holds": {"27-28": "UFA"}}
bio_unsigned = {"type": "player", "salaries": {"25-26": "$5,000,000", "26-27": "$8,000,000"},
                "cap_holds": {"26-27": "UFA"}}
check("a salaried season is a contract year", fv.contract_year(bio_contract, "27-28", "26-27") == "contract")
check("past the deal, no hold: gone", fv.contract_year(bio_contract, "28-29", "26-27") is None)
check("a UFA hold season is not a contract year", fv.contract_year(bio_ending, "27-28") is None)
check("but with the window open, he re-signs", fv.contract_year(bio_ending, "27-28", "26-27") == "resigned")
check("and stays re-signed the season after", fv.contract_year(bio_ending, "28-29", "26-27") == "resigned")
check("at his hold amount", fv.hold_amount(bio_ending, "28-29", "26-27") == 30_000_000)
check("an unsigned free agent now is not assumed back", fv.contract_year(bio_unsigned, "27-28", "26-27") is None)
check("draft rights are never on the books",
      fv.contract_year({"type": "draft-rights", "salaries": {"27-28": "$1"}}, "27-28", "26-27") is None)

print("\nre-signing stops at the ceiling")
bios = {
    "star": {"type": "player", "dob": "2000-01-01", "salaries": {"26-27": "$40,000,000", "27-28": "$50,000,000"},
             "cap_holds": {"27-28": "UFA"}},
    "role": {"type": "player", "dob": "2000-01-01", "salaries": {"26-27": "$10,000,000", "27-28": "$15,000,000"},
             "cap_holds": {"27-28": "UFA"}},
    "vet": {"type": "player", "dob": "1990-01-01", "salaries": {"26-27": "$10,000,000", "27-28": "$20,000,000"},
            "cap_holds": {"27-28": "UFA"}},
    "kid": {"type": "player", "dob": "2005-01-01", "salaries": {"26-27": "$2,000,000", "27-28": "$2,000,000"}},
}
ovr = {"star": 92.0, "role": 78.0, "vet": 85.0, "kid": 70.0}
with mock.patch.object(fv, "_roster", return_value=list(bios)):
    bk = fv.books("AAA", "27-28", 1, bios, ovr, contracted=150_000_000, ceiling=210_000_000, kept=set())
    on = {p["slug"] for p in bk["players"]}
    check("the best free agent re-signs first", "star" in on)
    check("the next one that fits is skipped once the ceiling is hit", "role" not in on)
    check("contract players are always on", "kid" in on)
    check("too old to re-sign", "vet" not in on)
    check("re-signing money is the hold", bk["resign_money"] == 50_000_000)
    kept = fv.books("AAA", "27-28", 1, bios, ovr, contracted=200_000_000, ceiling=210_000_000, kept={"role"})
    check("a player kept last season stays, ceiling or not", "role" in {p["slug"] for p in kept["players"]})
    check("the young player is aged up", next(p for p in bk["players"] if p["slug"] == "kid")["ovr"] > 70.0)

    check("a player who walks is in the pool, with his team",
          [(p["slug"], p["from"]) for p in bk["walked"]] == [("role", "AAA")])
    check("one signed elsewhere is gone",
          "star" not in {p["slug"] for p in fv.books("AAA", "27-28", 1, bios, ovr, 150_000_000, 210_000_000,
                                                      set(), frozenset({"star"}))["players"]})
    over = fv.books("AAA", "27-28", 1, bios, ovr, contracted=200_000_000, ceiling=210_000_000, kept=set(),
                    cap=170_000_000)
    check("a bargain is kept past the ceiling", "star" in {p["slug"] for p in over["players"]})
    check("one worth less than his hold still walks", "role" not in {p["slug"] for p in over["players"]})

print("\nfree agency")
cap = 170_000_000
check("a better player asks more", fv.fa_ask(85, cap) > fv.fa_ask(80, cap))
check("an 84 asks about ¥18M", 16e6 < fv.fa_ask(84, cap) < 20e6)
check("never past the max", fv.fa_ask(99, cap) == cap * fv.FA_MAX_SHARE)
exc = {"ntmle_amount": 15e6, "tmle_amount": 6e6}
check("room is the budget", fv.fa_budget(130e6, cap, 210e6, 220e6, exc) == 40e6)
check("a little room still has the MLE", fv.fa_budget(165e6, cap, 210e6, 220e6, exc) == 15e6)
check("over the cap, the MLE", fv.fa_budget(200e6, cap, 210e6, 220e6, exc) == 15e6)
check("over the first apron, the taxpayer MLE", fv.fa_budget(215e6, cap, 210e6, 220e6, exc) == 6e6)
check("over the second, nothing", fv.fa_budget(225e6, cap, 210e6, 220e6, exc) == 0)
pool = [{"slug": "a", "ovr": 88.0, "from": "AAA"}, {"slug": "b", "ovr": 80.0, "from": "BBB"},
        {"slug": "c", "ovr": 70.0, "from": "BBB"}]
got = fv.free_agency(pool, {"AAA": 60e6, "BBB": 25e6, "CCC": 15e6}, {"AAA": 2, "BBB": 2, "CCC": 2},
                     cap, 77, 5e6, {"BBB": 5.0, "CCC": -3.0})
check("nobody signs back with the team that let him walk", not any(p["slug"] == "a" for p in got["AAA"]))
check("the star goes where the money is", [p["slug"] for p in got["BBB"]] == ["a"])
check("taking less than his price", got["BBB"][0]["money"] == 25e6 < fv.fa_ask(88, cap))
check("an equal offer goes to the better team, not the alphabet",
      [p["slug"] for p in fv.free_agency(pool[1:2], {"BBB": 0, "CCC": 15e6, "DDD": 15e6},
                                         {"BBB": 1, "CCC": 1, "DDD": 1}, cap, 77, 5e6,
                                         {"CCC": 4.0, "DDD": -4.0})["CCC"]] == ["b"])
check("a replacement-level player is left to the roster fill", not any(p["slug"] == "c" for v in got.values() for p in v))
check("no roster spot, no signing",
      fv.free_agency(pool[1:2], {"AAA": 60e6}, {"AAA": 0}, cap, 77, 5e6) == {"AAA": []})
check("an offer far under his price is turned down",
      fv.free_agency(pool[:1], {"BBB": 10e6}, {"BBB": 1}, cap, 77, 5e6) == {"BBB": []})

print("\nplayoff edge")
members = {
    "owner": {"tenures": [{"team": "AAA", "start": "2020-07-01", "end": None, "position": "owner"}]},
    "coach": {"tenures": [{"team": "AAA", "start": "2020-07-01", "end": "2022-06-30", "position": "coach"},
                          {"team": "BBB", "start": "2022-07-01", "end": None, "position": "owner"}]},
    "rival": {"tenures": [{"team": "CCC", "start": "2020-07-01", "end": None, "position": "gm"}]},
}
check("a staff is everyone on it that day, coach included", fv.staff_on(members, "AAA", "2021-05-01") == ["coach", "owner"])
check("and not after he left", fv.staff_on(members, "AAA", "2023-05-01") == ["owner"])
check("the playoff staff is read in May of the season's second year", fv.playoff_day("21-22") == "2022-05-01")
standings = [{"SEASON": s_, "TEAM": t, "DIFF": "0"} for s_ in ("20-21", "21-22", "22-23") for t in ("AAA", "BBB", "CCC")]
brackets = [{"SEASON": "20-21", "T1": "AAA", "T2": "CCC", "WINNER": "AAA"},
            {"SEASON": "21-22", "T1": "CCC", "T2": "AAA", "WINNER": "AAA"},
            {"SEASON": "22-23", "T1": "AAA", "T2": "CCC", "WINNER": "CCC"}]
rec = fv.playoff_records(members, brackets, standings, "22-23")
check("an even series won is +0.5 to everyone on the staff", rec["owner"] == [0.5, 0.5] and rec["coach"] == [0.5, 0.5])
check("and lost is -0.5", rec["rival"] == [-0.5, -0.5])
check("the season being priced, and later, don't count", len(fv.playoff_records(members, brackets, standings, "21-22")["owner"]) == 1)
check("a record is shrunk", 0 < fv.person_edge(rec["owner"]) < 0.5 and fv.person_edge([]) == 0)
ed = fv.playoff_edges("22-23", "2022-07-15", ["AAA", "BBB", "CCC", "DDD"], members, brackets, standings)
check("the coach's record goes with him to his new team", ed["BBB"]["edge"] == round(fv.person_edge(rec["coach"]), 4))
check("a team's edge is its staff's average", ed["AAA"]["edge"] == ed["BBB"]["edge"] > 0 > ed["CCC"]["edge"])
check("no staff, no edge", ed["DDD"] == {"edge": 0.0, "points": 0.0, "staff": []})
check("points are the edge at PLAYOFF_EDGE_POINTS",
      ed["AAA"]["points"] == round(fv.PLAYOFF_EDGE_POINTS * ed["AAA"]["edge"], 2))
lg = sorted(po.EAST)[:10] + sorted(po.WEST)[:10]
flat = {t: 0.0 for t in lg}
sched = [{"date": "2026-11-01", "home_team": a, "away_team": b} for a in lg for b in lg if a != b and (a in po.EAST) == (b in po.EAST)][:200]
plain = po.simulate([], sched, flat, sims=3000, seed=7)
same = po.simulate([], sched, flat, sims=3000, seed=7, playoff_bonus={})
boosted = po.simulate([], sched, flat, sims=3000, seed=7, playoff_bonus={lg[0]: 3.0})
title = lambda r, t: next(x for x in r["teams"] if x["team"] == t)["title"]
check("no bonus, the same simulation", plain == same)
check("a playoff bonus wins more titles", title(boosted, lg[0]) > title(plain, lg[0]))
check("but not more regular-season games", next(x for x in boosted["teams"] if x["team"] == lg[0])["proj_w"]
      == next(x for x in plain["teams"] if x["team"] == lg[0])["proj_w"])

print("\nageing")
check("young players improve", fv.aged_ovr(70, 21.0, 2) > 70)
check("prime players move under a point", abs(fv.aged_ovr(85, 28.0, 1) - 85) < 1)
check("old players decline", fv.aged_ovr(85, 33.5, 2) < 85)
check("a 21-year-old gains more than a 33-year-old loses in a year",
      fv.aged_ovr(80, 21.0, 1) - 80 > 0 and fv.aged_ovr(80, 33.0, 1) - 80 < 0)
check("unknown age: unchanged", fv.aged_ovr(80, None, 2) == 80)
check("never above 99", fv.aged_ovr(98, 20.0, 2) == 99)

print("\nrevenue on expected inputs")
row = {"playoffs": 1.0, "second_round": 1.0, "conf_finals": 1.0, "finals": 1.0, "title": 1.0}
none = dict.fromkeys(row, 0.0)
check("no playoffs, no playoff gate", fv.playoff_gate(none) == 0)
check("a certain title run pays every round", fv.playoff_gate(row) > fv.playoff_gate({**row, "finals": 0.0, "title": 0.0}))
fp_ = fv.finish_probs({"title": 0.2, "finals": 0.5, "conf_finals": 0.7, "second_round": 0.9})
check("finishes are the gaps between rounds",
      abs(fp_["Champion"] - 0.2) < 1e-9 and abs(fp_["Runner-Up"] - 0.3) < 1e-9
      and abs(fp_["Conf Finals"] - 0.2) < 1e-9 and abs(fp_["Second Round"] - 0.2) < 1e-9)
champ = fv.expected_bandwagon([{"Champion": 1.0}])
check("an actual title is the ledger's full bandwagon", abs(champ - 0.10) < 1e-9)
check("a half chance of a title is half of it", abs(fv.expected_bandwagon([{"Champion": 0.5}]) - 0.05) < 1e-9)
check("an older title fades", fv.expected_bandwagon([{}, {"Champion": 1.0}]) < champ)
check("better form, bigger gate", fv.gate_per_game(0.8, 0, 0.5, 0) > fv.gate_per_game(0.3, 0, 0.5, 0))
check("never past a sellout", fv.gate_per_game(1.0, 2, 2, 1.0) == 19_000 * 120)

print("\ndiscounting")
check("now is worth face value", fv.discount(0) == 1)
check("a season out is worth less", abs(fv.discount(1) - 1 / 1.08) < 1e-12)

print("\nthe seed")
check("one seed for the whole season", fv.season_seed("26-27") == fv.season_seed("26-27"))
check("a different seed each season", fv.season_seed("26-27") != fv.season_seed("27-28"))
src = Path(fv.__file__).read_text()
check("compute does not seed off the date", "today.replace" not in src)

print("\nlate box scores land on their own day")
def _row(d, w):
    return {"date": d, "season": "26-27", "teams": {"AAA": {"w": w}, "BBB": {"w": 0}}}
games = [{"date": "2026-10-20"}, {"date": "2026-10-21"}, {"date": "2026-10-21"}]
with mock.patch.object(fv, "read_history", lambda season=None: [_row("2026-10-20", 1), _row("2026-10-21", 1), _row("2026-10-23", 3)]):
    st = fv.stale_days("26-27", "2026-10-23", games)
check("a day short of its own games is redone", "2026-10-21" in st)
check("a missing day is filled", "2026-10-22" in st)
check("a complete day is left alone", "2026-10-20" not in st)
check("today is the hourly run's, not a late day", "2026-10-23" not in st)
with mock.patch.object(fv, "read_history", lambda season=None: []):
    check("no history, nothing to redo", fv.stale_days("26-27", "2026-10-23", games) == [])

print("\ninjuries")
check("a range is its middle", fv.injury_days("6-8 weeks") == 49)
check("day-to-day is short", fv.injury_days("Day-To-Day") == fv.INJURY_DAYS_SHORT)
check("career never ends", fv.injury_days("Career") == float("inf"))
check("unreadable is left out", fv.injury_days("soon") is None)
log = [
    {"date": "2026-10-20", "slug": "a", "kind": "new", "after": {"status": "Out", "duration": "2-4 weeks"}},
    {"date": "2026-10-22", "slug": "a", "kind": "changed", "after": {"status": "Hurt", "duration": "1-2 weeks"}},
    {"date": "2026-10-23", "slug": "a", "kind": "healed", "before": {}},
]
check("the list folds to a day", fv.injuries_as_of("2026-10-21", log)["a"]["status"] == "Out")
check("a change dates the injury from its report", fv.injuries_as_of("2026-10-22", log)["a"]["date"] == "2026-10-22")
check("healed is off the list", fv.injuries_as_of("2026-10-23", log) == {})
sched = [{"date": f"2026-11-{d:02d}", "home_team": "AAA", "away_team": "BBB"} for d in range(1, 11)]
check("a played game is not left", len(fv.unplayed_games(sched, [{"date": "2026-11-01", "home": "BBB", "away": "AAA"}])) == 9)
ovrs = {f"p{i}": 90 - i for i in range(10)}
full = fv._strength(list(ovrs.values()))
out = {"p0": {"status": "Out", "duration": "5 days", "date": "2026-11-01"}}
st, hurt = fv.injured_strength("AAA", ovrs, out, sched)
check("out for half the games costs half his drop",
      abs((full - st) - (full - fv._strength([v for k, v in ovrs.items() if k != "p0"])) * 0.5) < 1e-9)
check("and says how many he misses", hurt[0]["games"] == 5 and hurt[0]["of"] == 10)
hurt_only = {"p0": {"status": "Hurt", "duration": "5 days", "date": "2026-11-01", "ovr_delta": -4}}
check("hurt costs less than out", full - fv.injured_strength("AAA", ovrs, hurt_only, sched)[0] < full - st)
check("a bench player out costs nothing", fv.injured_strength("AAA", ovrs, {"p9": {**out["p0"]}}, sched)[0] == full)
check("a healed-by-now injury costs nothing",
      fv.injured_strength("AAA", ovrs, {"p0": {**out["p0"], "date": "2026-09-01"}}, sched)[0] == full)

print("\nbuzz")
check("an average team settles at no buzz", fv.rating_buzz(0) == 0)
check("a good one above it, inside the band", 0 < fv.rating_buzz(8) <= fin_mod.BUZZ_MAX)
check("the next ten games keep today's buzz", fv.rest_of_season_buzz(0.15, 0, 10) == 0.15)
check("then it runs to the rating's", abs(fv.rest_of_season_buzz(0.15, 0, 70) - 0.15 * 10 / 70) < 1e-12)
check("no games left, today's", fv.rest_of_season_buzz(0.1, 5, 0) == 0.1)

print("\ndraft picks")
check("the top pick is the best", fv.rookie_ovr(1) > fv.rookie_ovr(10) > fv.rookie_ovr(45))
check("a top pick is near 81", abs(fv.rookie_ovr(1) - 80.74) < 1e-9)
check("the 2027 draft follows 26-27", fv.draft_after(2027) == "26-27")
store = {"picks": [
    {"year": 2027, "round": 1, "orig": "AAA", "conveyance": {"type": "settled", "team": "BBB"}},
    {"year": 2027, "round": 1, "orig": "BBB", "conveyance": {"type": "settled", "team": "BBB"}},
    {"year": 2027, "round": 1, "orig": "CCC", "conveyance": {
        "type": "protected", "id": "p", "on": {"year": 2027, "round": 1, "orig": "CCC"},
        "bands": [{"min": 1, "max": 1, "to": "CCC"}, {"min": 2, "max": 6, "to": "AAA"}]}},
]}
orders = [[["CCC", "AAA", "BBB"]] * 3 + [["AAA", "CCC", "BBB"]]]
odds = fv.pick_odds(store, 2027, orders)[0]
check("a traded pick is its owner's", odds["BBB"][(2027, 1, "AAA")] == {2: 0.75, 1: 0.25})
check("a protected pick stays when it lands in the band", odds["CCC"][(2027, 1, "CCC")] == {1: 0.75})
check("and conveys when it doesn't", odds["AAA"][(2027, 1, "CCC")] == {2: 0.25})
check("round two slots follow round one", fv.pick_odds(
    {"picks": [{"year": 2027, "round": 2, "orig": "AAA", "conveyance": {"type": "settled", "team": "AAA"}}]},
    2027, [[["AAA", "BBB", "CCC"]]])[0]["AAA"][(2027, 2, "AAA")] == {4: 1.0})
check("no store, no picks", fv.pick_odds(None, 2027, orders) == [{}])
sim_rows = [{"date": "2026-11-01", "home_pts": 100, "away_pts": 90, "home": "AAA", "away": "BBB"}]
sched_po = [{"date": "2026-11-02", "home_team": a, "away_team": b} for a, b in (("AAA", "BBB"), ("BBB", "CCC"), ("CCC", "AAA"))] * 5
pri = {"AAA": 3.0, "BBB": -1.0, "CCC": 0.0}
plain = po.simulate(sim_rows, sched_po, pri, sims=200, seed=7)
drafty = po.simulate(sim_rows, sched_po, pri, sims=200, seed=7, draft_samples=50)
check("sampling draft orders leaves the odds alone", plain["teams"] == drafty["teams"])
check("and keeps as many as asked, every team once",
      len(drafty["draft_orders"]) == 50 and all(sorted(o) == ["AAA", "BBB", "CCC"] for o in drafty["draft_orders"]))

print("\nthe market price")
zs = [fv._shock("t", i) for i in range(4000)]
check("a shock is seeded", fv._shock("a", 1) == fv._shock("a", 1) and fv._shock("a", 1) != fv._shock("a", 2))
check("shocks are unit variance", abs(sum(z * z for z in zs) / len(zs) - 1) < 0.1)
check("a beta is fixed for the season and in its band",
      fv.team_beta("26-27", "AAA") == fv.team_beta("26-27", "AAA") and fv.BETA_RANGE[0] <= fv.team_beta("26-27", "AAA") <= fv.BETA_RANGE[1])
def days(prices):
    return [{"season": "26-27", "date": f"2026-11-{i + 1:02d}", "teams": {"AAA": {"price": p}}} for i, p in enumerate(prices)]
calm = fv.market_series(days([100.0] * 5), shock=lambda *a: 0.0)
check("no news, no shocks: the market sits at fair value", all(r["teams"]["AAA"]["market"] == 100.0 for r in calm))
jump = fv.market_series(days([100.0, 110.0] + [110.0] * 40), shock=lambda *a: 0.0)
m = [r["teams"]["AAA"]["market"] for r in jump]
check("news is overdone", m[1] > 110.0)
check("and given back", m[1] > m[10] > m[-1] > 110.0)
a1 = fv.market_series(days([100.0, 101.0, 99.0, 102.0]))
a2 = fv.market_series(days([100.0, 101.0, 99.0, 102.0]))
check("a rebuild gives the same series", [r["teams"]["AAA"]["market"] for r in a1] == [r["teams"]["AAA"]["market"] for r in a2])

print("\nthe crowd")
check("points, not a ratio", abs(fv.crowd_mood(4) - 0.04) < 1e-12)
check("capped either way", fv.crowd_mood(50) == fv.CROWD_CAP and fv.crowd_mood(-50) == -fv.CROWD_CAP)
fut = {"outcomes": [{"team": "AAA"}, {"team": "BBB"}],
       "history": [{"ts": "2026-10-01T16:00:00+00:00", "p": [50.0, 50.0]},
                   {"ts": "2026-11-02T03:00:00+00:00", "p": [60.0, 40.0]},     # Nov 1, 11pm in New York
                   {"ts": "2026-11-03T16:00:00+00:00", "p": [45.0, 55.0]}]}
at = fv.crowd_lookup("26-27", fut)
check("a trade counts on its league day", at("26-27", "2026-11-01", "AAA") == 10.0)
check("before any trade, nothing", at("26-27", "2026-09-01", "AAA") == 0.0)
check("the last move of the day holds", at("26-27", "2026-11-05", "BBB") == 5.0)
check("another season's rows are untouched", at("27-28", "2026-11-05", "AAA") == 0.0)
flat = fv.market_series(days([100.0] * 3), shock=lambda *a: 0.0, crowd=lambda s, d, t: 5.0)
check("the crowd's move is on the price, as a level",
      all(abs(r["teams"]["AAA"]["market"] - 100 * math.exp(0.05)) < 1e-3 for r in flat))

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

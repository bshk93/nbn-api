"""Fair value — the pieces of the DCF, on synthetic inputs.

Pins: the floored prior lands the simulator on the floored rating; who counts
as under contract or re-signed in a later season; re-signing stops at the
ceiling, best OVR first, and a kept player stays; ageing; the bandwagon and
playoff gate on expected finishes; discounting. Nothing reads the data dir.

    venv/bin/python -m tests.test_fair_value
"""
from __future__ import annotations

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

print("\nageing")
check("young players improve", fv.aged_ovr(70, 21.0, 2) > 70)
check("prime players hold", fv.aged_ovr(85, 28.0, 1) == 85)
check("old players decline", fv.aged_ovr(85, 33.5, 2) < 85)
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

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

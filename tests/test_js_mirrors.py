"""Python ports of nbn-today JS, run side by side on the same inputs.

Four rules live twice, once in a page's JS and once here for Discord, cards and
the ballot. Each side had its own tests, and nothing compared the two, so one
could drift without failing anything. The starting five already had: until
2026-11-16 discord._compute_starting_five broke ties slot by slot where
teams/lineup.js ranks total OVR, then position fit.

  names.js            nbnPlayerName        <->  routers/names.py player_name
  teams/lineup.js     computeStartingFive  <->  discord._compute_starting_five
  award-race.js       6MOY filter          <->  game_starters.sixth_man_row
  frivolities/        QUAD_VARS keys       <->  quadrants.STAT_KEYS

Needs node and the nbn-today checkout beside this one (as
test_contract_shorthand.py does). Without either it says so and skips.

    venv/bin/python -m tests.test_js_mirrors
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT.parent / "nbn-today"
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def node(js_files: list[Path], body: str, payload) -> object:
    """Load js_files as classic scripts into one context, run body with
    `input` bound to payload, and return what it prints as JSON."""
    src = "\n".join(p.read_text() for p in js_files)
    script = (
        "const vm=require('vm');const ctx={console,Math,JSON,Set,Map,Array,Object,String,Number,"
        "parseFloat,Infinity};ctx.globalThis=ctx;ctx.window=ctx;vm.createContext(ctx);"
        f"vm.runInContext({json.dumps(src)}+'\\n;globalThis.__run=function(input){{{body}}};',ctx);"
        "process.stdout.write(JSON.stringify(ctx.__run(JSON.parse(require('fs').readFileSync(0,'utf8')))));"
    )
    out = subprocess.run(["node", "-e", script], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


# ── names ────────────────────────────────────────────────────────────────────
def test_names():
    from routers.names import player_name, _WORDS, _INITIALS
    print("\nnames.js <-> names.py")
    js = SITE / "names.js"
    words = node([js], "return {w:Object.values(_NBN_NAME_WORDS).sort(),i:[..._NBN_INITIALS].sort()}", None)
    check("same spelling list", words["w"] == sorted(_WORDS.values()),
          f"js-only {set(words['w']) - set(_WORDS.values())}, py-only {set(_WORDS.values()) - set(words['w'])}")
    check("same initials", words["i"] == sorted(_INITIALS))
    cases = [
        "MCCOLLUM, CJ", "JAMES, LEBRON", "GILGEOUS-ALEXANDER, SHAI", "BROWN JR, MIKEL",
        "WILLIAMS III, ROBERT", "ONEALE, ROYCE", "RUSSELL, DANGELO", "MCCAIN, JARED",
        "VANVLEET, FRED", "DA SILVA, TRISTAN", "N'DIAYE, X", "SMITH JR., JABARI",
        "nance, larry", "Already Cased", "", "OG, ANUNOBY", "MC, X", "MCDANIELS, JADEN",
        "O’NEALE, ROYCE", "HOLMES II, RICHAUN", "DEROZAN, DEMAR", "JACKSON-DAVIS, TRAYCE",
    ]
    got = node([js], "return input.map(nbnPlayerName)", cases)
    for raw, g in zip(cases, got):
        check(f"{raw!r}", g == player_name(raw), f"js {g!r} vs py {player_name(raw)!r}")


# ── starting five ────────────────────────────────────────────────────────────
POS = ["PG", "SG", "SF", "PF", "C"]


def _random_roster(rng: random.Random, n: int) -> list[dict]:
    out = []
    for i in range(n):
        primary = rng.choice(POS)
        k = POS.index(primary)
        pos = [primary]
        if rng.random() < 0.8:
            pos.append(POS[max(0, min(4, k + rng.choice([-1, 1])))])
        out.append({"slug": f"p{i}", "pos_list": list(dict.fromkeys(pos)),
                    "ovr": rng.choice([70, 72, 75, 75, 78, 80, 80, 84])})
    return out


def test_lineup():
    from routers.discord import _compute_starting_five
    print("\nteams/lineup.js <-> discord._compute_starting_five")
    rng = random.Random(20261116)
    rosters = [_random_roster(rng, rng.randint(3, 15)) for _ in range(300)]
    # The tie the old port got wrong: a C and a PF who could swap, the PF
    # rated higher. Position fit keeps each at his own spot.
    rosters.append([{"slug": "c", "pos_list": ["C", "PF"], "ovr": 80},
                    {"slug": "pf", "pos_list": ["PF", "C"], "ovr": 84},
                    {"slug": "g1", "pos_list": ["PG"], "ovr": 75},
                    {"slug": "g2", "pos_list": ["SG"], "ovr": 75},
                    {"slug": "w", "pos_list": ["SF"], "ovr": 75}])
    js_in = [[{"slug": p["slug"], "_posList": p["pos_list"], "OVR": p["ovr"]} for p in r] for r in rosters]
    js = node([SITE / "teams" / "lineup.js"],
              "return input.map(r => computeStartingFive(r).map(p => p ? p.slug : null))", js_in)
    bad = 0
    for i, r in enumerate(rosters):
        py = [p["slug"] if p else None for p in _compute_starting_five(r)]
        if py != js[i]:
            bad += 1
            if bad <= 3:
                print(f"    roster {i}: js {js[i]} py {py}")
    check(f"same five on {len(rosters)} rosters", bad == 0, f"{bad} differ")
    check("C and PF keep their own spots", js[-1][3:] == ["pf", "c"])


# ── 6MOY ─────────────────────────────────────────────────────────────────────
def test_sixth_man():
    from routers.game_starters import sixth_man_row
    print("\naward-race.js 6MOY <-> game_starters.sixth_man_row")
    rng = random.Random(6)
    cases = []
    for _ in range(400):
        teams = rng.sample(["ATL", "BOS", "CHI"], rng.randint(1, 2))
        known_by_team = {t: rng.randint(0, 20) for t in teams}
        g = rng.randint(1, 30)
        cases.append({"G": g, "teams": teams, "known": known_by_team, "gs": rng.randint(0, g)})
    # ladder() needs real rows; the filter is what's mirrored, so run that
    # expression from award-race.js on the same numbers.
    src = (SITE / "award-race.js").read_text()
    m = re.search(r"const known = (Math\.min\(p\.G, .*?\));\n.*?\.filter\(p => (.*?)\);", src, re.S)
    check("found the 6MOY filter in award-race.js", m is not None)
    if not m:
        return
    known_expr, keep_expr = m.group(1), m.group(2)
    body = ("return input.map(c => { const st={team_games_known:c.known}; const p={G:c.G,teams:c.teams,gs:c.gs};"
            f" const known = {known_expr}; p.known = known; return [known, {keep_expr}]; }})")
    js = node([], body, cases)
    bad = 0
    for c, (known, keep) in zip(cases, js):
        row = sixth_man_row(c["G"], c["teams"], c["known"], c["gs"])
        # The race also drops players with no known games (nothing to rank on);
        # the ballot keeps them eligible (nothing counts against them).
        if row["known"] != known or (known > 0 and row["eligible"] != keep):
            bad += 1
    check(f"same known count and threshold on {len(cases)} cases", bad == 0, f"{bad} differ")


# ── quadrant stat keys ───────────────────────────────────────────────────────
def test_quadrants():
    from routers.quadrants import STAT_KEYS
    print("\nfrivolities AXIS_VARS + ovr <-> quadrants.STAT_KEYS")
    src = (SITE / "frivolities" / "index.html").read_text()
    m = re.search(r"const AXIS_VARS = \{(.*?)\n    \};", src, re.S)
    check("found AXIS_VARS", m is not None)
    if not m:
        return
    keys = set(re.findall(r"^\s{6}(?:'([^']+)'|([a-z0-9_%]+))\s*:", m.group(1), re.M))
    keys = {a or b for a, b in keys}
    extra = set(re.findall(r"QUAD_VARS = \{ \.\.\.AXIS_VARS, ([a-z0-9_]+):", src))
    page = keys | extra
    check("every chart stat can be saved", page <= STAT_KEYS, f"missing {sorted(page - STAT_KEYS)}")
    check("no saved stat the page can't draw", STAT_KEYS <= page, f"unused {sorted(STAT_KEYS - page)}")


def main():
    if not shutil.which("node") or not SITE.is_dir():
        print("[skip] needs node and ../nbn-today")
        return 0
    test_names()
    test_lineup()
    test_sixth_man()
    test_quadrants()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("JS MIRROR TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

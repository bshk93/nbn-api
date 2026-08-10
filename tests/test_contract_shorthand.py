"""Pins `discord_notify._contract_str` to the shared JS shorthand in
nbn-today/contract.js.

The site renders a deal's shape — "2+1 PO" — on team pages, the PDC review
dashboard and the transactions log, all from one function
(`summarizeContract` in contract.js). The Discord embeds render the same shape
from `_contract_str`, which cannot import a JS module and so is a deliberate
mirror. A mirror maintained by comment drifts the first time someone edits one
side; this suite is what actually holds them together.

**Only the shape is shared.** The money suffix is deliberately different —
Discord shows `· $25.0M`, the site shows `, $25M` — so the comparison is on the
part before the money, plus a separate check that both are summing the same set
of years (the trailing UFA/RFA hold excluded, which is the bug this grammar
exists to avoid).

    venv/bin/python -m tests.test_contract_shorthand
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.discord_notify import _contract_str, _contract_parts, _dollars  # noqa: E402

# The site repo sits beside this one. Overridable so a checkout elsewhere can
# still run the suite.
CONTRACT_JS = Path(os.environ.get(
    "NBN_CONTRACT_JS",
    Path(__file__).resolve().parents[2] / "nbn-today" / "contract.js",
))

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def _js_shorthand(cases: list[dict]) -> list[str]:
    """Run contract.js's summarizeContract over `cases` under Node."""
    script = (
        f"const c = require({str(CONTRACT_JS)!r});"
        "const cases = JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(cases.map(x => c.summarizeContract(x))));"
    )
    out = subprocess.run(
        ["node", "-e", script, json.dumps(cases)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def _shape(s: str) -> str:
    """The year-shape prefix, with each side's money suffix stripped."""
    for sep in (" · $", ", $"):
        if sep in s:
            return s.split(sep)[0]
    return s


CASES = [
    # (label, contract)
    ("1 guaranteed year",
     {"salaries": {"26-27": "$5,000,000"}, "cap_holds": {}}),
    ("3 guaranteed years",
     {"salaries": {"26-27": "$5,000,000", "27-28": "$5,250,000", "28-29": "$5,500,000"},
      "cap_holds": {}}),
    ("2 guaranteed + 1 player option",
     {"salaries": {"26-27": "$10,000,000", "27-28": "$10,500,000", "28-29": "$11,000,000"},
      "cap_holds": {"28-29": "PLAYER_OPT"}}),
    ("2 guaranteed + 1 team option, rolling into a UFA hold",
     {"salaries": {"26-27": "$10,000,000", "27-28": "$10,500,000", "28-29": "$11,000,000",
                   "29-30": "$16,000,000"},
      "cap_holds": {"28-29": "TEAM_OPT", "29-30": "UFA"}}),
    ("non-guaranteed year then a team option",
     {"salaries": {"26-27": "$2,000,000", "27-28": "$2,100,000"},
      "cap_holds": {"26-27": "NON_GTD", "27-28": "TEAM_OPT"}}),
    ("every year an option",
     {"salaries": {"26-27": "$3,000,000", "27-28": "$3,100,000"},
      "cap_holds": {"26-27": "PLAYER_OPT", "27-28": "PLAYER_OPT"}}),
    ("nothing but a hold",
     {"salaries": {"26-27": "$8,000,000"}, "cap_holds": {"26-27": "UFA"}}),
    ("4 years, option run at the end",
     {"salaries": {"26-27": "$20,000,000", "27-28": "$21,000,000",
                   "28-29": "$22,000,000", "29-30": "$23,000,000"},
      "cap_holds": {"28-29": "PLAYER_OPT", "29-30": "PLAYER_OPT"}}),
]


def main():
    if not CONTRACT_JS.exists():
        print(f"contract.js not found at {CONTRACT_JS} — skipping (set NBN_CONTRACT_JS)")
        return 0
    if not shutil.which("node"):
        print("node not on PATH — skipping")
        return 0

    contracts = [c for _, c in CASES]
    js = _js_shorthand(contracts)

    print("Shape agrees between contract.js and _contract_str")
    for (label, contract), js_out in zip(CASES, js):
        py_out = _contract_str(contract)
        ok = _shape(js_out) == _shape(py_out)
        check(f"{label}: js {_shape(js_out)!r} == py {_shape(py_out)!r}", ok)

    print("\nBoth sum the deal years only — never the trailing hold")
    rolling = CASES[3][1]
    deal, trailing = _contract_parts(rolling)
    check("the 29-30 UFA line is read as a trailing hold, not a year",
          trailing is not None and trailing[0] == "29-30" and len(deal) == 3)
    check("_contract_str's total is the 3 deal years ($31.5M), not $47.5M",
          "$31.5M" in _contract_str(rolling))
    check("the shared JS agrees on the same total",
          "$31.5M" in js[3])
    check("deal-year sum matches the parts helper",
          sum(_dollars(a) for _, a, _ in deal) == 31_500_000)

    print("\nEmpty and two-way differ by design, and are each other's business")
    check("_contract_str reports a two-way as 'Two-Way'",
          _contract_str({"salaries": {}, "type": "two-way"}) == "Two-Way")
    check("the JS reports an empty deal as an em dash",
          _js_shorthand([{"salaries": {}, "cap_holds": {}}])[0] == "—")

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

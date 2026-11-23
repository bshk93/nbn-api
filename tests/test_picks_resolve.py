"""POST /api/picks/resolve, on a small made-up store (never the live one).

    venv/bin/python -m tests.test_picks_resolve
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from routers import picks_resolve as PR  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# CHI's 2027 1st is top-4 protected to DEN; BOS's own is settled.
STORE = {"picks": [
    {"year": 2027, "round": 1, "orig": "BOS",
     "conveyance": {"type": "settled", "team": "BOS"}},
    {"year": 2027, "round": 1, "orig": "CHI",
     "conveyance": {"type": "protected", "id": "p1", "on": {"year": 2027, "round": 1, "orig": "CHI"},
                    "bands": [{"min": 1, "max": 4, "to": "CHI"}, {"min": 5, "max": 60, "to": "DEN"}]}},
    {"year": 2028, "round": 1, "orig": "BOS",
     "conveyance": {"type": "settled", "team": "BOS"}},
]}


def main():
    PR.conv_store.load_store = lambda: STORE

    out = PR.resolve_order(PR.ResolveBody(year=2027, round1=["CHI", "BOS"]))["owners"]
    check("a protected pick in its band stays home", out.get("1-CHI") == "CHI")
    check("a settled pick goes to its owner", out.get("1-BOS") == "BOS")
    check("only the asked year comes back", all(k.startswith("1-") for k in out) and len(out) == 2)

    out = PR.resolve_order(PR.ResolveBody(year=2027, round1=["BOS", "ATL", "BKN", "CHA", "CHI"]))["owners"]
    check("past its band the protected pick conveys", out.get("1-CHI") == "DEN")

    for bad in (["XXX"], ["BOS", "BOS"]):
        try:
            PR.resolve_order(PR.ResolveBody(year=2027, round1=bad))
            check(f"refuses {bad}", False)
        except HTTPException as e:
            check(f"refuses {bad}", e.status_code == 422)

    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nall picks/resolve checks passed")


if __name__ == "__main__":
    main()

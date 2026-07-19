"""Ladder resolution tests — the three real ladders from the worksheet.

    venv/bin/python -m picks_conveyance.tests.test_ladders
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import resolver, curated  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def ladder(id):
    return next(L for L in curated.LADDERS if L["id"] == id)


def test_sas_was():
    store = {"ladders": [ladder("sas_was")]}
    # 2026 SAS lands #5 (top-14, protected) -> stays SAS; rolls to 2027 unprotected -> WAS
    r = resolver.resolve_all(store, {(2026, 1, "SAS"): 5, (2027, 1, "SAS"): 9})
    check("2026 protected -> stays SAS", r.get((2026, 1, "SAS")), "SAS")
    check("2027 unprotected -> WAS", r.get((2027, 1, "SAS")), "WAS")
    # 2026 SAS lands #20 (outside top-14) -> conveys 2026, 2027 never reached
    r2 = resolver.resolve_all(store, {(2026, 1, "SAS"): 20, (2027, 1, "SAS"): 9})
    check("2026 conveys -> WAS", r2.get((2026, 1, "SAS")), "WAS")
    check("2027 not resolved once conveyed", (2027, 1, "SAS") in r2, False)


def test_mem_bkn():
    store = {"ladders": [ladder("mem_bkn")]}
    r = resolver.resolve_all(store, {(2026, 2, "MEM"): 33, (2027, 2, "MEM"): 40})
    check("2026 2nd top-37 protected -> stays MEM", r.get((2026, 2, "MEM")), "MEM")
    check("2027 2nd unprotected -> BKN", r.get((2027, 2, "MEM")), "BKN")


def test_sas_tor_fallback():
    store = {"ladders": [ladder("sas_tor")]}
    # 2029 SAS #6 (top-10, protected) -> never conveys -> fixed-asset fallback to TOR
    r = resolver.resolve_all(store, {(2029, 1, "SAS"): 6,
                                     (2029, 2, "SAS"): 45, (2030, 2, "SAS"): 50})
    check("2029 1st protected -> stays SAS", r.get((2029, 1, "SAS")), "SAS")
    check("fallback 2029 SAS 2nd -> TOR", r.get((2029, 2, "SAS")), "TOR")
    check("fallback 2030 SAS 2nd -> TOR", r.get((2030, 2, "SAS")), "TOR")
    # 2029 SAS #15 (outside top-10) -> conveys the 1st, no fallback
    r2 = resolver.resolve_all(store, {(2029, 1, "SAS"): 15,
                                      (2029, 2, "SAS"): 45, (2030, 2, "SAS"): 50})
    check("2029 1st conveys -> TOR", r2.get((2029, 1, "SAS")), "TOR")
    check("no fallback when 1st conveyed (2029 2nd)", (2029, 2, "SAS") in r2, False)


if __name__ == "__main__":
    for t in (test_sas_was, test_mem_bkn, test_sas_tor_fallback):
        print(t.__name__)
        t()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LADDER TESTS PASS")

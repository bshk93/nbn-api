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


def test_orig_decoupled_from_from():
    """A ladder step can govern a pick whose orig differs from the team the
    ladder's `from`/`to` describe — e.g. team X holds only a share of a pick
    Y originated, and owes team Z a fallback if it stays protected. Mirrors
    the real 2029 OKC/BKN trade this was built for: OKC holds the unprotected
    share of the 2029 DET 1st (orig=DET, not OKC), and if it stays top-5
    protected, BKN is instead owed a 2031 OKC 1st."""
    ladder = {"id": "okc_bkn_test", "from": "OKC", "to": "BKN",
              "steps": [{"year": 2029, "round": 1, "orig": "DET", "protect_top": 5}],
              "fallback": {"type": "fixed_asset",
                           "picks": [{"year": 2031, "round": 1, "orig": "OKC"}]}}
    store = {"ladders": [ladder]}
    # lands #3 (within top-5, protected) -> no separate protected node here,
    # so the ladder is authoritative for the step's own key too
    r = resolver.resolve_all(store, {(2029, 1, "DET"): 3})
    check("no protected node -> ladder decides 2029 DET 1st itself (protected -> OKC)",
          r.get((2029, 1, "DET")), "OKC")
    check("fallback 2031 OKC 1st -> BKN", r.get((2031, 1, "OKC")), "BKN")
    # lands #20 (outside top-5) -> conveys, no fallback
    r2 = resolver.resolve_all(store, {(2029, 1, "DET"): 20})
    check("no protected node -> ladder decides 2029 DET 1st itself (conveys -> BKN)",
          r2.get((2029, 1, "DET")), "BKN")
    check("no fallback when unprotected", (2031, 1, "OKC") in r2, False)


def test_ladder_defers_to_existing_protected_node():
    """When the pick ALSO carries a genuine protected node (e.g. a third
    team's independent claim from an earlier, unrelated trade — DET's real
    top-3 claim on its own 2029 1st, of which OKC only ever held the band
    4-60 share), the ladder must NOT override that node's answer for the
    pick's own key. It should still correctly gate the fallback using the
    real draft position."""
    protected_node = {
        "type": "protected", "id": "p_test",
        "on": {"year": 2029, "round": 1, "orig": "DET"},
        "bands": [{"min": 1, "max": 3, "to": "DET"},
                  {"min": 4, "max": 5, "to": "OKC"},
                  {"min": 6, "max": 60, "to": "BKN"}],
    }
    ladder = {"id": "okc_bkn_test2", "from": "OKC", "to": "BKN",
              "steps": [{"year": 2029, "round": 1, "orig": "DET", "protect_top": 5}],
              "fallback": {"type": "fixed_asset",
                           "picks": [{"year": 2031, "round": 1, "orig": "OKC"}]}}
    store = {
        "picks": [{"year": 2029, "round": 1, "orig": "DET", "conveyance": protected_node}],
        "ladders": [ladder],
    }
    # lands #2 (DET's own top-3 claim, unrelated to this trade) -> stays DET,
    # NOT overridden to "OKC" by the ladder; fallback still fires (protected)
    r = resolver.resolve_all(store, {(2029, 1, "DET"): 2})
    check("DET's genuine top-3 claim is preserved, not overwritten by the ladder",
          r.get((2029, 1, "DET")), "DET")
    check("fallback still fires for DET's protected range", r.get((2031, 1, "OKC")), "BKN")
    # lands #5 (OKC's new sub-protection band) -> stays OKC per the real node;
    # fallback fires (still within protect_top=5)
    r2 = resolver.resolve_all(store, {(2029, 1, "DET"): 5})
    check("OKC's sub-band is honored by the real protected node",
          r2.get((2029, 1, "DET")), "OKC")
    check("fallback fires for OKC's protected sub-band too", r2.get((2031, 1, "OKC")), "BKN")
    # lands #6 (conveys to BKN per the real node) -> no fallback
    r3 = resolver.resolve_all(store, {(2029, 1, "DET"): 6})
    check("unprotected range conveys to BKN per the real node",
          r3.get((2029, 1, "DET")), "BKN")
    check("no fallback once the real pick conveys", (2031, 1, "OKC") in r3, False)


if __name__ == "__main__":
    for t in (test_sas_was, test_mem_bkn, test_sas_tor_fallback,
              test_orig_decoupled_from_from, test_ladder_defers_to_existing_protected_node):
        print(t.__name__)
        t()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL LADDER TESTS PASS")

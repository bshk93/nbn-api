"""Resolver unit tests against the worked examples from the migration worksheet.

Proves the draft-time engine produces correct owners for protections, range
splits, simultaneous swaps, a nested-protection swap slot, and the ordered
binary-swap chain. Synthetic draft positions; asserts exact owner assignments.

    python3 -m picks_conveyance.tests.test_resolver
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from picks_conveyance import model, resolver  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got:  {got}\n        want: {want}")
        FAILS.append(name)


def pref(year, rnd, orig):
    return {"year": year, "round": rnd, "orig": orig}


# 1. protected w/ extinguish-style keeper — row 00 (2027 CHI top-4 -> DEN)
def test_protected():
    node = model.protected("n_chi27", pref(2027, 1, "CHI"),
                           [{"min": 1, "max": 4, "to": "CHI"},
                            {"min": 5, "max": 60, "to": "DEN"}])
    model.validate(node)
    store = {"picks": [{"year": 2027, "round": 1, "orig": "CHI", "conveyance": node}]}
    check("CHI lands #3 -> protected, stays CHI",
          resolver.resolve_all(store, {(2027, 1, "CHI"): 3}).get((2027, 1, "CHI")), "CHI")
    check("CHI lands #10 -> conveys to DEN",
          resolver.resolve_all(store, {(2027, 1, "CHI"): 10}).get((2027, 1, "CHI")), "DEN")


# 2. range split — row 55 (2031 GSW 2nd: 31-55 HOU, 56-60 OKC)
def test_range_split():
    node = model.protected("n_gsw31", pref(2031, 2, "GSW"),
                           [{"min": 31, "max": 55, "to": "HOU"},
                            {"min": 56, "max": 60, "to": "OKC"}])
    model.validate(node)
    store = {"picks": [{"year": 2031, "round": 2, "orig": "GSW", "conveyance": node}]}
    check("GSW 2nd #40 -> HOU",
          resolver.resolve_all(store, {(2031, 2, "GSW"): 40}).get((2031, 2, "GSW")), "HOU")
    check("GSW 2nd #58 -> OKC",
          resolver.resolve_all(store, {(2031, 2, "GSW"): 58}).get((2031, 2, "GSW")), "OKC")


# 3. simultaneous swap — 2028 BOS/CHI, priority [BOS, CHI]
def test_swap_group():
    store = {"swap_groups": {"sg": {
        "members": [pref(2028, 1, "BOS"), pref(2028, 1, "CHI")],
        "priority": ["BOS", "CHI"]}}}
    r = resolver.resolve_all(store, {(2028, 1, "BOS"): 5, (2028, 1, "CHI"): 12})
    check("BOS pick better -> BOS keeps, CHI keeps",
          (r.get((2028, 1, "BOS")), r.get((2028, 1, "CHI"))), ("BOS", "CHI"))
    r2 = resolver.resolve_all(store, {(2028, 1, "BOS"): 12, (2028, 1, "CHI"): 5})
    check("CHI pick better -> BOS takes CHI's pick",
          (r2.get((2028, 1, "CHI")), r2.get((2028, 1, "BOS"))), ("BOS", "CHI"))


# 4. nested protection in a swap slot — rows 38/39 (DAL higher, DET lower, MIN if top-3)
def test_nested_swap_protection():
    lower = model.protected("n_low", "__slot1__",
                            [{"min": 1, "max": 3, "to": "MIN"},
                             {"min": 4, "max": 60, "to": "DET"}])
    store = {"swap_groups": {"sg": {
        "members": [pref(2029, 1, "MIA"), pref(2029, 1, "MIN")],
        "priority": ["DAL", lower]}}}
    # MIA=5 (higher), MIN=8 (lower, not top-3) -> DAL, DET
    r = resolver.resolve_all(store, {(2029, 1, "MIA"): 5, (2029, 1, "MIN"): 8})
    check("higher->DAL, lower(#8)->DET",
          (r.get((2029, 1, "MIA")), r.get((2029, 1, "MIN"))), ("DAL", "DET"))
    # MIA=2 (higher), MIN=3 (lower, top-3) -> DAL, MIN keeps
    r2 = resolver.resolve_all(store, {(2029, 1, "MIA"): 2, (2029, 1, "MIN"): 3})
    check("lower is top-3 -> MIN keeps",
          (r2.get((2029, 1, "MIA")), r2.get((2029, 1, "MIN"))), ("DAL", "MIN"))


# 5. ordered binary-swap chain — 2030 NOP/MIL/POR/DET/HOU (spec 7.5b)
def test_binary_chain():
    bs = {
        "s1": {"type": "binary_swap", "id": "s1",
               "a": pref(2030, 1, "NOP"), "b": pref(2030, 1, "MIL"),
               "better_to": {"ref": "s2", "as": "b"}, "worse_to": "MIL"},
        "s2": {"type": "binary_swap", "id": "s2",
               "a": pref(2030, 1, "POR"), "b": {"ref": "s1", "output": "better"},
               "better_to": "POR", "worse_to": {"ref": "s3", "as": "b"}},
        "s3": {"type": "binary_swap", "id": "s3",
               "a": pref(2030, 1, "DET"), "b": {"ref": "s2", "output": "worse"},
               "better_to": "DET", "worse_to": {"ref": "s4", "as": "a"}},
        "s4": {"type": "binary_swap", "id": "s4",
               "a": {"ref": "s3", "output": "worse"}, "b": pref(2030, 1, "HOU"),
               "better_to": "NOP", "worse_to": "HOU"},
    }
    for s in bs.values():
        model.validate(s)
    store = {"binary_swaps": bs}
    pos = {(2030, 1, "NOP"): 10, (2030, 1, "MIL"): 20, (2030, 1, "POR"): 5,
           (2030, 1, "DET"): 15, (2030, 1, "HOU"): 25}
    r = resolver.resolve_all(store, pos)
    check("MIL keeps worse", r.get((2030, 1, "MIL")), "MIL")
    check("POR takes best (its own #5)", r.get((2030, 1, "POR")), "POR")
    check("NOP-orig pick -> DET", r.get((2030, 1, "NOP")), "DET")
    check("DET-orig pick -> NOP", r.get((2030, 1, "DET")), "NOP")
    check("HOU keeps worst", r.get((2030, 1, "HOU")), "HOU")


# 6. ranked swap group, priority shorter than members -- "best goes to X,
# 2nd goes to Y, nobody said anything about 3rd so it stays home." Built for
# the 2028 SAC/DAL/MIA/PHX/CHA cluster's 3-way (MIA best, SAC 2nd).
def test_ranked_group_fallback_to_own():
    store = {"swap_groups": {"rg": {
        "members": [pref(2028, 1, "SAC"), pref(2028, 1, "DAL"), pref(2028, 1, "PHX")],
        "priority": ["MIA", "SAC"]}}}
    # SAC best, DAL 2nd, PHX worst -> MIA takes SAC's, SAC takes DAL's, PHX keeps its own
    r = resolver.resolve_all(store, {(2028, 1, "SAC"): 3, (2028, 1, "DAL"): 10, (2028, 1, "PHX"): 25})
    check("SAC best (#3) -> MIA", r.get((2028, 1, "SAC")), "MIA")
    check("DAL 2nd (#10) -> SAC", r.get((2028, 1, "DAL")), "SAC")
    check("PHX worst (#25) -> stays PHX (no named claimant)", r.get((2028, 1, "PHX")), "PHX")
    # PHX best, SAC 2nd, DAL worst -> MIA takes PHX's, SAC keeps its own (2nd), DAL keeps its own
    r2 = resolver.resolve_all(store, {(2028, 1, "SAC"): 15, (2028, 1, "DAL"): 30, (2028, 1, "PHX"): 2})
    check("PHX best (#2) -> MIA", r2.get((2028, 1, "PHX")), "MIA")
    check("SAC 2nd (#15) -> stays SAC", r2.get((2028, 1, "SAC")), "SAC")
    check("DAL worst (#30) -> stays DAL (no named claimant)", r2.get((2028, 1, "DAL")), "DAL")


# 7. ranked swap group with a DYNAMIC member -- one of the ranked candidates
# isn't a fixed pick, it's "whichever pick a team ended up with from an
# earlier swap" (an output-ref into a binary_swap chain). Also from the 2028
# cluster: SAC's own vs a MIA-origin pick resolve first (chain), then that
# winner enters the ranked group alongside DAL/PHX.
def test_ranked_group_dynamic_member():
    bs = {
        "pre": {"type": "binary_swap", "id": "pre",
                "a": pref(2028, 1, "SAC"), "b": pref(2028, 1, "MIA"),
                "better_to": "SAC", "worse_to": "CHA"},
    }
    for s in bs.values():
        model.validate(s)
    group = {
        "members": [{"ref": "pre", "output": "better"}, pref(2028, 1, "DAL"), pref(2028, 1, "PHX")],
        "priority": ["MIA", "SAC"],
    }
    model.validate_swap_group(group)
    store = {"binary_swaps": bs, "swap_groups": {"rg": group}}
    # SAC's own (#4) beats MIA-origin (#20) in the pre-swap -> SAC keeps its
    # own for now (dynamic member = SAC's own pick, #4). That (#4) beats
    # DAL (#12) and PHX (#30) -> MIA takes it; DAL keeps 2nd... wait DAL(#12)
    # vs PHX(#30): DAL is 2nd overall, PHX worst.
    # Note: the ranked group's own assignment for the dynamic member's
    # resolved key runs AFTER the chain pass and correctly overwrites
    # whatever the chain alone produced for it (Pass 3 after Pass 2) — so
    # only the FINAL merged value is checked below, not an intermediate
    # chain-only snapshot (resolve_all doesn't expose one, by design).
    r = resolver.resolve_all(store, {(2028, 1, "SAC"): 4, (2028, 1, "MIA"): 20,
                                     (2028, 1, "DAL"): 12, (2028, 1, "PHX"): 30})
    check("pre-swap: MIA-origin worse -> CHA", r.get((2028, 1, "MIA")), "CHA")
    check("dynamic member (SAC's own, #4) best overall -> MIA", r.get((2028, 1, "SAC")), "MIA")
    check("DAL 2nd (#12) -> SAC", r.get((2028, 1, "DAL")), "SAC")
    check("PHX worst (#30) -> stays PHX", r.get((2028, 1, "PHX")), "PHX")

    # Different branch: MIA-origin (#2) beats SAC's own (#25) in the pre-swap
    # -> the dynamic member becomes the MIA-origin pick (now owned by SAC).
    # That (#2) is also better than DAL/PHX -> MIA takes the MIA-origin pick.
    r2 = resolver.resolve_all(store, {(2028, 1, "SAC"): 25, (2028, 1, "MIA"): 2,
                                      (2028, 1, "DAL"): 12, (2028, 1, "PHX"): 30})
    check("pre-swap: SAC's own worse -> CHA", r2.get((2028, 1, "SAC")), "CHA")
    check("dynamic member (MIA-origin, #2) best overall -> MIA", r2.get((2028, 1, "MIA")), "MIA")
    check("DAL 2nd (#12) -> SAC", r2.get((2028, 1, "DAL")), "SAC")
    check("PHX worst (#30) -> stays PHX", r2.get((2028, 1, "PHX")), "PHX")


if __name__ == "__main__":
    for t in (test_protected, test_range_split, test_swap_group,
              test_nested_swap_protection, test_binary_chain,
              test_ranked_group_fallback_to_own, test_ranked_group_dynamic_member):
        print(t.__name__)
        t()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        raise SystemExit(1)
    print("ALL RESOLVER TESTS PASS")

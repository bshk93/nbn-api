"""Curated conveyance nodes — the machine-readable form of the migration
worksheet (docs/picks-migration-worksheet.md). Every entry here was confirmed
during the 2026-07-19 walkthrough.

This turns the reconciliation from prose into executable, validated data: the
resolver runs these, the projection displays them. Applying `apply_curated` to a
seeded (all-settled) store overrides the relevant picks with their real
conveyance and populates the shared swap-group / binary-chain / ladder tables.

Nothing here is wired into a live endpoint yet.
"""
from __future__ import annotations


def pk(year: int, rnd: int, orig: str) -> dict:
    return {"year": year, "round": rnd, "orig": orig}


# --- protected picks (pickkey -> bands) ------------------------------------
# 2-band single-threshold protections + the range-split 2nds.
PROTECTED = {
    (2027, 1, "CHI"): {"on": pk(2027, 1, "CHI"),
                       "bands": [(1, 4, "CHI"), (5, 60, "DEN")]},   # row 00, top-4
    (2027, 2, "IND"): {"on": pk(2027, 2, "IND"),
                       "bands": [(31, 55, "NYK"), (56, 60, "MIA")]},  # row 13, keeper NYK
    (2029, 1, "DET"): {"on": pk(2029, 1, "DET"),
                       "bands": [(1, 3, "DET"), (4, 60, "OKC")]},   # flat protected=3
    (2027, 2, "GSW"): {"on": pk(2027, 2, "GSW"),
                       "bands": [(31, 55, "UTA"), (56, 60, "LAC")]},
    (2027, 2, "LAC"): {"on": pk(2027, 2, "LAC"),
                       "bands": [(31, 38, "LAC"), (39, 60, "NYK")]},
    (2028, 2, "IND"): {"on": pk(2028, 2, "IND"),
                       "bands": [(31, 44, "TOR"), (45, 60, "POR")]},
}


# --- swap groups (gid -> {members, priority}) ------------------------------
SWAP_GROUPS = {
    "sg_bos_chi_28_1": {"members": [pk(2028, 1, "BOS"), pk(2028, 1, "CHI")],
                        "priority": ["BOS", "CHI"]},
    "sg_okc_den_28_1": {"members": [pk(2028, 1, "OKC"), pk(2028, 1, "DEN")],
                        "priority": ["BOS", "OKC"]},
    "sg_por_den_29_1": {"members": [pk(2029, 1, "POR"), pk(2029, 1, "DEN")],
                        "priority": ["DEN", "POR"]},
    "sg_gsw_phi_29_1": {"members": [pk(2029, 1, "GSW"), pk(2029, 1, "PHI")],
                        "priority": ["GSW", "PHI"]},
    "sg_min_lal_30_1": {"members": [pk(2030, 1, "MIN"), pk(2030, 1, "LAL")],
                        "priority": ["MIN", "LAL"]},
    "sg_nop_cha_31_1": {"members": [pk(2031, 1, "NOP"), pk(2031, 1, "CHA")],
                        "priority": ["NOP", "CHA"]},
    "sg_uta_dal_32_1": {"members": [pk(2032, 1, "UTA"), pk(2032, 1, "DAL")],
                        "priority": ["UTA", "MIA"]},   # DAL-orig owned by MIA
    "sg_min_lal_32_1": {"members": [pk(2032, 1, "MIN"), pk(2032, 1, "LAL")],
                        "priority": ["MIN", "LAL"]},
    "sg_cha_bkn_28_2": {"members": [pk(2028, 2, "CHA"), pk(2028, 2, "BKN")],
                        "priority": ["CHA", "CHI"]},
    "sg_bos_tor_28_2": {"members": [pk(2028, 2, "BOS"), pk(2028, 2, "TOR")],
                        "priority": ["DAL", "TOR"]},   # was MIA; superseded by
                        # Trade 35 (2026-07-15) — DAL received the "2028
                        # BOS/TOR 2nd" swap right from MIA. Caught stale via
                        # the shadow ownership check (2026-07-19).
    "sg_ind_hou_32_2": {"members": [pk(2032, 2, "IND"), pk(2032, 2, "HOU")],
                        "priority": ["IND", "CLE"]},
    "sg_sas_okc_27_2": {"members": [pk(2027, 2, "SAS"), pk(2027, 2, "OKC")],
                        "priority": ["BOS", "NOP"]},   # BOS better, NOP worse (Trade 73)
    "sg_uta_chi_30_1": {"members": [pk(2030, 1, "UTA"), pk(2030, 1, "CHI")],
                        "priority": ["UTA", "CHI"]},
    "sg_tor_dal_30_1": {"members": [pk(2030, 1, "TOR"), pk(2030, 1, "DAL")],
                        "priority": ["TOR", "MIA"]},   # DAL-orig owned by MIA
    "sg_was_bos_26_1": {"members": [pk(2026, 1, "WAS"), pk(2026, 1, "BOS")],
                        "priority": ["WAS", "BOS"]},   # WAS swap right (sheet WAS F77)
    "sg_nop_hou_32_1": {"members": [pk(2032, 1, "NOP"), pk(2032, 1, "HOU")],
                        "priority": ["NOP", "HOU"]},   # sheet HOU F87
    # Resolved out of LEGACY 2026-07-19: Trade 54 (2026-02-03) gave IND the
    # 2031 MIN 1st; Trade 73 (2026-02-06, 3 days later) granted HOU swap
    # rights over "2031 MIN/HOU 1st" -- HOU takes the better of its own pick
    # vs. the MIN pick IND now holds, IND keeps whichever is worse. The old
    # LEGACY note's "protected (meaning/origin unclear)" fragment on HOU's own
    # pick doesn't appear in either trade's text -- not carried forward here;
    # flag if real evidence for it ever turns up.
    "sg_hou_min_31_1": {"members": [pk(2031, 1, "HOU"), pk(2031, 1, "MIN")],
                        "priority": ["HOU", "IND"],
                        "txn_ids": [{"id": "5c8d8e94e26d940a", "date": "2026-02-06"},   # Trade 73
                                   {"id": "c65f1a6c7036afd3", "date": "2026-02-03"}]},  # Trade 54
}


# --- binary-swap chains (chain id -> ordered list of binary_swap nodes) -----
# `txn_ids`: the real transaction(s) (id + date) in transactions.json that
# created this specific swap step, surfaced to `GET /api/picks` via
# ownership.list_leaves so the site can show "why does this exist" instead of
# just the resulting structure. Left as [] where no backing transaction could
# be found (pre-dates the tracked ledger, or the Discord backfill hasn't
# reached it yet) — don't guess a citation, an empty list is the honest state.
def _bs(id, a, b, better_to, worse_to, txn_ids=None):
    return {"type": "binary_swap", "id": id, "a": a, "b": b,
            "better_to": better_to, "worse_to": worse_to,
            "txn_ids": txn_ids or []}


BINARY_CHAINS = {
    # 2030 NOP/MIL/POR/DET/HOU (spec 7.5b)
    "nmpdh": [
        _bs("nmpdh_s1", pk(2030, 1, "NOP"), pk(2030, 1, "MIL"),
            {"ref": "nmpdh_s2", "as": "b"}, "MIL",
            txn_ids=[{"id": "52c52018e46f8bb4", "date": "2024-06-22"}]),  # Trade 11
        _bs("nmpdh_s2", pk(2030, 1, "POR"), {"ref": "nmpdh_s1", "output": "better"},
            "POR", {"ref": "nmpdh_s3", "as": "b"},
            txn_ids=[{"id": "280699c56ef58b63", "date": "2025-07-30"}]),  # Trade 29
        _bs("nmpdh_s3", pk(2030, 1, "DET"), {"ref": "nmpdh_s2", "output": "worse"},
            "DET", {"ref": "nmpdh_s4", "as": "a"},
            txn_ids=[{"id": "23eb5d48412327c5", "date": "2026-01-21"}]),  # Trade 46
        _bs("nmpdh_s4", {"ref": "nmpdh_s3", "output": "worse"}, pk(2030, 1, "HOU"),
            "NOP", "HOU"),  # no transactions.json record found for the NOP/HOU leg
    ],
    # 2028 ORL/MIL/WAS 3-way (spec 7.5c)
    "omw": [
        _bs("omw_s1", pk(2028, 1, "MIL"), pk(2028, 1, "ORL"),
            {"ref": "omw_s2", "as": "a"}, "MIL"),  # no transactions.json record found for the MIL/ORL leg
        _bs("omw_s2", {"ref": "omw_s1", "output": "better"}, pk(2028, 1, "WAS"),
            "ORL", "WAS",
            txn_ids=[{"id": "c92f582f69144473", "date": "2021-08-02"}]),  # Trade 23
    ],
    # 2028 SAS/NOP then DET tail (block A compound)
    "snd": [
        _bs("snd_s1", pk(2028, 1, "SAS"), pk(2028, 1, "NOP"),
            {"ref": "snd_s2", "as": "b"}, "NOP",
            txn_ids=[{"id": "367133dabab00c59", "date": "2022-02-10"}]),  # Trade 71
        _bs("snd_s2", pk(2028, 1, "DET"), {"ref": "snd_s1", "output": "better"},
            "DET", "SAS",
            txn_ids=[{"id": "23eb5d48412327c5", "date": "2026-01-21"}]),  # Trade 46
    ],
    # 2029 NOP: two separate NOP-favor swap rights on NOP's own 2029 1st --
    # vs BOS (Trade 30, 2026-07-10) and vs HOU (Trade 34, 2026-07-14). Resolves
    # as NOP takes best of the three; whoever NOP took from gets NOP's pick.
    "nop29": [
        _bs("nop29_s1", pk(2029, 1, "NOP"), pk(2029, 1, "BOS"),
            {"ref": "nop29_s2", "as": "a"}, "BOS",
            txn_ids=[{"id": "3013f6472e569110", "date": "2026-07-10"}]),  # Trade 30
        _bs("nop29_s2", {"ref": "nop29_s1", "output": "better"}, pk(2029, 1, "HOU"),
            "NOP", "HOU",
            txn_ids=[{"id": "682ab5647c51e84f", "date": "2026-07-14"}]),  # Trade 34
    ],
    # Resolved out of LEGACY 2026-07-19: 2027 PHI/CHA/TOR/DAL chained swap.
    # "Charlotte has the right to swap Philadelphia's '27 1st for Toronto's
    # '27 1st; afterwards, Toronto has the right to swap the less favorable
    # pick for '27 Dallas 1st." Trade 18 (2025-07-01) sent PHI's own pick
    # (already carrying TOR swap rights) to NOP; Trade 27 (2025-07-21) sent it
    # onward from NOP to CHA, where it sits today. No transactions.json record
    # found for the TOR/DAL leg itself (or for the original grant of TOR's
    # swap right over PHI's pick, predating both trades above) -- the
    # structure matches the note's prose exactly regardless.
    "pctd": [
        _bs("pctd_s1", pk(2027, 1, "PHI"), pk(2027, 1, "TOR"),
            "CHA", {"ref": "pctd_s2", "as": "a"},
            txn_ids=[{"id": "cf9ae5b6831e3e36", "date": "2025-07-01"},    # Trade 18
                     {"id": "520fbf101c33f24f", "date": "2025-07-21"}]),  # Trade 27
        _bs("pctd_s2", {"ref": "pctd_s1", "output": "worse"}, pk(2027, 1, "DAL"),
            "TOR", "DAL"),  # no transactions.json record found for the TOR/DAL leg
    ],
}
# pickkeys whose owner each chain decides (for display markers)
CHAIN_MEMBERS = {
    "nmpdh": [(2030, 1, t) for t in ("NOP", "MIL", "POR", "DET", "HOU")],
    "omw":   [(2028, 1, t) for t in ("MIL", "ORL", "WAS")],
    "snd":   [(2028, 1, t) for t in ("SAS", "NOP", "DET")],
    "nop29": [(2029, 1, t) for t in ("NOP", "BOS", "HOU")],
    "pctd":  [(2027, 1, t) for t in ("PHI", "TOR", "DAL")],
}


# --- ladders (multi-year chained protections) ------------------------------
LADDERS = [
    {"id": "sas_was", "from": "SAS", "to": "WAS",
     "steps": [{"year": 2026, "round": 1, "protect_top": 14},
               {"year": 2027, "round": 1, "protect_top": 0}],
     "fallback": None},
    {"id": "mem_bkn", "from": "MEM", "to": "BKN",
     "steps": [{"year": 2026, "round": 2, "protect_top": 37},
               {"year": 2027, "round": 2, "protect_top": 0}],
     "fallback": None},
    {"id": "sas_tor", "from": "SAS", "to": "TOR",
     "steps": [{"year": 2029, "round": 1, "protect_top": 10}],
     "fallback": {"type": "fixed_asset",
                  "picks": [pk(2029, 2, "SAS"), pk(2030, 2, "SAS")]}},
]


# --- legacy (unmodelable; resolver skips, prose kept) ----------------------
# 2031 HOU/MIN and 2027 PHI/CHA/TOR/DAL resolved out 2026-07-19 (real
# transactions found, see SWAP_GROUPS/BINARY_CHAINS above). 2027 DET cascade
# and 2028 SAC/DAL/MIA/MEM/NYK/CHA cluster remain here deliberately: the
# former has a real unresolved ambiguity (does DET's swap-right-vs-GSW still
# apply if the HOU/2026-TOR-1st conditional reroutes the underlying asset to
# HOU instead of DET?) and the latter's only found transaction record
# (8685c0dfc2717083) captured a plain 2-team SAC/MIA trade, not the 3-way
# "MIA gets best of SAC/DAL/PHX" swap its own description claims -- don't
# guess at either without more digging or committee confirmation.
LEGACY = {
    (2028, 1, "MIA"): "2028 SAC/DAL/MIA/MEM/NYK/CHA sequential swap-of-swaps cluster",
    (2027, 1, "DET"): "2027 DET 5-team cascade (PHX/OKC/LAC/DET/HOU, then DET<->GSW)",
    (2027, 1, "GSW"): "part of 2027 DET cascade (see 2027 DET 1st)",
    (2027, 1, "OKC"): "part of 2027 DET cascade",
    (2027, 1, "LAC"): "part of 2027 DET cascade",
    (2028, 1, "CHA"): "part of 2028 SAC/DAL/MIA/MEM/NYK/CHA cluster (NYK swaps into CHA)",
}


def _tup_bands(bands):
    return [{"min": lo, "max": hi, "to": to} for (lo, hi, to) in bands]


def apply_curated(store: dict) -> dict:
    """Merge curated nodes into a seeded store (mutates and returns it)."""
    store.setdefault("swap_groups", {})
    store.setdefault("binary_swaps", {})
    store.setdefault("chains", {})
    store.setdefault("ladders", [])
    by_key = {(p["year"], p["round"], p["orig"]): p for p in store["picks"]}

    def set_node(k, node):
        p = by_key.get(k)
        if p is None:
            raise KeyError(f"curated pick {k} not present in store")
        if p.get("player"):
            # Already drafted: the flat CSV's OWNER is now a settled historical
            # fact, not a pending contingency (_apply_pick stamps OWNER=team
            # directly when a pick is used). The base seed already captured
            # that as settled(OWNER) — trust it over this static curated
            # snapshot, which predates the draft and doesn't know it resolved.
            return
        p["conveyance"] = node
        p.pop("needs_structure", None)
        p.pop("_flat", None)

    for k, spec in PROTECTED.items():
        set_node(k, {"type": "protected", "id": f"p_{k[0]}_{k[1]}_{k[2]}",
                     "on": spec["on"], "bands": _tup_bands(spec["bands"])})

    for gid, g in SWAP_GROUPS.items():
        store["swap_groups"][gid] = g
        for m in g["members"]:
            set_node((m["year"], m["round"], m["orig"]),
                     {"type": "swap", "id": f"s_{gid}", "group": gid})

    for cid, nodes in BINARY_CHAINS.items():
        store["chains"][cid] = [n["id"] for n in nodes]
        for n in nodes:
            store["binary_swaps"][n["id"]] = n
        for k in CHAIN_MEMBERS[cid]:
            set_node(k, {"type": "binary", "chain": cid})

    store["ladders"].extend(LADDERS)

    for k, reason in LEGACY.items():
        prev = by_key.get(k, {}).get("conveyance", {})
        owner = prev.get("team")   # the seeded settled owner, kept for display
        set_node(k, {"type": "legacy", "reason": reason, "owner": owner})

    store["meta"] = store.get("meta", {})
    store["meta"]["curated"] = {
        "protected": len(PROTECTED), "swap_groups": len(SWAP_GROUPS),
        "binary_chains": len(BINARY_CHAINS), "ladders": len(LADDERS),
        "legacy": len(LEGACY),
    }
    return store

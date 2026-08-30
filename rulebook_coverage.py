"""Which rulebook sections the transaction validators actually enforce.

The rulebook (`nbn-today/rulebook/index.html`) badges each section 🔒
system-enforced or 👁 manual review. Those badges were curated by hand against
code that moves underneath them, and they went stale repeatedly — § 7.2 read
👁-only for two and a half weeks after Stepien went live, § 3.12's story
changed twice in one day, and by 2026-08-30 ten sections were enforced while
still reading manual-only. This module is the manifest the badges are rendered
from instead, and `nbn-today/build/check_rulebook_badges.py` is what fails when
the page disagrees with it.

The 🔒 half is computed, not declared: `extract()` parses
`routers/transactions.py` with `ast` and finds every `CheckResult(...)` the
`_VALIDATORS` table can reach, so a section is enforced only if real code
emits a check for it. The declared half below is the part that cannot be
computed:

  CHECK_SECTIONS      which § each check id is enforcing. A check's *message*
                      often cites one, but 51 of 134 emit sites cite nothing,
                      and some cite a neighbouring section rather than their
                      own (`raise_limit` says "§ 3.12 minimum scale" while
                      enforcing § 3.9). So the mapping is declared, and
                      `tests/test_rulebook_coverage.py` asserts it names
                      exactly the ids the AST pass finds — a new check cannot
                      be added without declaring what it enforces.

  SECTION_ENFORCED_BY enforcement that is real but emits no check, because the
                      system simply does the thing (§ 5.2 dead cap is computed
                      at release, never validated). Without this the badge
                      would have to lie one way or the other.

  SECTION_REVIEW      why a section still needs a human, i.e. the 👁 badge.
                      "Partially enforced" is a judgement about the gap between
                      a rule and the check covering it; nothing in the code can
                      derive it. A section carrying both badges is the normal
                      case, not a contradiction.

    python3 -m rulebook_coverage            # human-readable table
    python3 -m rulebook_coverage --json     # the manifest
"""
from __future__ import annotations

import ast
import collections
import json
import re
import sys
from pathlib import Path

_ROUTERS = Path(__file__).resolve().parent / "routers"

# Every module that emits a CheckResult. `transactions.py` is the bulk of it and
# the only one with a `_VALIDATORS` table to walk; `waivers.py` carries the one
# § 1.5 claim restriction, and scanning it is what stops that check from being
# invisible here purely because it lives next door.
SOURCES = [_ROUTERS / "transactions.py", _ROUTERS / "waivers.py"]
TRANSACTIONS_PY = SOURCES[0]

_SECTION_RE = re.compile(r"§\s*([0-9]+\.[0-9]+[a-z]?)")


# ── Declared: check id → the rulebook sections it enforces ────────────────────
# Ids ending in `{}` are f-string families (`hard_cap_{team}` → `hard_cap_atl`).
CHECK_SECTIONS: dict[str, tuple[str, ...]] = {
    "apron1_contagion_{}":        ("1.4", "4.2", "4.3"),
    "apron2_aggregation_{}":      ("1.4", "4.4"),
    "bird_rights_forfeited":      ("3.8", "3.10"),
    "bird_rights_tenure":         ("3.8",),
    "buyout_signing_{}":          ("1.5",),
    "byc_{}":                     ("4.2",),
    "contract_has_salary_years":  ("3.13",),
    "draft_rights":               ("7.1",),
    "empty_roster_charge_{}":     ("2.1a",),
    "extension_cap_position":     ("6.2",),
    "extension_eligibility":      ("6.2",),
    "extension_max_year1":        ("6.2",),
    "extension_min_length":       ("6.2",),
    "extension_not_minimum":      ("3.12", "6.2"),
    "extension_raises":           ("3.9", "6.2"),
    "extension_service":          ("6.2",),
    "extension_start_season":     ("6.2",),
    "extension_team_match":       ("6.2",),
    "extension_trade_freeze_{}":  ("4.5", "6.2"),
    "extension_window":           ("6.3",),
    "hard_cap_league_{}":         ("1.3",),
    "hard_cap_{}":                ("1.3", "1.4"),
    "max_salary":                 ("3.11",),
    "min_contract_exempt_{}":     ("3.12", "4.2"),
    "minimum_contract_cap_hit":   ("3.12",),
    "minimum_salary":             ("3.12",),
    "offer_sheet_already_open":   ("3.15",),
    "offer_sheet_decision":       ("3.15",),
    "offer_sheet_own_player":     ("3.15",),
    "offer_sheet_resolvable":     ("3.15",),
    "offer_sheet_rfa":            ("3.15",),
    "pick_advance_limit":         ("7.2",),
    "pick_advance_limit_{}_{}":   ("7.2",),
    # cites § 3.12 only to name the minimum-scale exemption; the rule is § 3.9,
    # and § 3.13 is where the 8% Full Bird ceiling lives.
    "raise_limit":                ("3.9", "3.13"),
    "renounce_eligible":          ("3.10",),
    "rescind_cap_restriction":    ("3.10",),
    "rookie_scale":               ("7.1",),
    "roster_minimum":             ("2.1", "2.1a"),
    "roster_size":                ("2.1",),
    "roster_size_{}":             ("2.1",),
    # one check name covers all four absorption routes, so it is the only
    # enforcement § 4.2a and the BAE's trade half have.
    "salary_matching_{}":         ("3.5", "4.1a", "4.2", "4.2a", "4.3"),
    "sat_bird_rights_{}":         ("3.14",),
    "sat_contract_length_{}":     ("3.14",),
    "sat_no_mle_{}":              ("3.14",),
    "sat_receiving_apron_{}":     ("4.3",),
    "sat_tmle_exclusion_{}":      ("3.14",),
    "second_round_scale":         ("7.1",),
    "signing_eligibility":        ("3.1",),
    "signing_method_declared_{}": ("3.1", "3.6"),
    # `_check_exception_absorption` resolves the MLE bucket here, which is what
    # makes NTMLE/TMLE availability at each apron a real check.
    "signing_method_{}":          ("1.6", "3.2", "3.3", "3.4", "3.6"),
    "stepien_rule_{}":            ("7.2",),
    "trade_min_legs":             ("4.1",),
    "two_way_slots":              ("2.2",),
    # routers/waivers.py — § 1.5.2's buyout restriction, extended to claims.
    "waiver_claim_apron_{}":      ("1.5", "5.1"),
}


# ── Declared: enforcement that emits no CheckResult ───────────────────────────
# A section here is 🔒 because the system performs the rule rather than
# validating it — there is nothing for a check to reject.
SECTION_ENFORCED_BY: dict[str, str] = {
    "5.2": "dead cap is computed at release by `_dead_cap_from_schedule` / "
           "`_apply_release`, never entered by hand, so there is no submission "
           "to reject",
}


# ── Declared: why a section still needs a human (the 👁 badge) ────────────────
SECTION_REVIEW: dict[str, str] = {
    "1.2":  "no check blocks a team for being over the soft cap; the position "
            "is reported and read by a human",
    "1.3":  "the ceiling is enforced on every write, but the grace period is not "
            "modeled — an over-the-cap team inside it is cleared by hand",
    "1.5":  "only the buyout-signing restriction is checked; the rest of the "
            "standing restrictions are read off the team page",
    "1.6":  "only TMLE availability is checked; the aggregation and cash "
            "restrictions are read by hand",
    "3.1":  "eligibility and the declared method are checked, but the method "
            "itself is self-declared and never verified against tenure",
    "3.6":  "availability is checked when the Room Exception is the declared "
            "method; whether the team actually used room to get there is not",
    "3.7":  "no DPE exception type exists in the system at all",
    "3.8":  "Bird tenure is self-declared on the submission; the ledger scan "
            "backing it still has gaps",
    "3.10": "renounce and rescind are checked; the hold amounts themselves are "
            "priced from the fact sheet and reviewed",
    "3.11": "the max is checked against the scale, but the 25/30/35% tier a "
            "player qualifies for is not derived from service time",
    "3.12": "the scale is enforced; re-pricing an existing minimum deal when "
            "the scale moves is manual",
    "3.13": "salary years and the raise ceiling are checked; option and "
            "guarantee structure is reviewed",
    "3.14": "the four sign-and-trade restrictions are checked; the trade half "
            "still goes through committee review",
    "3.15": "the offer sheet and the match decision are checked; the funding "
            "the match consumes is not linked back to the holds",
    "4.5":  "only the extension trade freeze is checked; the rest of § 4.5 is "
            "read by a human",
    "4.6":  "the Touch Rule is not modeled — multi-team trades are reviewed",
    "5.1":  "the apron restriction on a claim is checked; release legality "
            "itself is not, and the waiver window is run by hand",
    "6.1":  "options are applied as submitted, with no eligibility check",
    "6.2":  "the eight extension rules are checked, but eligibility rests on an "
            "acquisition record 116 rostered players are still missing",
    "6.3":  "the window is checked; the approval process around it is human",
    "7.1":  "the rookie scale and draft rights are checked; the rest of the "
            "draft format is run by hand",
    "7.3":  "the second-apron pick freeze is not computed",
    "7.4":  "international rights are tracked by hand",
}


# ── The AST pass ──────────────────────────────────────────────────────────────

def _strings_in(node: ast.AST) -> str:
    """Every string constant under `node`, joined — enough to read the § out of
    an f-string message without evaluating it."""
    return " ".join(n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _family(node: ast.AST | None) -> str | None:
    """A check id as written: a plain literal, or an f-string with each
    interpolation collapsed to `{}` (`f"hard_cap_{team}"` → `hard_cap_{}`)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}"
                       for p in node.values)
    return None


def _splatted_check(call: ast.Call) -> set[str]:
    """`CheckResult(**{**r.model_dump(), "check": "extension_raises"})` — the id
    is a literal inside the splatted dict."""
    out = set()
    for kw in call.keywords:
        if kw.arg is not None:
            continue
        for node in ast.walk(kw.value):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "check" and _family(v):
                    out.add(_family(v))
    return out


def extract(sources: list[Path] | None = None) -> dict:
    """Every check id these modules emit, which sections their messages cite,
    and which transaction types can reach each one through `_VALIDATORS`."""
    cited: dict[str, set[str]] = collections.defaultdict(set)   # id -> sections
    origin: dict[str, set[str]] = collections.defaultdict(set)  # id -> module
    by_type: dict[str, set[str]] = collections.defaultdict(set)
    unresolved: list[str] = []
    validators: dict[str, str] = {}

    for source in (sources if sources is not None else SOURCES):
        _scan(source, cited, origin, by_type, validators, unresolved)

    return {
        "checks": {cid: sorted(cited[cid]) for cid in sorted(cited)},
        "sources": {cid: sorted(origin[cid]) for cid in sorted(origin)},
        "validators": validators,
        "by_type": {t: sorted(ids) for t, ids in sorted(by_type.items())},
        "unresolved": unresolved,
    }


def _scan(source, cited, origin, by_type, validators, unresolved) -> None:
    tree = ast.parse(source.read_text())
    module = f"{source.parent.name}/{source.name}"

    emits: dict[str, set[str]] = collections.defaultdict(set)   # func -> ids
    calls: dict[str, set[str]] = collections.defaultdict(set)   # func -> callees

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        # `check=check_name` where check_name was assigned a literal (or one
        # branch of a conditional) earlier in this same function.
        env: dict[str, set[str]] = collections.defaultdict(set)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            values = ([node.value.body, node.value.orelse]
                      if isinstance(node.value, ast.IfExp) else [node.value])
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env[target.id].update(f for f in map(_family, values) if f)

        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "CheckResult":
                calls[fn.name].add(node.func.id)
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            arg = kwargs.get("check") or (node.args[0] if node.args else None)
            if _family(arg):
                ids = {_family(arg)}
            elif isinstance(arg, ast.Name):
                ids = set(env.get(arg.id, ()))
            else:
                ids = _splatted_check(node)
            if not ids:
                unresolved.append(f"{source.name}:{node.lineno} in {fn.name}()")
                continue
            sections = set(_SECTION_RE.findall(_strings_in(kwargs["message"])
                                               if "message" in kwargs else ""))
            for cid in ids:
                emits[fn.name].add(cid)
                cited[cid] |= sections
                origin[cid].add(module)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_VALIDATORS" for t in node.targets):
            for k, v in zip(node.value.keys, node.value.values):
                validators[k.value] = v.id

    known = set(calls) | set(emits)

    def reachable(name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in known:
            return set()
        seen.add(name)
        found = set(emits.get(name, ()))
        for callee in calls.get(name, ()):
            found |= reachable(callee, seen)
        return found

    for txn_type, fn_name in validators.items():
        by_type[txn_type] |= reachable(fn_name, set())


# ── The manifest ──────────────────────────────────────────────────────────────

def manifest(sources: list[Path] | None = None) -> dict:
    """`{section: {enforced, manual, checks, types, ...}}` — what the rulebook
    badges are rendered from."""
    found = extract(sources)
    sections: dict[str, dict] = {}

    def slot(sec: str) -> dict:
        return sections.setdefault(sec, {
            "enforced": False, "manual": False, "checks": [], "types": [],
        })

    for cid in found["checks"]:
        for sec in CHECK_SECTIONS.get(cid, ()):
            entry = slot(sec)
            entry["enforced"] = True
            entry["checks"].append(cid)
            entry["types"] = sorted(set(entry["types"]) | {
                t for t, ids in found["by_type"].items() if cid in ids})

    for sec, how in SECTION_ENFORCED_BY.items():
        entry = slot(sec)
        entry["enforced"] = True
        entry["enforced_by"] = how

    for sec, why in SECTION_REVIEW.items():
        entry = slot(sec)
        entry["manual"] = True
        entry["review_note"] = why

    for entry in sections.values():
        entry["checks"].sort()

    return {
        "sources": sorted({m for mods in found["sources"].values() for m in mods}),
        "sections": dict(sorted(sections.items(), key=_section_key)),
        "unresolved": found["unresolved"],
        "stub_types": sorted(t for t, ids in found["by_type"].items() if not ids),
    }


def _section_key(item) -> tuple:
    sec = item[0] if isinstance(item, tuple) else item
    major, minor = sec.split(".", 1)
    return int(major), int(re.match(r"\d+", minor).group()), minor


def badges(section: str, man: dict | None = None) -> tuple[bool, bool]:
    """(enforced, manual) for one section — the two badges, and nothing else."""
    entry = (man or manifest())["sections"].get(section, {})
    return bool(entry.get("enforced")), bool(entry.get("manual"))


def _main(argv: list[str]) -> int:
    man = manifest()
    if "--json" in argv:
        print(json.dumps(man, indent=2))
        return 0
    print(f"{'§':7s} {'badge':7s} checks")
    for sec, entry in man["sections"].items():
        badge = ("🔒" if entry["enforced"] else "  ") + ("👁" if entry["manual"] else "")
        detail = ", ".join(entry["checks"]) or entry.get("enforced_by", "—")
        print(f"{sec:7s} {badge:7s} {detail}")
    if man["stub_types"]:
        print("\nvalidators emitting no checks:", ", ".join(man["stub_types"]))
    if man["unresolved"]:
        print("\nUNRESOLVED check ids:", "; ".join(man["unresolved"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

"""Pins the rulebook coverage manifest to the code it claims to describe.

`rulebook_coverage.CHECK_SECTIONS` says which § each transaction check
enforces, and the rulebook's 🔒/👁 badges are rendered from it. The map is
declared by hand — it has to be, since half the check messages cite no section
and some cite a neighbour — so the thing that keeps it honest is this test:
the declared ids must be *exactly* the ids the AST pass finds in
`routers/transactions.py`. Add a check without declaring its section and this
fails; delete one and it fails the other way.

    venv/bin/python -m tests.test_rulebook_coverage
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rulebook_coverage as rc  # noqa: E402

# The site repo sits beside this one. Overridable so a checkout elsewhere can
# still run the suite.
RULEBOOK = Path(os.environ.get(
    "NBN_RULEBOOK_HTML",
    Path(__file__).resolve().parents[2] / "nbn-today" / "rulebook" / "index.html",
))

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


FOUND = rc.extract()
MANIFEST = rc.manifest()


def rulebook_sections() -> set[str]:
    html = RULEBOOK.read_text()
    return set(re.findall(r'<span class="sec-num">§\s*([0-9.a-z]+)</span>', html))


def test_every_check_id_resolves():
    print("\n-- every CheckResult site yields a readable check id --")
    check("no unresolved emit sites", not FOUND["unresolved"])
    if FOUND["unresolved"]:
        print("      " + "\n      ".join(FOUND["unresolved"]))


def test_declared_map_matches_the_code():
    print("\n-- CHECK_SECTIONS names exactly the ids the code emits --")
    found = set(FOUND["checks"])
    declared = set(rc.CHECK_SECTIONS)
    missing = sorted(found - declared)
    extra = sorted(declared - found)
    check(f"no check emitted but undeclared ({len(missing)})", not missing)
    if missing:
        print("      add to CHECK_SECTIONS: " + ", ".join(missing))
    check(f"no check declared but no longer emitted ({len(extra)})", not extra)
    if extra:
        print("      drop from CHECK_SECTIONS: " + ", ".join(extra))


def test_every_check_enforces_something():
    print("\n-- no check is declared against an empty section list --")
    empty = sorted(cid for cid, secs in rc.CHECK_SECTIONS.items() if not secs)
    check("every declared check names a §", not empty)
    if empty:
        print("      " + ", ".join(empty))


def test_declared_sections_exist_in_the_rulebook():
    print("\n-- every § named anywhere in the manifest is a real section --")
    if not RULEBOOK.exists():
        print(f"  [skip] no rulebook at {RULEBOOK}")
        return
    real = rulebook_sections()
    named = set(MANIFEST["sections"])
    unknown = sorted(named - real)
    check(f"no manifest § missing from the rulebook ({len(unknown)})", not unknown)
    if unknown:
        print("      " + ", ".join(unknown))


def test_notes_are_not_blank():
    print("\n-- the declared halves carry their reason --")
    check("every review note is prose",
          all(len(v) > 20 for v in rc.SECTION_REVIEW.values()))
    check("every non-check enforcement says how",
          all(len(v) > 20 for v in rc.SECTION_ENFORCED_BY.values()))


def test_stub_validators_stay_known():
    print("\n-- a validator silently losing all its checks is caught --")
    # Documented in nbn-today/CLAUDE.md: these three are deliberate stubs, and
    # `guarantee` has never had checks. Anything else here is a regression.
    check("stub set unchanged",
          MANIFEST["stub_types"] == ["guarantee", "option", "pick", "release"])
    if MANIFEST["stub_types"] != ["guarantee", "option", "pick", "release"]:
        print("      now: " + ", ".join(MANIFEST["stub_types"]))


def test_a_section_with_no_story_is_absent():
    print("\n-- a section nothing enforces and nobody reviews carries no badge --")
    # § 1.1 is a table of cap numbers; there is no rule in it to enforce.
    check("§ 1.1 is not in the manifest", "1.1" not in MANIFEST["sections"])


def test_known_enforced_sections():
    print("\n-- the sections whose enforcement went stale before now read 🔒 --")
    for sec in ("7.2", "3.12", "2.2", "3.10", "6.2", "7.1", "4.5"):
        enforced, _ = rc.badges(sec, MANIFEST)
        check(f"§ {sec} enforced", enforced)


test_every_check_id_resolves()
test_declared_map_matches_the_code()
test_every_check_enforces_something()
test_declared_sections_exist_in_the_rulebook()
test_notes_are_not_blank()
test_stub_validators_stay_known()
test_a_section_with_no_story_is_absent()
test_known_enforced_sections()

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))
sys.exit(1 if FAILS else 0)

"""Regression tests for the targeted-correction path — `edit_allstats.py` and
`allstats_guard.write_allstats_edit`.

This path exists to do the one thing the append contract forbids: rewrite a row
already on disk. That makes it the most dangerous code that touches the raw box
scores, so the tests are almost entirely about what it **refuses**. The property
being defended is narrow and checkable: a write must differ from disk in exactly
the cells the caller declared, and nowhere else. A tool that fixes one name and
silently drops a season would satisfy `allow_shrink=True`; it must not satisfy
this.

Nothing here touches the live data directory.

    venv/bin/python -m tests.test_allstats_edit
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edit_allstats                                                   # noqa: E402
from routers.allstats_guard import AllstatsGuardError, write_allstats_edit  # noqa: E402
from routers.storage import read_csv                                   # noqa: E402

FAILS = []
HEADERS = ["TEAM", "DATE", "OPP", "PLAYER", "M", "P"]


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def refuses(name, fn, fragment):
    try:
        fn()
    except (AllstatsGuardError, SystemExit) as exc:
        check(name, fragment in str(exc), str(exc)[:150])
        return
    check(name, False, "nothing raised")


def rows():
    return [
        {"TEAM": "DEN", "DATE": "2024-10-29", "OPP": "@BKN", "PLAYER": "JOKIC, NIKOLA", "M": "34", "P": "30"},
        {"TEAM": "DEN", "DATE": "2024-10-29", "OPP": "@BKN", "PLAYER": "HOLIDAY, JRUE", "M": "24", "P": "13"},
        {"TEAM": "DEN", "DATE": "2024-10-29", "OPP": "@BKN", "PLAYER": "HOLIDAY, JRUE", "M": "3", "P": "3"},
    ]


def seed(d: Path) -> Path:
    p = d / "allstats-24-25.csv"
    body = ",".join(HEADERS) + "\n"
    for r in rows():
        body += ",".join(f'"{r[c]}"' if "," in r[c] else r[c] for c in HEADERS) + "\n"
    p.write_text(body)
    return p


def main():
    print("allstats edit path")

    # ── the guard contract ────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        p = seed(d)
        headers, on_disk = read_csv(p)

        # The good case, so the refusals below mean something.
        new = [dict(r) for r in on_disk]
        new[2]["PLAYER"] = "HOLIDAY, AARON"
        write_allstats_edit(p, headers, new,
                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "HOLIDAY, AARON")}})
        _, after = read_csv(p)
        check("a declared edit is written", after[2]["PLAYER"] == "HOLIDAY, AARON")
        check("and every other row is untouched",
              [r["PLAYER"] for r in after[:2]] == [r["PLAYER"] for r in on_disk[:2]])
        check("and the row count is unchanged", len(after) == len(on_disk))

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        p = seed(d)
        headers, on_disk = read_csv(p)

        # The failure this whole path exists to prevent: a caller that means to
        # change one cell and instead hands over a truncated list.
        refuses("dropping rows is refused",
                lambda: write_allstats_edit(p, headers, on_disk[:2],
                                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "X")}}),
                "must not change the row count")
        refuses("adding rows is refused",
                lambda: write_allstats_edit(p, headers, on_disk + [dict(on_disk[0])],
                                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "X")}}),
                "must not change the row count")

        # A change nobody declared, in a row that also has a declared change.
        sneaky = [dict(r) for r in on_disk]
        sneaky[2]["PLAYER"] = "HOLIDAY, AARON"
        sneaky[2]["P"] = "999"
        refuses("an undeclared cell change in the same row is refused",
                lambda: write_allstats_edit(p, headers, sneaky,
                                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "HOLIDAY, AARON")}}),
                "no declared edit covers")

        # A change in a row that was never named at all.
        elsewhere = [dict(r) for r in on_disk]
        elsewhere[2]["PLAYER"] = "HOLIDAY, AARON"
        elsewhere[0]["P"] = "1"
        refuses("an undeclared change in another row is refused",
                lambda: write_allstats_edit(p, headers, elsewhere,
                                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "HOLIDAY, AARON")}}),
                "no declared edit covers")

        # The file moved since the plan was built.
        stale = [dict(r) for r in on_disk]
        stale[2]["PLAYER"] = "HOLIDAY, AARON"
        refuses("a stale `before` is refused",
                lambda: write_allstats_edit(p, headers, stale,
                                            expected_edits={2: {"PLAYER": ("SOMEONE, ELSE", "HOLIDAY, AARON")}}),
                "re-plan it")

        # Declaring an `after` the write does not actually make.
        refuses("an `after` that disagrees with the write is refused",
                lambda: write_allstats_edit(p, headers, stale,
                                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "SOMEONE, ELSE")}}),
                "declared")

        refuses("dropping a column is refused",
                lambda: write_allstats_edit(p, [h for h in headers if h != "P"], on_disk,
                                            expected_edits={2: {"PLAYER": ("HOLIDAY, JRUE", "X")}}),
                "would drop column")

        refuses("declaring no edits is refused",
                lambda: write_allstats_edit(p, headers, on_disk, expected_edits={}),
                "no edits declared")

        _, unchanged = read_csv(p)
        check("none of the refusals wrote anything",
              [r["PLAYER"] for r in unchanged] == [r["PLAYER"] for r in on_disk])

    # ── the CLI ───────────────────────────────────────────────────────────
    def run(d: Path, *args):
        return edit_allstats.main(["--data-dir", str(d), "--file", "allstats-24-25.csv"] + list(args))

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        p = seed(d)
        original = p.read_text()

        # A selector that matches more than one row must fail before writing —
        # "PLAYER=HOLIDAY, JRUE" alone matches both Holiday rows.
        rc = run(d, "--where", "TEAM=DEN", "PLAYER=HOLIDAY, JRUE",
                 "--set", "PLAYER=HOLIDAY, AARON", "--reason", "r")
        check("an ambiguous selector exits non-zero", rc == 2)
        check("and changes nothing", p.read_text() == original)

        rc = run(d, "--where", "TEAM=DEN", "NOPE=1", "--set", "PLAYER=X", "--reason", "r")
        check("an unknown column exits non-zero", rc == 2)

        rc = run(d, "--where", "TEAM=XXX", "--set", "PLAYER=X", "--reason", "r")
        check("a selector matching nothing exits non-zero", rc == 2)

        rc = run(d, "--where", "TEAM=DEN", "--set", "PLAYER=X", "--reason", "  ")
        check("an empty --reason is refused", rc == 2)

        # Dry run is the default.
        rc = run(d, "--where", "TEAM=DEN", "PLAYER=HOLIDAY, JRUE", "M=3",
                 "--set", "PLAYER=HOLIDAY, AARON", "--reason", "aaron not jrue")
        check("a dry run succeeds", rc == 0)
        check("a dry run writes nothing", p.read_text() == original)
        check("a dry run leaves no edit log", not (d / edit_allstats.EDIT_LOG_NAME).exists())

        # And --apply is the deliberate second act.
        rc = run(d, "--where", "TEAM=DEN", "PLAYER=HOLIDAY, JRUE", "M=3",
                 "--set", "PLAYER=HOLIDAY, AARON", "--reason", "aaron not jrue", "--apply")
        check("--apply succeeds", rc == 0)
        _, after = read_csv(p)
        check("exactly the selected row changed",
              [r["PLAYER"] for r in after] ==
              ["JOKIC, NIKOLA", "HOLIDAY, JRUE", "HOLIDAY, AARON"], [r["PLAYER"] for r in after])
        check("the file is one line different, not rewritten",
              len(p.read_text().splitlines()) == len(original.splitlines()))

        entries = [json.loads(l) for l in
                   (d / edit_allstats.EDIT_LOG_NAME).read_text().splitlines() if l.strip()]
        check("the edit is logged once", len(entries) == 1)
        check("the log carries the before and after",
              entries[0]["changes"]["PLAYER"] == {"before": "HOLIDAY, JRUE", "after": "HOLIDAY, AARON"})
        check("the log carries the reason", entries[0]["reason"] == "aaron not jrue")
        check("the log identifies the row",
              entries[0]["row"]["DATE"] == "2024-10-29" and entries[0]["row"]["M"] == "3")

        # Re-running an applied edit is a no-op, not an error: the guard would
        # otherwise reject it on a stale `before` and look like a failure.
        text = p.read_text()
        rc = run(d, "--where", "TEAM=DEN", "PLAYER=HOLIDAY, AARON",
                 "--set", "PLAYER=HOLIDAY, AARON", "--reason", "again", "--apply")
        check("re-applying is a clean no-op", rc == 0 and p.read_text() == text)
        check("and does not log a second time",
              len((d / edit_allstats.EDIT_LOG_NAME).read_text().splitlines()) == 1)

    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

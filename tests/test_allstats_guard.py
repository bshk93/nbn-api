"""Regression tests for routers.allstats_guard — the append-only contract on the
raw box score CSVs (dev-deploy spec, Phase 2 item 9).

The guard exists because these files cannot be rebuilt, so the tests are about
what it *refuses*: a truncation, a rewrite of history dressed up as a write, and
a header list that would silently drop a column that only the older seasons have.

The last one is not hypothetical — 20-21 through 23-24 have no `OPP_RAW` and
several carry `SEASON`, so committing a game to an old season with the current
header constant would have erased a column across the whole file.

Nothing here touches the live data directory; every case runs against a tmp file.

    venv/bin/python -m tests.test_allstats_guard
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.allstats_guard import write_allstats, AllstatsGuardError  # noqa: E402
from routers.storage import read_csv  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def refuses(name, fn, expect_fragment):
    try:
        fn()
    except AllstatsGuardError as exc:
        check(name, expect_fragment in str(exc), str(exc)[:120])
        return
    check(name, False, "no AllstatsGuardError raised")


HEADERS = ["TEAM", "DATE", "PLAYER", "P"]
ROWS = [
    {"TEAM": "PHX", "DATE": "2026-01-01", "PLAYER": "Booker, Devin", "P": "30"},
    {"TEAM": "PHX", "DATE": "2026-01-01", "PLAYER": "Durant, Kevin", "P": "28"},
]


def seed(tmp: Path, headers=HEADERS, rows=ROWS) -> Path:
    path = tmp / "allstats-25-26.csv"
    write_allstats(path, headers, rows)
    return path


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # ── The happy path: a new file, then an append ────────────────────────────
    path = seed(tmp)
    _, on_disk = read_csv(path)
    check("creates a file that does not exist yet", len(on_disk) == 2)

    added = {"TEAM": "LAL", "DATE": "2026-01-01", "PLAYER": "James, LeBron", "P": "25"}
    write_allstats(path, HEADERS, ROWS + [added])
    _, on_disk = read_csv(path)
    check("appends rows", len(on_disk) == 3 and on_disk[2]["PLAYER"] == "James, LeBron")

    # An append is judged against what is on disk, not what the caller believes.
    write_allstats(path, HEADERS, list(on_disk))
    check("a no-op rewrite of the current contents is allowed", len(read_csv(path)[1]) == 3)

    # ── Refusal 1: truncation ─────────────────────────────────────────────────
    refuses("refuses a write with fewer rows than are on disk",
            lambda: write_allstats(path, HEADERS, ROWS[:1]), "shrink the file from 3 to 1")
    check("the file is untouched after a refusal", len(read_csv(path)[1]) == 3)

    # An empty write is the catastrophic case and must be refused too.
    refuses("refuses an empty write", lambda: write_allstats(path, HEADERS, []), "shrink the file")

    # ── Refusal 2: a rewrite that is not an append ────────────────────────────
    edited = [dict(r) for r in read_csv(path)[1]]
    edited[0]["P"] = "99"
    refuses("refuses editing an existing row, even at the same length",
            lambda: write_allstats(path, HEADERS, edited), "not an append")

    reordered = [dict(r) for r in read_csv(path)[1]][::-1]
    refuses("refuses reordering the rows already on disk",
            lambda: write_allstats(path, HEADERS, reordered), "not an append")

    grown_but_edited = edited + [dict(added, PLAYER="Reaves, Austin")]
    refuses("refuses a longer write that rewrites history",
            lambda: write_allstats(path, HEADERS, grown_but_edited), "not an append")

    # ── Refusal 3: a column that only the older files have ────────────────────
    legacy = tmp / "allstats-22-23.csv"
    legacy_headers = HEADERS + ["SEASON"]
    write_allstats(legacy, legacy_headers, [dict(r, SEASON="22-23") for r in ROWS])
    refuses("refuses a write whose headers drop a column present on disk",
            lambda: write_allstats(legacy, HEADERS, [dict(r, SEASON="22-23") for r in ROWS] + [added]),
            "drop column(s) ['SEASON']")
    check("the legacy column survives the refusal", "SEASON" in read_csv(legacy)[0])

    # Adding a column is fine — it only ever grows.
    write_allstats(legacy, legacy_headers + ["OPP_RAW"],
                   [dict(r, SEASON="22-23", OPP_RAW="LAL") for r in ROWS])
    check("allows a write that adds a column", "OPP_RAW" in read_csv(legacy)[0])

    # ── The override ──────────────────────────────────────────────────────────
    write_allstats(path, HEADERS, ROWS[:1], allow_shrink=True)
    check("allow_shrink=True permits a deliberate removal", len(read_csv(path)[1]) == 1)

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_allstats_guard: all pass")

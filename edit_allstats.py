#!/usr/bin/env python3
"""Make one targeted correction to a raw box score file, safely.

The raw `allstats-*.csv` files are append-only by contract
(`routers/allstats_guard.py`) because a code path that rewrites where it meant
to append is the way this dataset dies. But corrections are a real need — the
three 24-25 team-games that list one player twice under another's name (see
BACKLOG.md) can only be fixed by changing a cell that is already on disk.

Before this existed the only way through was `allow_shrink=True`, which is not
a scalpel: it turns off *every* check at once, so a script that meant to fix one
name and instead rebuilt the row list wrong would be written in full and mirrored
off-box by the snapshot timer ten minutes later.

This is the scalpel. Four properties, in the order they matter:

1. **The selector must match exactly one row** (or `--expect N` of them). A
   `--where` that is too loose fails before anything is read for writing, rather
   than quietly editing forty rows.
2. **Dry run by default.** It prints the row, the before/after of every cell, and
   stops. `--apply` is a separate, deliberate act.
3. **The write is checked against disk cell by cell**
   (`allstats_guard.write_allstats_edit`): same row count, every untouched row
   byte-identical, every touched cell matching the `before` the plan was built
   from. A file that moved under you fails instead of clobbering.
4. **It re-runs the corpus checks afterwards** (`stats_build/checks.py`) and
   reports any change in the finding count, so an edit that fixes one thing and
   breaks another cannot look like a success.

Every applied edit appends one line to `allstats-edits.jsonl` in the data dir,
with the row's identifying fields, the before/after, and a **mandatory
`--reason`**. That log is the only record of why a hand-corrected row differs
from what was originally parsed; the screenshots are long gone.

Deliberately NOT supported: adding or removing rows. A missing game is an
append, which is what `POST /api/boxscore/commit` is for; a game that should not
be there is rare enough to want a human writing the migration and saying so.

    # see what it would do
    venv/bin/python edit_allstats.py --file allstats-24-25.csv \
        --where TEAM=DEN DATE=2024-10-29 "PLAYER=HOLIDAY, JRUE" M=3 \
        --set "PLAYER=HOLIDAY, AARON" --reason "3min cameo was Aaron, not Jrue"

    # do it
    ... --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routers.allstats_guard import AllstatsGuardError, write_allstats_edit  # noqa: E402
from routers.storage import read_csv                                       # noqa: E402
from stats_build import checks                                             # noqa: E402

EDIT_LOG_NAME = "allstats-edits.jsonl"

# Printed with every matched row so a human can recognise the game at a glance.
IDENTITY = ("TEAM", "DATE", "OPP", "PLAYER", "M", "P")


def parse_pairs(items: list[str], flag: str) -> dict[str, str]:
    """`["TEAM=DEN", "PLAYER=HOLIDAY, JRUE"]` -> dict. Values may contain `=`."""
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"{flag}: {item!r} is not COLUMN=VALUE")
        col, val = item.split("=", 1)
        col = col.strip()
        if not col:
            raise SystemExit(f"{flag}: {item!r} has an empty column name")
        out[col] = val
    return out


def select(rows: list[dict], where: dict[str, str]) -> list[int]:
    """Indices of rows matching every COLUMN=VALUE, compared as trimmed strings."""
    hits = []
    for i, r in enumerate(rows):
        if all((r.get(c) or "").strip() == v.strip() for c, v in where.items()):
            hits.append(i)
    return hits


def describe(row: dict) -> str:
    return "  ".join(f"{c}={(row.get(c) or '')!r}" for c in IDENTITY if c in row)


def corpus_findings(data_dir: Path) -> list:
    """Every value-level finding over the whole corpus, for the before/after count."""
    files = []
    for p in sorted(data_dir.glob("allstats-*.csv")):
        _, rows = read_csv(p)
        files.append((p.name, rows))
    names = None
    bio = data_dir / "player-bios.json"
    if bio.exists():
        try:
            bios = json.loads(bio.read_text())
            names = {(b.get("name") or "").strip().upper()
                     for b in bios.values() if b.get("name")}
        except (OSError, json.JSONDecodeError):
            names = None
    return checks.check_corpus(files, names)


def log_edit(data_dir: Path, path: Path, row: dict, changes: dict, reason: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown",
        "file": path.name,
        "row": {c: (row.get(c) or "") for c in IDENTITY if c in row},
        "changes": {c: {"before": b, "after": a} for c, (b, a) in changes.items()},
        "reason": reason,
    }
    with (data_dir / EDIT_LOG_NAME).open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("NBS_DATA_DIR", "/var/lib/nothing-but-stats")))
    ap.add_argument("--file", required=True, help="e.g. allstats-24-25.csv")
    ap.add_argument("--where", nargs="+", required=True, metavar="COL=VALUE",
                    help="narrows to the row(s) to change; must match --expect exactly")
    ap.add_argument("--set", nargs="+", required=True, metavar="COL=VALUE",
                    dest="set_", help="the new cell values")
    ap.add_argument("--reason", required=True,
                    help="why — recorded in allstats-edits.jsonl, which is the only "
                         "record of why this row differs from what was parsed")
    ap.add_argument("--expect", type=int, default=1,
                    help="how many rows --where must match (default 1)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    a = ap.parse_args(argv)

    path = a.data_dir / a.file
    if not path.exists():
        print(f"ERROR: {path} does not exist", file=sys.stderr)
        return 2
    if not a.reason.strip():
        print("ERROR: --reason must not be empty", file=sys.stderr)
        return 2

    where = parse_pairs(a.where, "--where")
    updates = parse_pairs(a.set_, "--set")

    headers, rows = read_csv(path)
    unknown = [c for c in list(where) + list(updates) if c not in headers]
    if unknown:
        print(f"ERROR: {path.name} has no column(s) {unknown}. Columns are: "
              f"{', '.join(headers)}", file=sys.stderr)
        return 2

    hits = select(rows, where)
    if len(hits) != a.expect:
        print(f"ERROR: --where matched {len(hits)} row(s), expected {a.expect}.",
              file=sys.stderr)
        for i in hits[:10]:
            print(f"    row {i}: {describe(rows[i])}", file=sys.stderr)
        if len(hits) > 10:
            print(f"    …and {len(hits) - 10} more", file=sys.stderr)
        print("Narrow --where, or pass --expect if this many is intended.",
              file=sys.stderr)
        return 2

    # Build the plan, and drop cells that are already the requested value so a
    # re-run of an applied edit is a clean no-op rather than a guard error.
    expected_edits: dict[int, dict[str, tuple[str, str]]] = {}
    for i in hits:
        changes = {c: ((rows[i].get(c) or ""), v) for c, v in updates.items()
                   if (rows[i].get(c) or "") != v}
        if changes:
            expected_edits[i] = changes

    print(f"{path.name}: {len(rows):,} rows, --where matched {len(hits)}\n")
    for i in hits:
        print(f"  row {i}: {describe(rows[i])}")
        changes = expected_edits.get(i, {})
        if not changes:
            print("    (already has every requested value — nothing to change)")
        for c, (before, after) in changes.items():
            print(f"    {c}: {before!r}  ->  {after!r}")
    print()

    if not expected_edits:
        print("Nothing to do.")
        return 0

    before_findings = corpus_findings(a.data_dir)
    print(f"corpus before: {checks.summarize(before_findings)}")

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to make the change.")
        return 0

    new_rows = [dict(r) for r in rows]
    for i, changes in expected_edits.items():
        for c, (_before, after) in changes.items():
            new_rows[i][c] = after

    try:
        write_allstats_edit(path, headers, new_rows, expected_edits=expected_edits)
    except AllstatsGuardError as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 1

    for i, changes in expected_edits.items():
        log_edit(a.data_dir, path, rows[i], changes, a.reason.strip())

    after_findings = corpus_findings(a.data_dir)
    print(f"corpus after:  {checks.summarize(after_findings)}")
    delta = len(after_findings) - len(before_findings)
    if delta > 0:
        print(f"\nWARNING: {delta} MORE finding(s) than before this edit:",
              file=sys.stderr)
        before_set = {str(f) for f in before_findings}
        for f in after_findings:
            if str(f) not in before_set:
                print(f"    {f}", file=sys.stderr)

    print(f"\nApplied. Logged to {a.data_dir / EDIT_LOG_NAME}")
    print("The derived files still hold the old value — run the build:")
    print("    bash /home/skim/projects/nbn-today/build/build.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())

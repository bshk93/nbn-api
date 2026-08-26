"""Append-only contract for the raw box score CSVs (dev-deploy spec, Phase 2 item 9).

`allstats-{season}.csv` / `allstats-playoffs-{yy}.csv` are the one asset on this
box that cannot be rebuilt — 157k hand-entered rows over six seasons, with the
source screenshots deliberately destroyed after parsing. Every derived CSV, every
page on the site, is a function of them.

They only ever grow. That is a property worth *enforcing* rather than merely
monitoring: the weekly integrity check (`check_stats_integrity.py`) turns a
truncation into something discovered within a week, but this turns the most
likely catastrophe — a code path that rewrites where it meant to append — into
something that cannot happen through the API at all.

Three refusals, each one a real failure mode rather than a hypothetical:

1. **Fewer rows than are on disk.** The truncation case.
2. **The rows on disk are not a prefix of what is being written.** A same-length
   or longer write that edits history is not an append; it is the corruption the
   backup would faithfully mirror.
3. **A column present on disk is missing from the write.** `write_csv` uses
   `extrasaction="ignore"`, so a header list that omits a column drops it from
   every row silently. The older season files genuinely differ — 20-21 through
   23-24 have no `OPP_RAW`, several carry `SEASON`, the blank column is `...27`
   rather than `' '` — so committing a game to an old season with the current
   header constant would erase a column across the whole file.

None of this constrains the live loop: the current-season headers match the
constants in `boxscores.py` exactly, and a commit appends.

**The override is explicit and per-call.** Legitimate shrinking writes exist (a
mis-parsed game removed, a migration); they pass `allow_shrink=True`, or set
`NBN_ALLSTATS_ALLOW_SHRINK=1` for a one-off script. Both log at WARNING with the
row delta, so an override is never silent. Hand-editing the file with an editor
is outside this guard by construction — it only covers writes through the API,
which is where the code paths are.
"""
from __future__ import annotations

import os
from pathlib import Path

from .constants import logger
from .storage import read_csv, write_csv


class AllstatsGuardError(RuntimeError):
    """A write to a raw box score file that would lose data."""


def _row_key(row: dict, cols: list[str]) -> tuple:
    return tuple((row.get(c) or "") for c in cols)


def write_allstats(path: Path, headers: list[str], rows: list[dict], *,
                   allow_shrink: bool = False) -> None:
    """`write_csv` for a raw box score file, refusing anything but an append.

    Raises `AllstatsGuardError` rather than writing. The caller has not committed
    anything at that point — this runs before the file is touched.
    """
    override = allow_shrink or os.environ.get("NBN_ALLSTATS_ALLOW_SHRINK") == "1"

    if path.exists():
        old_headers, existing = read_csv(path)

        lost = [c for c in old_headers if c not in headers]
        if lost and not override:
            raise AllstatsGuardError(
                f"{path.name}: write would drop column(s) {lost} from {len(existing)} "
                f"existing rows. Writing an older season with the current header "
                f"constant does this. Pass allow_shrink=True if it is intended."
            )

        if len(rows) < len(existing) and not override:
            raise AllstatsGuardError(
                f"{path.name}: write would shrink the file from {len(existing)} to "
                f"{len(rows)} rows. Raw box score files only ever grow. Pass "
                f"allow_shrink=True if this removal is intended."
            )

        if not override and existing:
            cols = [c for c in old_headers if c in headers]
            head = [_row_key(r, cols) for r in rows[:len(existing)]]
            if head != [_row_key(r, cols) for r in existing]:
                raise AllstatsGuardError(
                    f"{path.name}: write is not an append — the {len(existing)} rows "
                    f"already on disk are not the first {len(existing)} rows being "
                    f"written. Pass allow_shrink=True if this rewrite is intended."
                )

        if override and len(rows) < len(existing):
            logger.warning(
                "ALLSTATS OVERRIDE: %s shrinking from %d to %d rows",
                path.name, len(existing), len(rows),
            )

    write_csv(path, headers, rows)


def write_allstats_edit(path: Path, headers: list[str], rows: list[dict], *,
                        expected_edits: dict[int, dict[str, tuple[str, str]]]) -> None:
    """`write_csv` for a *targeted correction* to a raw box score file.

    The append contract above cannot express this: a correction rewrites a row
    already on disk, which `write_allstats` refuses by design and which
    `allow_shrink=True` would wave through wholesale. That override is the wrong
    tool here — it disables every check at once, so a script that meant to fix
    one cell and instead rebuilt the list wrong would be committed in full.

    So this is a second contract, no weaker than the first, just different: the
    write must differ from what is on disk in **exactly** the cells named in
    `expected_edits` and nowhere else. Keyed by row index, each entry maps a
    column to the `(before, after)` pair the caller believes it is changing.
    Anything else — a row count that moved, an untouched row that did not stay
    byte-identical, a named cell whose current value is not `before`, a change
    in a column nobody declared — raises rather than writing.

    That makes the dangerous half impossible to reach by accident. A caller
    cannot "fix one name" and silently drop a season, because dropping a season
    is not one of the cells it declared.
    """
    if not path.exists():
        raise AllstatsGuardError(f"{path.name}: cannot edit a file that does not exist")
    if not expected_edits:
        raise AllstatsGuardError(f"{path.name}: no edits declared — nothing to do")

    old_headers, existing = read_csv(path)

    lost = [c for c in old_headers if c not in headers]
    if lost:
        raise AllstatsGuardError(
            f"{path.name}: edit would drop column(s) {lost} from every row.")

    if len(rows) != len(existing):
        raise AllstatsGuardError(
            f"{path.name}: an edit must not change the row count "
            f"({len(existing)} on disk, {len(rows)} being written). Adding or "
            f"removing games is not what this path is for.")

    for i, (old, new) in enumerate(zip(existing, rows)):
        declared = expected_edits.get(i, {})
        for col in old_headers:
            before, after = (old.get(col) or ""), (new.get(col) or "")
            if col in declared:
                want_before, want_after = declared[col]
                if before != want_before:
                    raise AllstatsGuardError(
                        f"{path.name}: row {i} column {col!r} is {before!r} on "
                        f"disk, but the edit expected {want_before!r}. The file "
                        f"changed since the plan was made — re-plan it.")
                if after != want_after:
                    raise AllstatsGuardError(
                        f"{path.name}: row {i} column {col!r} would become "
                        f"{after!r}, but the edit declared {want_after!r}.")
            elif before != after:
                raise AllstatsGuardError(
                    f"{path.name}: row {i} column {col!r} would change from "
                    f"{before!r} to {after!r}, which no declared edit covers. "
                    f"Every changed cell must be declared.")

    logger.warning("ALLSTATS EDIT: %s correcting %d cell(s) across %d row(s)",
                   path.name, sum(len(v) for v in expected_edits.values()),
                   len(expected_edits))
    write_csv(path, headers, rows)

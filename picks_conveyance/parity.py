"""Does the conveyance store still agree with the flat picks ledger?

`/api/picks` reads the conveyance store (`PICKS_READ_SOURCE=conveyance`), but
every write lands in `draft-picks.csv` first and the store is regenerated from
it plus the registry (`resync.py`). For a pick inside a registry structure —
a protection, swap group, binary chain or ladder — the registry wins, so a
flat `OWNER` change the registry never heard about is silently dropped.

That happened. Three trades entered between 2026-06-20 and 2026-07-09, before
the conveyance write path existed, were missing from the registry it was
seeded from on 2026-07-19. The flat ledger had the new owner; the site did not
list the pick for them at all. Nothing noticed for two months.

The flat model is lossy, so the two can't be compared for equality: a swap
reads `DET|HOU` in one and a single `DET` in the other, legitimately. The
one-way question is always answerable, though: **every team the flat ledger
names as an owner must hold some claim in what the site serves.** A team that
does not is a team whose pick has vanished from its page.

Pure: takes rows and a store, returns strings. No I/O, so it's testable and
callable from the weekly integrity job.
"""
from __future__ import annotations

import re

from . import projection

# A NOTES value that only labels a structure ("protected", "conditional:
# BOS/HOU"), as opposed to prose. The label means nothing once the structure is
# gone, and nothing else in the flat row says so: the SAS 2029 and 2030 2nds
# kept "protected" after Trade 54 removed the ladder they were the fallback of.
STRUCTURE_LABEL = re.compile(r"^\s*(protected|conditional:\s*[A-Z|/ ]+)\s*$", re.I)


def _key(year, rnd, orig) -> str:
    return f"{int(year)} R{int(rnd)} {orig}"


def claimants(flat: dict) -> set[str]:
    """Every team with a claim on one served (`project_to_flat`-shaped) pick —
    the same set `/api/picks/{team}` would serve it to, plus leaf and ladder
    parties."""
    teams: set[str] = set()
    owner = (flat.get("owner") or "").strip()
    if owner and owner != "?":
        teams.update(owner.split("|"))
    elif owner == "?":
        teams.add(flat["orig"])
    teams.update(leaf["team"] for leaf in flat.get("leaves") or [] if leaf.get("team"))
    for field in ("ladder", "ladder_fallback_of"):
        lad = flat.get(field) or {}
        teams.update(t for t in (lad.get("from"), lad.get("to")) if t)
    return teams


def has_structure(flat: dict) -> bool:
    """True when a served pick carries any contingent structure."""
    return bool(flat.get("leaves") or flat.get("ladder") or flat.get("ladder_fallback_of")
                or flat.get("group_id") or flat.get("protected") or flat.get("legacy"))


def owner_mismatches(csv_rows: list[dict], store: dict) -> list[str]:
    """One message per disagreement between the flat ledger and the store."""
    served = {}
    for pick in store.get("picks", []):
        flat = projection.project_to_flat(pick, store)
        served[_key(flat["year"], flat["round"], flat["orig"])] = flat

    out = []
    seen = set()
    for row in csv_rows:
        key = _key(row["YEAR"], row["ROUND"], row["ORIG"])
        seen.add(key)
        flat = served.get(key)
        if flat is None:
            out.append(f"picks: {key} is in draft-picks.csv but not in the conveyance store")
            continue
        owner = (row.get("OWNER") or "").strip()
        if not owner or owner == "?":
            continue
        missing = sorted(set(owner.split("|")) - claimants(flat))
        if missing:
            out.append(
                f"picks: {key} — draft-picks.csv says {owner}, the site serves "
                f"{flat['owner']}; {', '.join(missing)} has no claim on it")
    for key in sorted(set(served) - seen):
        out.append(f"picks: {key} is in the conveyance store but not in draft-picks.csv")
    return out


def stale_labels(csv_rows: list[dict], store: dict) -> list[str]:
    """One message per flat row whose NOTES labels a structure the site no
    longer serves. A trade that removes a protection clears the label it
    knows about (`_clear_structure_labels` in routers/transactions.py); this
    catches the rest, such as a label left from the league sheet."""
    served = {}
    for pick in store.get("picks", []):
        flat = projection.project_to_flat(pick, store)
        served[_key(flat["year"], flat["round"], flat["orig"])] = flat
    out = []
    for row in csv_rows:
        note = row.get("NOTES") or ""
        if not STRUCTURE_LABEL.match(note):
            continue
        key = _key(row["YEAR"], row["ROUND"], row["ORIG"])
        flat = served.get(key)
        if flat is not None and not has_structure(flat):
            out.append(f"picks: {key} is labelled {note.strip()!r} but has no "
                       "protection, swap or ladder; clear the note")
    return out

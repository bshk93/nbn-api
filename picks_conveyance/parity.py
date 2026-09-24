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

from . import projection


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

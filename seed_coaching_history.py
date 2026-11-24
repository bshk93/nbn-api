"""Start coaching-history.jsonl from the coaching records as they stand.

History is only kept from the day `routers/coaching_history.py` shipped, so
without this every team's History section would start empty. This writes, for
each team that has no history yet:

  * an `entered` line for what a streamer last put into 2K (the record's
    `entered_values`/`entered_minutes`, at `entered_at`), if anything was;
  * a `saved` line for the current settings, if they are waiting to be
    entered (`pending`), or if nothing was ever entered;
  * a `note` line for the team's note, and a `note_cleared` line if a
    streamer has marked it handled.

Every line carries `seeded: true`. A team that already has any history is
skipped, so running it twice writes nothing the second time.

    venv/bin/python seed_coaching_history.py            # write
    venv/bin/python seed_coaching_history.py --dry-run  # say what it would write
"""
import argparse

from routers import coaching_history
from routers.coaching_settings import _entered_snapshot, load_coaching_notes, load_coaching_settings
from routers.constants import VALID_TEAMS


def lines_for(team: str, rec: dict, note: dict) -> list[dict]:
    out = []
    snap = _entered_snapshot(rec) if rec else None
    if snap is not None and rec.get("entered_at"):
        values, minutes = snap
        out.append({"kind": "entered", "at": rec["entered_at"], "by": rec.get("entered_by"),
                    "values": values, "minutes": minutes,
                    "version": None if rec.get("pending") else rec.get("updated_at")})
    if rec and rec.get("updated_at") and (rec.get("pending") or snap is None):
        out.append({"kind": "saved", "at": rec["updated_at"], "by": rec.get("updated_by"),
                    "values": rec.get("values") or {}, "minutes": rec.get("minutes") or {}})
    if note and note.get("text"):
        out.append({"kind": "note", "at": note.get("updated_at"), "by": note.get("updated_by"),
                    "text": note["text"]})
        if note.get("cleared_at"):
            out.append({"kind": "note_cleared", "at": note["cleared_at"], "by": note.get("cleared_by"),
                        "text": note["text"]})
    return sorted(out, key=lambda e: e.get("at") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    settings, notes = load_coaching_settings(), load_coaching_notes()
    written = 0
    for team in sorted(VALID_TEAMS):
        lines = lines_for(team, settings.get(team) or {}, notes.get(team) or {})
        if not lines:
            continue
        if coaching_history.has_team(team):
            print(f"{team}: has history, skipped")
            continue
        print(f"{team}: " + ", ".join(f"{e['kind']} {e['at']}" for e in lines))
        written += len(lines)
        if args.dry_run:
            continue
        for e in lines:
            kind, at, by = e.pop("kind"), e.pop("at"), e.pop("by")
            coaching_history.record(team, kind, at, by, seeded=True, **e)
    print(f"{'would write' if args.dry_run else 'wrote'} {written} lines")


if __name__ == "__main__":
    main()

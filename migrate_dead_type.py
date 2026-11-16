#!/usr/bin/env python3
"""Retire the legacy `type: "dead"` on player bios.

Nothing has written `"dead"` since dead cap moved to `{team}-deadcap.csv`
(a player can carry dead money on one team while playing for another, which
one `type` field can't say). About 27 bios still have it, so eleven pages and
`news.py` still read it as "roster-exempt". This lists those bios, and with
`--apply` clears the type on the ones where that is provably safe.

**Report first.** For each bio it prints:

- which roster CSV lists him, if any (a "dead" player still on a roster is
  what the type exempts from the roster count today);
- whether a `{team}-deadcap.csv` row already carries him;
- what his `salaries` still hold from this season on.

`--apply` changes only bios that are on **no** roster: the type then exempts
nothing, so clearing it (to `""`, what a release writes now) changes no count
and no cap figure. A bio still on a roster is left alone and listed as
needing a person: moving him off the roster and into the deadcap CSV is a
roster and cap change, done through the rosters committee's tools, not here.

    venv/bin/python migrate_dead_type.py            # the report; writes nothing
    venv/bin/python migrate_dead_type.py --apply    # clear the safe ones

The write goes through save_player_bios (atomic, and logged to edits.jsonl as
`system`), and re-reads the bios immediately before writing so it only ever
changes the one field. Run it at a quiet moment: it is a separate process, so
the API's in-process lock can't cover it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routers.constants import DATA_DIR, VALID_TEAMS  # noqa: E402
from routers.players import load_player_bios, save_player_bios, _build_team_map  # noqa: E402
from routers.storage import read_csv, _current_league_year  # noqa: E402


def deadcap_slugs() -> dict[str, str]:
    out = {}
    for team in VALID_TEAMS:
        path = DATA_DIR / f"{team.lower()}-deadcap.csv"
        if path.exists():
            for row in read_csv(path)[1]:
                if (row.get("SLUG") or "").strip():
                    out[row["SLUG"].strip()] = team
    return out


def plan() -> tuple[list[str], list[str]]:
    bios = load_player_bios()
    rosters = _build_team_map()
    dead = deadcap_slugs()
    season = _current_league_year()
    safe, held = [], []
    print(f"{'slug':32} {'roster':6} {'deadcap':7} salaries from {season}")
    for slug, bio in sorted(bios.items()):
        if bio.get("type") != "dead":
            continue
        later = {k: v for k, v in (bio.get("salaries") or {}).items() if k >= season}
        print(f"{slug:32} {rosters.get(slug, '-'):6} {dead.get(slug, '-'):7} {later or '-'}")
        (held if slug in rosters else safe).append(slug)
    print(f"\n{len(safe)} on no roster: clearing the type changes nothing they count toward.")
    print(f"{len(held)} still on a roster: left alone, for the rosters committee: {held}")
    return safe, held


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="clear the type on bios on no roster")
    args = ap.parse_args()
    if not (DATA_DIR / "player-bios.json").exists():
        print(f"no player-bios.json under {DATA_DIR}", file=sys.stderr)
        return 2
    safe, _ = plan()
    if not args.apply or not safe:
        print("\nDry run: nothing written." if not args.apply else "\nNothing to change.")
        return 0
    bios = load_player_bios()            # fresh, right before the write
    rosters = _build_team_map()
    changed = [s for s in safe if bios.get(s, {}).get("type") == "dead" and s not in rosters]
    for slug in changed:
        bios[slug]["type"] = ""
    save_player_bios(bios)
    print(f"\nCleared type on {len(changed)} bios.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

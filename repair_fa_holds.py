#!/usr/bin/env python3
"""Add missing § 3.10 trailing holds, and price holds the sign path got wrong.

Two faults, found together on 2026-09-24:

1. **Missing holds.** An expiring contract rolls into a UFA/RFA hold the season
   after it ends (§ 3.10). Nothing added that tag for the office — its contract
   helpers default to one, but a hand-built deal could leave it off — so 24
   contracts from the 2026 FA wave end with no hold at all, and their teams'
   out-year books are short by that much. `_check_trailing_hold` now warns on
   new ones; this adds the tag to the existing ones.

2. **Minimum-contract holds priced as a percentage.** § 3.10's "Coming off a
   minimum contract" row makes the hold the minimum salary, capped at the
   2-year veteran minimum. `_autofill_fa_hold_amounts` ran every hold through
   the Bird-tier percentage instead, so ~40 minimum deals carry holds from
   $2.94M up to $6.1M where the rule says $2.57M-$2.69M. The sign path is
   fixed; this reprices what it already wrote. The $1 figures the league
   sheet uses as shorthand for the same thing are repriced too.

Every figure comes from `_autofill_fa_hold_amounts`, the function a signing
itself uses, run against a copy of the bio — so this cannot price a hold
differently from how a fresh signing would.

Scope: rostered, non-two-way players; holds from the current season on.
Two-way deals keep the $0/$1 convention. A hold after a rookie-scale deal
(§ 3.10's 250%/300% row) is not implemented anywhere, so those are listed
for a human rather than guessed at.

    venv/bin/python repair_fa_holds.py                 # dry run: the plan
    venv/bin/python repair_fa_holds.py --player hauser-sam
    venv/bin/python repair_fa_holds.py --eaps above:mathurin-bennedict --apply

`--eaps above:SLUG` / `below:SLUG` answers the Full Bird question (150% vs
190%) for a season with no EAPS on file; the default is "below", the same
placeholder the office has used for this wave, and each such figure carries
a note saying it is one.

`player-bios.json` is on the audited allowlist (`routers/audit.py`), so an
applied run lands in `edits.jsonl` as one value-level diff.
"""
from __future__ import annotations

import argparse
import copy
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import HTTPException                                      # noqa: E402
from routers import audit                                              # noqa: E402
from routers.constants import CAP_LEVELS_FILE, DATA_DIR                # noqa: E402
from routers.players import load_player_bios, save_player_bios         # noqa: E402
from routers.storage import _load_json, _parse_dollar, _season_start, _current_league_year  # noqa: E402
from routers.transactions import (                                     # noqa: E402
    _FA_HOLD_TYPES, _autofill_fa_hold_amounts, _season_shift,
)

# Holds after a rookie-scale deal: § 3.10's 250%/300% row has no
# implementation, so a percentage here would be a guess.
MANUAL = {
    "prosper-omax": "hold follows a rookie-scale deal (§ 3.10 250%/300% row isn't implemented)",
}

# A deal that pays exactly the minimum but was entered under another method.
# Larry Nance's 1-year CLE deal (2026-08-11) went in as `bird_rights` at the
# 26-27 1-year minimum, $2,449,421; the league sheet holds him as a minimum
# player ($1 shorthand), and a 190% Full Bird hold on a minimum salary is not
# what § 3.10 describes.
METHOD_OVERRIDE = {
    "nance-larry": "minimum",
}


def _rostered() -> dict[str, str]:
    out = {}
    for path in sorted(DATA_DIR.glob("*-roster.csv")):
        team = path.name[:3].upper()
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                out[row["SLUG"].strip()] = team
    return out


def _governing_method(slug: str, bio: dict, prev_season: str, cap_levels: dict, stub: bool):
    """The signing method of the contract a hold follows. Trusts a contract
    record only if it covers the season before the hold. With no record
    (backfilled from the sheet), only a $0/$1 hold is read as following a
    minimum deal: that is the sheet's shorthand for exactly this row of
    § 3.10, and a salary at or under that season's 2-year minimum confirms
    it. A real figure with no record is the sheet's own pricing — often a
    second-round or rookie deal — and is left alone."""
    contracts = bio.get("contracts") or []
    if contracts and prev_season in (contracts[-1].get("salaries") or {}):
        return METHOD_OVERRIDE.get(slug) or contracts[-1].get("signing_method")
    if not stub:
        return None
    prev = _parse_dollar((bio.get("salaries") or {}).get(prev_season, "") or "")
    two_yr = ((cap_levels.get(prev_season) or {}).get("min_salary_scale") or {}).get("2")
    if prev and two_yr and prev <= two_yr:
        return "minimum"
    return None


def build_plan(bios, cap_levels, players=None, eaps=None):
    eaps = eaps or {}
    cur = _current_league_year()
    changes, manual = [], []
    for slug, team in sorted(_rostered().items(), key=lambda kv: (kv[1], kv[0])):
        if players and slug not in players:
            continue
        bio = bios.get(slug)
        if not bio or bio.get("type") in ("two-way", "draft-rights", "dead"):
            continue
        salaries = bio.get("salaries") or {}
        holds = dict(bio.get("cap_holds") or {})
        years = [s for s in salaries if _season_start(s) >= _season_start(cur)
                 and holds.get(s) not in _FA_HOLD_TYPES and _parse_dollar(salaries[s] or "")]
        added = None
        if years:
            after = _season_shift(max(years, key=_season_start), 1)
            if holds.get(after) not in _FA_HOLD_TYPES:
                holds[after] = "UFA"
                added = after

        for season, tag in sorted(holds.items(), key=lambda kv: _season_start(kv[0])):
            if tag not in _FA_HOLD_TYPES or _season_start(season) < _season_start(cur):
                continue
            prev = _season_shift(season, -1)
            before = salaries.get(season)
            stub = not _parse_dollar(before or "") or _parse_dollar(before or "") <= 1
            method = _governing_method(slug, bio, prev, cap_levels, stub)
            if not (season == added or stub or method == "minimum"):
                continue  # a real, percentage-priced hold: not this job
            if slug in MANUAL:
                manual.append((slug, team, season, MANUAL[slug]))
                continue
            if season not in cap_levels:
                # No cap figures for the season at all: the hold is tagged and
                # left unpriced, the same as every other hold that far out
                # (the rosters committee lists these as "not yet calculated").
                if season == added or (before is not None and stub):
                    changes.append({
                        "player": slug, "team": team, "season": season, "tag": tag,
                        "add_tag": season == added, "before": before, "after": None,
                        "note": None, "old_note": None, "basis": f"no {season} cap levels",
                    })
                continue
            probe = copy.deepcopy(bio)
            probe["salaries"].pop(season, None)
            try:
                notes = _autofill_fa_hold_amounts(
                    probe, team, {season: tag}, {}, cap_levels,
                    eaps_assumption=eaps.get(slug, "below"), slug=slug,
                    signing_method=method or "",
                )
            except HTTPException as e:
                manual.append((slug, team, season, str(e.detail)))
                continue
            after_amt = probe["salaries"].get(season)
            note = notes.get(season)
            old_note = (bio.get("cap_hold_notes") or {}).get(season)
            if season != added and after_amt == before and note == old_note:
                continue
            changes.append({
                "player": slug, "team": team, "season": season, "tag": tag,
                "add_tag": season == added, "before": before, "after": after_amt,
                "note": note, "old_note": old_note, "basis": method or "Bird %",
            })
    return changes, manual


def print_plan(bios, changes, manual):
    print(f"{len(changes)} hold(s) to write:")
    for c in changes:
        name = (bios.get(c["player"]) or {}).get("name") or c["player"]
        what = "ADD " if c["add_tag"] else ""
        print(f"  {c['team']} {name} ({c['player']})  {c['season']} {what}{c['tag']}: "
              f"{c['before']} -> {c['after'] or 'unpriced'}  [{c['basis']}]"
              + (f"\n      note: {c['note']}" if c["note"] else ""))
    if manual:
        print(f"\n{len(manual)} left for a human:")
        for slug, team, season, why in manual:
            print(f"  {team} {slug} {season}: {why}")


def apply_plan(changes):
    """Reloads fresh and writes a hold only if it still reads what the plan
    was built from. One save, so one audit entry."""
    bios = load_player_bios()
    applied, stale = [], []
    for c in changes:
        bio = bios.get(c["player"])
        if not bio or (bio.get("salaries") or {}).get(c["season"]) != c["before"]:
            stale.append(c)
            continue
        holds = bio.setdefault("cap_holds", {})
        if c["add_tag"]:
            if holds.get(c["season"]) in _FA_HOLD_TYPES:
                stale.append(c)
                continue
            holds[c["season"]] = c["tag"]
            contracts = bio.get("contracts") or []
            prev = _season_shift(c["season"], -1)
            if contracts and prev in (contracts[-1].get("salaries") or {}):
                contracts[-1].setdefault("cap_holds", {})[c["season"]] = c["tag"]
        if c["after"] is None:
            bio["salaries"].pop(c["season"], None)
        else:
            bio["salaries"][c["season"]] = c["after"]
        notes = bio.get("cap_hold_notes") or {}
        if c["note"]:
            notes[c["season"]] = c["note"]
        else:
            notes.pop(c["season"], None)
        if notes or "cap_hold_notes" in bio:
            bio["cap_hold_notes"] = notes
        applied.append(c)
    if applied:
        save_player_bios(bios)
    return applied, stale


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", action="append", default=None, help="limit to this slug (repeatable)")
    ap.add_argument("--eaps", action="append", default=[],
                    help="above:SLUG or below:SLUG — Full Bird placeholder side (default below)")
    ap.add_argument("--apply", action="store_true", help="actually write; without it this is a dry run")
    a = ap.parse_args(argv)

    eaps = {}
    for item in a.eaps:
        side, _, slug = item.partition(":")
        if side not in ("above", "below") or not slug:
            ap.error(f"--eaps takes above:SLUG or below:SLUG, not {item!r}")
        eaps[slug] = side

    bios = load_player_bios()
    cap_levels = _load_json(CAP_LEVELS_FILE, {})
    changes, manual = build_plan(bios, cap_levels, set(a.player) if a.player else None, eaps)
    print_plan(bios, changes, manual)
    if not changes:
        return 0
    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to make the change.")
        return 0

    audit.begin_request("CLI", "repair_fa_holds.py")
    audit.set_actor(os.environ.get("SUDO_USER") or os.environ.get("USER") or "system")
    applied, stale = apply_plan(changes)
    print(f"\nApplied {len(applied)} change(s).")
    if stale:
        print(f"{len(stale)} skipped — moved since the plan was built; re-run:", file=sys.stderr)
        for c in stale:
            print(f"  {c['player']} {c['season']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

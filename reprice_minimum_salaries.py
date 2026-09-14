#!/usr/bin/env python3
"""Re-price § 3.12 minimum contracts to the current Minimum Salary Scale.

A minimum contract's salary in any season is *the applicable minimum for
that season*, not the dollar figure recorded when it was signed (§ 3.12,
rulebook `s3-12`) — the scale is revised every offseason, and every minimum
deal running through a revised season is worth the revised amount. Nothing
does that revision automatically; the recorded `salaries` figures only ever
move by hand. This is the tool that does it, on demand, reviewed.

Walks every player's `bio["contracts"]`, filtering to entries with
`signing_method == "minimum"` — never by salary level, since second-round
rookie-scale deals coincidentally sit at similar dollar figures and must
never be touched here. For each season still open on such a contract, it
recomputes what that season *should* pay (reusing the same helpers the
signing validator uses — `_min_salary_for` / `_one_year_min_cap_hit` in
`routers/transactions.py` — so this can never disagree with what a fresh
signing would be checked against) and compares it to what's on file.

Three guards, in the order they matter:

1. **Dry run by default.** Prints the full plan and stops; `--apply` is a
   separate, deliberate act.
2. **A season is only touched if it's still governed by the contract entry
   proposing the change** — if `bio["salaries"][season]` no longer matches
   that entry's own recorded figure for that season, something later
   (an extension, a re-sign) has superseded it, and this tool leaves it
   alone rather than guessing which one is right.
3. **The write is re-verified against disk immediately before it happens** —
   `bio["salaries"]` and `player-bios.json` have no lock between processes,
   so `--apply` reloads fresh and only writes a cell whose "before" value
   still matches what the plan was built from; anything that moved
   underneath is skipped with a note to re-run.

`player-bios.json` is already on the audited allowlist (`routers/audit.py`),
so every applied write gets a value-level diff appended to `edits.jsonl` for
free — no separate log to build. The script self-attributes to whoever ran
it (falling back to "system") rather than leaving every entry generic.

    # see what it would change, across every minimum contract in the league
    venv/bin/python reprice_minimum_salaries.py

    # narrow to one player while reviewing
    venv/bin/python reprice_minimum_salaries.py --player pedulla-sean

    # apply
    venv/bin/python reprice_minimum_salaries.py --apply

Deliberately not built: no hook on `PUT /api/cap-levels/{season}`, no timer.
A mutation triggered purely by editing a config value means a typo in
`/cap-settings` would silently re-price the league — this stays a manual,
reviewed step.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routers import audit                                              # noqa: E402
from routers.constants import CAP_LEVELS_FILE                          # noqa: E402
from routers.players import load_player_bios, save_player_bios         # noqa: E402
from routers.storage import _load_json, _parse_dollar, _season_start, _current_league_year  # noqa: E402
from routers.transactions import _min_salary_for, _one_year_min_cap_hit  # noqa: E402

EXEMPT_TYPES = {"two-way", "dead"}


def _fmt_dollar(amount: int) -> str:
    return f"${amount:,}"


def _contract_target(bio: dict, season: str, cap_levels: dict, entry: dict):
    """The one figure `season` should pay under this contract entry, reusing
    the exact helpers the signing validator checks against. `entry` is a
    plain dict from `bio["contracts"]`, but both helpers read the tier via
    `getattr(contract, "years_experience", None)` — a plain dict never has
    that attribute, so it must be wrapped or the persisted value is silently
    ignored and every contract falls through to the draft_year proxy."""
    contract = SimpleNamespace(years_experience=entry.get("years_experience"))
    is_one_year = len(entry.get("salaries") or {}) == 1
    fn = _one_year_min_cap_hit if is_one_year else _min_salary_for
    return fn(bio, season, cap_levels, contract)


def build_plan(bios: dict, cap_levels: dict, players: set[str] | None = None):
    """Read-only. Returns (changes, skips) — everything needed to both print
    and, on --apply, write."""
    current = _current_league_year()
    changes = []
    skips = []
    for slug, bio in bios.items():
        if players and slug not in players:
            continue
        if (bio.get("type") or "") in EXEMPT_TYPES:
            continue
        for idx, entry in enumerate(bio.get("contracts") or []):
            if entry.get("signing_method") != "minimum":
                continue
            salaries = entry.get("salaries") or {}
            hold_seasons = {s for s, t in (entry.get("cap_holds") or {}).items()
                            if t in ("UFA", "RFA")}
            for season in sorted(salaries, key=_season_start):
                if _season_start(season) < _season_start(current):
                    continue  # history — never rewritten
                raw = salaries[season]
                if season in hold_seasons:
                    # The same season is tagged as a trailing FA hold on this same
                    # contract. A real minimum year and a hold placeholder can't
                    # both be true of one season — _autofill_fa_hold_amounts skips
                    # auto-pricing the hold whenever `salaries` already has an
                    # entry here, so an explicit figure in this spot (often a
                    # stray "$1"/"1" stub — see bagley-marvin 27-28) usually means
                    # the hold never got priced, not that this is a real § 3.12
                    # year. Too ambiguous to guess at — skip for a human.
                    skips.append({
                        "player": slug, "season": season,
                        "reason": f"this contract also tags {season} as a trailing "
                                  f"{entry['cap_holds'][season]} hold — ambiguous "
                                  f"whether {raw!r} is a real contract year or an "
                                  f"unpriced hold placeholder; not touching it",
                    })
                    continue
                live = (bio.get("salaries") or {}).get(season)
                if live != raw:
                    skips.append({
                        "player": slug, "season": season,
                        "reason": f"superseded — current salary is {live!r}, not this "
                                  f"contract's {raw!r}",
                    })
                    continue
                target = _contract_target(bio, season, cap_levels, entry)
                if target is None:
                    skips.append({
                        "player": slug, "season": season,
                        "reason": "can't establish an experience tier (no declared "
                                  "years_experience, no draft_year, or no scale configured)",
                    })
                    continue
                before_amt = _parse_dollar(raw)
                if before_amt == target:
                    continue
                basis = ("declared years_experience" if entry.get("years_experience") is not None
                         else "draft_year proxy")
                changes.append({
                    "player": slug, "contract_idx": idx, "season": season,
                    "before": raw, "after": _fmt_dollar(target), "basis": basis,
                })
    return changes, skips


def print_plan(bios: dict, changes: list, skips: list) -> None:
    if not changes:
        print("No minimum contracts need re-pricing.")
    for c in changes:
        name = (bios.get(c["player"]) or {}).get("name") or c["player"]
        print(f"  {name} ({c['player']})  {c['season']}: {c['before']} -> {c['after']}"
              f"  [{c['basis']}]")
    if skips:
        print(f"\n{len(skips)} season(s) skipped:")
        for s in skips:
            print(f"  {s['player']} {s['season']}: {s['reason']}")


def apply_plan(changes: list) -> tuple[list, list]:
    """Reloads player-bios.json fresh and only writes a cell whose current
    value still matches what the plan above was built from. Everything that
    still matches is applied in one save (one audit-log entry)."""
    bios = load_player_bios()
    applied, stale = [], []
    for c in changes:
        bio = bios.get(c["player"])
        entries = (bio or {}).get("contracts") or []
        if not bio or c["contract_idx"] >= len(entries):
            stale.append(c)
            continue
        entry = entries[c["contract_idx"]]
        live = (bio.get("salaries") or {}).get(c["season"])
        entry_salary = (entry.get("salaries") or {}).get(c["season"])
        if live != c["before"] or entry_salary != c["before"]:
            stale.append(c)
            continue
        bio["salaries"][c["season"]] = c["after"]
        entry["salaries"][c["season"]] = c["after"]
        applied.append(c)
    if applied:
        save_player_bios(bios)
    return applied, stale


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", action="append", default=None,
                    help="limit to this player slug (repeatable)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    a = ap.parse_args(argv)

    bios = load_player_bios()
    cap_levels = _load_json(CAP_LEVELS_FILE, {})
    players = set(a.player) if a.player else None

    changes, skips = build_plan(bios, cap_levels, players)
    print_plan(bios, changes, skips)

    if not changes:
        return 0

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to make the change.")
        return 0

    audit.begin_request("CLI", "reprice_minimum_salaries.py")
    audit.set_actor(os.environ.get("SUDO_USER") or os.environ.get("USER") or "system")
    applied, stale = apply_plan(changes)
    print(f"\nApplied {len(applied)} change(s).")
    if stale:
        print(f"\n{len(stale)} change(s) skipped — value moved since the plan was built; "
              f"re-run to pick them up:", file=sys.stderr)
        for c in stale:
            print(f"  {c['player']} {c['season']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

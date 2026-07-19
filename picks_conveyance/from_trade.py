"""Translate a trade's flat protection/swap fields into real conveyance
structures, registered at write time (not guessed later from the CSV).

Called from transactions.py's `_apply_trade` the moment a trade sets
`asset.protection` or `asset.swap_with` on a pick — this is the only place
`from_team`/`to_team` are known with certainty; the flat CSV afterward only
remembers the current owner, not who it came from.

Direction conventions (verified against real, note-documented rows before
encoding — see nbn-api/docs/picks-migration-worksheet.md "write-path
auto-derivation"):
  - protection: keeper if it stays protected = `from_team` (whoever held the
    pick before this trade); conveys to `to_team` beyond the threshold. Matches
    the model rule confirmed throughout this project's reconciliation.
  - swap: the *named* team (`asset.swap_with`) gets the more favorable of the
    two picks; `to_team` (this trade's receiving side) gets the less favorable.
    Verified against the 2031 HOU/IND/MIN row ("HOU holds the right to swap...
    whichever is more favorable to HOU") in both branches before shipping.
"""
from __future__ import annotations

from . import registry


def register_protection(pick_key: tuple, from_team: str, to_team: str,
                        threshold: int) -> None:
    """Brand new protection (no existing structure): spans the whole pick with
    a simple 2-band split. If the pick already carries real protected
    structure — e.g. a third team's claim from an earlier, unrelated trade —
    defer to subdivide_protected_band instead, so that earlier claim survives
    and only the band from_team actually holds gets split."""
    if registry.get_protected_spec(pick_key) is not None:
        registry.subdivide_protected_band(pick_key, from_team, to_team, threshold)
        return
    year, rnd, orig = pick_key
    on = {"year": year, "round": rnd, "orig": orig}
    bands = [{"min": 1, "max": threshold, "to": from_team},
             {"min": threshold + 1, "max": 60, "to": to_team}]
    registry.add_protected(pick_key, on=on, bands=bands)


def register_ladder(pick_key: tuple, from_team: str, to_team: str,
                    protect_top: int,
                    fallback_picks: list[tuple] | None = None) -> None:
    """Register a protection ladder — a compensation trigger layered on top
    of a pick's own conveyance, not a replacement for it. If the pick's real
    recipient (whether decided by its own protected/swap/binary node or a
    plain settled owner) doesn't convey to `to_team` because it lands within
    `protect_top`, `to_team` is instead owed `fallback_picks` (a list of
    (year, round, orig) tuples) as compensation.

    `pick_key`'s `orig` is threaded through as the step's explicit `orig` —
    see resolver._resolve_ladders — since the pick being protected here may
    have originated from a different team than `from_team` (e.g. `from_team`
    holds only a share of a pick some third team originated)."""
    year, rnd, orig = pick_key
    ladder = {
        "id": f"ladder_trade_{year}_{rnd}_{orig}_{to_team}",
        "from": from_team, "to": to_team,
        "steps": [{"year": year, "round": rnd, "orig": orig, "protect_top": protect_top}],
        "fallback": ({"type": "fixed_asset",
                      "picks": [{"year": y, "round": r, "orig": o}
                               for (y, r, o) in fallback_picks]}
                     if fallback_picks else None),
    }
    registry.add_ladder(ladder)


def _find_counterpart(pick_key: tuple, swap_with: str, picks_snapshot: list[dict]):
    """The pick swap_with currently owns for the same year/round — mirrors the
    lookup enrich_swap_conveys does at display time (routers/roster_picks.py).
    Returns a (year, round, orig) tuple, or None if no such pick is found."""
    year, rnd, orig = pick_key
    for p in picks_snapshot:
        if (int(p["YEAR"]) == year and int(p["ROUND"]) == rnd
                and (p.get("OWNER") or "").upper() == swap_with
                and p["ORIG"].upper() != orig):
            return (int(p["YEAR"]), int(p["ROUND"]), p["ORIG"].upper())
    return None


def register_swap(pick_key: tuple, to_team: str, swap_with: str,
                  picks_snapshot: list[dict]) -> bool:
    """Register a 2-team SwapGroup: swap_with gets the better pick, to_team
    gets the worse. Returns True if a counterpart pick was found and
    registered, False if not (caller falls back to flat passthrough, same as
    before this feature existed — never silently drops data)."""
    counterpart = _find_counterpart(pick_key, swap_with, picks_snapshot)
    if counterpart is None:
        return False
    year, rnd, orig = pick_key
    members = [{"year": year, "round": rnd, "orig": orig},
               {"year": counterpart[0], "round": counterpart[1], "orig": counterpart[2]}]
    gid = f"sg_auto_{year}_{rnd}_{'_'.join(sorted([orig, counterpart[2]]))}"
    registry.add_swap_group(gid, members=members, priority=[swap_with, to_team])
    return True

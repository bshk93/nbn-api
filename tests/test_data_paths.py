"""Every file path the API reads at request time actually exists.

Written 2026-08-19 after a live outage: the 2026-08-18 data move deleted the
149 symlinks in the site repo that made build output reachable at
`/home/skim/projects/nbn-today/players/...`, and five routers still read those
paths. `/perry` and `/poeltl` 500'd, and every Discord stats command with them,
for a day before a member reported it.

The move was verified by checking that all 21 URL families still served — which
they did, because nginx reads the new location. Nothing checked what the API
reads from **disk**, and that is exactly what broke. This closes that gap: it
imports each router and resolves its module-level data paths.

Deliberately checks existence rather than mocking anything, because the failure
was environmental, not logical. It runs against the real data directory and
skips cleanly when there isn't one.
"""

import sys
from pathlib import Path

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


DATA_DIR = Path("/var/lib/nothing-but-stats")
if not DATA_DIR.is_dir():
    print("  [skip] no data directory on this box")
    sys.exit(0)

from routers import constants, discord, misc, perry, poeltl, waivers  # noqa: E402

print("build output the API reads at request time")
for label, path in [
    ("perry: player seasons", perry.PLAYER_SEASONS_CSV),
    ("poeltl: player seasons", poeltl.PLAYER_SEASONS_CSV),
    ("misc: standings history", misc.NBN_STANDINGS_CSV),
    ("waivers: standings history", waivers.STANDINGS_CSV),
    ("discord /player: seasons", discord.SEASONS_CSV),
    ("discord /player: playoffs", discord.PLAYOFFS_CSV),
    ("discord /player: awards", discord.AWARDS_CSV),
    ("discord /standings", discord.STANDINGS_CSV),
    ("discord /standings: brackets", discord.BRACKETS_CSV),
    ("discord /h2h", discord.H2H_CSV),
    ("discord /h2h: playoffs", discord.H2H_PO_CSV),
]:
    check(f"{label} -> {path}", Path(path).exists())

print("\nleague state the API writes and reads back")
for label, path in [
    ("a team roster", DATA_DIR / "atl-roster.csv"),
    ("a team's season history", constants.DERIVED_DIR / "data" / "atl-seasons.csv"),
    ("player bios", constants.PLAYER_BIOS_FILE),
]:
    check(f"{label} -> {path}", Path(path).exists())

print("\nthe two directories are not the same thing")
check("DERIVED_DIR is build output, under the data directory",
      constants.DERIVED_DIR == DATA_DIR / "derived")
check("no router reads through the site repo any more",
      not any("nbn-today" in str(p) for p in
              (perry.PLAYER_SEASONS_CSV, poeltl.PLAYER_SEASONS_CSV,
               misc.NBN_STANDINGS_CSV, waivers.STANDINGS_CSV, discord.SEASONS_CSV)))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

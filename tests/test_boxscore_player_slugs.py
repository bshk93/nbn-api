"""`GET /api/boxscores` — every player line carries its slug and photo.

The raw box score rows hold a name and no slug, so the endpoint used to read a
`SLUG` column that does not exist and sent `""` for every player. Nothing
could link a box score line to a player page. The slug now comes from the bio
whose name matches, the same match `/api/players/{slug}/gamelog` makes.

    venv/bin/python -m tests.test_boxscore_player_slugs
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import boxscores  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


boxscores.load_player_bios = lambda: {
    "curry-stephen": {"name": "CURRY, STEPHEN", "photo_url": "https://x/curry.png"},
    "no-name": {"name": ""},
}
bios = boxscores._bios_by_name()

print("a line is matched to its bio by name")
row = boxscores._boxscore_player_row({"PLAYER": "CURRY, STEPHEN", "P": "30"}, bios)
check("slug", row["slug"] == "curry-stephen")
check("photo", row["photo_url"] == "https://x/curry.png")

print("case and stray spaces do not break the match")
row = boxscores._boxscore_player_row({"PLAYER": " Curry, Stephen "}, bios)
check("slug", row["slug"] == "curry-stephen")

print("an unknown name gets empty strings, not an error")
row = boxscores._boxscore_player_row({"PLAYER": "NOBODY, AT ALL"}, bios)
check("slug", row["slug"] == "")
check("photo", row["photo_url"] == "")

print("a bio with no name is not indexed")
check("no empty key", "" not in bios)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall checks passed")

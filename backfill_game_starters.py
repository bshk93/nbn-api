"""Record starters for games committed before game-starters.jsonl existed.

Opening week of 26-27 was committed before box score commits recorded who
started (routers/game_starters.py). This reads each such game's box score and
writes a line from every team's entered coaching settings *as they stand now*,
marked `reconstructed: true` — right as long as the team hasn't changed its
entered five since, which is why this is for the first few nights only and not
something to run on old seasons.

Dry run by default; prints what it would write.

    venv/bin/python backfill_game_starters.py            # show
    venv/bin/python backfill_game_starters.py --apply    # write
"""
from __future__ import annotations

import argparse
import json
import sys

from routers.constants import PLAYER_BIOS_FILE
from routers.coaching_settings import load_coaching_settings
from routers.storage import read_csv, _current_league_year
from routers import game_starters as gs
from routers.boxscores import allstats_path


def committed_games(season: str) -> list[dict]:
    """Every game in the season's raw files: date, type, teams, and each
    side's {slug, min} rows (slug matched from the bio name)."""
    bios = json.loads(PLAYER_BIOS_FILE.read_text())
    slug_of = {}
    for slug, b in bios.items():
        slug_of.setdefault((b.get("name") or "").strip().upper(), slug)
    games: dict[tuple, dict] = {}
    for gtype in ("REG", "PLAYOFF"):
        path = allstats_path(season, gtype)
        if not path.exists():
            continue
        _, rows = read_csv(path)
        for r in rows:
            opp = r.get("OPP", "").strip()
            team = r.get("TEAM", "").strip()
            other = r.get("OPP_RAW", opp).strip().lstrip("@")
            home, away = (team, other) if not opp.startswith("@") else (other, team)
            key = (r["DATE"], gtype, home, away)
            g = games.setdefault(key, {"date": r["DATE"], "game_type": gtype, "home_team": home,
                                       "away_team": away, "home_rows": [], "away_rows": []})
            row = {"slug": slug_of.get(r.get("PLAYER", "").strip().upper()), "min": int(r.get("M") or 0)}
            (g["home_rows"] if team == home else g["away_rows"]).append(row)
    return sorted(games.values(), key=lambda g: g["date"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    season = args.season or _current_league_year()

    have = {(e["date"], e["game_type"], *sorted((e["home_team"], e["away_team"]))) for e in gs.load(season)}
    coaching = load_coaching_settings()
    todo = [g for g in committed_games(season)
            if (g["date"], g["game_type"], *sorted((g["home_team"], g["away_team"]))) not in have]
    if not todo:
        print(f"{season}: every committed game already has a starters line.")
        return 0
    for g in todo:
        e = gs.build_entry(season=season, coaching=coaching, reconstructed=True, **g)
        bits = []
        for team, t in e["teams"].items():
            if t["starters"]:
                bits.append(f"{team} {','.join(t['starters'])}"
                            + (f" FLAGS {t['flags']}" if t["flags"] else "")
                            + (" (entered after game)" if t["entered_after_game"] else ""))
            else:
                bits.append(f"{team} unknown (never entered)")
        print(f"{g['date']} {g['away_team']}@{g['home_team']}: " + " | ".join(bits))
        if args.apply:
            gs.append(e)
    print(f"\n{len(todo)} game(s) {'written' if args.apply else 'would be written — rerun with --apply'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

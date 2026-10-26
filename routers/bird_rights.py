"""§ 3.8 Bird rights on every rostered player's free-agent hold, for team and
player pages.

Only a signing made through the transaction path records a hold's tier
(`bird_tiers` on the bio), which is a few dozen of ~500 holds. The rest are
derived here, read-only, by the same tenure walk the validators use
(`_bird_tenure`), so a page can never show a tier the office wouldn't accept.

Each hold says how its tier is known, because the answers are not equally
good:

- `record`: stored by the signing that created the hold.
- `ledger`: derived from a signing on the transaction ledger.
- `draft`: derived from the draft, assuming no gap the ledger doesn't show.
- `trade_floor`: acquired by trade with no earlier record, so the tier is a
  lower bound.
- `unknown`: nothing on file. `tier` is null. This is never Non-Bird: a
  player with no record is usually a long-tenured one.
"""

from typing import Optional

from fastapi import APIRouter, Query

from .constants import DATA_DIR
from .players import load_player_bios
from .storage import _current_league_year, _season_start, read_csv
from .transactions import _FA_HOLD_TYPES, _bird_tenure

router = APIRouter()


def _rosters() -> dict[str, str]:
    """slug -> team abbr, from every roster file."""
    out = {}
    for path in sorted(DATA_DIR.glob("*-roster.csv")):
        team = path.name.split("-")[0].upper()
        _, rows = read_csv(path)
        for r in rows:
            slug = (r.get("SLUG") or "").strip()
            if slug:
                out[slug] = team
    return out


def bird_rights_for(slug: str, team: str, bio: dict, cur: str) -> dict:
    """{season: {type, tier, source, seasons, evidence}} for each UFA/RFA hold
    from the current season on."""
    out = {}
    stored = bio.get("bird_tiers") or {}
    for season, hold in (bio.get("cap_holds") or {}).items():
        if hold not in _FA_HOLD_TYPES or _season_start(season) < _season_start(cur):
            continue
        t = _bird_tenure(slug, team, season, bio)
        tier, source = t["tier"], t["basis"] if t["tier"] else "unknown"
        if stored.get(season):
            tier, source = stored[season], "record"
        out[season] = {"type": hold, "tier": tier, "source": source,
                       "seasons": t["seasons"], "evidence": t["evidence"]}
    return out


@router.get("/api/bird-rights")
def get_bird_rights(team: Optional[str] = Query(None), player: Optional[str] = Query(None)):
    """Public. `{players: {slug: {team, holds: {season: {...}}}}}` for every
    rostered player with a hold, narrowed by `team` or `player`."""
    bios = load_player_bios()
    cur = _current_league_year()
    team = team.upper() if team else None
    players = {}
    for slug, t in _rosters().items():
        if (team and t != team) or (player and slug != player):
            continue
        holds = bird_rights_for(slug, t, bios.get(slug) or {}, cur)
        if holds:
            players[slug] = {"team": t, "holds": holds}
    return {"players": players}

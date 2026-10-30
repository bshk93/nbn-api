"""Possession-based team stats: pace, offensive/defensive rating, Four Factors.

    python3 -m stats_build.advanced              # write into NBN_OUT_DIR
    python3 -m stats_build.advanced --dry-run    # print a summary, write nothing

One output, under `derived/`:

- `data/team-advanced.csv`: one row per team per regular season, plus a
  league row per season (`TEAM` is `LG`) for context.

**Regular season only, for now.** The playoff box scores don't look like the
regular season's, and not consistently: offensive rebounds are 22-24% of all
rebounds in every regular season, but 8-18% in the playoffs, varying by year
(8% in 26, where 69% of 20+ minute lines have none). Turnovers swing the same
way. Whether that is how playoff games play or a parse that credits offensive
rebounds as defensive is not knowable from the rows, so playoff Four Factors
are left out rather than published wrong. `TYPE` is kept so they can be added.

**Possessions are an estimate.** The raw box scores are player rows only, so
team turnovers (shot-clock and 8-second violations) and team rebounds are not
in them. Team rebounds don't matter: the possession formula only uses
offensive rebounds, and Basketball-Reference also counts player-credited ones
there. Missing team turnovers mean possessions run slightly low, so pace reads
a little low and ratings a little high, for every team alike. Checked on every
season: the two sides' estimates for one game differ by about 2, with no bias
toward either side. Each game's count is the average of both sides' estimates,
as Basketball-Reference does, which cuts that noise.

Per team-game, from the summed player rows:

    poss  = FGA + 0.4*FTA - 1.07 * ORB/(ORB + opp DRB) * (FGA - FGM) + TOV
    game  = (poss + opp poss) / 2
    pace  = 48 * possessions / (team minutes / 5)
    ORtg  = 100 * points / possessions,   DRtg = 100 * allowed / possessions

Four Factors (Dean Oliver, Basketball-Reference's definitions):
eFG% = (FGM + 0.5*3PM)/FGA, TOV% = TOV/(FGA + 0.44*FTA + TOV),
ORB% = ORB/(ORB + opp DRB), FT rate = FTM/FGA. The defensive four are the
opponent's eFG%, TOV% and FT rate, and DRB% = DRB/(DRB + opp ORB).

Not part of `pipeline.build` on purpose: the port harness diffs that tree
against the dormant R build, and R never wrote this. `build.sh` runs it as its
own step, and a failure here does not fail the stats build.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from stats_build import csvio, pipeline
from stats_build.buildargs import DATA_DIR, OUT_DIR, BuildArgs

OUTPUT = "data/team-advanced.csv"
LEAGUE = "LG"

# Summed from player rows. `M` is minutes, which gives the game's length (240
# in regulation, more with overtime).
BOX = ("M", "P", "FGM", "FGA", "3PM", "3PA", "FTM", "FTA", "OR", "DR", "TO")

HEADER = ["TEAM", "SEASON", "TYPE", "G", "MIN", "POSS", "PACE",
          "ORTG", "DRTG", "NRTG",
          "EFG", "TOV_PCT", "ORB_PCT", "FT_RATE",
          "OPP_EFG", "OPP_TOV_PCT", "DRB_PCT", "OPP_FT_RATE"]


def _box_totals(rows: list[dict]) -> dict[tuple[str, str, str, str], dict[str, float]]:
    """(season, date, team, opp) -> that team's summed box score for the game."""
    out: dict = defaultdict(lambda: dict.fromkeys(BOX, 0.0))
    for r in rows:
        team, opp = r.get("TEAM"), (r.get("OPP") or "").replace("@", "")
        if not team or not opp:
            continue
        t = out[(r["SEASON"], r["DATE"], team, opp)]
        for col in BOX:
            v = pipeline._stat(r, col)
            if v is not None:
                t[col] += v
    return out


def _side_poss(own: dict, opp: dict) -> float:
    reb = own["OR"] + opp["DR"]
    orb_share = own["OR"] / reb if reb else 0.0
    return (own["FGA"] + 0.4 * own["FTA"]
            - 1.07 * orb_share * (own["FGA"] - own["FGM"]) + own["TO"])


def team_games(rows: list[dict]) -> list[dict]:
    """One record per team per game: own box, opponent box, possessions.

    A game whose opponent has no rows is dropped (it has no defense to
    measure). Returned so the player stats can divide by the same possessions.
    """
    boxes = _box_totals(rows)
    out = []
    for (season, date, team, opp), own in boxes.items():
        theirs = boxes.get((season, date, opp, team))
        if theirs is None:
            continue
        poss = (_side_poss(own, theirs) + _side_poss(theirs, own)) / 2
        out.append({"SEASON": season, "DATE": date, "TEAM": team, "OPP": opp,
                    "own": own, "opp": theirs, "POSS": poss})
    return out


def _ratio(num: float, den: float, digits: int) -> float | None:
    return round(num / den, digits) if den else None


def _summarize(team: str, season: str, kind: str, games: list[dict]) -> list:
    own = dict.fromkeys(BOX, 0.0)
    opp = dict.fromkeys(BOX, 0.0)
    poss = 0.0
    for g in games:
        poss += g["POSS"]
        for col in BOX:
            own[col] += g["own"][col]
            opp[col] += g["opp"][col]
    # A league row sums both sides of every game, so its games, minutes and
    # possessions are doubled. Rates are unaffected; the counts divide it out.
    d = 2 if team == LEAGUE else 1
    minutes = own["M"]
    ortg = _ratio(100 * own["P"], poss, 1)
    drtg = _ratio(100 * opp["P"], poss, 1)
    return [
        team, season, kind, len(games) // d, round(minutes / d), round(poss / d),
        _ratio(48 * poss, minutes / 5, 1),
        ortg, drtg,
        round(ortg - drtg, 1) if ortg is not None and drtg is not None else None,
        _ratio(own["FGM"] + 0.5 * own["3PM"], own["FGA"], 3),
        _ratio(own["TO"], own["FGA"] + 0.44 * own["FTA"] + own["TO"], 3),
        _ratio(own["OR"], own["OR"] + opp["DR"], 3),
        _ratio(own["FTM"], own["FGA"], 3),
        _ratio(opp["FGM"] + 0.5 * opp["3PM"], opp["FGA"], 3),
        _ratio(opp["TO"], opp["FGA"] + 0.44 * opp["FTA"] + opp["TO"], 3),
        _ratio(own["DR"], own["DR"] + opp["OR"], 3),
        _ratio(opp["FTM"], opp["FGA"], 3),
    ]


def _labelled_rows(data_dir: Path) -> list[tuple[str, list[dict]]]:
    """(REG | PLAYOFF, rows with SEASON set to the bare "YY-YY")."""
    out = []
    for playoffs, kind in ((False, "REG"),):
        for label, rows in pipeline.load_allstats(data_dir, playoffs=playoffs):
            season = label.replace(" Playoffs", "")
            for r in rows:
                r["SEASON"] = season
            out.append((kind, rows))
    return out


def team_advanced(data_dir: Path) -> list[list]:
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for kind, rows in _labelled_rows(data_dir):
        for g in team_games(rows):
            by_key[(g["SEASON"], kind, g["TEAM"])].append(g)
            by_key[(g["SEASON"], kind, LEAGUE)].append(g)
    order = {"REG": 0, "PLAYOFF": 1}
    keys = sorted(by_key, key=lambda k: (k[0], order[k[1]], k[2] == LEAGUE, k[2]))
    return [_summarize(team, season, kind, by_key[(season, kind, team)])
            for season, kind, team in keys]


def write(out_dir: Path, data_dir: Path) -> list[str]:
    csvio.write_csv(Path(out_dir) / OUTPUT, HEADER, team_advanced(Path(data_dir)))
    return [OUTPUT]


def summary(rows: list[list]) -> str:
    col = {c: i for i, c in enumerate(HEADER)}
    lines = []
    for r in rows:
        if r[col["TEAM"]] == LEAGUE:
            lines.append(f"  {r[col['SEASON']]} {r[col['TYPE']]:<7} pace {r[col['PACE']]}  "
                         f"ORtg {r[col['ORTG']]}  eFG {r[col['EFG']]}  TOV% {r[col['TOV_PCT']]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stats_build.advanced", description=__doc__.split("\n")[0])
    ap.add_argument("--season", help="unused; every season is built. Accepted for symmetry")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    a = ap.parse_args(argv)
    BuildArgs.resolve(a.season, None)
    if a.dry_run:
        print(summary(team_advanced(a.data_dir)))
        return 0
    written = write(a.out, a.data_dir)
    print(f"advanced: wrote {', '.join(written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

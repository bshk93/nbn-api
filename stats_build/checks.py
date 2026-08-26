"""Value-level checks over the whole raw box score corpus.

The Sheets-era R build ran `check_allstats()` on every refresh
(`refresh/refresh-utils.R` in the retired `nothing-but-stats` checkout): per-team
minutes had to land on a legal total, every row had to satisfy the points
identity and the made-vs-attempted bounds, and no required field could be blank.
Those did not survive the move to `allstats-*.csv` + the Python port. What is
left is `build/smoke_test.py`, which asserts the *schema* of the 86 derived files
and no values at all, and `build/validate_boxscore.py`, which sees exactly one
game at parse time and only when a human runs the skill. A game typed in by hand,
a row edited on disk, and all 157k rows already committed get nothing.

So this restores the three original checks and adds three more that the original
set implies but never made — and the additions are not hypothetical. Run against
the corpus for the first time on 2026-08-26, the arithmetic was spotless (0
violations in 157,430 rows), and the two new ones found four real errors that had
been sitting in the data for months:

  * **`unknown_player`** — `PLAYER_FIXES` in `pipeline.py` is a hand-maintained
    alias table with nothing asserting it is complete. `RILEY, WENDELL` (GSW,
    2026-05-11) was not in it, so the build minted a `riley-wendell` slug and
    published a player who does not exist into
    `players/player_seasons_playoffs.csv`.
  * **`duplicate_player`** — three team-games in 24-25 list the same player
    twice with *different* stat lines. Minutes and points still reconcile, so
    every other check passes: one of the two rows is a different player under
    the wrong name.

Both are the same underlying failure — a name that is wrong rather than a number
that is wrong — and neither is visible to any per-game check, because each row
is individually perfectly legal.

Pure functions over rows: no I/O, no argparse, no Discord. `check_stats_integrity.py`
runs them weekly and owns the alerting; keeping them here means they can be
tested directly, which is what `tests/test_stats_checks.py` does.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

# Imported rather than restated: an alias table that disagreed with the build's
# would report violations the build had already fixed, or miss ones it had not.
from stats_build.pipeline import PLAYER_FIXES

# A team plays 240 player-minutes in regulation and 25 more per overtime period.
# The R check hardcoded exactly these four; a fifth OT has never been seen.
LEGAL_TEAM_MINUTES = (240, 265, 290, 315)

# Blank in any of these makes the row unusable. R's `bad_missing` list, verbatim.
REQUIRED = ("DATE", "PLAYER", "M", "P", "R", "A", "S", "B", "TO",
            "FGA", "FGM", "3PA", "3PM", "FTM", "FTA", "PF")

VALID_TEAMS = frozenset("""
ATL BKN BOS CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK
OKC ORL PHI PHX POR SAC SAS TOR UTA WAS
""".split())


@dataclass(frozen=True)
class Finding:
    """One violation, with enough to find it in the file by hand."""
    category: str
    where: str      # "allstats-24-25.csv DEN 2024-10-29"
    detail: str

    def __str__(self) -> str:
        return f"{self.category}: {self.where} — {self.detail}"


def _int(row: dict, col: str):
    """A stat cell as an int, or None when blank/NA/not a number."""
    raw = (row.get(col) or "").strip()
    if raw in ("", "NA"):
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _game_key(row: dict) -> tuple:
    return (row.get("TEAM") or "", row.get("DATE") or "", row.get("OPP") or "")


def _where(filename: str, row: dict) -> str:
    return f"{filename} {row.get('TEAM','?')} {row.get('DATE','?')}"


def check_rows(filename: str, rows: list[dict]) -> list[Finding]:
    """Per-row arithmetic and bounds — R's `bad_sanity_checks` and `bad_missing`."""
    out = []
    for r in rows:
        missing = [c for c in REQUIRED if _int(r, c) is None
                   and not (r.get(c) or "").strip()]
        if missing:
            out.append(Finding("missing_data", _where(filename, r),
                               f"{r.get('PLAYER','?')}: blank {', '.join(missing)}"))
            continue

        p, reb, orb, drb = (_int(r, "P"), _int(r, "R"), _int(r, "OR"), _int(r, "DR"))
        fgm, fga = _int(r, "FGM"), _int(r, "FGA")
        tpm, tpa = _int(r, "3PM"), _int(r, "3PA")
        ftm, fta = _int(r, "FTM"), _int(r, "FTA")
        pf = _int(r, "PF")
        if None in (p, reb, fgm, fga, tpm, tpa, ftm, fta, pf):
            out.append(Finding("missing_data", _where(filename, r),
                               f"{r.get('PLAYER','?')}: unparseable stat cell"))
            continue

        bad = []
        # R checked `P != FTM + 2*FGM + 3PM`, which is the same identity the
        # per-game validator writes as (FGM-3PM)*2 + 3PM*3 + FTM.
        if p != ftm + 2 * fgm + tpm:
            bad.append(f"PTS {p} != FTM {ftm} + 2*FGM {fgm} + 3PM {tpm}")
        if fgm > fga:
            bad.append(f"FGM {fgm} > FGA {fga}")
        if tpm > tpa:
            bad.append(f"3PM {tpm} > 3PA {tpa}")
        if tpm > fgm:
            bad.append(f"3PM {tpm} > FGM {fgm}")
        if ftm > fta:
            bad.append(f"FTM {ftm} > FTA {fta}")
        if pf > 6:
            bad.append(f"PF {pf} > 6")
        if orb is not None and orb > reb:
            bad.append(f"OR {orb} > R {reb}")
        if drb is not None and drb > reb:
            bad.append(f"DR {drb} > R {reb}")
        if orb is not None and drb is not None and orb + drb != reb:
            bad.append(f"OR {orb} + DR {drb} != R {reb}")

        if bad:
            out.append(Finding("bad_arithmetic", _where(filename, r),
                               f"{r.get('PLAYER','?')}: " + "; ".join(bad)))
    return out


def check_games(filename: str, rows: list[dict]) -> list[Finding]:
    """Team-game level: minutes, the team score, W/L, and duplicate players."""
    out = []
    games = defaultdict(list)
    for r in rows:
        games[_game_key(r)].append(r)

    for (team, date, opp), game in sorted(games.items()):
        where = f"{filename} {team} {date} vs {opp}"

        minutes = sum(_int(r, "M") or 0 for r in game)
        if minutes not in LEGAL_TEAM_MINUTES:
            out.append(Finding("bad_minutes", where,
                               f"team minutes {minutes}, expected one of "
                               f"{', '.join(map(str, LEGAL_TEAM_MINUTES))}"))

        # A name appearing twice in one team-game is not a duplicated row — the
        # minutes and points still reconcile — it is one player entered under
        # another's name. Nothing else catches it: both lines are legal.
        names = Counter((r.get("PLAYER") or "").strip() for r in game)
        for name, n in sorted(names.items()):
            if n > 1:
                lines = "; ".join(
                    f"{_int(r,'M')}min/{_int(r,'P')}pt"
                    for r in game if (r.get("PLAYER") or "").strip() == name)

                out.append(Finding("duplicate_player", where,
                                   f"{name} appears {n} times ({lines}) — one of "
                                   f"these is a different player under the wrong name"))

        team_pts = _int(game[0], "TEAM_PTS")
        opp_pts = _int(game[0], "OPP_TEAM_PTS")
        scored = sum(_int(r, "P") or 0 for r in game)
        if team_pts is not None and scored != team_pts:
            out.append(Finding("score_mismatch", where,
                               f"players sum to {scored} points, TEAM_PTS says {team_pts}"))
        if team_pts is not None and opp_pts is not None:
            wl = (game[0].get("WL") or "").strip()
            if team_pts == opp_pts:
                out.append(Finding("tied_game", where,
                                   f"TEAM_PTS == OPP_TEAM_PTS == {team_pts}"))
            elif wl in ("W", "L") and wl != ("W" if team_pts > opp_pts else "L"):
                out.append(Finding("bad_wl", where,
                                   f"WL is {wl} but the score is {team_pts}-{opp_pts}"))

        for col in ("TEAM", "OPP_TEAM"):
            val = (game[0].get(col) or "").strip().upper().lstrip("@")
            if val and val not in VALID_TEAMS:
                out.append(Finding("bad_team", where, f"{col} is {val!r}"))
    return out


def check_players(filename: str, rows: list[dict], bio_names: set[str]) -> list[Finding]:
    """Every PLAYER must resolve to a real bio once PLAYER_FIXES is applied.

    This is the check that guards the alias table. Without it, a misspelling is
    indistinguishable from a new player: the build slugifies whatever string it
    finds, and a phantom appears in the derived output with a career of its own.
    """
    out = []
    seen = Counter()
    for r in rows:
        raw = (r.get("PLAYER") or "").strip()
        if not raw:
            continue
        fixed = PLAYER_FIXES.get(raw, raw)
        if fixed.upper() not in bio_names:
            seen[raw] += 1
    for name, n in seen.most_common():
        out.append(Finding(
            "unknown_player", filename,
            f"{name!r} ({n} row{'s' if n != 1 else ''}) matches no player bio. "
            f"Either add it to PLAYER_FIXES in stats_build/pipeline.py, or fix "
            f"the rows — the build will otherwise publish it as a real player."))
    return out


def check_corpus(files: list[tuple[str, list[dict]]],
                 bio_names: set[str] | None = None) -> list[Finding]:
    """Every check over every file. `files` is [(filename, rows), ...]."""
    out = []
    for filename, rows in files:
        out += check_rows(filename, rows)
        out += check_games(filename, rows)
        if bio_names is not None:
            out += check_players(filename, rows, bio_names)
    return out


def summarize(findings: list[Finding]) -> str:
    """One line per category with a count — the headline before the detail."""
    if not findings:
        return "no violations"
    counts = Counter(f.category for f in findings)
    return ", ".join(f"{n} {cat}" for cat, n in counts.most_common())

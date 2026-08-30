"""The sheet-vs-site reconciliation, sliced small enough for a team page.

`poopoo.json` is the whole reconciliation: every team's full sheet snapshot and
site snapshot, the draft-pick report, and a bio audit over ~1,000 players. It is
1.1 MB on disk, 583 KB minified. `/poopoo` loads all of it because it renders
all of it. A team page needs one team's diff list — and every owner loads a team
page, so it cannot pull the whole file to read five rows out of it. Stripped to
the diffs, all 30 teams come to 16 KB (3 KB gzipped).

Served from the API rather than written as a second static file because `/api`
is already proxied on nbn.today, pdc.nbn.today and dev.nbn.today; a
`/data/poopoo-summary.json` would need an exact-match `location` added to all
three nginx vhosts before it resolved anywhere, including the dev host the UI is
built against.

**This router only ever reads.** `build/poopoo.py` in nbn-today is the sole
author of that file, on a 10-minute timer; nothing here writes, and no cap math
happens here — the numbers are the job's, unchanged.

Two things about freshness, both of which read wrong if taken at face value:

- `generated_at` is when the answer last **changed**, not when it was last
  checked. The job rewrites the file only when the diff set moves (its own
  docstring explains why: unconditional rewrites filled the data-dir backup
  with 144 empty commits a day). A healthy job that keeps finding the same
  diffs leaves `generated_at` days old, which reads as a broken job.
- The file mtime is when the job last **ran**, and is returned separately as
  `checked_at`. `/poopoo` recovers this from the response's `Last-Modified`
  header, which only works because it fetches the file directly; anything
  reading through this endpoint gets it as a field instead.

Grouping: a diff is either `open` — a live disagreement about this season, or
about who is on the roster at all — or `deferred`, meaning it is a future
season's figure or one side has never computed a number to disagree with.
The distinction exists because it is the difference between "your team has a
problem" and "the site has not built this yet", and 65 of the 136 rows live
today are the latter. Collapsing them together produces a wall of red that
every owner correctly learns to ignore.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from .constants import DATA_DIR, VALID_TEAMS, logger

router = APIRouter()

POOPOO_FILE = DATA_DIR / "poopoo.json"

# `player_future_years` is by construction beyond the current season — the job
# compares this season under `player_salary`/`player_hold` and collapses every
# later one into this category. `player_hold_uncalculated` is the site sitting
# at a $1 placeholder, which /poopoo's own legend already calls "a known,
# systemic gap, not a fresh dispute".
DEFERRED_CATEGORIES = {"player_future_years", "player_hold_uncalculated"}


def _group(category: str) -> str:
    return "deferred" if category in DEFERRED_CATEGORIES else "open"


def _headline_mag(open_diffs: list[dict]) -> Optional[float]:
    """How far apart the two sources are on this team, as one number.

    It is the team-level Guaranteed Salary row, **not** the sum of the rows.
    Summing is the obvious thing to do and it double-counts: BKN's rows today
    are a $19.21M disagreement about Team Salary, a $9.37M one about the MLE
    used, and a $9.37M one about Julian Champagnie's salary — but the last two
    *are* part of the first. Adding them reports $37.9M apart on a team whose
    books differ by $19.2M.

    None when the two sides agree at the team level (individual rows can still
    disagree and cancel out, which is itself worth seeing) or when the report
    carries no magnitude for that row.
    """
    for d in open_diffs:
        if d.get("category") == "aggregate" and d.get("field") == "Guaranteed Salary":
            return d.get("mag")
    return None


def _team_summary(entry: dict) -> dict:
    """One team's diffs, with the per-team counts a caller would otherwise
    have to derive. `mag` is stamped by the job on every diff (see
    `fill_magnitudes` in build/poopoo.py) — it is not recomputed here, so this
    endpoint cannot report a different dollar figure than /poopoo does."""
    diffs = [dict(d, group=_group(d.get("category", ""))) for d in entry.get("diffs", [])]
    open_diffs = [d for d in diffs if d["group"] == "open"]
    return {
        "team": entry.get("team"),
        "diff_count": len(diffs),
        "open_count": len(open_diffs),
        "deferred_count": len(diffs) - len(open_diffs),
        "headline_mag": _headline_mag(open_diffs),
        "diffs": diffs,
    }


def summary(team: Optional[str] = None) -> dict:
    """The whole report reduced to its diffs. Missing file is `available:
    false` rather than an error: the job not having run is a real state a page
    should be able to say out loud, and a 404 would be indistinguishable from
    a page bug."""
    if not POOPOO_FILE.exists():
        logger.warning("poopoo: %s does not exist", POOPOO_FILE)
        return {"available": False, "season": None, "generated_at": None,
                "checked_at": None, "teams": []}
    try:
        doc = json.loads(POOPOO_FILE.read_text())
    except ValueError:
        # A torn read of a file being rewritten under us. The job writes it
        # whole, so the next 10-minute run fixes it; say so rather than 500.
        logger.warning("poopoo: %s is not valid JSON", POOPOO_FILE)
        return {"available": False, "season": None, "generated_at": None,
                "checked_at": None, "teams": []}

    checked_at = datetime.fromtimestamp(
        POOPOO_FILE.stat().st_mtime, tz=timezone.utc).isoformat()
    teams = [_team_summary(t) for t in doc.get("teams", [])]
    if team:
        teams = [t for t in teams if t["team"] == team]
    return {
        "available": True,
        "season": doc.get("season"),
        "generated_at": doc.get("generated_at"),
        "checked_at": checked_at,
        "teams": teams,
    }


@router.get("/api/poopoo/summary")
def get_poopoo_summary(team: Optional[str] = None):
    """Public, like /poopoo's own data file already is (nginx serves
    /data/poopoo.json unauthenticated; the `admin` role on the nav entry is a
    courtesy, not a boundary). A team that exists but has no diffs comes back
    as an empty `teams` list, same as a team with none on file."""
    if team:
        team = team.upper()
        if team not in VALID_TEAMS:
            raise HTTPException(status_code=404, detail=f"Unknown team '{team}'")
    return summary(team)

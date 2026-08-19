"""Per-game provenance for committed box scores (dev-deploy spec, Phase 2 item 12).

The source screenshots are deleted once a game is committed, deliberately: the
league plays 1,230 games a season, so keeping both images is ~1GB a year forever
on a box with 19GB free, to insure against a mis-parse — which is the *recover-
able* failure. The unrecoverable one is losing the CSVs, and the whole six-season
dataset is 3.4MB gzipped. That trade is argued in full in the spec.

**Keep the provenance, drop the pixels.** This records what an image would
actually be consulted for — "where did this line come from, and who do I ask
about it?" — at kilobytes a season, in a per-season JSONL beside the data.

One line per committed game, appended and never rewritten. It is a log, not
state: nothing reads it to make a decision, so a missing or malformed line can
never affect a transaction, a build, or a page. That is also why every failure
here is swallowed — a provenance write must never be the reason a real box score
fails to commit.

`uploaded_by` is recovered by matching the still-pending screenshot upload on
(date, teams), since the commit request carries no upload id. When a game was
typed in by hand rather than parsed there is no upload to find, and `source`
says `manual`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .constants import DATA_DIR, PENDING_BOXSCORES_DIR, logger


def provenance_path(season: str) -> Path:
    return DATA_DIR / f"boxscore-provenance-{season}.jsonl"


def _matching_upload(date: str, home_team: str, away_team: str) -> dict | None:
    """The pending upload this commit came from, if it is still on disk. The
    client deletes it in a separate request after committing, so at this point
    it normally still exists."""
    if not PENDING_BOXSCORES_DIR.exists():
        return None
    teams = {home_team.upper(), away_team.upper()}
    for item_dir in sorted(PENDING_BOXSCORES_DIR.iterdir()):
        meta_path = item_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("date") != date:
            continue
        if {str(meta.get("home_team", "")).upper(), str(meta.get("away_team", "")).upper()} == teams:
            return meta
    return None


def record_commit(*, season: str, date: str, game_type: str, home_team: str,
                  away_team: str, home_pts: int, away_pts: int, rows_added: int,
                  file_rows_after: int, filename: str, committed_by: str) -> None:
    """Append one line. Never raises."""
    try:
        upload = _matching_upload(date, home_team, away_team) or {}
        entry = {
            "committed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "committed_by": committed_by,
            "date": date,
            "season": season,
            "game_type": game_type,
            "home_team": home_team,
            "away_team": away_team,
            "home_pts": home_pts,
            "away_pts": away_pts,
            "rows_added": rows_added,
            "file": filename,
            "file_rows_after": file_rows_after,
            "source": "screenshot" if upload else "manual",
            "upload_id": upload.get("id"),
            "uploaded_by": upload.get("uploaded_by"),
            "uploaded_at": upload.get("uploaded_at"),
        }
        with provenance_path(season).open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.warning("Provenance record failed for %s vs %s on %s: %s",
                       home_team, away_team, date, exc)

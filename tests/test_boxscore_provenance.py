"""Regression tests for routers.boxscore_provenance — the per-game audit trail
that replaces the deleted screenshots (dev-deploy spec, Phase 2 item 12).

Two properties, and the second is the important one:

  * It records enough to answer "who do I ask about this line?" — including
    recovering the uploader from the still-pending screenshot upload, since the
    commit request carries no upload id and the images are deleted right after.
  * **It can never break a commit.** Nothing reads this log to make a decision,
    so a failure writing it must not surface as a failed box score submission.
    Every call is wrapped; a read-only directory or a bad meta.json is a log
    line, not a 500.

    venv/bin/python -m tests.test_boxscore_provenance
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.boxscore_provenance as bp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


GAME = dict(season="25-26", date="2026-01-05", game_type="REG",
            home_team="PHX", away_team="LAL", home_pts=110, away_pts=105,
            rows_added=26, file_rows_after=24149, filename="allstats-25-26.csv",
            committed_by="kim")


def lines(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    bp.DATA_DIR = tmp
    bp.PENDING_BOXSCORES_DIR = tmp / "pending-boxscores"

    # ── No pending upload: a hand-entered game ────────────────────────────────
    bp.record_commit(**GAME)
    out = lines(bp.provenance_path("25-26"))
    check("writes one line per commit", len(out) == 1)
    check("a game with no upload on disk is marked manual", out[0]["source"] == "manual")
    check("it names who committed it", out[0]["committed_by"] == "kim")
    check("it records where the rows landed",
          out[0]["file"] == "allstats-25-26.csv" and out[0]["file_rows_after"] == 24149)

    # ── With the pending upload still there ──────────────────────────────────
    item = bp.PENDING_BOXSCORES_DIR / "abc123"
    item.mkdir(parents=True)
    (item / "meta.json").write_text(json.dumps({
        "id": "abc123", "date": "2026-01-06", "home_team": "PHX", "away_team": "LAL",
        "uploaded_by": "dave", "uploaded_at": "2026-01-06T02:00:00+00:00",
    }))
    bp.record_commit(**{**GAME, "date": "2026-01-06"})
    out = lines(bp.provenance_path("25-26"))
    check("appends rather than replacing", len(out) == 2)
    check("recovers the uploader from the pending upload", out[1]["uploaded_by"] == "dave")
    check("and the upload id", out[1]["upload_id"] == "abc123")
    check("marking it as parsed from a screenshot", out[1]["source"] == "screenshot")

    # Teams reversed in the upload meta is the same game.
    (item / "meta.json").write_text(json.dumps({
        "id": "abc123", "date": "2026-01-07", "home_team": "lal", "away_team": "phx",
        "uploaded_by": "dave",
    }))
    bp.record_commit(**{**GAME, "date": "2026-01-07"})
    check("matches the upload regardless of home/away order or case",
          lines(bp.provenance_path("25-26"))[2]["uploaded_by"] == "dave")

    # A different date must not borrow another game's upload.
    bp.record_commit(**{**GAME, "date": "2026-02-01"})
    check("does not attribute an unrelated upload",
          lines(bp.provenance_path("25-26"))[3]["uploaded_by"] is None)

    # ── Seasons are separate files ────────────────────────────────────────────
    bp.record_commit(**{**GAME, "season": "24-25"})
    check("each season gets its own file", len(lines(bp.provenance_path("24-25"))) == 1)
    check("without disturbing the other", len(lines(bp.provenance_path("25-26"))) == 4)

    # ── It can never break a commit ───────────────────────────────────────────
    (item / "meta.json").write_text("{not json")
    bp.record_commit(**{**GAME, "date": "2026-03-01"})
    check("an unreadable upload meta is skipped, not raised",
          lines(bp.provenance_path("25-26"))[4]["source"] == "manual")

    bp.DATA_DIR = Path("/nonexistent-directory-for-this-test")
    bp.record_commit(**GAME)      # must not raise
    check("an unwritable log is swallowed", True)

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_boxscore_provenance: all pass")

"""Box score screenshots, one team-side at a time — routers/boxscore_shots.py and
the routes in routers/boxscores.py that use it.

What is pinned:

  * **Two sides, one game.** The first side to arrive creates the game's
    pending item; the second is added to it, never a second item. The side is
    decided by team against the item's own home/away, so a caller that has the
    sides swapped still files the image under the right team.
  * **`ready` means both sides are in.** That is what /parse-boxscores waits for.
  * **The reward is paid once per side**, half the per-game amount, and removing
    and re-adding a screenshot does not pay it again.
  * **Commit moves the screenshots, it doesn't delete them.** They leave the
    parse queue, are re-encoded as WebP, and carry a 14-day expiry; the sweep
    removes them after that.
  * **Anyone can read them**, and nothing from the URL reaches a path without
    matching a strict pattern first.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_boxscore_screenshots
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.boxscores as bx  # noqa: E402
import routers.boxscore_shots as shots  # noqa: E402
import routers.boxscore_provenance as bp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

STATS_TOKEN = "k" * 64
STATS2_TOKEN = "j" * 64
PLAIN_TOKEN = "n" * 64
MEMBERS = {
    "Statsy": {"token": STATS_TOKEN, "roles": ["stats"], "tenures": []},
    "Statso": {"token": STATS2_TOKEN, "roles": ["stats"], "tenures": []},
    "Nobody": {"token": PLAIN_TOKEN, "roles": [], "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-screenshots-test-"))
bx.DATA_DIR = TMP
bx.PENDING_BOXSCORES_DIR = shots.PENDING_BOXSCORES_DIR = bp.PENDING_BOXSCORES_DIR = TMP / "pending-boxscores"
shots.KEPT_BOXSCORES_DIR = TMP / "boxscore-screenshots"
bx.MANUAL_QUEUE_FILE = TMP / "pending-manual-queue.json"

REWARDS = []
bx._award_amount = lambda name, amt, reason: (REWARDS.append((name, amt)), (amt, 0.0))[1]
bx._award_submission_reward = lambda name: (REWARDS.append((name, 200.0)), (200.0, 0.0))[1]

app = FastAPI()
app.include_router(bx.router)
c = TestClient(app)

STATS = {"Authorization": "Bearer " + STATS_TOKEN}
STATS2 = {"Authorization": "Bearer " + STATS2_TOKEN}
PLAIN = {"Authorization": "Bearer " + PLAIN_TOKEN}


def png(color=(20, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 32), color).save(buf, "PNG")
    return buf.getvalue()


GAME = {"date": "2026-10-24", "home_team": "PHX", "away_team": "LAL", "season": "26-27", "game_type": "REG"}


def upload(team, headers=STATS, data=None, game=None):
    return c.post("/api/boxscore/upload/side", data={**(game or GAME), "team": team},
                  files={"image": ("shot.png", data or png(), "image/png")}, headers=headers)


# ── the first side creates the game ───────────────────────────────────────────

print("one side at a time")
check("no token cannot upload", upload("LAL", headers={}).status_code in (401, 403))
check("a member without the role cannot upload", upload("LAL", headers=PLAIN).status_code == 403)
check("a team not in the game is refused", upload("BOS").status_code == 422)
check("an unreadable format is refused", upload("LAL", data=b"not an image").status_code == 415)
check("nothing was created by the refusals", c.get("/api/boxscore/screenshots").json() == [])

r = upload("LAL")
check("the away side uploads", r.status_code == 200, r.text)
item = r.json()["item"]
check("it lands on the away side", len(item["images"]["away"]) == 1 and not item["images"]["home"])
check("one side is not ready to parse", item["ready"] is False)
check("the first side pays half the reward", REWARDS == [("Statsy", 100.0)])

# A second member, with home and away the other way round.
r = upload("PHX", headers=STATS2, game={**GAME, "home_team": "LAL", "away_team": "PHX"})
item2 = r.json()["item"]
check("the other side joins the same game", item2["id"] == item["id"])
check("and is filed by team, not by the caller's home/away", len(item2["images"]["home"]) == 1)
check("both sides in: ready", item2["ready"] is True)
check("the second side pays its half", REWARDS[-1] == ("Statso", 100.0))

r = upload("LAL")
item3 = r.json()["item"]
check("a side can take a second image", [i["file"] for i in item3["images"]["away"]] == ["away-1.png", "away-2.png"])
check("a second image on a side pays nothing", len(REWARDS) == 2)

listed = c.get("/api/boxscore/screenshots").json()
check("the list is public", len(listed) == 1 and listed[0]["status"] == "pending")
url = listed[0]["images"]["away"][0]["url"]
img = c.get(url)
check("an image is public too", img.status_code == 200 and img.content[:4] == b"\x89PNG")

pend = c.get("/api/boxscore/pending", headers=STATS).json()
check("the parse queue sees one ready game", len(pend) == 1 and pend[0]["ready"] is True)
check("with the images listed per side", len(pend[0]["images"]["away"]) == 2)

# ── removing ──────────────────────────────────────────────────────────────────

print("removing")
iid = item["id"]
check("removing needs the role",
      c.delete(f"/api/boxscore/pending/{iid}/images/away-2.png", headers=PLAIN).status_code == 403)
r = c.delete(f"/api/boxscore/pending/{iid}/images/away-2.png", headers=STATS)
check("one image can be removed", r.status_code == 200 and len(r.json()["item"]["images"]["away"]) == 1)
check("an unknown file is a 404", c.delete(f"/api/boxscore/pending/{iid}/images/away-9.png", headers=STATS).status_code == 404)
c.delete(f"/api/boxscore/pending/{iid}/images/away-1.png", headers=STATS)
r = upload("LAL")
check("re-adding a removed side pays nothing", len(REWARDS) == 2)
check("and fills the side again", r.json()["item"]["ready"] is True)

# ── paths ─────────────────────────────────────────────────────────────────────

print("paths")
# Called directly: over HTTP the client normalizes ".." out of the URL before
# the route ever sees it, which would test nothing.
from fastapi import HTTPException  # noqa: E402
try:
    bx.delete_pending_boxscore("..", info={"name": "Statsy"})
    check("a traversal id cannot delete the data dir", False)
except HTTPException as e:
    check("a traversal id cannot delete the data dir", e.status_code == 404)
check("the data dir is still there", TMP.exists() and (TMP / "pending-boxscores").exists())
check("a bad filename is a 404", c.get(f"/api/boxscore/screenshots/{iid}/meta.json").status_code == 404)
check("a bad id is a 404", c.get("/api/boxscore/screenshots/..%2F..%2Fx/home-1.png").status_code == 404)

# ── commit archives ───────────────────────────────────────────────────────────

print("commit")
bp.DATA_DIR = TMP
(TMP / "allstats-26-27.csv").write_text(",".join(bx.REG_ALLSTATS_HEADERS) + "\n")
row = {"player": "X, Y", "slug": "x-y", "min": 48, "pts": 10, "reb": 0, "oreb": 0, "dreb": 0,
       "ast": 0, "stl": 0, "blk": 0, "tov": 0, "pf": 0, "fgm": 5, "fga": 5, "tpm": 0, "tpa": 0,
       "ftm": 0, "fta": 0}
r = c.post("/api/boxscore/commit", headers=STATS, json={
    **GAME, "home_pts": 10, "away_pts": 8, "home_rows": [row], "away_rows": [{**row, "pts": 8, "fgm": 4}],
    "skip_build": True, "skip_reward": True})
check("the game commits", r.status_code == 200, r.text)
prov = [json.loads(l) for l in (TMP / "boxscore-provenance-26-27.jsonl").read_text().splitlines()]
check("provenance still finds the uploader", prov[-1]["source"] == "screenshot" and prov[-1]["uploaded_by"] == "Statsy")
check("the game leaves the parse queue", c.get("/api/boxscore/pending", headers=STATS).json() == [])
kept = c.get("/api/boxscore/screenshots").json()
check("and is kept", len(kept) == 1 and kept[0]["status"] == "committed")
k = kept[0]
check("as WebP", all(i["file"].endswith(".webp") for s in ("home", "away") for i in k["images"][s]))
exp = datetime.fromisoformat(k["expires_at"])
check("expiring in 14 days", timedelta(days=13) < exp - datetime.now(timezone.utc) <= timedelta(days=14))
check("still public after commit", c.get(k["images"]["home"][0]["url"]).status_code == 200)
check("a committed game's screenshots cannot be removed from the queue",
      c.delete(f"/api/boxscore/pending/{k['id']}/images/{k['images']['home'][0]['file']}", headers=STATS).status_code == 404)
check("archiving a game with nothing pending is a no-op", shots.archive_for_game("2026-01-01", "BOS", "NYK") is None)

print("sweep")
meta_p = shots.KEPT_BOXSCORES_DIR / k["id"] / "meta.json"
meta = json.loads(meta_p.read_text())
meta["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
meta_p.write_text(json.dumps(meta))
check("an expired game is gone from the list", c.get("/api/boxscore/screenshots").json() == [])
check("and from disk", not (shots.KEPT_BOXSCORES_DIR / k["id"]).exists())

# ── the two-image form still works ────────────────────────────────────────────

print("both sides in one request")
g2 = {**GAME, "date": "2026-10-25"}
r = c.post("/api/boxscore/upload", data=g2, headers=STATS,
           files={"home_image": ("h.png", png(), "image/png"), "away_image": ("a.png", png(), "image/png")})
check("the manual form uploads", r.status_code == 200, r.text)
one = c.get("/api/boxscore/screenshots?date=2026-10-25").json()
check("as one ready game", len(one) == 1 and one[0]["ready"])
check("adding to it pays nothing more", upload("LAL", game=g2).status_code == 200 and REWARDS[-1] == ("Statsy", 200.0))

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_boxscore_screenshots: all pass")

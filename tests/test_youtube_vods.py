"""`routers/youtube_vods.py` and the per-game video link in `routers/schedule.py`.

- **The title is the whole match.** `NBN 10/21: MIN @ MIA` links that one game;
  `NBN 1/29: Box Scores` links that date's day video. Anything else is ignored.
- **Titles carry no year.** A 12/31 game posted on Jan 1 is last year's.
- **A manual link is never overwritten**, and a video is only linked once, so
  clearing a wrong auto link sticks.
- **A title naming a game the schedule lacks is retried**, not dropped, and
  Stats is told once.
- **The hand-set route only takes YouTube video links** and stores one spelling.

Writes go to a temp directory, and the feed is a string; nothing touches the
network or live data.

    venv/bin/python -m tests.test_youtube_vods
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.schedule as sched  # noqa: E402
import routers.streaming_days as sd  # noqa: E402
import routers.youtube_vods as yv  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


STATS_TOKEN  = "k" * 64
STREAM_TOKEN = "s" * 64
PLAIN_TOKEN  = "n" * 64
MEMBERS = {
    "Statsy":  {"token": STATS_TOKEN,  "roles": ["stats"],    "tenures": []},
    "Streamy": {"token": STREAM_TOKEN, "roles": ["streamer"], "tenures": []},
    "Nobody":  {"token": PLAIN_TOKEN,  "roles": [],           "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-youtube-vods-test-"))
sched.DATA_DIR = TMP
sched._current_league_year = lambda: "26-27"
sched.log_write = lambda info, msg: None
sd.STREAMING_DAYS_FILE = TMP / "streaming-days.json"
yv.YOUTUBE_VODS_FILE = TMP / "youtube-vods.json"
yv.log_write = lambda info, msg: None
NOTES = []
yv.inbox.notify_roles = lambda roles, text, link=None: NOTES.append((roles, text))

(TMP / "schedule-26-27.json").write_text(json.dumps({"season": "26-27", "source": "", "games": [
    {"id": "g1", "date": "2026-10-21", "time_et": "7:30p", "away_team": "MIN", "home_team": "MIA",
     "arena": "", "note": "", "source_id": "", "stream": True},
    {"id": "g2", "date": "2026-10-21", "time_et": "8:30p", "away_team": "DAL", "home_team": "HOU",
     "arena": "", "note": "", "source_id": "", "stream": True},
    {"id": "g3", "date": "2026-12-31", "time_et": "7:00p", "away_team": "BOS", "home_team": "NYK",
     "arena": "", "note": "", "source_id": ""},
]}))

app = FastAPI()
app.include_router(sched.router)
app.include_router(yv.router)
c = TestClient(app)
STATS  = {"Authorization": "Bearer " + STATS_TOKEN}
STREAM = {"Authorization": "Bearer " + STREAM_TOKEN}
PLAIN  = {"Authorization": "Bearer " + PLAIN_TOKEN}


def game(gid):
    return next(g for g in sched._load("26-27")["games"] if g["id"] == gid)


def feed(*entries):
    body = "".join(
        f"<entry><yt:videoId>{vid}</yt:videoId><title>{title}</title>"
        f"<published>{pub}</published></entry>" for vid, title, pub in entries)
    return ('<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns="http://www.w3.org/2005/Atom">{body}</feed>')


print("titles")
check("game title", yv.parse_title("NBN 10/21: MIN @ MIA")
      == {"kind": "game", "month": 10, "day": 21, "away": "MIN", "home": "MIA"})
check("box scores title", yv.parse_title("NBN 1/29: Box Scores") == {"kind": "box", "month": 1, "day": 29})
check("loose spacing and case", yv.parse_title(" nbn 1/29 : box score ")["kind"] == "box")
check("anything else is not ours", yv.parse_title("NBN Draft Night 2026") is None)
check("impossible month is not ours", yv.parse_title("NBN 13/2: MIN @ MIA") is None)

print("\nyears")
check("same-year date", yv.resolve_date(10, 21, datetime.fromisoformat("2026-10-22T03:10:00+00:00")) == "2026-10-21")
check("12/31 posted Jan 1 is last year",
      yv.resolve_date(12, 31, datetime.fromisoformat("2027-01-01T06:00:00+00:00")) == "2026-12-31")
check("a day of slack for a video scheduled ahead",
      yv.resolve_date(10, 22, datetime.fromisoformat("2026-10-21T20:00:00+00:00")) == "2026-10-22")

print("\nfeed → links")
xml = feed(("NCBRotH6BrQ", "NBN 10/21: MIN @ MIA", "2026-10-22T02:15:00+00:00"),
           ("BoxScore001", "NBN 10/21: Box Scores", "2026-10-22T05:00:00+00:00"),
           ("Podcast0001", "NBN Podcast ep. 4", "2026-10-20T20:00:00+00:00"),
           ("Missing0001", "NBN 10/21: CHI @ TOR", "2026-10-22T04:00:00+00:00"))
state = {"videos": {}}
changed = yv.process(yv.parse_feed(xml), state)
check("game linked", game("g1").get("youtube_url") == "https://www.youtube.com/watch?v=NCBRotH6BrQ")
check("game link marked auto", game("g1").get("youtube_source") == "auto")
check("box scores is the date's video",
      sd.load_streaming_days().get("2026-10-21", {}).get("youtube_url") == "https://www.youtube.com/watch?v=BoxScore001")
check("box scores doesn't touch any game", "youtube_url" not in game("g2"))
check("podcast ignored", state["videos"]["Podcast0001"]["status"] == "ignored")
check("game not on the schedule is unmatched", state["videos"]["Missing0001"]["status"] == "unmatched")
check("four changes reported", len(changed) == 4)

print("\nsecond poll")
check("nothing new the second time", yv.process(yv.parse_feed(xml), state) == [])

print("\nmanual links win")
r = c.put("/api/schedule/g2/youtube", json={"url": "https://youtu.be/HandSet0001?t=90"}, headers=STATS)
check("stats can set one", r.status_code == 200)
check("stored as one spelling with its start time",
      r.json()["youtube_url"] == "https://www.youtube.com/watch?v=HandSet0001&t=90")
check("marked manual", game("g2").get("youtube_source") == "manual")
yv.process(yv.parse_feed(feed(("AutoDal0001", "NBN 10/21: DAL @ HOU", "2026-10-22T05:00:00+00:00"))), state)
check("poller didn't overwrite it", game("g2")["youtube_url"].endswith("HandSet0001&t=90"))
check("and says why", state["videos"]["AutoDal0001"]["status"] == "skipped")

print("\nclearing an auto link sticks")
r = c.put("/api/schedule/g1/youtube", json={"url": ""}, headers=STREAM)
check("streamer can clear one", r.status_code == 200 and r.json()["youtube_url"] is None)
check("every youtube field dropped", not any(k.startswith("youtube") for k in game("g1")))
yv.process(yv.parse_feed(xml), state)
check("next poll leaves it cleared", "youtube_url" not in game("g1"))

print("\nhand-set route")
check("not a YouTube link → 422",
      c.put("/api/schedule/g1/youtube", json={"url": "https://vimeo.com/123"}, headers=STATS).status_code == 422)
check("channel page isn't a video → 422",
      c.put("/api/schedule/g1/youtube", json={"url": "https://youtube.com/@nothingbutnetNBN"}, headers=STATS).status_code == 422)
check("plain member → 403",
      c.put("/api/schedule/g1/youtube", json={"url": "https://youtu.be/NCBRotH6BrQ"}, headers=PLAIN).status_code == 403)
check("public schedule carries youtube_url",
      all("youtube_url" in g for g in c.get("/api/schedule").json()["games"]))
check("/live/ links normalize",
      sched.youtube_watch_url("https://www.youtube.com/live/NCBRotH6BrQ?si=x") == "https://www.youtube.com/watch?v=NCBRotH6BrQ")

print("\nunmatched retries")
(TMP / "schedule-26-27.json").write_text(json.dumps({**sched._load("26-27"), "games": sched._load("26-27")["games"] + [
    {"id": "g4", "date": "2026-10-21", "time_et": "7:30p", "away_team": "CHI", "home_team": "TOR",
     "arena": "", "note": "", "source_id": ""}]}))
yv.process(yv.parse_feed(xml), state)
check("links once the game exists", game("g4").get("youtube_url", "").endswith("Missing0001"))

print("\npoll_once tells Stats about an unmatched title, once")
yv._get = lambda url: feed(("Typo0000001", "NBN 10/21: MIN @ MIAA", "2026-10-22T05:00:00+00:00"))
yv.YOUTUBE_VODS_FILE.write_text(json.dumps({"channel_id": "UC" + "x" * 22, "videos": {}}))
NOTES.clear()
yv.poll_once()
yv.poll_once()
check("one notice", len(NOTES) == 1 and NOTES[0][0] == ["stats"])
check("status route lists it",
      c.get("/api/youtube-vods", headers=STATS).json()["videos"][0]["status"] == "unmatched")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all passed")

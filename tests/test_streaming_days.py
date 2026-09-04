"""`routers/streaming_days.py` — per-date streaming status.

- **`done` is one shared flag per date**, not per streamer — whoever marks it
  is stamped (`done_by`/`done_at`), same shape as coaching-settings'
  pending/entered, and the same reasoning: a day usually has one streamer,
  and there is no per-streamer state to keep separate.
- **The YouTube link is independent of `done`** — a date can carry a video
  link before anyone marks it done, or be done with no link yet, mirroring
  how a schedule game's `streamer`/`stream` are independent (routers/schedule.py).
- **Falsy fields are omitted from storage, not stored as null/false** — same
  convention schedule.py uses, verified here by reading the file back.
- **No cross-check against /api/schedule.** A streamer marking a date done
  doesn't need the server to agree a game existed there.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_streaming_days
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.streaming_days as sd  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

STREAM_TOKEN  = "s" * 64
STREAM2_TOKEN = "t" * 64
PLAIN_TOKEN   = "n" * 64
MEMBERS = {
    "Streamy":  {"token": STREAM_TOKEN,  "roles": ["streamer"], "tenures": []},
    "Costream": {"token": STREAM2_TOKEN, "roles": ["streamer"], "tenures": []},
    "Nobody":   {"token": PLAIN_TOKEN,   "roles": [],           "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-streaming-days-test-"))
sd.STREAMING_DAYS_FILE = TMP / "streaming-days.json"
sd.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(sd.router)
c = TestClient(app)

STREAM  = {"Authorization": "Bearer " + STREAM_TOKEN}
STREAM2 = {"Authorization": "Bearer " + STREAM2_TOKEN}
PLAIN   = {"Authorization": "Bearer " + PLAIN_TOKEN}

DATE = "2026-10-24"


def all_days():
    return c.get("/api/streaming-days").json()


# ── reading ───────────────────────────────────────────────────────────────────

print("reading")
check("an empty store reads as {}", all_days() == {})
check("reading needs no token", c.get("/api/streaming-days").status_code == 200)

# ── done ──────────────────────────────────────────────────────────────────────

print("done")
check("no token cannot mark done", c.post(f"/api/streaming-days/{DATE}/done").status_code in (401, 403))
check("a member without the role cannot mark done", c.post(f"/api/streaming-days/{DATE}/done", headers=PLAIN).status_code == 403)
check("bad date is a 422", c.post("/api/streaming-days/not-a-date/done", headers=STREAM).status_code == 422)
check("undoing a date never marked is a 404", c.delete(f"/api/streaming-days/{DATE}/done", headers=STREAM).status_code == 404)

r = c.post(f"/api/streaming-days/{DATE}/done", headers=STREAM)
check("a streamer can mark a date done", r.status_code == 200)
body = r.json()
check("done is set and stamped", body["done"] is True and body["done_by"] == "Streamy" and bool(body["done_at"]))
check("it shows up on the public list", all_days()[DATE]["done"] is True)
check("re-marking (a different streamer confirming) is not an error",
      c.post(f"/api/streaming-days/{DATE}/done", headers=STREAM2).json()["done_by"] == "Costream")

r2 = c.delete(f"/api/streaming-days/{DATE}/done", headers=STREAM)
check("undoing clears done", r2.status_code == 200 and "done" not in r2.json())
check("undoing twice is a 404", c.delete(f"/api/streaming-days/{DATE}/done", headers=STREAM).status_code == 404)

# ── youtube ───────────────────────────────────────────────────────────────────

print("youtube")
check("no token cannot set a link",
      c.put(f"/api/streaming-days/{DATE}/youtube", json={"url": "https://youtube.com/x"}).status_code in (401, 403))
check("a member without the role cannot set a link",
      c.put(f"/api/streaming-days/{DATE}/youtube", json={"url": "https://youtube.com/x"}, headers=PLAIN).status_code == 403)
check("a non-http(s) url is a 422",
      c.put(f"/api/streaming-days/{DATE}/youtube", json={"url": "ftp://youtube.com/x"}, headers=STREAM).status_code == 422)

r3 = c.put(f"/api/streaming-days/{DATE}/youtube", json={"url": "https://youtube.com/watch?v=abc"}, headers=STREAM)
check("a streamer can set a link", r3.status_code == 200)
y = r3.json()
check("the link is stamped", y["youtube_url"] == "https://youtube.com/watch?v=abc" and y["youtube_set_by"] == "Streamy")
check("it shows up on the public list", all_days()[DATE]["youtube_url"] == "https://youtube.com/watch?v=abc")

r4 = c.put(f"/api/streaming-days/{DATE}/youtube", json={"url": ""}, headers=STREAM)
check("an empty url clears the link", "youtube_url" not in r4.json())

# ── independence ──────────────────────────────────────────────────────────────

print("done and youtube are independent")
c.put(f"/api/streaming-days/{DATE}/youtube", json={"url": "https://youtu.be/xyz"}, headers=STREAM)
check("a link with no done mark is fine on its own",
      all_days()[DATE].get("done") is None and all_days()[DATE]["youtube_url"] == "https://youtu.be/xyz")
c.post(f"/api/streaming-days/{DATE}/done", headers=STREAM)
c.delete(f"/api/streaming-days/{DATE}/done", headers=STREAM)
check("undoing done leaves the link untouched",
      all_days()[DATE]["youtube_url"] == "https://youtu.be/xyz" and "done" not in all_days()[DATE])

# ── the file on disk ─────────────────────────────────────────────────────────

print("the file on disk")
raw = json.loads(sd.STREAMING_DAYS_FILE.read_text())
check("only the one date appears", set(raw.keys()) == {DATE})
check("no leftover done/done_at/done_by keys", not ({"done", "done_at", "done_by"} & set(raw[DATE].keys())))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

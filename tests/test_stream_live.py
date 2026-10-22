"""`GET /api/stream/live` — the homepage live banner's one question.

What it promises: public, always 200, `live: true` only when Twitch says the
channel is live, `live: false` on any failure, and at most one Twitch call a
minute however many visitors ask. Twitch itself is never called: `fetch_status`
is swapped out, and the HTTP layer is driven through httpx.MockTransport.

    venv/bin/python -m tests.test_stream_live
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.stream_live as sl  # noqa: E402
import routers.pickem as pickem  # noqa: E402

# A live answer locks today's Pick'em. Never let a faked one reach the live
# data dir: this test once wrote a real lock that closed a game day at 6 AM.
LIVE_NOTES = []
pickem.note_stream_live = lambda: LIVE_NOTES.append(1)

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


app = FastAPI()
app.include_router(sl.router)
client = TestClient(app)


def reset():
    sl._cache.update(body=None, at=0.0)
    sl._token.update(value=None, expires=0.0)


real_fetch = sl.fetch_status
calls = []


def fake(body=None, exc=None):
    def f():
        calls.append(1)
        if exc:
            raise exc
        return body
    return f


print("live")
reset(); calls.clear()
sl.fetch_status = fake({"live": True, "channel": "nothingbutnet", "title": "Night 42",
                        "game": "NBA 2K27", "viewers": 31, "started_at": "2026-10-22T00:00:00Z"})
r = client.get("/api/stream/live")
check("answers 200 with no token", r.status_code == 200)
check("live is true", r.json().get("live") is True)
check("carries the title", r.json().get("title") == "Night 42")
check("tells Pick'em the day has started", len(LIVE_NOTES) == 1)

print("cached for a minute")
client.get("/api/stream/live"); client.get("/api/stream/live")
check("three requests, one Twitch call", len(calls) == 1)
sl._cache["at"] -= sl.CACHE_SECONDS + 1
client.get("/api/stream/live")
check("asks again once the minute is up", len(calls) == 2)

print("offline")
reset()
sl.fetch_status = fake({"live": False, "channel": "nothingbutnet"})
LIVE_NOTES.clear()
check("live is false", client.get("/api/stream/live").json().get("live") is False)
check("offline tells Pick'em nothing", LIVE_NOTES == [])

print("Twitch fails")
reset(); calls.clear()
sl.fetch_status = fake(exc=httpx.ConnectError("down"))
r = client.get("/api/stream/live")
check("still 200", r.status_code == 200)
check("reads as offline", r.json().get("live") is False)
client.get("/api/stream/live")
check("the failure is cached too", len(calls) == 1)
sl.fetch_status = real_fetch

print("no credentials")
reset()
saved = {k: os.environ.pop(k, None) for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")}
check("reads as offline", client.get("/api/stream/live").json().get("live") is False)

print("fetch_status against a mocked Twitch")
os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"] = "cid", "secret"
seen = {"token": 0}


def handler(stream_data):
    def h(req: httpx.Request):
        if req.url.host == "id.twitch.tv":
            seen["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        check("sends the app token", req.headers.get("Authorization") == "Bearer tok")
        return httpx.Response(200, json={"data": stream_data})
    return h


RealClient = httpx.Client


def use(stream_data):
    sl.httpx.Client = lambda **kw: RealClient(transport=httpx.MockTransport(handler(stream_data)), **kw)


reset()
use([{"type": "live", "title": "Night 42", "game_name": "NBA 2K27", "viewer_count": 31,
      "started_at": "2026-10-22T00:00:00Z"}])
s = sl.fetch_status()
check("live stream parsed", s["live"] and s["title"] == "Night 42" and s["viewers"] == 31)
use([])
check("empty data is offline", sl.fetch_status()["live"] is False)
check("token minted once and reused", seen["token"] == 1)
sl.httpx.Client = RealClient

for k, v in saved.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

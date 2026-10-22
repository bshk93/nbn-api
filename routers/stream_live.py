"""Is the league's Twitch channel live — `GET /api/stream/live`, behind the
homepage's live banner.

The page can't ask Twitch itself: Helix wants an app token, and minting one
takes the client secret. So the API asks, once a minute at most, and every
visitor shares the answer.

It uses a **client-credentials** app token, not the broadcaster's refresh
token that `fetch_twitch_subs.py` rotates. `GET /helix/streams` is public
data and needs no user consent, and a second process refreshing that token
would race the monthly sub job's rewrite of `.env`.

Any failure — no credentials, Twitch down, a bad token — answers
`{"live": false}` with a 200. A missing banner is harmless; a broken
homepage or a false "LIVE" is not.

Requires (in .env, already there for the sub job):
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TWITCH_BROADCASTER_ID   (optional; falls back to the channel login)

    venv/bin/python -m tests.test_stream_live
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx
from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger(__name__)

CHANNEL = "nothingbutnet"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
CACHE_SECONDS = 60

_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}
_cache: dict = {"body": None, "at": 0.0}


def _app_token(client: httpx.Client, client_id: str, secret: str) -> str:
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    resp = client.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials",
    })
    resp.raise_for_status()
    data = resp.json()
    _token["value"] = data["access_token"]
    # Renew a minute early rather than ride a token to its last second.
    _token["expires"] = time.time() + max(int(data.get("expires_in", 0)) - 60, 0)
    return _token["value"]


def fetch_status() -> dict:
    """Ask Twitch directly. Raises on any failure; the route turns that into offline."""
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not secret:
        raise RuntimeError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set")
    broadcaster = os.environ.get("TWITCH_BROADCASTER_ID")
    params = {"user_id": broadcaster} if broadcaster else {"user_login": CHANNEL}
    with httpx.Client(timeout=5) as client:
        token = _app_token(client, client_id, secret)
        resp = client.get(STREAMS_URL, params=params,
                          headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            # Revoked or expired early — drop it so the next poll mints a fresh one.
            _token["value"] = None
        resp.raise_for_status()
        streams = resp.json().get("data") or []
    live = next((s for s in streams if s.get("type") == "live"), None)
    if not live:
        return {"live": False, "channel": CHANNEL}
    return {
        "live": True,
        "channel": CHANNEL,
        "title": live.get("title") or "",
        "game": live.get("game_name") or "",
        "viewers": live.get("viewer_count"),
        "started_at": live.get("started_at"),
    }


@router.get("/api/stream/live")
def stream_live():
    with _lock:
        if _cache["body"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["body"]
        try:
            body = fetch_status()
        except Exception as e:  # noqa: BLE001 — any failure reads as offline
            log.warning("stream_live: Twitch check failed: %s", e)
            body = {"live": False, "channel": CHANNEL}
        # Failures are cached too, so a Twitch outage costs one call a minute, not one per visitor.
        _cache["body"], _cache["at"] = body, time.time()
        if body.get("live"):
            # Live means today's games have started: lock Pick'em.
            try:
                from .pickem import note_stream_live
                note_stream_live()
            except Exception as e:  # noqa: BLE001 — never break the banner
                log.warning("stream_live: pickem lock failed: %s", e)
        return body

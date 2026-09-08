"""
Pull the live Twitch subscriber list for the NBN channel (twitch.tv/nothingbutnet),
matched against the `twitch` login field on each member.json entry.

The broadcaster owns the Twitch account, not us — getting here required a
one-time OAuth consent from them (scope channel:read:subscriptions). What's
stored is the *refresh* token, which doesn't expire on its own. This script
exchanges it for a short-lived access token on every run rather than needing
that consent flow redone.

Twitch's refresh call returns a new refresh_token alongside the access token.
The new one isn't guaranteed to keep working forever if it's discarded, so
this overwrites TWITCH_REFRESH_TOKEN in .env in place after every run — the
whole point is that a lost refresh token means doing the OAuth dance with the
broadcaster all over again, which is the thing we're trying to avoid.

Requires (in .env):
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TWITCH_REFRESH_TOKEN
  TWITCH_BROADCASTER_ID   (156572449 for nothingbutnet — resolved once via
                            GET /helix/users?login=nothingbutnet)

Usage:
  set -a && source .env && set +a && venv/bin/python3 fetch_twitch_subs.py
"""
import json
import os
import re
import sys
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).resolve().parent / ".env"
MEMBERS_PATH = Path("/var/lib/nothing-but-stats/members.json")
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
SUBS_URL = "https://api.twitch.tv/helix/subscriptions"


def _update_env_var(key: str, value: str):
    """Rewrite one KEY=... line in .env in place, preserving everything else."""
    text = ENV_PATH.read_text()
    pattern = re.compile(rf"^{key}=.*$", re.MULTILINE)
    new_line = f"{key}={value}"
    text = pattern.sub(new_line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{new_line}\n"
    ENV_PATH.write_text(text)


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["refresh_token"]


def fetch_subscriptions(client_id: str, access_token: str, broadcaster_id: str) -> list[dict]:
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {access_token}"}
    subs, cursor = [], None
    while True:
        params = {"broadcaster_id": broadcaster_id, "first": 100}
        if cursor:
            params["after"] = cursor
        resp = httpx.get(SUBS_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        subs.extend(data["data"])
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor:
            break
    # The broadcaster always shows up "subscribed" to their own channel — not a real sub.
    return [s for s in subs if s["user_id"] != broadcaster_id]


def main():
    client_id = os.environ["TWITCH_CLIENT_ID"]
    client_secret = os.environ["TWITCH_CLIENT_SECRET"]
    refresh_token = os.environ["TWITCH_REFRESH_TOKEN"]
    broadcaster_id = os.environ["TWITCH_BROADCASTER_ID"]

    access_token, new_refresh_token = refresh_access_token(client_id, client_secret, refresh_token)
    if new_refresh_token != refresh_token:
        _update_env_var("TWITCH_REFRESH_TOKEN", new_refresh_token)

    subs = fetch_subscriptions(client_id, access_token, broadcaster_id)

    members = json.loads(MEMBERS_PATH.read_text())
    by_twitch_login = {
        info["twitch"].lower(): name
        for name, info in members.items()
        if info.get("twitch")
    }

    rows = []
    for s in subs:
        login = s["user_login"].lower()
        rows.append({
            "member": by_twitch_login.get(login),
            "twitch_login": s["user_login"],
            "tier": int(s["tier"]) // 1000,
            "is_gift": s["is_gift"],
        })

    for r in sorted(rows, key=lambda r: (-r["tier"], r["twitch_login"])):
        name = r["member"] or "(unmatched)"
        gift = " (gifted)" if r["is_gift"] else ""
        print(f"{name:20} {r['twitch_login']:20} tier {r['tier']}{gift}")

    unmatched = [r for r in rows if not r["member"]]
    if unmatched:
        print(f"\n{len(unmatched)} subscriber(s) with no matching member.twitch field", file=sys.stderr)

    return rows


if __name__ == "__main__":
    main()

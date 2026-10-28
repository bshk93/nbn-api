"""
Subscribe Twitch EventSub `stream.online` for twitch.tv/nothingbutnet to
POST /api/twitch/eventsub (routers/stream_notify.py). Run once; a
subscription lasts until it's deleted or Twitch revokes it (the API logs a
revocation).

Twitch calls the endpoint with a challenge as soon as the subscription is
created, so deploy the API with TWITCH_EVENTSUB_SECRET set first. `list` should
then show the subscription as `enabled`.

Requires (in .env):
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TWITCH_EVENTSUB_SECRET
  TWITCH_BROADCASTER_ID   (156572449 for nothingbutnet)

Usage:
  set -a && source .env && set +a && venv/bin/python subscribe_twitch_eventsub.py [list|create|delete <id>]
"""
import os
import sys

import httpx

from routers.stream_live import _app_token

SUBS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
CALLBACK = os.environ.get("TWITCH_EVENTSUB_CALLBACK", "https://nbn.today/api/twitch/eventsub")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    client_id = os.environ["TWITCH_CLIENT_ID"]
    with httpx.Client(timeout=10) as client:
        token = _app_token(client, client_id, os.environ["TWITCH_CLIENT_SECRET"])
        headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
        if cmd == "create":
            r = client.post(SUBS_URL, headers=headers, json={
                "type": "stream.online", "version": "1",
                "condition": {"broadcaster_user_id": os.environ["TWITCH_BROADCASTER_ID"]},
                "transport": {"method": "webhook", "callback": CALLBACK,
                              "secret": os.environ["TWITCH_EVENTSUB_SECRET"]},
            })
        elif cmd == "delete" and len(sys.argv) > 2:
            r = client.delete(SUBS_URL, headers=headers, params={"id": sys.argv[2]})
        elif cmd == "list":
            r = client.get(SUBS_URL, headers=headers)
            if r.status_code < 300:
                for s in r.json().get("data", []):
                    print(s["id"], s["type"], s["status"], s["transport"].get("callback"))
                return 0
        else:
            print(__doc__)
            return 2
        print(r.status_code, r.text)
        return 0 if r.status_code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Subscribe Twitch EventSub `stream.online` and `stream.offline` for
twitch.tv/nothingbutnet to POST /api/twitch/eventsub (routers/stream_notify.py).
Run once; a subscription lasts until it's deleted or Twitch revokes it (the API
logs a revocation). Running `create` again is safe: a type that's already
subscribed answers 409 and is left as it is. `stream.offline` is what tells a
stream coming back after a drop from the next stream of the night.

Twitch calls the endpoint with a challenge as soon as the subscription is
created, so deploy the API with TWITCH_EVENTSUB_SECRET set first. `list` should
then show both subscriptions as `enabled`.

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
TYPES = ("stream.online", "stream.offline")
CALLBACK = os.environ.get("TWITCH_EVENTSUB_CALLBACK", "https://nbn.today/api/twitch/eventsub")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    client_id = os.environ["TWITCH_CLIENT_ID"]
    with httpx.Client(timeout=10) as client:
        token = _app_token(client, client_id, os.environ["TWITCH_CLIENT_SECRET"])
        headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
        if cmd == "create":
            failed = False
            for sub_type in TYPES:
                r = client.post(SUBS_URL, headers=headers, json={
                    "type": sub_type, "version": "1",
                    "condition": {"broadcaster_user_id": os.environ["TWITCH_BROADCASTER_ID"]},
                    "transport": {"method": "webhook", "callback": CALLBACK,
                                  "secret": os.environ["TWITCH_EVENTSUB_SECRET"]},
                })
                if r.status_code == 409:
                    print(sub_type, "already subscribed")
                else:
                    print(sub_type, r.status_code, r.text)
                    failed = failed or r.status_code >= 300
            return 1 if failed else 0
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

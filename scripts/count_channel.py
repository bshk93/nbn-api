"""Count a Discord channel's messages by month, without keeping any text.

Read-only. Pages through the channel's full history with the league bot and
prints, per month: all messages, messages by people (not bots), and
"substantive" ones (by people, 5+ words). No message content is written to
disk or printed. Used to size a channel before deciding whether to read it.

    cd ~/projects/nbn-api && venv/bin/python ../nbn-api-dev/scripts/count_channel.py leaguetalk

Needs DISCORD_BOT_TOKEN (and optionally DISCORD_GUILD_ID) in the environment
or in ./.env, and Read Message History on the channel.
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000


def load_env() -> None:
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    want = (sys.argv[1] if len(sys.argv) > 1 else "leaguetalk").lower().lstrip("#")
    load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set (run from ~/projects/nbn-api, which has the .env)")
    client = httpx.Client(headers={"Authorization": f"Bot {token}"}, timeout=30)

    def get(path, **params):
        while True:
            r = client.get(API + path, params=params)
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 1))
                continue
            r.raise_for_status()
            return r.json()

    gid = os.environ.get("DISCORD_GUILD_ID") or get("/users/@me/guilds")[0]["id"]
    chans = [c for c in get(f"/guilds/{gid}/channels")
             if c["type"] in (0, 5) and c["name"].lower().replace("-", "") == want.replace("-", "")]
    if not chans:
        names = ", ".join(sorted(c["name"] for c in get(f"/guilds/{gid}/channels") if c["type"] == 0))
        sys.exit(f"no channel named {want!r}. Text channels: {names}")
    ch = chans[0]

    months = defaultdict(lambda: [0, 0, 0, 0])   # all, people, substantive, words(people)
    before, pages = None, 0
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        batch = get(f"/channels/{ch['id']}/messages", **params)
        if not batch:
            break
        for m in batch:
            month = m["timestamp"][:7]
            row = months[month]
            row[0] += 1
            if not m["author"].get("bot"):
                words = len((m.get("content") or "").split())
                row[1] += 1
                row[3] += words
                if words >= 5:
                    row[2] += 1
        before = batch[-1]["id"]
        pages += 1
        if pages % 50 == 0:
            print(f"  ...{pages * 100:,} messages scanned, back to {batch[-1]['timestamp'][:10]}", file=sys.stderr)

    print(f"\n#{ch['name']}\n")
    print(f"{'month':<8} {'all':>8} {'people':>8} {'5+ words':>9} {'words':>9}")
    tot = [0, 0, 0, 0]
    years = defaultdict(lambda: [0, 0, 0, 0])
    for month in sorted(months):
        row = months[month]
        print(f"{month:<8} {row[0]:>8,} {row[1]:>8,} {row[2]:>9,} {row[3]:>9,}")
        for i in range(4):
            tot[i] += row[i]
            years[month[:4]][i] += row[i]
    print("\nby year")
    for y in sorted(years):
        r = years[y]
        print(f"{y:<8} {r[0]:>8,} {r[1]:>8,} {r[2]:>9,} {r[3]:>9,}")
    print(f"\n{'total':<8} {tot[0]:>8,} {tot[1]:>8,} {tot[2]:>9,} {tot[3]:>9,}")


if __name__ == "__main__":
    main()

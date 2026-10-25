"""Export one Discord channel's messages for a date range, oldest first.

Read-only against Discord. Writes one JSON object per message, one file per
month, to a private directory outside the web root and outside the data dir
(league chat is not league data, and nothing under /var/lib/nothing-but-stats
should ever be published from it). Nothing is filtered here: short messages
and replies are kept, since "lmao" under a trade is context. Filtering
happens when the export is read.

    cd ~/projects/nbn-api && venv/bin/python ../nbn-api-dev/scripts/fetch_channel.py leaguetalk 2020-01-01 2020-11-01

The end date is exclusive. Output: ~/nbn-lore/raw/<channel>/<YYYY-MM>.jsonl.
A month file is rewritten whole each run, so re-running a range is safe.

Needs DISCORD_BOT_TOKEN (and optionally DISCORD_GUILD_ID) in the environment
or in ./.env, and Read Message History on the channel.
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
OUT_ROOT = Path.home() / "nbn-lore" / "raw"
TEXT_TYPES = {0, 5}   # text, announcement


def load_env() -> None:
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def snowflake(day: str) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def slim(m: dict) -> dict:
    a = m["author"]
    ref = m.get("message_reference") or {}
    return {
        "id": m["id"],
        "ts": m["timestamp"][:19],
        "author": a.get("global_name") or a.get("username"),
        "author_id": a["id"],
        "bot": bool(a.get("bot")),
        "content": m.get("content") or "",
        "reply_to": ref.get("message_id"),
        "attachments": len(m.get("attachments") or []),
        "embeds": len(m.get("embeds") or []),
        "reactions": sum(r.get("count", 0) for r in (m.get("reactions") or [])),
    }


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    want, start, end = sys.argv[1].lower().lstrip("#"), sys.argv[2], sys.argv[3]
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
             if c["type"] in TEXT_TYPES and c["name"].lower().replace("-", "") == want.replace("-", "")]
    if len(chans) != 1:
        sys.exit(f"expected one text channel named {want!r}, found {len(chans)}")
    ch = chans[0]

    after, stop = snowflake(start), snowflake(end)
    months = defaultdict(list)
    n = 0
    while True:
        batch = get(f"/channels/{ch['id']}/messages", limit=100, after=str(after))
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))   # Discord pages newest-first even with `after`
        done = False
        for m in batch:
            if int(m["id"]) >= stop:
                done = True
                break
            months[m["timestamp"][:7]].append(slim(m))
            n += 1
        if done:
            break
        after = int(batch[-1]["id"])
        if n and n % 5000 < 100:
            print(f"  ...{n:,} messages, up to {batch[-1]['timestamp'][:10]}", file=sys.stderr)

    out = OUT_ROOT / ch["name"]
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT_ROOT.parent, 0o700)
    for month, msgs in sorted(months.items()):
        path = out / f"{month}.jsonl"
        with path.open("w") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"{month}: {len(msgs):>6,} messages -> {path}")
    print(f"\n{n:,} messages from #{ch['name']}, {start} to {end} (exclusive)")


if __name__ == "__main__":
    main()

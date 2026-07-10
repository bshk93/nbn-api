"""
Fetch historical trade-announcement messages from Discord channels for the
manual transaction-backfill workflow (see nbn-today's parse-transactions-backfill
skill). Read-only against Discord — writes raw messages to disk for a later,
human-reviewed parse pass. Does not touch nbn-api's own data or transaction log.

Requires:
  DISCORD_BOT_TOKEN  — bot token (Bot -> Reset Token in the dev portal)
                        with "Message Content Intent" enabled and
                        View Channel + Read Message History on the target channels.
  DISCORD_GUILD_ID   — optional; if unset, the bot must be in exactly one guild.

Usage:
  python3 fetch_discord_transactions.py [--pattern transaction] [--out-dir DIR]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

API_BASE = "https://discord.com/api/v10"
DEFAULT_OUT_DIR = Path("/var/lib/nothing-but-stats/discord-transactions-raw")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}"}


def _get(client: httpx.Client, url: str, **params) -> list | dict:
    while True:
        resp = client.get(url, params=params)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            print(f"  rate limited, sleeping {retry_after}s", file=sys.stderr)
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and float(remaining) <= 0:
            time.sleep(float(resp.headers.get("x-ratelimit-reset-after", 1)))
        return resp.json()


def resolve_guild_id(client: httpx.Client) -> str:
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if guild_id:
        return guild_id
    guilds = _get(client, f"{API_BASE}/users/@me/guilds")
    if len(guilds) != 1:
        names = ", ".join(f"{g['name']} ({g['id']})" for g in guilds)
        sys.exit(
            f"Bot is in {len(guilds)} guilds, can't auto-pick one. "
            f"Set DISCORD_GUILD_ID to one of: {names}"
        )
    return guilds[0]["id"]


def find_matching_channels(client: httpx.Client, guild_id: str, pattern: str) -> list[dict]:
    channels = _get(client, f"{API_BASE}/guilds/{guild_id}/channels")
    pattern = pattern.lower()
    return [
        c for c in channels
        if c.get("type") == 0 and pattern in c.get("name", "").lower()
    ]


def fetch_channel_history(client: httpx.Client, channel_id: str) -> list[dict]:
    messages = []
    before = None
    while True:
        batch = _get(
            client, f"{API_BASE}/channels/{channel_id}/messages",
            limit=100, **({"before": before} if before else {}),
        )
        if not batch:
            break
        messages.extend(batch)
        before = batch[-1]["id"]
        print(f"    fetched {len(messages)} messages so far...", file=sys.stderr)
    return messages


def simplify(msg: dict, channel_name: str) -> dict:
    return {
        "id": msg["id"],
        "channel": channel_name,
        "author": msg.get("author", {}).get("username", "unknown"),
        "timestamp": msg["timestamp"],
        "edited_timestamp": msg.get("edited_timestamp"),
        "content": msg.get("content", ""),
        "attachments": [a["url"] for a in msg.get("attachments", [])],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="transaction",
                         help="substring to match in channel names (case-insensitive)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(headers=_headers(token), timeout=30) as client:
        guild_id = resolve_guild_id(client)
        channels = find_matching_channels(client, guild_id, args.pattern)
        if not channels:
            sys.exit(f"No channels matched pattern {args.pattern!r}")

        print(f"Matched {len(channels)} channel(s): "
              f"{', '.join(c['name'] for c in channels)}")

        for ch in channels:
            print(f"Fetching #{ch['name']} ({ch['id']})...")
            raw = fetch_channel_history(client, ch["id"])
            simplified = sorted(
                (simplify(m, ch["name"]) for m in raw),
                key=lambda m: m["timestamp"],
            )
            out_path = args.out_dir / f"{ch['name']}.json"
            out_path.write_text(json.dumps(simplified, indent=2))
            print(f"  wrote {len(simplified)} messages -> {out_path}")


if __name__ == "__main__":
    main()

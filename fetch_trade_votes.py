"""
Scrape every Discord channel whose name contains "transaction" for reactions
on trade messages, and work out, per member per team, how often they backed
that team when it was actually a side in the trade being reacted to.

A member "votes on a trade" by reacting to its message at all — any emoji,
team logo or otherwise. For every team named in that trade (parsed from the
"TEAM receives: ..." headers the same way resolve_discord_trades.py does),
each member who reacted to the message counts as FOR that team if one of
their reactions was one of that team's own logo emoji (teams have 2-3
logo-art variants in the guild, e.g. "Hawks"/"Hawks2"/"hawkstb", pooled as
equivalent), and AGAINST that team otherwise — they showed up to the trade
and didn't back this side of it. A message that mentions 3 teams gives every
reactor a for/against verdict on all 3, independently.

Messages whose team headers don't parse (BLOCK_RE finds 0 or 1 team — ~2% of
the corpus, mostly malformed/picks-only posts) are skipped entirely: there's
nothing to attribute a for/against verdict to.

Requires:
  DISCORD_BOT_TOKEN  — bot token with Message Content Intent, View Channel,
                        and Read Message History on the target channels.
  DISCORD_GUILD_ID   — optional; if unset, the bot must be in exactly one guild.

Usage:
  python3 fetch_trade_votes.py [--pattern transaction] [--out PATH]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from resolve_discord_trades import BLOCK_RE, _alias_to_abbr

API_BASE = "https://discord.com/api/v10"
DEFAULT_OUT = Path("/var/lib/nothing-but-stats/trade-votes.json")
MEMBERS_PATH = Path("/var/lib/nothing-but-stats/members.json")

# Team nickname stems used to match guild emoji names (which come in several
# undocumented spellings per team, e.g. "Hawks", "Hawks2", "hawkstb") to an
# abbreviation. Checked both against the raw lowercased emoji name and against
# the name with a trailing "2" or "tb" stripped.
NICKNAMES = {
    "ATL": ["hawk", "hawks"],
    "BKN": ["net", "nets"],
    "BOS": ["celtic", "celtics"],
    "CHA": ["hornet", "hornets"],
    "CHI": ["bull", "bulls"],
    "CLE": ["cav", "cavs", "cavalier", "cavaliers"],
    "DAL": ["mav", "mavs", "maverick", "mavericks"],
    "DEN": ["nugget", "nuggets"],
    "DET": ["piston", "pistons"],
    "GSW": ["warrior", "warriors"],
    "HOU": ["rocket", "rockets"],
    "IND": ["pacer", "pacers"],
    "LAC": ["clipper", "clippers"],
    "LAL": ["laker", "lakers"],
    "MEM": ["grizzly", "grizzlies"],
    "MIA": ["heat"],
    "MIL": ["buck", "bucks"],
    "MIN": ["wolf", "wolves", "timberwolf", "timberwolves"],
    "NOP": ["pelican", "pelicans"],
    "NYK": ["knick", "knicks"],
    "OKC": ["thunder"],
    "ORL": ["magic"],
    "PHI": ["sixer", "sixers", "76er", "76ers"],
    "PHX": ["sun", "suns"],
    "POR": ["blazer", "blazers", "trailblazer", "trailblazers"],
    "SAC": ["king", "kings"],
    "SAS": ["spur", "spurs"],
    "TOR": ["raptor", "raptors"],
    "UTA": ["jazz"],
    "WAS": ["wizard", "wizards"],
}


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


def build_emoji_team_map(client: httpx.Client, guild_id: str) -> tuple[dict, list]:
    """Returns {emoji_id: team_abbr}, plus the list of emoji names that didn't match any team."""
    emojis = _get(client, f"{API_BASE}/guilds/{guild_id}/emojis")
    emoji_team = {}
    unmatched = []
    for e in emojis:
        name = e["name"].lower()
        stripped = re.sub(r"(tb|2)$", "", name)
        matched = None
        for abbr, nicks in NICKNAMES.items():
            if name in nicks or stripped in nicks:
                if matched and matched != abbr:
                    print(f"  WARNING: emoji {e['name']!r} matches both {matched} and {abbr}", file=sys.stderr)
                matched = abbr
        if matched:
            emoji_team[e["id"]] = matched
        else:
            unmatched.append(e["name"])
    return emoji_team, unmatched


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
    return messages


def fetch_reactors(client: httpx.Client, channel_id: str, message_id: str,
                    emoji_name: str, emoji_id: str | None) -> list[dict]:
    users = []
    after = None
    emoji_key = urllib.parse.quote(f"{emoji_name}:{emoji_id}" if emoji_id else emoji_name)
    while True:
        batch = _get(
            client,
            f"{API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{emoji_key}",
            limit=100, **({"after": after} if after else {}),
        )
        if not batch:
            break
        users.extend(batch)
        if len(batch) < 100:
            break
        after = batch[-1]["id"]
    return users


def parse_teams(content: str) -> set[str]:
    teams = set()
    for match in BLOCK_RE.finditer(content):
        abbr = _alias_to_abbr.get(match.group("team").lower())
        if abbr:
            teams.add(abbr)
    return teams


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="transaction",
                         help="substring to match in channel names (case-insensitive)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set")

    with httpx.Client(headers=_headers(token), timeout=30) as client:
        guild_id = resolve_guild_id(client)

        emoji_team, unmatched = build_emoji_team_map(client, guild_id)
        print(f"Mapped {len(emoji_team)} team emoji across {len(set(emoji_team.values()))} teams.")
        if unmatched:
            print(f"  (unmatched non-team emoji, ignored for the for/against check: {unmatched})", file=sys.stderr)
        missing_teams = set(NICKNAMES) - set(emoji_team.values())
        if missing_teams:
            print(f"  WARNING: no emoji found for: {sorted(missing_teams)}", file=sys.stderr)

        channels = find_matching_channels(client, guild_id, args.pattern)
        if not channels:
            sys.exit(f"No channels matched pattern {args.pattern!r}")
        print(f"Matched {len(channels)} channel(s): {', '.join(c['name'] for c in channels)}")

        # per_user[discord_id] = {"discord_username":..., "teams": {abbr: {"for": n, "against": n}}}
        per_user = {}
        unparseable = 0
        scored_messages = 0

        for ch in channels:
            print(f"Scanning #{ch['name']}...")
            msgs = fetch_channel_history(client, ch["id"])
            trade_msgs = [m for m in msgs if m.get("reactions")]
            print(f"  {len(msgs)} messages, {len(trade_msgs)} with reactions")

            for m in trade_msgs:
                teams_in_trade = parse_teams(m.get("content", ""))
                if len(teams_in_trade) < 2:
                    unparseable += 1
                    continue
                scored_messages += 1

                # participant_id -> set of team abbrs they backed via a logo-emoji reaction
                participant_backed = defaultdict(set)
                participant_username = {}

                for r in m["reactions"]:
                    eid = r["emoji"].get("id")
                    ename = r["emoji"]["name"]
                    reactors = fetch_reactors(client, ch["id"], m["id"], ename, eid)
                    team = emoji_team.get(eid) if eid else None
                    for u in reactors:
                        participant_username[u["id"]] = u.get("username", "unknown")
                        if team:
                            participant_backed[u["id"]].add(team)
                        else:
                            participant_backed.setdefault(u["id"], set())

                for uid, backed in participant_backed.items():
                    rec = per_user.setdefault(uid, {
                        "discord_username": participant_username[uid],
                        "teams": defaultdict(lambda: {"for": 0, "against": 0}),
                    })
                    for team in teams_in_trade:
                        if team in backed:
                            rec["teams"][team]["for"] += 1
                        else:
                            rec["teams"][team]["against"] += 1

        print(f"Scored {scored_messages} trade messages ({unparseable} skipped — team headers didn't parse).")

        members = {}
        if MEMBERS_PATH.exists():
            members = json.loads(MEMBERS_PATH.read_text())
        discord_to_member = {
            str(m["discord_id"]): name
            for name, m in members.items()
            if m.get("discord_id")
        }

        users_out = []
        for uid, rec in per_user.items():
            teams_out = {}
            total_for = total_against = 0
            for team, counts in rec["teams"].items():
                f, a = counts["for"], counts["against"]
                total_for += f
                total_against += a
                teams_out[team] = {
                    "for": f, "against": a, "total": f + a,
                    "pct": round(f / (f + a), 4) if (f + a) else None,
                }
            total = total_for + total_against
            users_out.append({
                "discord_id": uid,
                "name": discord_to_member.get(uid) or rec["discord_username"],
                "linked": uid in discord_to_member,
                "teams": teams_out,
                "overall": {
                    "for": total_for, "against": total_against, "total": total,
                    "pct": round(total_for / total, 4) if total else None,
                },
            })
        users_out.sort(key=lambda u: -u["overall"]["total"])

        out = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "channels": [c["name"] for c in channels],
            "scored_messages": scored_messages,
            "skipped_messages": unparseable,
            "scope": (
                "For each trade message a member reacted to (any emoji), they're scored FOR "
                "every team in that trade whose logo emoji (any variant) they also reacted with, "
                "and AGAINST every other team named in that same trade."
            ),
            "users": users_out,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=2))
        print(f"Wrote {len(users_out)} users -> {args.out}")


if __name__ == "__main__":
    main()

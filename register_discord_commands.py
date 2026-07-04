#!/usr/bin/env python3
"""Register NBN's Discord slash commands with the Discord API.

Run this once (and again whenever the COMMANDS list below changes). Slash command
*definitions* are registered with Discord here; the actual handling happens in
routers/discord.py at request time.

Env vars required:
  DISCORD_APP_ID     — the application (client) ID
  DISCORD_BOT_TOKEN  — a bot token for the same application (Bot -> Reset Token)
  DISCORD_GUILD_ID   — optional; if set, registers to that one server (instant).
                       Without it, commands register globally (up to ~1h to appear).

  venv/bin/python register_discord_commands.py
"""

import os
import sys

import httpx

APP_ID    = os.environ["DISCORD_APP_ID"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID  = os.environ.get("DISCORD_GUILD_ID", "").strip()

# Option types: 3 = STRING. See Discord ApplicationCommandOptionType.
_PLAYER_OPT = {
    "name": "player",
    "description": "Player name, e.g. Kevin Durant",
    "type": 3,
    "required": True,
}

COMMANDS = [
    {
        "name": "help",
        "description": "List all NBN bot commands",
    },
    {
        "name": "stats",
        "description": "Season-by-season averages for a player (regular season + playoffs)",
        "options": [_PLAYER_OPT],
    },
    {
        "name": "career",
        "description": "Career totals + averages for a player (regular season + playoffs)",
        "options": [_PLAYER_OPT],
    },
    {
        "name": "awards",
        "description": "A player's honors: rings, MVP, All-NBN, All-Star, and more",
        "options": [_PLAYER_OPT],
    },
    {
        "name": "team",
        "description": "A team's roster + per-game stats for a season (defaults to latest)",
        "options": [
            {
                "name": "team",
                "description": "Team abbreviation, e.g. HOU",
                "type": 3,
                "required": True,
            },
            {
                "name": "season",
                "description": "Season, e.g. 25-26 (defaults to most recent)",
                "type": 3,
                "required": False,
            },
        ],
    },
    {
        "name": "leaders",
        "description": "Top 10 in a stat (regular-season totals), optionally filtered by season/team",
        "options": [
            {
                "name": "stat",
                "description": "Stat category (default: points)",
                "type": 3,
                "required": False,
                "choices": [
                    {"name": "Points",      "value": "PTS"},
                    {"name": "Rebounds",    "value": "REB"},
                    {"name": "Assists",     "value": "AST"},
                    {"name": "Steals",      "value": "STL"},
                    {"name": "Blocks",      "value": "BLK"},
                    {"name": "3-Pointers",  "value": "3PM"},
                ],
            },
            {"name": "season", "description": "Season, e.g. 25-26", "type": 3, "required": False},
            {"name": "team",   "description": "Team abbreviation, e.g. HOU", "type": 3, "required": False},
        ],
    },
    {
        "name": "compare",
        "description": "Compare two players' per-game averages (career or one season)",
        "options": [
            {"name": "player1", "description": "First player",  "type": 3, "required": True},
            {"name": "player2", "description": "Second player", "type": 3, "required": True},
            {"name": "season",  "description": "Season, e.g. 25-26 (default: career)", "type": 3, "required": False},
        ],
    },
    {
        "name": "standings",
        "description": "Conference standings for a season (defaults to latest)",
        "options": [
            {"name": "season", "description": "Season, e.g. 25-26 (defaults to most recent)", "type": 3, "required": False},
        ],
    },
    {
        "name": "playoff-series",
        "description": "Result of a playoff series between two teams in a given year",
        "options": [
            {"name": "year",  "description": "Season/year, e.g. 25-26 or 2026", "type": 3, "required": True},
            {"name": "team1", "description": "First team, e.g. PHX",  "type": 3, "required": True},
            {"name": "team2", "description": "Second team, e.g. LAL", "type": 3, "required": True},
        ],
    },
    {
        "name": "nbyen-leaders",
        "description": "Net worth leaders — cash + NBN Wall Street holdings",
    },
    {
        "name": "trades",
        "description": "A member's NBN Wall Street P&L per stock (realized + unrealized)",
        "options": [
            {"name": "member", "description": "Member name (defaults to you)", "type": 3, "required": False},
        ],
    },
    {
        "name": "h2h",
        "description": "All-time head-to-head record between two teams",
        "options": [
            {"name": "team1", "description": "Team abbreviation, e.g. PHX", "type": 3, "required": True},
            {"name": "team2", "description": "Team abbreviation, e.g. LAL", "type": 3, "required": True},
        ],
    },
    {
        "name": "mh2h",
        "description": "All-time head-to-head record between two NBN members",
        "options": [
            {"name": "member1", "description": "First member", "type": 6, "required": True},
            {"name": "member2", "description": "Second member", "type": 6, "required": True},
        ],
    },
    {
        "name": "champions",
        "description": "Every season's NBN champion and runner-up",
    },
    {
        "name": "whoami",
        "description": "Show your Discord ID and which NBN member you're linked to",
    },
    {
        "name": "nbyen",
        "description": "Check your NB¥ balance (only you can see it)",
    },
    {
        "name": "tip",
        "description": "Tip NB¥ to another member",
        "options": [
            {"name": "user",    "description": "Who to tip", "type": 6, "required": True},
            {"name": "amount",  "description": "How much NB¥", "type": 4, "required": True, "min_value": 1},
            {"name": "message", "description": "Optional note (shown for tips ≥ 25)", "type": 3, "required": False},
        ],
    },
    {
        "name": "link",
        "description": "(Admin) Link a Discord user to an NBN member",
        "options": [
            {"name": "member", "description": "NBN member name", "type": 3, "required": True},
            {"name": "user",   "description": "Discord user to link", "type": 6, "required": True},
        ],
    },
]

if GUILD_ID:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
else:
    url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"

# PUT bulk-overwrites the full command set (removes anything not listed).
resp = httpx.put(url, json=COMMANDS,
                 headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=30)

print(resp.status_code)
print(resp.text)
if resp.status_code >= 300:
    sys.exit(1)
print(f"\nRegistered {len(COMMANDS)} command(s) "
      + (f"to guild {GUILD_ID}." if GUILD_ID else "globally."))

"""Discord interactions (slash commands).

Discord delivers slash commands as HTTPS POSTs to a single Interactions Endpoint
URL (configured in the Discord Developer Portal). Every request is signed with
the application's Ed25519 key and MUST be verified, or Discord refuses to even
register the endpoint. This router exposes that one endpoint and dispatches by
command name.

Commands are stateless reads against the static stats CSVs the R build produces
in the nbn-today repo, so responses are returned inline (type 4) well within
Discord's 3-second deadline. No bot gateway / long-running process is involved.

Setup (one-time, see register_discord_commands.py and docs):
  * DISCORD_PUBLIC_KEY env var — the app's public key (hex), used to verify sigs.
  * Register the command definitions once via register_discord_commands.py.
  * Point the portal's "Interactions Endpoint URL" at /api/discord/interactions.
"""

import csv
import io
import json
import logging
import os
import re
from difflib import get_close_matches
from pathlib import Path

from fastapi import APIRouter, Request, Response

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .constants import DATA_DIR  # NBS_DATA_DIR; holds raw playoff box scores
from .auth import load_members, save_members
from .tips import perform_tip, TipError
from .invest import get_all_holdings, compute_member_pnl  # net worth + per-stock P&L

logger = logging.getLogger("nbn-api.discord")

router = APIRouter()

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
# Discord user id allowed to /link accounts (bootstraps the admin before anyone
# is linked). Members with the `admin` role can also link once they're linked.
DISCORD_ADMIN_ID = os.environ.get("DISCORD_ADMIN_ID", "")

# Stats CSVs live in the site repo (written by build/build.sh), not in DATA_DIR.
SITE_DIR     = Path("/home/skim/projects/nbn-today")
SEASONS_CSV  = SITE_DIR / "players" / "player_seasons.csv"
PLAYOFFS_CSV = SITE_DIR / "players" / "player_seasons_playoffs.csv"
AWARDS_CSV   = SITE_DIR / "players" / "player_awards.csv"
STANDINGS_CSV = SITE_DIR / "standings" / "standings-history.csv"
BRACKETS_CSV  = SITE_DIR / "standings" / "playoff-brackets.csv"
H2H_CSV       = SITE_DIR / "data" / "h2h-alltime.csv"
H2H_PO_CSV    = SITE_DIR / "data" / "h2h-playoffs.csv"
BALANCES_JSON = DATA_DIR / "member-balances.json"
ALLSTATS_GLOB = "allstats-*.csv"

# Stat option value -> (CSV column, display label) for /leaders.
LEADER_STATS = {
    "PTS": "Points", "REB": "Rebounds", "AST": "Assists",
    "STL": "Steals", "BLK": "Blocks", "3PM": "3-Pointers",
}

ROUND_NAMES = {"1": "First Round", "2": "Conf Semifinals",
               "3": "Conf Finals", "4": "NBN Finals"}

# Discord interaction + response type enums (from the Discord API docs).
PING                       = 1
APPLICATION_COMMAND        = 2
PONG                       = 1
CHANNEL_MESSAGE            = 4   # CHANNEL_MESSAGE_WITH_SOURCE
EPHEMERAL                  = 1 << 6  # message flag: only the invoker sees it

# Fields summed when collapsing multiple team-rows into one season, mirroring
# aggregateBySeasonRows() in players/index.html.
SUM_FIELDS = ["G", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV", "PF",
              "FGM", "FGA", "3PM", "3PA", "FTM", "FTA", "GMSC"]

NBN_BLUE = 0x1D63E6  # fallback embed accent
GREEN    = 0x2ECC71
RED      = 0xE74C3C

TEAM_NAMES = {
    "ATL": "Atlanta Hawks", "BKN": "Brooklyn Nets", "BOS": "Boston Celtics",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}

# Team primary colors — used as the embed accent for the player's current team.
TEAM_COLORS = {
    "ATL": 0xE03A3E, "BKN": 0x000000, "BOS": 0x007A33, "CHA": 0x1D1160,
    "CHI": 0xCE1141, "CLE": 0x860038, "DAL": 0x00538C, "DEN": 0x0E2240,
    "DET": 0xC8102E, "GSW": 0x1D428A, "HOU": 0xCE1141, "IND": 0xFDBB30,
    "LAC": 0xC8102E, "LAL": 0x552583, "MEM": 0x5D76A9, "MIA": 0x98002E,
    "MIL": 0x00471B, "MIN": 0x0C2340, "NOP": 0x0C2340, "NYK": 0xF58426,
    "OKC": 0x007AC1, "ORL": 0x0077C0, "PHI": 0x006BB6, "PHX": 0x1D1160,
    "POR": 0xE03A3E, "SAC": 0x5A2D81, "SAS": 0xC4CED4, "TOR": 0xCE1141,
    "UTA": 0x002B5C, "WAS": 0x002B5C,
}


# ── Signature verification ────────────────────────────────────────────────────

def _verify(request_body: bytes, signature: str, timestamp: str) -> bool:
    if not DISCORD_PUBLIC_KEY or not signature or not timestamp:
        return False
    try:
        vk = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        vk.verify(timestamp.encode() + request_body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


# ── Stats CSV loading (mtime-cached) ──────────────────────────────────────────

_csv_cache: dict[str, tuple[float, list[dict]]] = {}


def _load_csv(path: Path) -> list[dict]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _csv_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    _csv_cache[str(path)] = (mtime, rows)
    return rows


# ── Player name resolution ────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _display(player_field: str) -> str:
    """'Durant, Kevin' -> 'Kevin Durant'."""
    if "," in player_field:
        last, _, first = player_field.partition(",")
        return f"{first.strip()} {last.strip()}"
    return player_field


def resolve_player(query: str):
    """Resolve a free-form name to (slug, display_name), or return a dict of
    suggestions / not-found info. Only players who actually have stats rows are
    considered, since those are the only ones we can show."""
    rows = _load_csv(SEASONS_CSV)
    # slug -> display name (one entry per player)
    players: dict[str, str] = {}
    for r in rows:
        slug = (r.get("SLUG") or "").strip()
        if slug and slug not in players:
            players[slug] = _display(r.get("PLAYER", ""))

    q = _norm(query)
    if not q:
        return {"error": "Usage: `/stats player:<name>`"}

    # Index normalized name + slug -> slug.
    by_name: dict[str, str] = {}
    for slug, name in players.items():
        by_name.setdefault(_norm(name), slug)
        by_name.setdefault(_norm(slug), slug)

    # 1) exact normalized match
    if q in by_name:
        slug = by_name[q]
        return slug, players[slug]

    # 2) substring match
    contains = [(slug, name) for slug, name in players.items() if q in _norm(name)]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        names = sorted({name for _, name in contains})[:8]
        return {"error": f"**{query}** is ambiguous. Did you mean: "
                         + ", ".join(names) + "?"}

    # 3) fuzzy fallback
    close = get_close_matches(q, list(by_name.keys()), n=5, cutoff=0.6)
    if close:
        names = sorted({players[by_name[c]] for c in close})
        return {"error": f"No exact match for **{query}**. Did you mean: "
                         + ", ".join(names) + "?"}
    return {"error": f"No player found matching **{query}**."}


# ── Season aggregation ────────────────────────────────────────────────────────

def _to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def aggregate_seasons(rows: list[dict]) -> list[dict]:
    """Collapse a player's rows into one entry per season. When a player has
    >1 team in a season (mid-season trade), the teams are listed chronologically
    (by LAST_DATE) joined with '->', and the stats are summed."""
    by_season: dict[str, dict] = {}
    for r in rows:
        season = r.get("SEASON", "")
        if not season or season == "NA":
            continue
        agg = by_season.setdefault(season, {"SEASON": season, "_teams": []})
        agg["_teams"].append((r.get("LAST_DATE", ""), r.get("TEAM", "")))
        for f in SUM_FIELDS:
            agg[f] = agg.get(f, 0.0) + _to_num(r.get(f))

    out = []
    for agg in by_season.values():
        teams = [t for _, t in sorted(agg["_teams"], key=lambda x: x[0])]
        # de-dupe while preserving chronological order
        seen, ordered = set(), []
        for t in teams:
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)
        agg["TEAM"] = "/".join(ordered) if ordered else "—"
        out.append(agg)
    out.sort(key=lambda a: _season_sort_key(a["SEASON"]))
    return out


def _season_sort_key(season: str) -> float:
    m = re.match(r"(\d+)", season)
    return float(m.group(1)) if m else 0.0


# ── /stats response formatting ────────────────────────────────────────────────

def _pg(agg, field, d=1):
    g = agg.get("G", 0.0)
    return f"{agg.get(field, 0.0) / g:.{d}f}" if g else "0.0"


def _pct(agg, made, att):
    a = agg.get(att, 0.0)
    return f"{agg.get(made, 0.0) / a * 100:.1f}" if a else "—"


# columns: (header, min-width, left-aligned?, value-fn)
_COLS = [
    ("SEASON", 6, True,  lambda a: a["SEASON"].replace(" Playoffs", "")),
    ("TEAM",   4, True,  lambda a: a["TEAM"]),
    ("G",      2, False, lambda a: str(int(a.get("G", 0)))),
    ("MPG",    4, False, lambda a: _pg(a, "MIN")),
    ("PPG",    4, False, lambda a: _pg(a, "PTS")),
    ("RPG",    4, False, lambda a: _pg(a, "REB")),
    ("APG",    4, False, lambda a: _pg(a, "AST")),
    ("SPG",    4, False, lambda a: _pg(a, "STL")),
    ("BPG",    4, False, lambda a: _pg(a, "BLK")),
]


def _table(seasons: list[dict]) -> str:
    # Size each column to the widest cell (incl. header), then join with a space
    # so values never collide even when one fills its column exactly.
    cells = [[fn(a) for _, _, _, fn in _COLS] for a in seasons]
    widths = []
    for i, (h, w, _, _) in enumerate(_COLS):
        widths.append(max([w, len(h)] + [len(row[i]) for row in cells]))

    def fmt(values):
        return " ".join(
            v.ljust(widths[i]) if _COLS[i][2] else v.rjust(widths[i])
            for i, v in enumerate(values)
        )

    lines = [fmt([h for h, _, _, _ in _COLS])]
    lines += [fmt(row) for row in cells]
    return "\n".join(lines)


def _grid(cols: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    """Generic auto-width table. `cols` is [(header, left_aligned?), …]; `rows`
    is a list of equal-length string lists. Columns are space-separated so cells
    never collide."""
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, (h, _) in enumerate(cols)]

    def fmt(vals):
        return " ".join(v.ljust(widths[i]) if cols[i][1] else v.rjust(widths[i])
                        for i, v in enumerate(vals))

    return "\n".join([fmt([h for h, _ in cols])] + [fmt(r) for r in rows])


def _short_name(player: str) -> str:
    """'Green, Jalen' -> 'J. Green'."""
    p = player.strip()
    if "," in p:
        last, _, first = p.partition(",")
        first, last = first.strip(), last.strip().title()
        return (f"{first[0].upper()}. {last}" if first else last)
    return p


def _player_meta(rows: list[dict]) -> tuple[str, str]:
    """Photo URL + most-recent team from a player's raw rows (latest LAST_DATE)."""
    photo, team = "", ""
    latest = ""
    for r in rows:
        d = r.get("LAST_DATE", "")
        if d >= latest:
            latest = d
            team = (r.get("TEAM") or "").strip()
        if not photo and (r.get("PHOTO_URL") or "").strip():
            photo = r["PHOTO_URL"].strip()
    return photo, team


def _error(msg: str) -> dict:
    return {"type": CHANNEL_MESSAGE, "data": {"content": msg, "flags": EPHEMERAL}}


def _player_embed(slug: str, name: str, meta_rows: list[dict],
                  description: str, footer: str) -> dict:
    """Standard player card shared by all player commands: name links to the
    profile, accent colored to the current team, photo on its own line below."""
    photo, team = _player_meta(meta_rows)
    if len(description) > 4090:  # embed description hard cap is 4096
        description = description[:4080] + "\n…"
    embed = {
        "title": name,
        "url": f"https://nbn.today/players/?p={slug}",
        "color": TEAM_COLORS.get(team, NBN_BLUE),
        "description": description,
        "footer": {"text": footer + (f" · {team}" if team else "")},
    }
    if photo:
        # `image` (full-width, own line, below the text) rather than `thumbnail`
        # (top-right) so the photo never narrows the stat tables.
        embed["image"] = {"url": photo}
    return {"type": CHANNEL_MESSAGE, "data": {"embeds": [embed]}}


def stats_response(query: str) -> dict:
    resolved = resolve_player(query)
    if isinstance(resolved, dict):  # error / suggestions
        return _error(resolved["error"])
    slug, name = resolved

    reg_rows = [r for r in _load_csv(SEASONS_CSV)  if (r.get("SLUG") or "").strip() == slug]
    po_rows  = [r for r in _load_csv(PLAYOFFS_CSV) if (r.get("SLUG") or "").strip() == slug]
    reg = aggregate_seasons(reg_rows)
    po  = aggregate_seasons(po_rows)

    if not reg and not po:
        return _error(f"No stats on record for **{name}**.")

    sections = []
    if reg:
        sections.append("**Regular Season**\n```\n" + _table(reg) + "\n```")
    if po:
        sections.append("**Playoffs**\n```\n" + _table(po) + "\n```")
    return _player_embed(slug, name, reg_rows or po_rows,
                         "\n".join(sections), "NBN season averages")


# ── /career ───────────────────────────────────────────────────────────────────

def _sum_rows(rows: list[dict]) -> dict:
    agg = {f: 0.0 for f in SUM_FIELDS}
    for r in rows:
        for f in SUM_FIELDS:
            agg[f] += _to_num(r.get(f))
    return agg


def _career_block(label: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    a = _sum_rows(rows)
    g = int(a["G"]) or 1
    avg = (f"{a['PTS']/g:.1f} PPG · {a['REB']/g:.1f} RPG · {a['AST']/g:.1f} APG"
           f" · {a['STL']/g:.1f} SPG · {a['BLK']/g:.1f} BPG")
    shoot = f"{_pct(a,'FGM','FGA')} FG% · {_pct(a,'3PM','3PA')} 3P% · {_pct(a,'FTM','FTA')} FT%"
    totals = (f"{int(a['PTS']):,} PTS · {int(a['REB']):,} REB · {int(a['AST']):,} AST"
              f" · {int(a['3PM']):,} 3PM")
    return (f"**{label} — {int(a['G'])} G**\n"
            f"{avg}\n{shoot}\nTotals: {totals}")


def career_response(query: str) -> dict:
    resolved = resolve_player(query)
    if isinstance(resolved, dict):
        return _error(resolved["error"])
    slug, name = resolved

    reg_rows = [r for r in _load_csv(SEASONS_CSV)  if (r.get("SLUG") or "").strip() == slug]
    po_rows  = [r for r in _load_csv(PLAYOFFS_CSV) if (r.get("SLUG") or "").strip() == slug]
    if not reg_rows and not po_rows:
        return _error(f"No stats on record for **{name}**.")

    blocks = [b for b in (_career_block("Regular Season", reg_rows),
                          _career_block("Playoffs", po_rows)) if b]
    return _player_embed(slug, name, reg_rows or po_rows,
                         "\n\n".join(blocks), "NBN career")


# ── /awards ───────────────────────────────────────────────────────────────────

# (csv award name, display label, emoji) in the order they should be listed.
_AWARD_ORDER = [
    ("Champion",                     "Champion",   "🏆"),
    ("Most Valuable Player",         "MVP",        "⭐"),
    ("Defensive Player of the Year", "DPOY",       "🛡️"),
    ("Rookie of the Year",           "ROY",        "🌟"),
    ("Sixth Man of the Year",        "6MOY",       "🔥"),
    ("Most Improved Player",         "MIP",        "📈"),
    ("All-NBN First Team",           "All-NBN 1st", "🏅"),
    ("All-NBN Second Team",          "All-NBN 2nd", "🏅"),
    ("All-NBN Third Team",           "All-NBN 3rd", "🏅"),
    ("All-Defense",                  "All-Defense", "🔒"),
    ("All-Star",                     "All-Star",   "🌠"),
    ("All-Rookie",                   "All-Rookie", "🍼"),
]


def _clean_season(s: str) -> str:
    return s.replace(" Playoffs", "")


def awards_response(query: str) -> dict:
    resolved = resolve_player(query)
    if isinstance(resolved, dict):
        return _error(resolved["error"])
    slug, name = resolved

    seasons_by_award: dict[str, list[str]] = {}
    for r in _load_csv(AWARDS_CSV):
        if (r.get("SLUG") or "").strip() == slug:
            seasons_by_award.setdefault(r.get("AWARD", ""), []).append(
                _clean_season(r.get("SEASON", "")))

    if not seasons_by_award:
        return _error(f"No awards on record for **{name}**.")

    lines = []
    for award, label, emoji in _AWARD_ORDER:
        seasons = sorted(set(seasons_by_award.get(award, [])))
        if not seasons:
            continue
        n = len(seasons)
        count = f"{n}× " if n > 1 else ""
        lines.append(f"{emoji} {count}{label} ({', '.join(seasons)})")

    meta_rows = [r for r in _load_csv(SEASONS_CSV) if (r.get("SLUG") or "").strip() == slug]
    return _player_embed(slug, name, meta_rows, "\n".join(lines), "NBN honors")


# ── /team ─────────────────────────────────────────────────────────────────────

_TEAM_COLS = [("PLAYER", True), ("G", False), ("PPG", False), ("RPG", False),
              ("APG", False), ("SPG", False), ("BPG", False)]


def _team_header(team: str, season: str) -> str:
    """Record / seed / margin line from data/{abbr}-seasons.csv, or '' if absent."""
    path = SITE_DIR / "data" / f"{team.lower()}-seasons.csv"
    row = next((r for r in _load_csv(path) if r.get("SEASON") == season), None)
    if not row:
        return ""
    try:
        pct = f"{float(row['PCT']):.3f}".lstrip("0")
    except (ValueError, KeyError):
        pct = row.get("PCT", "")
    badges = ""
    if str(row.get("FOTY")).upper() == "TRUE":
        badges += " · 🏆 FOTY"
    if str(row.get("COTY")).upper() == "TRUE":
        badges += " · 🧠 COTY"
    line1 = f"**{row.get('W')}–{row.get('L')}** ({pct}) · {row.get('SEED')} · {row.get('PLAYOFF_RESULT')}{badges}"
    diff = _to_num(row.get("DIFF"))
    line2 = f"{row.get('PPG')} PPG · {row.get('OPPG')} OPP · {diff:+.1f} DIFF"
    return f"{line1}\n{line2}"


def team_response(team_arg: str, season_arg: str) -> dict:
    team = team_arg.strip().upper()
    if team not in TEAM_NAMES:
        return _error(f"Unknown team **{team_arg}**. Use a 3-letter abbreviation, e.g. `HOU`.")

    team_rows = [r for r in _load_csv(SEASONS_CSV) if r.get("TEAM") == team]
    seasons = sorted({r["SEASON"] for r in team_rows if r.get("SEASON") and r["SEASON"] != "NA"},
                     key=_season_sort_key)
    if not seasons:
        return _error(f"No stats on record for **{TEAM_NAMES[team]}**.")

    season = season_arg.strip() or seasons[-1]
    players = [r for r in team_rows if r.get("SEASON") == season]
    if not players:
        return _error(f"No **{team}** roster for **{season}**. "
                      f"Seasons on record: {', '.join(seasons)}.")

    players.sort(key=lambda r: -_to_num(r.get("PTS")))
    data_rows = []
    for r in players:
        g = _to_num(r.get("G")) or 1
        data_rows.append([
            _short_name(r.get("PLAYER", "")),
            str(int(_to_num(r.get("G")))),
            f"{_to_num(r.get('PTS'))/g:.1f}",
            f"{_to_num(r.get('REB'))/g:.1f}",
            f"{_to_num(r.get('AST'))/g:.1f}",
            f"{_to_num(r.get('STL'))/g:.1f}",
            f"{_to_num(r.get('BLK'))/g:.1f}",
        ])

    header = _team_header(team, season)
    desc = (header + "\n\n" if header else "") + "```\n" + _grid(_TEAM_COLS, data_rows) + "\n```"
    if len(desc) > 4090:
        desc = desc[:4080] + "\n…"

    embed = {
        "title": f"{TEAM_NAMES[team]} — {season}",
        "url": f"https://nbn.today/teams/{team}/",
        "color": TEAM_COLORS.get(team, NBN_BLUE),
        "description": desc,
        "footer": {"text": "NBN team season · per-game averages"},
    }
    return {"type": CHANNEL_MESSAGE, "data": {"embeds": [embed]}}


def _embed(title, description, color=NBN_BLUE, url=None, footer=None):
    if len(description) > 4090:
        description = description[:4080] + "\n…"
    e = {"title": title, "color": color, "description": description}
    if url:
        e["url"] = url
    if footer:
        e["footer"] = {"text": footer}
    return {"type": CHANNEL_MESSAGE, "data": {"embeds": [e]}}


def _norm_season(s: str) -> str:
    """Accept '25-26' as-is, or a 4-digit year like 2026 -> '25-26'."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{4}", s):
        y = int(s)
        return f"{(y - 1) % 100:02d}-{y % 100:02d}"
    return s


# ── /leaders ──────────────────────────────────────────────────────────────────

def leaders_response(stat_arg: str, season_arg: str, team_arg: str) -> dict:
    stat = (stat_arg or "PTS").strip().upper()
    if stat not in LEADER_STATS:
        stat = "PTS"
    season = _norm_season(season_arg)
    team = (team_arg or "").strip().upper()
    if team and team not in TEAM_NAMES:
        return _error(f"Unknown team **{team_arg}**. Use a 3-letter abbreviation, e.g. `HOU`.")

    agg: dict[str, list] = {}  # slug -> [short_name, total]
    for r in _load_csv(SEASONS_CSV):
        if season and r.get("SEASON") != season:
            continue
        if team and r.get("TEAM") != team:
            continue
        slug = (r.get("SLUG") or "").strip()
        if not slug:
            continue
        e = agg.setdefault(slug, [_short_name(r.get("PLAYER", "")), 0.0])
        e[1] += _to_num(r.get(stat))

    if not agg:
        scope = " / ".join(x for x in (season, team) if x) or "those filters"
        return _error(f"No data for {scope}.")

    ranked = sorted(agg.values(), key=lambda x: -x[1])[:10]
    data_rows = [[f"{i+1}.", nm, f"{int(tot):,}"] for i, (nm, tot) in enumerate(ranked)]
    table = _grid([("#", True), ("PLAYER", True), (stat, False)], data_rows)

    scope = " ".join(x for x in (season, team) if x)
    title = f"{scope + ' ' if scope else 'All-Time '}{LEADER_STATS[stat]} Leaders"
    return _embed(title, "```\n" + table + "\n```",
                  color=TEAM_COLORS.get(team, NBN_BLUE),
                  footer="NBN leaders · regular-season totals")


# ── /compare ──────────────────────────────────────────────────────────────────

def compare_response(p1: str, p2: str, season_arg: str) -> dict:
    r1, r2 = resolve_player(p1), resolve_player(p2)
    if isinstance(r1, dict):
        return _error(r1["error"])
    if isinstance(r2, dict):
        return _error(r2["error"])
    (slug1, name1), (slug2, name2) = r1, r2
    season = _norm_season(season_arg)

    def gather(slug):
        rows = [r for r in _load_csv(SEASONS_CSV)
                if (r.get("SLUG") or "").strip() == slug
                and (not season or r.get("SEASON") == season)]
        return _sum_rows(rows), rows

    a1, rows1 = gather(slug1)
    a2, rows2 = gather(slug2)
    when = f" in {season}" if season else ""
    if not rows1:
        return _error(f"No stats for **{name1}**{when}.")
    if not rows2:
        return _error(f"No stats for **{name2}**{when}.")

    def pg(a, f):
        return f"{a[f] / (a['G'] or 1):.1f}"

    metrics = [
        ("G",   str(int(a1["G"])),              str(int(a2["G"]))),
        ("PPG", pg(a1, "PTS"),                  pg(a2, "PTS")),
        ("RPG", pg(a1, "REB"),                  pg(a2, "REB")),
        ("APG", pg(a1, "AST"),                  pg(a2, "AST")),
        ("SPG", pg(a1, "STL"),                  pg(a2, "STL")),
        ("BPG", pg(a1, "BLK"),                  pg(a2, "BLK")),
        ("FG%", _pct(a1, "FGM", "FGA"),         _pct(a2, "FGM", "FGA")),
        ("3P%", _pct(a1, "3PM", "3PA"),         _pct(a2, "3PM", "3PA")),
        ("FT%", _pct(a1, "FTM", "FTA"),         _pct(a2, "FTM", "FTA")),
    ]
    n1, n2 = _short_name(name1), _short_name(name2)
    table = _grid([("", True), (n1, False), (n2, False)], [list(m) for m in metrics])
    title = f"{name1} vs {name2} — {season if season else 'career'}"
    return _embed(title, "```\n" + table + "\n```",
                  footer="NBN compare · per-game averages")


# ── /standings ────────────────────────────────────────────────────────────────

def standings_response(season_arg: str) -> dict:
    rows = _load_csv(STANDINGS_CSV)
    seasons = sorted({r["SEASON"] for r in rows if r.get("SEASON")}, key=_season_sort_key)
    if not seasons:
        return _error("No standings on record.")
    season = _norm_season(season_arg) or seasons[-1]
    srows = [r for r in rows if r.get("SEASON") == season]
    if not srows:
        return _error(f"No standings for **{season}**. Seasons on record: {', '.join(seasons)}.")

    conf: dict[str, list] = {}
    for r in srows:
        c = (r.get("SEED") or "?-").split("-")[0]
        conf.setdefault(c, []).append(r)

    parts = []
    for c in ("East", "West"):
        teams = conf.get(c)
        if not teams:
            continue
        teams.sort(key=lambda r: int(r.get("SEED_NUM") or 999))
        data = [[f"{r.get('SEED_NUM')}.", r.get("TEAM"), f"{r.get('W')}–{r.get('L')}"]
                for r in teams]
        parts.append(f"**{c}ern Conference**\n```\n"
                     + _grid([("#", True), ("TM", True), ("W-L", False)], data) + "\n```")

    return _embed(f"Standings — {season}", "\n".join(parts), footer="NBN standings")


# ── /playoff-series ───────────────────────────────────────────────────────────

def _series_games_and_leaders(season: str, a: str, b: str) -> tuple[str, str]:
    """Game-by-game scores (team `a` first) and series P/R/A leaders from the
    raw playoff box scores in NBS_DATA_DIR. Returns ('', '') if unavailable."""
    yy = season.split("-")[-1]
    path = DATA_DIR / f"allstats-playoffs-{yy}.csv"
    if not path.exists():
        return "", ""
    rows = [r for r in _load_csv(path) if {r.get("TEAM"), r.get("OPP_TEAM")} == {a, b}]
    if not rows:
        return "", ""

    # One line per game, from team `a`'s perspective.
    games: dict[str, tuple[int, int]] = {}
    for r in rows:
        if r.get("TEAM") != a:
            continue
        g = r.get("GAME", "")
        if g not in games:
            games[g] = (int(_to_num(r.get("TEAM_PTS"))), int(_to_num(r.get("OPP_TEAM_PTS"))))
    glines = []
    for g in sorted(games, key=lambda x: int(x) if x.isdigit() else 0):
        ap, bp = games[g]
        glines.append(f"G{g}  {a} {ap:>3} – {bp:<3} {b}")
    games_str = "\n".join(glines)

    # Series totals per player, then top 3 in each category.
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("PLAYER", ""), r.get("TEAM", ""))
        e = agg.setdefault(key, {"P": 0.0, "R": 0.0, "A": 0.0})
        e["P"] += _to_num(r.get("P"))
        e["R"] += _to_num(r.get("R"))
        e["A"] += _to_num(r.get("A"))

    data_rows = []
    for label, key in (("PTS", "P"), ("REB", "R"), ("AST", "A")):
        top = sorted(agg.items(), key=lambda kv: -kv[1][key])[:3]
        for i, ((pl, tm), v) in enumerate(top):
            data_rows.append([label if i == 0 else "", _short_name(pl), tm, str(int(v[key]))])
    leaders_str = _grid([("", True), ("PLAYER", True), ("TM", True), ("TOT", False)], data_rows)
    return games_str, leaders_str


def playoff_series_response(year_arg: str, t1_arg: str, t2_arg: str) -> dict:
    season = _norm_season(year_arg)
    a, b = t1_arg.strip().upper(), t2_arg.strip().upper()
    for t, raw in ((a, t1_arg), (b, t2_arg)):
        if t not in TEAM_NAMES:
            return _error(f"Unknown team **{raw}**. Use a 3-letter abbreviation, e.g. `HOU`.")
    if a == b:
        return _error("Pick two different teams.")

    row = next((r for r in _load_csv(BRACKETS_CSV)
                if r.get("SEASON") == season and {r.get("T1"), r.get("T2")} == {a, b}), None)
    if not row:
        return _error(f"No playoff series between **{a}** and **{b}** in **{season}**.")

    t1, t2 = row["T1"], row["T2"]
    w1, w2 = int(_to_num(row["T1_W"])), int(_to_num(row["T2_W"]))
    winner = row["WINNER"]
    if winner == t1:
        wt, lt, ws, ls, wseed, lseed = t1, t2, w1, w2, row["T1_SEED"], row["T2_SEED"]
    else:
        wt, lt, ws, ls, wseed, lseed = t2, t1, w2, w1, row["T2_SEED"], row["T1_SEED"]

    rd = ROUND_NAMES.get(str(row.get("ROUND")), f"Round {row.get('ROUND')}")
    desc = (f"**{TEAM_NAMES[wt]}** ({wseed}) def. **{TEAM_NAMES[lt]}** ({lseed})\n"
            f"Series: **{ws}–{ls}** · {rd}")

    games_str, leaders_str = _series_games_and_leaders(season, a, b)
    if games_str:
        desc += f"\n\n**Games**\n```\n{games_str}\n```"
    if leaders_str:
        desc += f"\n**Series Leaders**\n```\n{leaders_str}\n```"

    return _embed(f"{a} vs {b} — {season} {rd}", desc,
                  color=TEAM_COLORS.get(winner, NBN_BLUE),
                  url=f"https://nbn.today/teams/{winner}/",
                  footer="NBN playoff series")


# ── /balances ─────────────────────────────────────────────────────────────────

def _money(v) -> str:
    return f"{int(round(v)):,}"


def balances_response() -> dict:
    # net worth = liquid NB¥ + current market value of NBN Wall Street holdings
    # (long shares + short equity), valued exactly as the site does.
    rows = [r for r in get_all_holdings() if r["net_worth"] > 0][:10]
    if not rows:
        return _error("No NB¥ balances on record yet.")
    data = [[f"{i+1}.", r["member"], _money(r["net_worth"]), _money(r["liquid"]), _money(r["equity"])]
            for i, r in enumerate(rows)]
    table = _grid([("#", True), ("MEMBER", True), ("NET", False), ("CASH", False), ("STK", False)], data)
    return _embed("💰 NB¥ Net Worth", "```\n" + table + "\n```",
                  footer="NBN economy · cash + Wall Street holdings")


# ── /h2h ──────────────────────────────────────────────────────────────────────

def _h2h_cell(path, a: str, b: str):
    row = next((r for r in _load_csv(path) if r.get("TEAM") == a), None)
    return (row.get(b) or "").strip() if row else ""


def _h2h_line(label: str, a: str, b: str, rec: str) -> str:
    if not rec:
        return f"{label}: no games"
    w, _, l = rec.partition("-")
    wi, li = int(_to_num(w)), int(_to_num(l))
    if wi > li:
        verdict = f"**{a} leads {wi}–{li}**"
    elif li > wi:
        verdict = f"**{b} leads {li}–{wi}**"
    else:
        verdict = f"tied {wi}–{li}"
    return f"{label}: {verdict}"


def _format_series_row(row) -> str:
    """'20-21 First Round: ATL def. BKN 4–0'"""
    season  = row.get("SEASON", "?")
    rd      = ROUND_NAMES.get(str(row.get("ROUND", "")), f"Round {row.get('ROUND')}")
    winner  = row.get("WINNER", "")
    t1, t2  = row.get("T1", ""), row.get("T2", "")
    w1, w2  = int(_to_num(row.get("T1_W", 0))), int(_to_num(row.get("T2_W", 0)))
    if winner == t1:
        loser, ww, lw = t2, w1, w2
    else:
        loser, ww, lw = t1, w2, w1
    return f"{season} {rd}: {winner} def. {loser} {ww}–{lw}"


def _playoff_series_block(series_rows) -> str:
    """Header line + one line per series, sorted chronologically."""
    rows = sorted(series_rows,
                  key=lambda r: (_season_sort_key(r.get("SEASON", "")), int(r.get("ROUND", 0))))
    n    = len(rows)
    noun = "series" if n == 1 else "series"
    header = f"Playoffs — {n} {noun}:"
    lines  = [_format_series_row(r) for r in rows]
    return header + "\n" + "\n".join(lines)


def h2h_response(t1_arg: str, t2_arg: str) -> dict:
    a, b = t1_arg.strip().upper(), t2_arg.strip().upper()
    # Auto-detect: if either arg isn't a known team abbr, try member H2H
    if a not in TEAM_NAMES or b not in TEAM_NAMES:
        return h2h_members_response(t1_arg, t2_arg)
    if a == b:
        return _error("Pick two different teams.")

    reg_line = _h2h_line("Regular season", a, b, _h2h_cell(H2H_CSV, a, b))
    series   = [r for r in _load_csv(BRACKETS_CSV) if {r.get("T1"), r.get("T2")} == {a, b}]
    po_block = _playoff_series_block(series) if series else "Playoffs: no series"

    desc = reg_line + "\n\n" + po_block
    return _embed(f"{a} vs {b} — all-time", desc,
                  color=TEAM_COLORS.get(a, NBN_BLUE), footer="NBN head-to-head")


def _load_all_games():
    """Load allstats CSVs; one entry per actual game (not per team-game row).

    Each game appears twice in the raw CSVs (once per team). We deduplicate using
    a canonical key of (date, sorted team pair, gametype) so the matching loop in
    h2h_members_response never counts the same game twice.
    """
    seen = set()
    games = []
    for path in sorted(DATA_DIR.glob(ALLSTATS_GLOB)):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                team = (row.get("TEAM") or "").strip()
                opp  = (row.get("OPP") or "").strip().lstrip("@")
                wl   = (row.get("WL") or "").strip()
                dt   = (row.get("DATE") or "").strip()
                gt   = (row.get("gametype") or "REG").strip()
                if not (team and opp and wl and dt):
                    continue
                # Canonical key: sort the two teams so each real game is stored once
                pair = tuple(sorted([team, opp]))
                key  = (dt, pair, gt)
                if key not in seen:
                    seen.add(key)
                    games.append((dt, team, opp, wl, gt))
    return games


def _tenures_for_member(name: str):
    """Return list of (team_upper, start_str, end_str) from members.json tenures."""
    member = load_members().get(name, {})
    result = []
    for t in member.get("tenures", []):
        team = (t.get("team") or "").strip().upper()
        start = (t.get("start") or "").strip()
        end   = (t.get("end") or "").strip() or "9999-12-31"
        if team and start:
            result.append((team, start, end))
    return result


def _resolve_member_fuzzy(arg: str):
    """Case-insensitive exact then fuzzy member name match. Returns (name, error_dict)."""
    al = (arg or "").strip().lower()
    members = load_members()
    # exact
    exact = next((n for n in members if n.lower() == al), None)
    if exact:
        return exact, None
    # fuzzy
    close = get_close_matches(al, [n.lower() for n in members], n=5, cutoff=0.6)
    if close:
        suggestions = [n for n in members if n.lower() in close][:5]
        return None, _error(f"No exact match for **{arg}**. Did you mean: "
                            + ", ".join(suggestions) + "?")
    return None, _error(f"Unknown member **{arg}**.")


def _season_playoff_date(season: str) -> str:
    """Return a representative playoff date for a season string like '20-21' → '2021-06-01'."""
    end_yy = (season.split("-")[-1] or "").strip()
    if len(end_yy) == 2:
        century = "19" if int(end_yy) > 50 else "20"
        return f"{century}{end_yy}-06-01"
    return f"{end_yy}-06-01"


def h2h_members_response(m1_arg: str, m2_arg: str) -> dict:
    m1_name, err = _resolve_member_fuzzy(m1_arg)
    if err:
        return err
    m2_name, err = _resolve_member_fuzzy(m2_arg)
    if err:
        return err
    if m1_name == m2_name:
        return _error("Pick two different members.")

    tenures1 = _tenures_for_member(m1_name)
    tenures2 = _tenures_for_member(m2_name)
    if not tenures1:
        return _error(f"**{m1_name}** has no tenure data.")
    if not tenures2:
        return _error(f"**{m2_name}** has no tenure data.")

    reg_w = reg_l = 0
    for dt, team, opp, wl, gt in _load_all_games():
        is_playoff = gt not in ("REG", "REGULAR", "")
        if is_playoff:
            continue
        m1_team = next((t for t, s, e in tenures1 if t == team and s <= dt <= e), None)
        m2_opp  = next((t for t, s, e in tenures2 if t == opp  and s <= dt <= e), None)
        if m1_team and m2_opp:
            if wl == "W": reg_w += 1
            elif wl == "L": reg_l += 1
            continue
        m2_team = next((t for t, s, e in tenures2 if t == team and s <= dt <= e), None)
        m1_opp  = next((t for t, s, e in tenures1 if t == opp  and s <= dt <= e), None)
        if m2_team and m1_opp:
            if wl == "W": reg_l += 1
            elif wl == "L": reg_w += 1

    # Playoff series from brackets, filtered by tenure
    po_series = []
    for row in _load_csv(BRACKETS_CSV):
        season  = row.get("SEASON", "")
        pd_date = _season_playoff_date(season)
        t1, t2  = row.get("T1", ""), row.get("T2", "")
        m1_t1 = next((t for t, s, e in tenures1 if t == t1 and s <= pd_date <= e), None)
        m2_t2 = next((t for t, s, e in tenures2 if t == t2 and s <= pd_date <= e), None)
        m2_t1 = next((t for t, s, e in tenures2 if t == t1 and s <= pd_date <= e), None)
        m1_t2 = next((t for t, s, e in tenures1 if t == t2 and s <= pd_date <= e), None)
        if (m1_t1 and m2_t2) or (m2_t1 and m1_t2):
            po_series.append(row)

    if reg_w + reg_l + len(po_series) == 0:
        return _error(f"No games found between **{m1_name}** and **{m2_name}** during overlapping tenures.")

    def fmt_reg(w, l):
        if w + l == 0:
            return "Regular season: no games"
        if w > l:   verdict = f"**{m1_name} leads {w}–{l}**"
        elif l > w: verdict = f"**{m2_name} leads {l}–{w}**"
        else:       verdict = f"tied {w}–{l}"
        return f"Regular season: {verdict}"

    desc = fmt_reg(reg_w, reg_l)
    if po_series:
        desc += "\n\n" + _playoff_series_block(po_series)
    elif reg_w + reg_l > 0:
        desc += "\n\nPlayoffs: no series"

    return _embed(f"{m1_name} vs {m2_name} — all-time", desc, footer="NBN head-to-head")


def mh2h_response(discord_id1: str, discord_id2: str) -> dict:
    """Member H2H via Discord user IDs (resolved from type-6 user options)."""
    m1 = member_by_discord_id(discord_id1)
    m2 = member_by_discord_id(discord_id2)
    if not m1:
        return _error("First user isn't linked to an NBN member. Ask an admin to `/link` them.")
    if not m2:
        return _error("Second user isn't linked to an NBN member. Ask an admin to `/link` them.")
    return h2h_members_response(m1, m2)


# ── /champions ────────────────────────────────────────────────────────────────

def champions_response() -> dict:
    champ, ru = {}, {}
    for r in _load_csv(STANDINGS_CSV):
        if r.get("PLAYOFF_RESULT") == "Champion":
            champ[r["SEASON"]] = r["TEAM"]
        elif r.get("PLAYOFF_RESULT") == "Runner-Up":
            ru[r["SEASON"]] = r["TEAM"]
    if not champ:
        return _error("No champions on record.")
    seasons = sorted(champ, key=_season_sort_key, reverse=True)
    data = [[s, champ[s], ru.get(s, "—")] for s in seasons]
    table = _grid([("SEASON", True), ("CHAMP", True), ("RUNNER-UP", True)], data)
    return _embed("🏆 NBN Champions", "```\n" + table + "\n```", footer="NBN history")


# ── Identity linking + economy (/whoami /link /balance /tip) ───────────────────

def _ephemeral(content: str) -> dict:
    return {"type": CHANNEL_MESSAGE, "data": {"content": content, "flags": EPHEMERAL}}


def member_by_discord_id(did: str):
    """Reverse-lookup a Discord user id -> NBN member name, or None."""
    if not did:
        return None
    for name, m in load_members().items():
        if str(m.get("discord_id") or "") == str(did):
            return name
    return None


def _is_discord_admin(did: str) -> bool:
    if did and DISCORD_ADMIN_ID and str(did) == str(DISCORD_ADMIN_ID):
        return True
    name = member_by_discord_id(did)
    return bool(name) and "admin" in (load_members().get(name, {}).get("roles", []))


def whoami_response(invoker: dict) -> dict:
    did = invoker.get("id", "")
    member = member_by_discord_id(did)
    line = f"Discord ID: `{did}`"
    if member:
        return _ephemeral(f"{line}\nLinked to NBN member: **{member}**")
    return _ephemeral(f"{line}\nNot linked yet — an admin can link you with "
                      f"`/link member:<your-name> user:@you`.")


def link_response(invoker: dict, member_arg: str, target_id: str) -> dict:
    if not _is_discord_admin(invoker.get("id", "")):
        return _error("Only an admin can link Discord accounts to members.")
    member = (member_arg or "").strip()
    if not member or not target_id:
        return _error("Usage: `/link member:<name> user:@discorduser`")
    members = load_members()
    if member not in members:
        return _error(f"Unknown member **{member}**.")
    # One Discord user maps to one member: drop the id from any other member.
    for n, m in members.items():
        if n != member and str(m.get("discord_id") or "") == str(target_id):
            m.pop("discord_id", None)
    members[member]["discord_id"] = str(target_id)
    save_members(members)
    return _ephemeral(f"✅ Linked <@{target_id}> to NBN member **{member}**.")


def balance_response(invoker: dict) -> dict:
    member = member_by_discord_id(invoker.get("id", ""))
    if not member:
        return _ephemeral("You're not linked to a member yet. Run `/whoami`, then ask "
                          "an admin to `/link` you.")
    bal = json.loads(BALANCES_JSON.read_text()).get(member, 0) if BALANCES_JSON.exists() else 0
    return _ephemeral(f"💰 **{member}** — NB¥{bal:,.2f}")


def tip_response(invoker: dict, target_id: str, amount_arg: str, message: str) -> dict:
    sender = member_by_discord_id(invoker.get("id", ""))
    if not sender:
        return _error("You're not linked to a member. Ask an admin to `/link` you first.")
    if not target_id:
        return _error("Pick someone to tip with the `user` option.")
    recipient = member_by_discord_id(target_id)
    if not recipient:
        return _error("That user isn't linked to an NBN member yet.")
    try:
        amount = int(str(amount_arg))
    except ValueError:
        return _error("Amount must be a whole number.")
    try:
        perform_tip(sender, recipient, float(amount), message)
    except TipError as e:
        return _error(str(e))
    msg = f"💸 **{sender}** tipped **{recipient}** NB¥{amount:,}!"
    if message.strip() and amount >= 25:
        msg += f"\n_{message.strip()}_"
    return {"type": CHANNEL_MESSAGE, "data": {"content": msg}}


def _resolve_member_name(arg: str):
    al = (arg or "").strip().lower()
    return next((name for name in load_members() if name.lower() == al), None)


def trades_response(invoker: dict, member_arg: str) -> dict:
    if member_arg.strip():
        member = _resolve_member_name(member_arg)
        if not member:
            return _error(f"Unknown member **{member_arg}**.")
    else:
        member = member_by_discord_id(invoker.get("id", ""))
        if not member:
            return _error("You're not linked. Use `/trades member:<name>` or ask an "
                          "admin to `/link` you.")

    data = compute_member_pnl(member)
    positions = data["positions"]
    if not positions:
        return _error(f"No NBN Wall Street activity for **{member}**.")

    sign = lambda v: f"{v:+,.0f}"
    rows = [[p["team"] + ("•" if p["open"] else ""), sign(p["realized"]), sign(p["unrealized"])]
            for p in positions[:15]]
    table = _grid([("STOCK", True), ("REAL", False), ("UNREAL", False)], rows)
    tr, tu = data["total_realized"], data["total_unrealized"]
    summary = f"**Realized {sign(tr)} · Unrealized {sign(tu)} · Net {sign(tr + tu)}** NB¥"
    desc = summary + "\n```\n" + table + "\n```\n• = currently held"
    return _embed(f"{member} — Wall Street P&L", desc,
                  color=GREEN if (tr + tu) >= 0 else RED,
                  footer="NBN economy · realized vs unrealized")


# ── /help ─────────────────────────────────────────────────────────────────────

_HELP_GROUPS = [
    ("Players", [
        "`/stats player` — season-by-season averages",
        "`/career player` — career totals + averages",
        "`/awards player` — rings, MVP, All-NBN, All-Star…",
        "`/compare player1 player2 [season]` — side-by-side averages",
    ]),
    ("Teams & League", [
        "`/team team [season]` — roster + per-game stats",
        "`/standings [season]` — conference standings",
        "`/leaders [stat] [season] [team]` — top-10 totals",
        "`/h2h team1 team2` — all-time H2H between two teams",
        "`/mh2h @member1 @member2` — all-time H2H between two members",
        "`/playoff-series year team1 team2` — series result, games, leaders",
        "`/champions` — every season's champion + runner-up",
    ]),
    ("NB¥ Economy", [
        "`/nbyen` — your balance (private)",
        "`/nbyen-leaders` — net worth leaders (cash + stocks)",
        "`/trades [member]` — Wall Street P&L per stock (realized + unrealized)",
        "`/tip user amount [message]` — tip NB¥ to a member",
        "`/whoami` — your Discord link status",
        "`/link member user` — (admin) link a Discord user to a member",
    ]),
]


def help_response() -> dict:
    fields = [{"name": title, "value": "\n".join(cmds), "inline": False}
              for title, cmds in _HELP_GROUPS]
    embed = {
        "title": "NBN Bot — Commands",
        "color": NBN_BLUE,
        "description": "Player names fuzzy-match; team args are 3-letter abbreviations "
                       "(e.g. `HOU`); seasons are `YY-YY` (e.g. `25-26`).",
        "fields": fields,
        "footer": {"text": "nbn.today"},
    }
    return {"type": CHANNEL_MESSAGE, "data": {"embeds": [embed], "flags": EPHEMERAL}}


# ── Command dispatch ──────────────────────────────────────────────────────────

def _option(data: dict, name: str) -> str:
    for opt in data.get("options", []):
        if opt.get("name") == name:
            return str(opt.get("value", "")).strip()
    return ""


def dispatch(data: dict, invoker: dict) -> dict:
    cmd = data.get("name", "")
    if cmd == "help":
        return help_response()
    if cmd == "whoami":
        return whoami_response(invoker)
    if cmd == "link":
        return link_response(invoker, _option(data, "member"), _option(data, "user"))
    if cmd == "nbyen":
        return balance_response(invoker)
    if cmd == "tip":
        return tip_response(invoker, _option(data, "user"), _option(data, "amount"),
                            _option(data, "message"))
    if cmd == "trades":
        return trades_response(invoker, _option(data, "member"))
    if cmd == "stats":
        return stats_response(_option(data, "player"))
    if cmd == "career":
        return career_response(_option(data, "player"))
    if cmd == "awards":
        return awards_response(_option(data, "player"))
    if cmd == "team":
        return team_response(_option(data, "team"), _option(data, "season"))
    if cmd == "leaders":
        return leaders_response(_option(data, "stat"), _option(data, "season"),
                                _option(data, "team"))
    if cmd == "compare":
        return compare_response(_option(data, "player1"), _option(data, "player2"),
                                _option(data, "season"))
    if cmd == "standings":
        return standings_response(_option(data, "season"))
    if cmd == "playoff-series":
        return playoff_series_response(_option(data, "year"), _option(data, "team1"),
                                       _option(data, "team2"))
    if cmd == "nbyen-leaders":
        return balances_response()
    if cmd == "h2h":
        return h2h_response(_option(data, "team1"), _option(data, "team2"))
    if cmd == "mh2h":
        u1 = next((o["value"] for o in data.get("options", []) if o["name"] == "member1"), None)
        u2 = next((o["value"] for o in data.get("options", []) if o["name"] == "member2"), None)
        return mh2h_response(str(u1), str(u2))
    if cmd == "champions":
        return champions_response()
    return _error(f"Unknown command: {cmd}")


@router.post("/api/discord/interactions")
async def interactions(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-Signature-Ed25519", "")
    ts   = request.headers.get("X-Signature-Timestamp", "")
    if not _verify(body, sig, ts):
        return Response("invalid request signature", status_code=401)

    payload = await request.json()
    itype = payload.get("type")
    if itype == PING:
        return {"type": PONG}
    if itype == APPLICATION_COMMAND:
        # Guild commands carry the invoker under member.user; DMs under user.
        invoker = (payload.get("member") or {}).get("user") or payload.get("user") or {}
        try:
            return dispatch(payload.get("data", {}), invoker)
        except Exception:
            logger.exception("discord command failed")
            return {"type": CHANNEL_MESSAGE,
                    "data": {"content": "Something went wrong.", "flags": EPHEMERAL}}
    return {"type": CHANNEL_MESSAGE,
            "data": {"content": "Unsupported interaction.", "flags": EPHEMERAL}}

"""
Resolve raw Discord trade-announcement messages (from fetch_discord_transactions.py)
into candidate historical trade transactions. Auto-resolves clean 2-team trades with
confidently matched players and team labels; flags anything ambiguous (3+ team trades,
unresolved team names, low-confidence player matches, or malformed parses) for manual
review rather than guessing.

Scope: this only extracts PLAYER movement (from_team -> to_team), not draft picks. The
original message text is preserved verbatim as the transaction's `description`.

Usage:
  python3 resolve_discord_trades.py [--in-dir DIR] [--out FILE]
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

import httpx

API = "http://localhost:8001"
IN_DIR_DEFAULT = Path("/var/lib/nothing-but-stats/discord-transactions-raw")
OUT_DEFAULT = Path("/var/lib/nothing-but-stats/discord-transactions-resolved.json")

TEAM_ALIASES = {
    "ATL": ["ATL", "Atlanta", "Hawks"],
    "BKN": ["BKN", "BRK", "Brooklyn", "Nets"],
    "BOS": ["BOS", "Boston", "Celtics"],
    "CHA": ["CHA", "Charlotte", "Hornets"],
    "CHI": ["CHI", "Chicago", "Bulls"],
    "CLE": ["CLE", "Cleveland", "Cavaliers", "Cavs"],
    "DAL": ["DAL", "Dallas", "Mavericks", "Mavs"],
    "DEN": ["DEN", "Denver", "Nuggets"],
    "DET": ["DET", "Detroit", "Pistons"],
    "GSW": ["GSW", "Golden State", "Warriors"],
    "HOU": ["HOU", "Houston", "Rockets"],
    "IND": ["IND", "Indiana", "Pacers"],
    "LAC": ["LAC", "Clippers", "LA Clippers", "Los Angeles Clippers"],
    "LAL": ["LAL", "Lakers", "LA Lakers", "Los Angeles Lakers"],
    "MEM": ["MEM", "Memphis", "Grizzlies"],
    "MIA": ["MIA", "Miami", "Heat"],
    "MIL": ["MIL", "Milwaukee", "Bucks"],
    "MIN": ["MIN", "Minnesota", "Timberwolves", "Wolves"],
    "NOP": ["NOP", "New Orleans", "Pelicans"],
    "NYK": ["NYK", "New York", "Knicks"],
    "OKC": ["OKC", "Oklahoma City", "Thunder"],
    "ORL": ["ORL", "Orlando", "Magic"],
    "PHI": ["PHI", "Philadelphia", "76ers", "Sixers"],
    "PHX": ["PHX", "Phoenix", "Suns"],
    "POR": ["POR", "Portland", "Trail Blazers", "Blazers"],
    "SAC": ["SAC", "Sacramento", "Kings"],
    "SAS": ["SAS", "San Antonio", "Spurs"],
    "TOR": ["TOR", "Toronto", "Raptors"],
    "UTA": ["UTA", "Utah", "Jazz"],
    "WAS": ["WAS", "WSH", "Washington", "Wizards"],
}

_alias_to_abbr = {
    alias.lower(): abbr for abbr, aliases in TEAM_ALIASES.items() for alias in aliases
}
_alias_pattern = "|".join(
    re.escape(a) for a in sorted(_alias_to_abbr, key=len, reverse=True)
)
BLOCK_RE = re.compile(
    rf"(?im)^[ \t]*(?P<team>{_alias_pattern})\b[ \t]*"
    rf"(?:rec(?:ei|ie)ves?)?[ \t]*:[ \t]*"
    rf"(?P<rest>.*?)"
    rf"(?=\n[ \t]*(?:{_alias_pattern})\b[ \t]*(?:rec(?:ei|ie)ves?)?[ \t]*:|\Z)",
    re.DOTALL,
)

PICK_RE = re.compile(
    r"(?i)\b(19|20)\d{2}\b|\b1st\b|\b2nd\b|\bpick\b|\bswap\b|\bprotect|"
    r"\bright(s)? to\b|\bconditional\b|\bfavorable\b|\bfirst\b|\bsecond\b|\bround\b|"
    r"^#\d+$|^\d+-\d+\b"
)
SUFFIX_RE = re.compile(r"(?i)\s+(jr\.?|sr\.?|ii|iii|iv)$")


def split_assets(text: str) -> list[str]:
    tokens, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth = max(0, depth - 1)
            cur += ch
        elif ch == "," and depth == 0:
            tokens.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        tokens.append(cur.strip())
    # Split further on top-level "and"/"&" joins (e.g. "Luke Kennard & Kevin Knox").
    final = []
    for t in tokens:
        parts = re.split(r"(?i)\s+(?:and|&)\s+", t)
        final.extend(p.strip() for p in parts if p.strip())
    return final


def build_name_map(bios: dict) -> tuple[dict, dict]:
    """Returns (full_name -> slug, last_name -> [slugs])."""
    name_map = {}
    by_last = {}
    for slug, bio in bios.items():
        name = bio.get("name", "")
        if "," not in name:
            continue
        last, first = [p.strip() for p in name.split(",", 1)]
        full = f"{first} {last}".lower()
        name_map.setdefault(full, slug)
        stripped = SUFFIX_RE.sub("", full)
        if stripped != full:
            name_map.setdefault(stripped, slug)
        by_last.setdefault(last.lower(), []).append(slug)
    return name_map, by_last


def resolve_player(token: str, name_map: dict, by_last: dict) -> tuple[str | None, str]:
    """Returns (slug_or_None, note)."""
    cleaned = re.sub(r"[’'`.]", "", token).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    candidates = [cleaned.lower(), SUFFIX_RE.sub("", cleaned.lower())]
    for cand in candidates:
        if cand in name_map:
            return name_map[cand], "exact"
    matches = difflib.get_close_matches(candidates[0], name_map.keys(), n=1, cutoff=0.85)
    if matches:
        return name_map[matches[0]], f"fuzzy:{matches[0]}"
    # Nickname fallback: if the token's last word uniquely identifies a
    # surname in the bios (e.g. "Mo Bamba" -> only one "Bamba"), accept it.
    last_word = SUFFIX_RE.sub("", cleaned).split(" ")[-1].lower()
    if last_word in by_last and len(by_last[last_word]) == 1:
        return by_last[last_word][0], f"lastname:{last_word}"
    return None, "unresolved"


def parse_message(content: str, name_map: dict, by_last: dict) -> dict:
    blocks = list(BLOCK_RE.finditer(content))
    reasons = []
    if len(blocks) < 2:
        return {"ok": False, "reasons": [f"only {len(blocks)} team block(s) parsed"]}

    parsed_blocks = []
    for m in blocks:
        abbr = _alias_to_abbr[m.group("team").lower()]
        assets = split_assets(m.group("rest"))
        players = []
        for a in assets:
            if PICK_RE.search(a):
                continue
            slug, note = resolve_player(a, name_map, by_last)
            if slug is None:
                reasons.append(f"unresolved player text: {a!r}")
            else:
                players.append((slug, note, a))
        parsed_blocks.append({"team": abbr, "players": players})

    teams = [b["team"] for b in parsed_blocks]
    if len(set(teams)) != len(teams):
        reasons.append(f"duplicate team block: {teams}")
    for b in parsed_blocks:
        for slug, note, _ in b["players"]:
            if note.startswith("fuzzy:") or note.startswith("lastname:"):
                reasons.append(f"low-confidence player match: {note} -> {slug}")

    if len(parsed_blocks) > 2:
        reasons.append(f"{len(parsed_blocks)}-team trade, from/to not derivable from text alone")

    if reasons:
        return {"ok": False, "reasons": reasons, "blocks": parsed_blocks}

    # Clean 2-team case: each side's players came from the other team.
    a, b = parsed_blocks
    transfers = []
    if a["players"]:
        transfers.append({
            "from_team": b["team"], "to_team": a["team"],
            "assets": [{"type": "player", "slug": s} for s, _, _ in a["players"]],
        })
    if b["players"]:
        transfers.append({
            "from_team": a["team"], "to_team": b["team"],
            "assets": [{"type": "player", "slug": s} for s, _, _ in b["players"]],
        })
    if not transfers:
        return {"ok": False, "reasons": ["no players found in either block (picks-only trade)"]}
    return {"ok": True, "transfers": transfers}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-dir", type=Path, default=IN_DIR_DEFAULT)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()

    bios = httpx.get(f"{API}/api/players", timeout=30).json()
    name_map, by_last = build_name_map(bios)

    resolved, flagged = [], []
    for path in sorted(args.in_dir.glob("*.json")):
        messages = json.loads(path.read_text())
        for msg in messages:
            result = parse_message(msg["content"], name_map, by_last)
            date = msg["timestamp"][:10]
            if result["ok"]:
                resolved.append({
                    "date": date,
                    "description": msg["content"],
                    "channel": msg["channel"],
                    "discord_id": msg["id"],
                    "transfers": result["transfers"],
                })
            else:
                flagged.append({
                    "date": date,
                    "description": msg["content"],
                    "channel": msg["channel"],
                    "discord_id": msg["id"],
                    "reasons": result["reasons"],
                })

    args.out.write_text(json.dumps({"resolved": resolved, "flagged": flagged}, indent=2))
    print(f"resolved: {len(resolved)}")
    print(f"flagged:  {len(flagged)}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()

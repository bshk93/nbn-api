"""
Resolve raw Discord fa-news messages (from fetch_discord_transactions.py, run
with --pattern fanews and --pattern fa-news against the dated fa-news
channels; the bare current-era `fa-news` channel is excluded on purpose --
see docs/discord-transaction-backfill.md) into candidate historical `sign`
and `option` transactions.

Scope (per user decision 2026-07-11): free-agent signings (including UDFA
signings once they resolve to an actual signing, not the procedural
round/window announcements) and team/player option accept/decline decisions.
Releases, renounces, two-way conversions, retirements, and trade-block chatter
that also live in these channels are out of scope and land in the `skipped`
bucket untouched.

Unlike resolve_discord_trades.py, a single message can yield MULTIPLE
candidate records (e.g. one message announcing three different signings), so
`resolved`/`flagged` entries are per-transaction, not per-message -- several
can share a `discord_id`.

Player resolution (name_map/by_last/resolve_player, nickname aliases,
false-match blocklist) is reused as-is from resolve_discord_trades.py rather
than re-implemented, since it was already refined over two sessions of manual
review there.

Usage:
  python3 resolve_discord_fa_signings.py [--in-dir DIR] [--out FILE]
"""
import argparse
import json
import re
from pathlib import Path

import httpx

from resolve_discord_trades import (
    TEAM_ALIASES, build_name_map, resolve_player,
)

API = "http://localhost:8001"
IN_DIR_DEFAULT = Path("/var/lib/nothing-but-stats/discord-fa-signings-raw")
OUT_DEFAULT = Path("/var/lib/nothing-but-stats/discord-fa-signings-resolved.json")
# role_id -> team abbr, built once via a Discord API call (guild roles happen
# to be named after team nicknames -- "Cavaliers", "Warriors", etc) since many
# fa-news messages reference a team by @-mentioning its role instead of
# spelling out the name. See docs/discord-transaction-backfill.md.
ROLE_TEAM_MAP_FILE = Path("/var/lib/nothing-but-stats/discord-role-team-map.json")

# Unlike trade headers ("HOU receives:"), fa-news messages consistently spell
# out the full "City Nickname" ("The Houston Rockets sign...") -- a two-word
# span resolve_discord_trades.py's single-token alias table doesn't cover on
# its own. Add the full names (and known alt-spellings) as extra aliases,
# local to this script so resolve_discord_trades's alias table is untouched.
FULL_TEAM_NAMES = {
    "ATL": "Atlanta Hawks", "BKN": "Brooklyn Nets", "BOS": "Boston Celtics",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "MEM": "Memphis Grizzlies", "MIA": "Miami Heat", "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers",
    "PHX": "Phoenix Suns", "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors", "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}
_EXTRA_ALIASES = {
    "Portland Trailblazers": "POR",  # one-word "Trailblazers" spelling seen in-corpus
    "Trailblazers": "POR",           # matches the guild's Discord role name for POR
    # One-off in-corpus typos of full team names, found reviewing the
    # "sign-mentioning but no template matched" skip bucket (session 5).
    "Orland Magic": "ORL",
    "New Orlean Pelicans": "NOP",
    "Porland Trailblazers": "POR",
    "Denver Nuggers": "DEN",
    "Golden Star Warriors": "GSW",
    "Indianapolis Pacers": "IND",
    "Washigton Wizards": "WAS",
    "Los Angles Lakers": "LAL",
    "Sacremento Kings": "SAC",
    "Portland Trail-Blazers": "POR",
}

_alias_to_abbr = {
    alias.lower(): abbr for abbr, aliases in TEAM_ALIASES.items() for alias in aliases
}
_alias_to_abbr.update({name.lower(): abbr for abbr, name in FULL_TEAM_NAMES.items()})
_alias_to_abbr.update({k.lower(): v for k, v in _EXTRA_ALIASES.items()})

# Longest-first so e.g. "Los Angeles Lakers" matches before "Lakers".
_TEAM_PATTERN = "|".join(
    re.escape(a) for a in sorted(_alias_to_abbr, key=len, reverse=True)
)

# ── Signings ───────────────────────────────────────────────────────────────
# Team-first: "The Denver Nuggets sign Judah Mintz to a 2yr 2way contract",
# "The Orlando Magic have now signed Chuma Okeke to his rookie contract.",
# "The Portland Trailblazers are signing Ty-Shon Alexander, ...", or with
# "The" dropped and just the nickname: "Utah signs Jase Richardson to his
# rookie scale contract", "Magic sign Cedric Coward to rookie scale".
SIGN_TEAM_FIRST_RE = re.compile(
    rf"(?i)^(?:The\s+)?(?P<team>{_TEAM_PATTERN})\s+"
    rf"(?:have\s+(?:now\s+)?signed|are\s+signing|sign(?:s|ed)?)\s+"
    rf"(?:their\s+UDFA\s+)?"
    rf"(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)"
    rf"(?:,?\s+an?\s+UDFA,?)?"
    rf"(?=\s+to\b|\s*[,.]|\s*$)"
)

# Player-first: "Myles Powell agrees to sign with the Phoenix Suns",
# "Matteo Spagnolo has signed with the Minnesota Timberwolves on a 1+1 TO min",
# "Steven Adams signs a 2yr $24,600,000 with the Miami Heat.", "Duncan Robinson
# has signed an offer sheet with the San Antonio Spurs on a 3yr ... contract.",
# "Vincent Poirier and Axel Toupane have signed with the Oklahoma City
# Thunder" (plural "have", two names -- _split_players handles the "and").
# "teh" tolerates a one-off in-corpus typo for "the".
SIGN_PLAYER_FIRST_RE = re.compile(
    rf"(?i)^(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+"
    rf"(?:agrees?\s+to\s+sign|(?:has|have)\s+signed|signs?)\b.*?\bwith\s+(?:the|teh)?\s*"
    rf"(?P<team>{_TEAM_PATTERN})\b"
)

# Deliberately narrow: "PLAYER will sign / has signed his/her qualifying
# offer ... with/from the TEAM" is an unconditional done deal (a qualifying
# offer is just a 1yr accept, no matching period) -- unlike the superficially
# similar "PLAYER will sign an/a ... offer sheet with TEAM. ORIGINAL_TEAM
# will have 48 hours to match", which is NOT final (the original team may
# still match and keep the player) and is deliberately left unmatched/flagged
# rather than risk recording a signing that didn't end up happening.
SIGN_QUALIFYING_OFFER_RE = re.compile(
    rf"(?i)^(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+"
    rf"(?:will\s+sign|has\s+signed)\s+(?:his|her)\s+qualifying\s+offer\b.*?\b(?:with|from)\s+(?:the|teh)?\s*"
    rf"(?P<team>{_TEAM_PATTERN})\b"
)

# Team-embedded variant: "Oshae Brissett has signed the Memphis Grizzlies'
# qualifying offer for $1,869,178" / "Chris Silva has signed the Miami
# Heat's qualifying offer for $1,869,178" -- team sits inside the clause
# ("the TEAM's qualifying offer") rather than after a with/from preposition.
SIGN_QUALIFYING_OFFER_TEAM_RE = re.compile(
    rf"(?i)^(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+"
    rf"has\s+signed\s+the\s+(?P<team>{_TEAM_PATTERN})'?s?\s+qualifying\s+offer\b"
)

# "The Orlando Magic elevate GG Jackson to the main roster and sign him to a
# 2+TO $6,433,026 contract" -- a two-way-to-standard conversion, worded as a
# "TEAM elevate PLAYER" clause with the actual "sign" verb applying to a
# pronoun ("him"), not the player's name, so the generic sign patterns can't
# reach the player name at all.
SIGN_ELEVATE_RE = re.compile(
    rf"(?i)^(?:The\s+)?(?P<team>{_TEAM_PATTERN})\s+elevate\s+"
    rf"(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+to\s+the\s+main\s+roster\b"
)

# "The Phoenix Suns match the Portland Trailblazers offer sheet and sign
# Deni Avdija to a 3+PO $89.5mil contract" -- an RFA match written as one
# sentence (vs. the two-sentence "elected to match ... offer for PLAYER.
# PLAYER has signed ..." form handled via sentence-splitting above). The
# team that matches is the team that signs, so just capture the first team
# named and the player right after the "sign" verb.
SIGN_MATCH_AND_SIGN_RE = re.compile(
    rf"(?i)^(?:The\s+)?(?P<team>{_TEAM_PATTERN})\s+match(?:es|ed)?\b.*?\bsign\s+"
    rf"(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+to\s+a\b"
)

# A restricted-FA offer sheet whose resolution is appended right in the same
# message: "PLAYER has signed ... offer sheet with TEAM_A ... TEAM_B has
# decided (not) to match". Without this, the generic patterns above (via
# sentence-splitting on the \n\n between the two clauses) capture only the
# FIRST clause and always land on TEAM_A -- wrong whenever the original team
# (TEAM_B) actually matched (2 real bugs found live this way: Isaiah Jackson
# and Peyton Watson, both recorded on the offering team despite their own
# source text stating the original team matched and kept them). `(?s)`
# lets `.*?` cross the \n\n so both clauses can be seen as one match; this
# must be checked whole-message, before any per-line splitting, since
# splitting is exactly what throws the second clause away.
SIGN_OFFER_SHEET_MATCH_RE = re.compile(
    rf"(?is)^(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+"
    rf"(?:has\s+signed|signs?)\b.*?\bwith\s+(?:the|teh)?\s*(?P<offer_team>{_TEAM_PATTERN})\b"
    rf".*?\b(?P<orig_team>{_TEAM_PATTERN})\s+has\s+decided\s+(?P<negation>not\s+to|to)\s+match\b"
)

# ── Options ──────────────────────────────────────────────────────────────
# Team option, single player: "The Atlanta Hawks decline Brandon Goodwin's
# 2020-21 team option of $1,701,593." / "...accept ...'s 2024-2025 team
# option of $2,019,699" / "...accept ...'s 2025-2026 team option" (no $) /
# "The San Antonio Spurs decline Trey Lyles 2020-21 team option" (no
# possessive marker at all) / "The Sacramento Kings decline Aaron Holiday's
# team option of $2,239,943" (no year stated at all -- falls back to
# `_season_from_date`) / "Orlando Magic decline Chuma Okeke ..." (no leading
# "The"). The negative lookahead keeps this from firing on the reordered
# "decline the/their (YEAR) team option for PLAYER" phrasing below -- without
# it, "the"/"their" itself satisfies `[A-Z]...` case-insensitively and gets
# captured as a bogus one-word "player" (seen in-corpus from 2024-06 on).
TEAM_OPTION_RE = re.compile(
    rf"(?i)(?:The\s+)?(?:{_TEAM_PATTERN})\s+(?:have\s+)?"
    rf"(?P<decision>accept(?:s|ed)?|declin(?:e|es|ed))\s+"
    rf"(?!(?:the|their)\b)"
    rf"(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)(?:'s|s'|’s)?\s+"
    rf"(?:(?P<year>\d{{4}}[-/]\d{{2,4}})\s+)?team\s+option"
)

# Reordered team option, single player: "The Chicago Bulls accept the
# 2024-2025 team option for Scotty Pippen Jr of $3,000,000." / "...accept the
# 2025-2026 team option for Trendon Watford" (no $) / "The Detroit Pistons
# decline the team option for Ty Jerome for $2,463,946." (no year) / "The
# Brooklyn Nets accept their team option for Luka Garza" ("their", not "the
# YEAR" -- also no year). Seen consistently from 2024-06 on, apparently a
# source-side phrasing change.
TEAM_OPTION_REORDERED_RE = re.compile(
    rf"(?i)(?:The\s+)?(?:{_TEAM_PATTERN})\s+(?:have\s+)?"
    rf"(?P<decision>accept(?:s|ed)?|declin(?:e|es|ed))\s+(?:the|their)\s+"
    rf"(?:(?P<year>\d{{4}}[-/]\d{{2,4}})\s+)?team\s+option\s+for\s+"
    rf"(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)"
    rf"(?=\s+(?:of|for)\s+\$|[.\n]|\s*$)"
)

# Team option, two players sharing one sentence: "The Minnesota Timberwolves
# accept Dante Exum's and Jaylin Williams options of 3,036,040 and 2,019,699
# respectively." `['’]` (not just `'`) because the source sometimes uses a
# curly apostrophe, which -- being also a valid name character -- otherwise
# gets silently swallowed into the lazy player group instead of terminating
# it, and the whole match fails outright rather than just mis-capturing.
TEAM_OPTION_DUAL_RE = re.compile(
    rf"(?i)(?:The\s+)?(?:{_TEAM_PATTERN})\s+(?:have\s+)?"
    rf"(?P<decision>accept(?:s|ed)?|declin(?:e|es|ed))\s+"
    rf"(?P<p1>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)['’]s\s+and\s+"
    rf"(?P<p2>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)['’]?s?\s+options?\s+of\s+"
    rf"[\d,]+\s+and\s+[\d,]+\s+respectively"
)

# Team option, 2-3 players sharing one clause with no per-player amounts:
# "The Los Angeles Clippers accept AJ Griffin & Chris Duarte's Team Option
# for the 2024-25 Season" / "The Sacramento Kings accept Scoot Henderson,
# Reed Shepard, and Stephon Castle's 2026-2027 team option". `_split_players`
# (already used for signings) handles turning the joined `p_list` capture
# into individual names.
TEAM_OPTION_LIST_RE = re.compile(
    rf"(?i)(?:The\s+)?(?:{_TEAM_PATTERN})\s+(?:have\s+)?"
    rf"(?P<decision>accept(?:s|ed)?|declin(?:e|es|ed))\s+"
    rf"(?P<p_list>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ&,.'’\- ]*?)['’]s\s+"
    rf"(?:(?P<year>\d{{4}}[-/]\d{{2,4}})\s+team\s+option\b"
    rf"|Team\s+Option\s+for\s+the\s+(?P<season_year>\d{{4}}-\d{{2,4}})\s+Season)"
)

# Player option: "Andre Drummond accepts his player option worth $X with the
# Cleveland Cavaliers." / "Taj Gibson has opted in to his $X player option"
PLAYER_OPTION_ACCEPT_RE = re.compile(
    r"(?i)^(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+"
    r"(?:accepts?\s+(?:his|her)\s+player\s+option\b|"
    r"(?:has\s+)?opted\s+in\s+to\s+(?:his|her)\b)"
)
# "LeBron James has opted out of his $X player option" / "Glenn Robinson III
# declines his player option worth $X with the TEAM."
PLAYER_OPTION_DECLINE_RE = re.compile(
    r"(?i)^(?P<player>[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ.'’\- ]*?)\s+"
    r"(?:(?:has\s+)?opted\s+out\s+of\s+(?:his|her)\b|"
    r"declines?\s+(?:his|her)\s+player\s+option\b)"
)

EXPLICIT_YEAR_RE = re.compile(r"(?i)\b(\d{4})-(\d{2,4})\b")

# A list-format batch of option decisions with no team/type stated per line
# (seen once, 2024-06-12: 29 players in one message) -- can't safely assign
# option_type without per-player context, flag the whole message.
OPTION_LIST_LINE_RE = re.compile(
    r"""(?im)^[A-Za-z][A-Za-z.'’\-\s]{2,40}\s*-\s*(Accept|Decline|Opt(?:ed)?\s?(?:In|Out)|Retired?)\s*(?:\$?\d[,\d]*)?\s*$"""
)

SIGN_STEM_RE = re.compile(r"(?i)\bsign\w*\b")
OPTION_STEM_RE = re.compile(r"(?i)\b(option|opted)\b")

# Splits on newlines as before, plus double-space-separated sentences on the
# same line -- e.g. "The Pelicans have elected to match the Spurs offer for
# Jarrett Allen.  Jarrett Allen has signed with the New Orleans Pelicans..."
# is one line with no \n. Without this, SIGN_PLAYER_FIRST_RE's lazy player
# group (anchored at the string start) swallows the entire preamble sentence
# looking for the first "sign" stem, producing a garbled unresolvable
# "player" instead of just matching the second sentence on its own. Requires
# 2+ spaces (not a single space) so it doesn't split names like "Bruce Brown
# Jr. signs with ..." where the abbreviation period is followed by one space.
_SENTENCE_SPLIT_RE = re.compile(r"\n+|(?<=[.!?])\s{2,}")


def _season_str(year4: str, year2_or_4: str) -> str:
    """'2024', '2025' -> '24-25'. '2020', '21' -> '20-21'."""
    y1 = int(year4) % 100
    y2 = int(year2_or_4) % 100
    return f"{y1:02d}-{y2:02d}"


_YEAR_SPLIT_RE = re.compile(r"[-/]")


def _season_str_from_range(year_range: str) -> str:
    """'2024-2025' or '2024/2025' -> '24-25'."""
    y1, y2 = _YEAR_SPLIT_RE.split(year_range)
    return _season_str(y1, y2)


def _season_from_date(date_str: str) -> str:
    """Fallback when no season is stated in text: the option/signing being
    decided in the offseason applies to the season starting that year (July 1
    cutoff, matching _default_season_start in nbn-api's storage.py)."""
    y, m = int(date_str[:4]), int(date_str[5:7])
    return f"{y % 100:02d}-{(y + 1) % 100:02d}" if m >= 7 else f"{(y - 1) % 100:02d}-{y % 100:02d}"


# A signing clause can name more than one player: "sign Reggie Perry and
# Immanuel Quickley", "sign Killian Tillie and Josh Hall to rookie scale
# deals", "signing Jordan Nwora (pick 48) and Markus Howard(pick 51)".
_PAREN_ASIDE_RE = re.compile(r"\([^)]*\)")
_PLAYER_SPLIT_RE = re.compile(r"\s*(?:,\s*)?(?:\band\b|&)\s*", re.I)


def _split_players(raw: str) -> list[str]:
    cleaned = _PAREN_ASIDE_RE.sub("", raw)
    return [p.strip() for p in _PLAYER_SPLIT_RE.split(cleaned) if p.strip()]


def resolve_signings(content: str, name_map: dict, by_last: dict) -> list[dict]:
    out = []

    m = SIGN_OFFER_SHEET_MATCH_RE.search(content)
    if m:
        offer_team = _alias_to_abbr[m.group("offer_team").lower()]
        orig_team = _alias_to_abbr[m.group("orig_team").lower()]
        matched = not m.group("negation").lower().startswith("not")
        final_team = orig_team if matched else offer_team
        for raw_player in _split_players(m.group("player")):
            slug, note = resolve_player(raw_player, name_map, by_last)
            out.append({
                "kind": "sign", "team": final_team, "slug": slug, "note": note,
                "raw_player": raw_player,
            })
        return out

    for line in _SENTENCE_SPLIT_RE.split(content):
        line = line.strip()
        if not line:
            continue
        # SIGN_QUALIFYING_OFFER_RE must be tried before SIGN_PLAYER_FIRST_RE:
        # the latter's generic "signs?" alternative also matches the bare
        # word "sign" in "... will sign his qualifying offer ...", swallowing
        # "will" into its lazy player group before ever reaching the intended
        # verb -- producing a garbled "PLAYER will" as the captured name. The
        # other three (ELEVATE, MATCH_AND_SIGN, QUALIFYING_OFFER_TEAM) don't
        # overlap with any other pattern's required verb/preposition, so
        # their position in the chain doesn't matter -- grouped here anyway
        # for readability.
        m = (SIGN_TEAM_FIRST_RE.search(line) or SIGN_ELEVATE_RE.search(line)
             or SIGN_MATCH_AND_SIGN_RE.search(line)
             or SIGN_QUALIFYING_OFFER_RE.search(line)
             or SIGN_QUALIFYING_OFFER_TEAM_RE.search(line)
             or SIGN_PLAYER_FIRST_RE.search(line))
        if not m:
            continue
        team = _alias_to_abbr[m.group("team").lower()]
        for raw_player in _split_players(m.group("player")):
            slug, note = resolve_player(raw_player, name_map, by_last)
            out.append({
                "kind": "sign", "team": team, "slug": slug, "note": note,
                "raw_player": raw_player,
            })
    return out


def resolve_options(content: str, date: str, name_map: dict, by_last: dict) -> list[dict]:
    out = []
    consumed_spans = []

    for m in TEAM_OPTION_DUAL_RE.finditer(content):
        decision = "accept" if m.group("decision").lower().startswith("accept") else "decline"
        ym = EXPLICIT_YEAR_RE.search(content[m.start():m.end() + 20])
        year = _season_str(ym.group(1), ym.group(2)) if ym else _season_from_date(date)
        for pgroup in ("p1", "p2"):
            slug, note = resolve_player(m.group(pgroup), name_map, by_last)
            out.append({
                "kind": "option", "slug": slug, "note": note,
                "raw_player": m.group(pgroup).strip(),
                "decision": decision, "option_type": "TEAM_OPT", "year": year,
            })
        consumed_spans.append(m.span())

    for m in TEAM_OPTION_LIST_RE.finditer(content):
        if any(s <= m.start() < e for s, e in consumed_spans):
            continue
        decision = "accept" if m.group("decision").lower().startswith("accept") else "decline"
        year_range = m.group("year") or m.group("season_year")
        year = _season_str_from_range(year_range) if year_range else _season_from_date(date)
        for raw_player in _split_players(m.group("p_list")):
            slug, note = resolve_player(raw_player, name_map, by_last)
            out.append({
                "kind": "option", "slug": slug, "note": note,
                "raw_player": raw_player,
                "decision": decision, "option_type": "TEAM_OPT", "year": year,
            })
        consumed_spans.append(m.span())

    for m in TEAM_OPTION_RE.finditer(content):
        if any(s <= m.start() < e for s, e in consumed_spans):
            continue
        decision = "accept" if m.group("decision").lower().startswith("accept") else "decline"
        slug, note = resolve_player(m.group("player"), name_map, by_last)
        year = _season_str_from_range(m.group("year")) if m.group("year") else _season_from_date(date)
        out.append({
            "kind": "option", "slug": slug, "note": note,
            "raw_player": m.group("player").strip(),
            "decision": decision, "option_type": "TEAM_OPT",
            "year": year,
        })
        consumed_spans.append(m.span())

    for m in TEAM_OPTION_REORDERED_RE.finditer(content):
        if any(s <= m.start() < e for s, e in consumed_spans):
            continue
        decision = "accept" if m.group("decision").lower().startswith("accept") else "decline"
        slug, note = resolve_player(m.group("player"), name_map, by_last)
        year = _season_str_from_range(m.group("year")) if m.group("year") else _season_from_date(date)
        out.append({
            "kind": "option", "slug": slug, "note": note,
            "raw_player": m.group("player").strip(),
            "decision": decision, "option_type": "TEAM_OPT",
            "year": year,
        })
        consumed_spans.append(m.span())

    for line in _SENTENCE_SPLIT_RE.split(content):
        line = line.strip()
        if not line:
            continue
        m = PLAYER_OPTION_ACCEPT_RE.search(line)
        if m:
            slug, note = resolve_player(m.group("player"), name_map, by_last)
            ym = EXPLICIT_YEAR_RE.search(line)
            year = _season_str(ym.group(1), ym.group(2)) if ym else _season_from_date(date)
            out.append({
                "kind": "option", "slug": slug, "note": note,
                "raw_player": m.group("player").strip(),
                "decision": "accept", "option_type": "PLAYER_OPT", "year": year,
            })
            continue
        m = PLAYER_OPTION_DECLINE_RE.search(line)
        if m:
            slug, note = resolve_player(m.group("player"), name_map, by_last)
            ym = EXPLICIT_YEAR_RE.search(line)
            year = _season_str(ym.group(1), ym.group(2)) if ym else _season_from_date(date)
            out.append({
                "kind": "option", "slug": slug, "note": note,
                "raw_player": m.group("player").strip(),
                "decision": "decline", "option_type": "PLAYER_OPT", "year": year,
            })
    return out


_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_ROLE_TEAM_MAP = (
    json.loads(ROLE_TEAM_MAP_FILE.read_text()) if ROLE_TEAM_MAP_FILE.exists() else {}
)


def _substitute_role_mentions(content: str) -> str:
    """Many fa-news messages @-mention a team's Discord role instead of
    spelling out its name ("signed ... with the <@&663946435024257044>").
    Swap in the plain team abbr (itself a valid alias) so the sign/option
    regexes below can match team names the same way regardless of whether
    the source used text or a role mention."""
    return _ROLE_MENTION_RE.sub(
        lambda m: _ROLE_TEAM_MAP.get(m.group(1), m.group(0)), content)


# Nicknames in straight double quotes ('Mamadi "Purdue Killer Jerkface"
# Diakite signs with...') break every player-name character class (no
# variant allows `"`), causing the whole match attempt to fail outright
# rather than just mis-capture. Strip the quoted aside entirely -- the
# nickname itself is never needed for resolution.
_QUOTE_ASIDE_RE = re.compile(r'\s*"[^"]*"\s*')
# Same idea for Discord bold/italic markup ('The *Houston Rockets* sign
# *Cooper Flagg* to his rookie scale contract') -- the `*` characters aren't
# in the character class either, so just drop them.
_MARKDOWN_STAR_RE = re.compile(r"\*")
# A one-off editorial prefix seen once in-corpus ("Correction: Terence Davis
# has signed with...") that defeats every regex's `^` anchor.
_CORRECTION_PREFIX_RE = re.compile(r"(?i)^correction:\s*")
# Discord strikethrough around a superseded decision ("The New Orleans
# Pelicans ~~decline~~ accept Austin Reaves' team option..."): the struck
# text is the wrong/original value being corrected, so drop it (markers and
# content both) rather than just the `~~` markers -- keeping the struck text
# would leave two decision words ("decline accept") in front of the player.
_STRIKETHROUGH_RE = re.compile(r"~~[^~]*~~\s*")


_PAREN_ASIDE_INLINE_RE = re.compile(r"\([^)]*\)")


def _clean_content(content: str) -> str:
    content = _STRIKETHROUGH_RE.sub("", content)
    content = _QUOTE_ASIDE_RE.sub(" ", content)
    content = _MARKDOWN_STAR_RE.sub("", content)
    content = _CORRECTION_PREFIX_RE.sub("", content)
    # Parenthetical asides ("Keaton Wallace (?) to a 1+TO min") break every
    # player-name character class the same way quotes/asterisks do -- no
    # variant allows "(" -- causing the whole match attempt to fail rather
    # than just mis-capture. Safe to strip globally: the only places parens
    # legitimately appear (pick numbers, "(PO)"/"(TO)" contract-type notes)
    # are always outside the team/player capture regions to begin with.
    content = _PAREN_ASIDE_INLINE_RE.sub(" ", content)
    return content


def parse_message(msg: dict, name_map: dict, by_last: dict) -> dict:
    content = _clean_content(_substitute_role_mentions(msg["content"]))
    date = msg["timestamp"][:10]

    if len(OPTION_LIST_LINE_RE.findall(content)) >= 3:
        return {"bucket": "flagged", "reasons": ["list-format batch of option decisions, "
                                                  "team/option_type not derivable per player"]}

    has_sign = bool(SIGN_STEM_RE.search(content))
    has_option = bool(OPTION_STEM_RE.search(content))
    if has_sign and has_option:
        return {"bucket": "flagged", "reasons": ["message mixes a signing and an option "
                                                  "decision (e.g. decline PO to sign an extension)"]}

    if has_option:
        candidates = resolve_options(content, date, name_map, by_last)
        if not candidates:
            return {"bucket": "skipped", "reasons": ["option-mentioning but no decision pattern matched"]}
        reasons = [f"unresolved player: {c['raw_player']!r}" for c in candidates if not c["slug"]]
        reasons += [f"low-confidence player match: {c['note']} -> {c['slug']}"
                    for c in candidates if c["note"].startswith(("fuzzy:", "lastname:"))]
        return {"bucket": "flagged" if reasons else "resolved", "reasons": reasons, "candidates": candidates}

    if has_sign:
        candidates = resolve_signings(content, name_map, by_last)
        if not candidates:
            return {"bucket": "skipped", "reasons": ["sign-mentioning but no template matched"]}
        reasons = [f"unresolved player: {c['raw_player']!r}" for c in candidates if not c["slug"]]
        reasons += [f"unresolved team in: {c['raw_player']!r}" for c in candidates if not c.get("team")]
        reasons += [f"low-confidence player match: {c['note']} -> {c['slug']}"
                    for c in candidates if c["note"].startswith(("fuzzy:", "lastname:"))]
        return {"bucket": "flagged" if reasons else "resolved", "reasons": reasons, "candidates": candidates}

    return {"bucket": "skipped", "reasons": ["no sign or option language detected (renounce/waiver/"
                                             "retirement/trade-block chatter/procedural, out of scope)"]}


# Unlike the transactions channels (clean handoff -- the last dated-channel
# message predates the first live trade), 2025-fa-news runs through
# 2026-05-21, past when the live system's first `sign` record already starts
# (2026-04-10) -- confirmed by cross-reference: the two auto-resolved and
# three flagged fa-news candidates dated on/after this cutoff are exact
# matches (by player + date) for 5 of the 8 live `sign` records. Anything
# from this cutoff on is already captured live; backfilling it would double
# it up on player pages.
LIVE_SIGN_CUTOFF = "2026-04-10"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-dir", type=Path, default=IN_DIR_DEFAULT)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()

    bios = httpx.get(f"{API}/api/players", timeout=30).json()
    name_map, by_last = build_name_map(bios)

    resolved, flagged, skipped, excluded_live_overlap = [], [], [], []
    for path in sorted(args.in_dir.glob("*.json")):
        messages = json.loads(path.read_text())
        for msg in messages:
            date = msg["timestamp"][:10]
            base = {
                "date": date,
                "description": msg["content"],
                "channel": msg["channel"],
                "discord_id": msg["id"],
            }
            if date >= LIVE_SIGN_CUTOFF:
                excluded_live_overlap.append(base)
                continue
            result = parse_message(msg, name_map, by_last)
            if result["bucket"] == "skipped":
                skipped.append({**base, "reasons": result["reasons"]})
            elif result["bucket"] == "flagged":
                entry = {**base, "reasons": result["reasons"]}
                if "candidates" in result:
                    entry["candidates"] = result["candidates"]
                flagged.append(entry)
            else:
                for c in result["candidates"]:
                    resolved.append({**base, **c})

    args.out.write_text(json.dumps(
        {"resolved": resolved, "flagged": flagged, "skipped": skipped,
         "excluded_live_overlap": excluded_live_overlap}, indent=2))
    print(f"resolved: {len(resolved)}")
    print(f"flagged:  {len(flagged)}")
    print(f"skipped:  {len(skipped)}")
    print(f"excluded (live overlap, >= {LIVE_SIGN_CUTOFF}): {len(excluded_live_overlap)}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()

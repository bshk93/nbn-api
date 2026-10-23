"""Backfill injury-history.jsonl from #injury-report's hand-typed posts.

Before the 2K injury screen was parsed (routers/injuries.py, 2026-10-22), the
streamers announced each injury in #injury-report by hand, one line each:

    @Kings De'Aaron Fox HURT 2-4 weeks
    @Kings De'Aaron Fox healed

This reads that channel through the bot and turns the posts into the same
change log the parsed reports write: `new`, `changed` and `healed` events, one
per line, oldest first. Every backfilled event carries `report_id: "discord"`
and the post's `message_id`, so it is always clear which lines came from here.

What the posts can't give, the events leave null: the 2K injury name (unless
the post typed one in capitals, "Trey Murphy POKED IN THE EYE HURT") and the
OVR hit.

- **Date** is the post's Eastern date, snapped back a day when the post landed
  after midnight following a game day.
- **Team** is the role the post tagged, the player's team at the time — not
  today's roster.
- **Name** is matched to a slug on the full name; failing that, among the
  players who played for the tagged team that season; failing that, `ALIASES`.
- An injury still open when a season ends had its heal never posted. An OUT
  one is closed on the player's first game back (`closed: "first-game-back"`),
  since an OUT player doesn't play; anything else, or an OUT player who never
  played again that season, on the season's last game day
  (`closed: "season-end"`).
- "healed" then "fully healed" with nothing between: the first cleared one of
  two injuries or moved him up from OUT, so the injury ends at the second.
- Posts from `CUTOVER` on are skipped: from there the parsed reports are the
  record.

Writes nothing to Discord. Dry run by default: prints a summary and every post
that needs a human look.

    venv/bin/python backfill_injury_history.py                  # fetch + show
    venv/bin/python backfill_injury_history.py --cache /tmp/x   # reuse a fetch
    venv/bin/python backfill_injury_history.py --apply          # write
"""
from __future__ import annotations

import argparse
import csv
import difflib
import glob
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from routers.constants import DATA_DIR, PLAYER_BIOS_FILE
from routers.discord_notify import TEAM_ROLE_IDS
from routers.injuries import HISTORY_FILE, DISCORD_INJURY_CHANNEL
from season_clock import season_for_date

API = "https://discord.com/api/v10"
ET = ZoneInfo("America/New_York")
CUTOVER = "2026-10-22"
REPORT_ID = "discord"

ROLE_TEAM = {v: k for k, v in TEAM_ROLE_IDS.items()}

# Names the matcher can't reach on its own: nicknames and short forms the bio
# doesn't use. Keyed by the name as typed, lowercased.
ALIASES: dict[str, str] = {
    "bub carrington": "carrington-carlton",
    "alexandre sarr": "sarr-alex",
    "olivier-maxence prosper": "prosper-omax",
    "lu dort": "dort-luguentz",
    "lugentz dort": "dort-luguentz",
    "bones hyland": "hyland-nahshon",
}

# Posts read by hand, by message id: the line to parse instead, or None to
# skip. Each is a correction or follow-up that only makes sense next to the
# post before it.
OVERRIDES: dict[str, str | None] = {
    # "Caleb Martin HURT" then "jk Martin is actually healed": never injured.
    "1316955818784067696": None,
    "1316955957179322380": None,
    # "^second injury HURT 1-2 weeks": the next line posts Fox's injury itself.
    "1318051896262525038": None,
    # "OUT* ^" under Gafford's HURT.
    "1338380460484595712": "<@&663946670899200000> Daniel Gafford OUT day-to-day",
    # "^ actually fully healed" under Bruce Brown's upgrade.
    "1346557033721364510": "<@&663946104056053760> Bruce Brown fully healed",
    # "OUT day-to-day … edit - 2k is just showing him as hurt".
    "1370252757465825411": "<@&663947943400833076> Derrick White HURT day-to-day",
    # "OUT*" under Suggs's HURT.
    "1467213863740244117": "<@&663947322060963860> Jalen Suggs OUT 2-4 weeks",
    # "playable at HURT check the injury report in the last vod".
    "1375619434810576947": "<@&663947022281474060> Bam Adebayo upgraded",
    "1375314502689755168": None,   # "don't get too excited", then Zach Collins
    # Injury words typed into the name.
    "1318052127926390876": "<@&663891272167325729> Shai Gilgeous-Alexander FLU HURT day to day",
    "1434303872981274744": "<@&663947196252684309> Brandon Miller RUPTURED ACHILLES OUT 6-8 months",
    "1461563289359351878": "<@&663946790873071626> Jase Richardson HURT 1-2 weeks",
    "1305007730129502238": "<@&663953717019803649> Huerter HURT day-to-day",
    # Untagged follow-ups: the team comes from the open injury or the season.
    "1306489463236726845": "James Harden upgraded day-to-day",
    "1357571699855458344": "Ziaire Williams HURT 2-4 weeks",
    "1448192919658958878": "Michael Porter Jr fully healed",
    "1459419969681883156": "Coby White HURT 2-4 weeks",
    "1486523801796874326": "Brandon Ingram OUT 6-8 weeks",
    # "@Blazers you were autosorted … and bad news", then this.
    "1470664583059476612": "<@&663953717019803649> Aaron Wiggins OUT 6-8 weeks",
}

STATUS_RE = re.compile(
    r"\b(fully\s+healed|healed|upgraded|out\s+for\s+(?:the\s+)?season|HURT|OUTA?)\b", re.I)
ROLE_RE = re.compile(r"<@&(\d+)>")
DATE_LINE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")
DURATION_RE = re.compile(
    r"^(day\s*-?\s*to\s*-?\s*day|\d+\s*-\s*\d+\s*(?:day|week|month)s?|for\s+(?:the\s+)?(?:season|series)|season)",
    re.I)
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
INJURY_WORDS = re.compile(
    r"fractur|sprain|strain|tear|torn|broke|achilles|knee|ankle|back|hamstring|concussion|"
    r"flu|finger|hand|foot|wrist|shoulder|hip|calf|groin|toe|elbow|eye", re.I)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(channel: str) -> list[dict]:
    token = os.environ["DISCORD_BOT_TOKEN"]
    out, before = [], None
    with httpx.Client(headers={"Authorization": f"Bot {token}"}, timeout=30) as c:
        while True:
            params = {"limit": 100, **({"before": before} if before else {})}
            r = c.get(f"{API}/channels/{channel}/messages", params=params)
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 1))
                continue
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out += batch
            before = batch[-1]["id"]
    out.reverse()
    return out


# ── Names ─────────────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", " ", re.sub(r"['’.]", "", s).replace("-", " "))
    return " ".join(w for w in s.split() if w not in SUFFIXES)


class Names:
    def __init__(self):
        bios = json.loads(PLAYER_BIOS_FILE.read_text())
        self.full: dict[str, set] = defaultdict(set)
        self.display: dict[str, str] = {}
        for slug, b in bios.items():
            name = b.get("name") or ""
            if "," in name:
                last, _, first = name.partition(",")
                self.full[norm(f"{first} {last}")].add(slug)
                self.display[slug] = f"{first.strip().title()} {last.strip().title()}"
        # (team, season) → slugs that played there, regular season and playoffs.
        self.team_season: dict[tuple, set] = defaultdict(set)
        for f in ("player_seasons.csv", "player_seasons_playoffs.csv"):
            path = DATA_DIR / "derived" / "players" / f
            with path.open(newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("SLUG") and r.get("TEAM") not in (None, "", "NA"):
                        self.team_season[(r["TEAM"], r["SEASON"])].add(r["SLUG"])

    def teams_in(self, slug: str, season: str) -> list[str]:
        return sorted(t for (t, s), slugs in self.team_season.items() if s == season and slug in slugs)

    def resolve(self, typed: str, team: str | None, season: str,
                open_slugs: set = frozenset()) -> tuple[str | None, str]:
        """(slug, how) — how is 'exact', 'team', 'alias' or why it failed.
        Two candidates on the team are settled by which one is injured."""
        key = typed.strip().lower()
        if key in ALIASES:
            return ALIASES[key], "alias"
        n = norm(typed)
        hits = self.full.get(n, set())
        if len(hits) == 1:
            return next(iter(hits)), "exact"
        pool = self.team_season.get((team, season), set()) if team else set()
        if hits & pool and len(hits & pool) == 1:
            return next(iter(hits & pool)), "exact"
        if not pool:
            return None, "no team pool"
        # Last name alone, or a close spelling, among that team's players.
        cands = {norm(self.display.get(s, "")): s for s in pool}
        words = n.split()
        if len(words) == 1:
            m = [s for c, s in cands.items() if c.split()[-1:] == words]
            if len(m) > 1 and len(set(m) & open_slugs) == 1:
                m = list(set(m) & open_slugs)
            if len(m) == 1:
                return m[0], "team"
        close = difflib.get_close_matches(n, list(cands), n=2, cutoff=0.8)
        if len(close) == 1 or (close and difflib.SequenceMatcher(None, n, close[0]).ratio() >= 0.9):
            return cands[close[0]], "team"
        return None, "no match"


# ── Parse ─────────────────────────────────────────────────────────────────────

def game_days() -> tuple[set[str], dict[str, list[str]]]:
    """Every date with a game, and each player's dates with minutes, by
    bio name ("LAST, FIRST" upper, as the box scores write it)."""
    days, played = set(), defaultdict(set)
    for f in glob.glob(str(DATA_DIR / "allstats-*.csv")):
        with open(f, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("DATE"):
                    days.add(r["DATE"][:10])
                    if (r.get("M") or "0") not in ("0", "", "NA"):
                        played[r["PLAYER"].strip().upper()].add(r["DATE"][:10])
    return days, {k: sorted(v) for k, v in played.items()}


def post_date(ts: str, days: set[str]) -> str:
    t = datetime.fromisoformat(ts).astimezone(ET)
    d = t.date().isoformat()
    prev = (t - timedelta(days=1)).date().isoformat()
    if d not in days and prev in days:
        return prev
    return d


def parse_line(line: str) -> dict | None:
    """One announcement, or None. {roles, name, injury, status, duration, note}."""
    roles = ROLE_RE.findall(line)
    body = ROLE_RE.sub(" ", line).strip()
    m = STATUS_RE.search(body)
    if not m:
        return None
    name, word, rest = body[:m.start()].strip(" -:"), m.group(1).lower(), body[m.end():].strip()
    # Capitalised words between the name and the status are the injury.
    injury = None
    toks = name.split()
    cut = len(toks)
    while cut > 2 and toks[cut - 1].isupper() and norm(toks[cut - 1]) not in SUFFIXES:
        cut -= 1
    if cut < len(toks):
        injury = " ".join(toks[cut:]).title()
        name = " ".join(toks[:cut])
    if "season" in word:
        status, duration = "Out", "Season"
    elif "healed" in word:
        status, duration = "healed", None
    elif word == "upgraded":
        status, duration = "upgraded", None
    else:
        status, duration = ("Out" if word.startswith("out") else "Hurt"), None
    # A parenthetical naming a body part is the injury; any other is a joke.
    for paren in re.findall(r"\(([^)]*)\)", rest):
        if not injury and INJURY_WORDS.search(paren):
            injury = paren.strip().title()
    rest = re.sub(r"\([^)]*\)", " ", rest)
    ovr_delta = None
    o = re.search(r"([+-]\d+)\s*OVR", rest, re.I)
    if o:
        ovr_delta = int(o.group(1))
        rest = rest[:o.start()] + rest[o.end():]
    rest = re.sub(r"^[\s?]*(?:additional\s+time|again|now|(?:to\s+)?hurt)?\s*", "", rest, flags=re.I)
    note = None
    d = DURATION_RE.match(rest)
    if d:
        duration = tidy_duration(d.group(1))
        note = rest[d.end():].strip(" .!") or None
    elif rest.strip():
        note = rest.strip()
    return {"roles": roles, "name": name, "injury": injury, "status": status,
            "duration": duration, "ovr_delta": ovr_delta, "note": note}


def tidy_duration(d: str) -> str:
    d = re.sub(r"\s+", " ", d.strip().lower())
    if re.match(r"day ?-? ?to ?-? ?day", d):
        return "Day-To-Day"
    if "season" in d:
        return "Season"
    if "series" in d:
        return "Series"
    m = re.match(r"(\d+) ?- ?(\d+) ?(day|week|month)", d)
    return f"{m.group(1)}-{m.group(2)} {m.group(3).title()}s" if m else d


# ── Build ─────────────────────────────────────────────────────────────────────

def build(msgs: list[dict]):
    names = Names()
    days, played_by_name = game_days()
    bios = json.loads(PLAYER_BIOS_FILE.read_text())

    def first_game_after(slug: str, after: str, until: str) -> str | None:
        dates = played_by_name.get((bios.get(slug, {}).get("name") or "").strip().upper(), [])
        return next((d for d in dates if after < d <= until), None)
    season_last_day: dict[str, str] = {}
    for d in days:
        s = season_for_date(d)
        season_last_day[s] = max(season_last_day.get(s, d), d)

    events: list[dict] = []
    review: list[tuple[str, str, str]] = []   # (date, why, text)
    open_: dict[str, dict] = {}               # slug → current entry
    open_season: dict[str, str] = {}
    counts = Counter()

    def close_season(before_season: str | None):
        for slug in list(open_):
            s = open_season[slug]
            if before_season is None or s < before_season:
                prev = open_.pop(slug)
                open_season.pop(slug)
                # Nobody posted the heal. An OUT player doesn't play, so his
                # first game back ends it; a HURT one can play through it, so
                # the season's end is all we know.
                end = season_last_day.get(s, prev["since"])
                back = first_game_after(slug, prev["status_since"], end) if prev["status"] == "Out" else None
                how = "first-game-back" if back else "season-end"
                events.append({"date": back or end, "report_id": REPORT_ID,
                               "kind": "healed", "slug": slug, "team": prev["team"],
                               "before": view(prev), "closed": how})
                counts[f"no heal posted: {how}"] += 1

    for msg in msgs:
        if msg["author"].get("bot"):
            continue
        date = post_date(msg["timestamp"], days)
        if date >= CUTOVER:
            continue
        content = msg["content"]
        if msg["id"] in OVERRIDES:
            if OVERRIDES[msg["id"]] is None:
                counts["skipped by hand"] += 1
                continue
            content = OVERRIDES[msg["id"]]
            counts["rewritten by hand"] += 1
        lines = content.splitlines() or [""]
        for line in lines:
            dl = DATE_LINE_RE.match(line)
            if dl:   # "11/10" on its own line dates the posts under it
                y = int(date[:4]) - (1 if int(dl.group(1)) > int(date[5:7]) else 0)
                date = f"{y:04d}-{int(dl.group(1)):02d}-{int(dl.group(2)):02d}"
                continue
            if not line.strip():
                continue
            p = parse_line(line)
            tagged = bool(ROLE_RE.search(line))
            if not p:
                if tagged or re.search(r"heal|hurt|\bout\b|injur|upgrad", line, re.I):
                    review.append((date, "not an announcement I can read", line.strip()))
                continue
            season = season_for_date(date)
            close_season(season)
            teams = [ROLE_TEAM[r] for r in p["roles"] if r in ROLE_TEAM]
            if tagged:
                team = teams[0] if len(teams) == 1 else None
                if len(teams) != 1:
                    review.append((date, f"tags {len(teams)} teams", line.strip()))
                slug, how = names.resolve(p["name"], team, season, set(open_))
            else:
                # Untagged: only a full name, and only when the team is clear
                # from his open injury or from the one team he played for.
                slug, how = names.resolve(p["name"], None, season)
                if how not in ("exact", "alias") and msg["id"] not in OVERRIDES:
                    review.append((date, "untagged, skipped", line.strip()))
                    continue
                if slug in open_:
                    team = open_[slug]["team"]
                else:
                    played = names.teams_in(slug, season) if slug else []
                    team = played[-1] if len(played) == 1 else None
                if msg["id"] not in OVERRIDES:
                    review.append((date, f"untagged, taken as {slug} ({team})", line.strip()))
            if not slug:
                review.append((date, f"name '{p['name']}' ({team}): {how}", line.strip()))
                continue
            if how == "team":
                review.append((date, f"matched '{p['name']}' → {slug} on {team}'s roster", line.strip()))
            if p["note"] and not re.fullmatch(r"\W*(rip|sorry|warrior|what a diva)?\W*", p["note"], re.I):
                review.append((date, f"note kept out: '{p['note']}'", line.strip()))
            counts[how] += 1
            prev = open_.get(slug)
            base = {"date": date, "report_id": REPORT_ID, "slug": slug,
                    "team": team or (prev or {}).get("team"), "message_id": msg["id"]}
            if p["status"] == "healed":
                if not prev:
                    # "healed" then "fully healed": the first only cleared one of
                    # two injuries, or moved him up from OUT. He was on the list
                    # until the second, so that is when this injury ended.
                    last = next((e for e in reversed(events) if e["slug"] == slug), None)
                    if (last and last["kind"] == "healed" and not last.get("closed")
                            and season_for_date(last["date"]) == season):
                        review.append((date, f"heal moved {last['date']} → {date}", line.strip()))
                        last["date"], last["message_id"] = date, msg["id"]
                        counts["heal moved to 'fully healed'"] += 1
                    else:
                        review.append((date, "heal with no injury posted, dropped", line.strip()))
                        counts["heal dropped"] += 1
                    continue
                open_.pop(slug)
                open_season.pop(slug)
                events.append({**base, "kind": "healed", "before": view(prev)})
                counts["healed"] += 1
                continue
            if p["status"] == "upgraded":
                if not prev:
                    review.append((date, "upgrade with nothing open", line.strip()))
                    continue
                entry = {**prev, "status": "Hurt", "duration": p["duration"] or prev["duration"]}
            else:
                entry = {"status": p["status"], "injury": p["injury"] or (prev or {}).get("injury"),
                         "duration": p["duration"], "ovr": None, "ovr_delta": p["ovr_delta"],
                         "team": base["team"], "since": (prev or {}).get("since", date)}
                if not p["duration"]:
                    review.append((date, "no duration", line.strip()))
            entry["status_since"] = (prev["status_since"] if prev and prev["status"] == entry["status"]
                                     else date)
            if not prev:
                events.append({**base, "kind": "new", "after": view(entry)})
                counts["new"] += 1
            elif view(prev) != view(entry):
                events.append({**base, "kind": "changed", "before": view(prev), "after": view(entry)})
                counts["changed"] += 1
            else:
                counts["repeat (no change)"] += 1
            open_[slug] = entry
            open_season[slug] = season
    close_season(None)
    events.sort(key=lambda e: e["date"])   # stable: same-day order is post order
    return events, review, counts, names


def view(e: dict) -> dict:
    return {k: e.get(k) for k in ("status", "injury", "duration", "ovr", "ovr_delta")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", help="read/write the fetched messages here")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.cache and Path(args.cache).exists():
        msgs = json.loads(Path(args.cache).read_text())
    else:
        msgs = fetch(DISCORD_INJURY_CHANNEL)
        if args.cache:
            Path(args.cache).write_text(json.dumps(msgs))
    events, review, counts, names = build(msgs)

    print(f"{len(msgs)} messages → {len(events)} events")
    for k, v in counts.most_common():
        print(f"  {k}: {v}")
    by_season = Counter(season_for_date(e["date"]) for e in events)
    print("  by season:", dict(sorted(by_season.items())))
    print(f"\n{len(review)} posts to look at:")
    for date, why, text in review:
        print(f"  {date}  {why}\n             {text[:140]}")

    if not args.apply:
        print("\nDry run. --apply to write.")
        return 0
    existing = HISTORY_FILE.read_text().splitlines() if HISTORY_FILE.exists() else []
    if any(json.loads(l).get("report_id") == REPORT_ID for l in existing if l.strip()):
        print("History already has backfilled events; refusing to add them twice.")
        return 1
    if existing and any(json.loads(l).get("date", "") < CUTOVER for l in existing if l.strip()):
        print(f"History has parsed events before {CUTOVER}; refusing.")
        return 1
    backup = HISTORY_FILE.with_suffix(".jsonl.pre-backfill")
    if HISTORY_FILE.exists():
        shutil.copy2(HISTORY_FILE, backup)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in events) +
                   "".join(l + "\n" for l in existing if l.strip()))
    tmp.replace(HISTORY_FILE)
    print(f"Wrote {len(events)} backfilled + {len(existing)} existing events. Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

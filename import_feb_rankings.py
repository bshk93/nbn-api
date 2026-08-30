#!/usr/bin/env python3
"""One-off import of the league's February 2026 power-rankings sheet.

The rankings ran on a Google Sheet for years before `/news` grew a ranking
type. That sheet holds everything the article model wants — six complete
ballots, the consensus, a blurb per team, and the January order in its
"Previous Rank" column — so the February edition goes in as a real published
edition rather than as a hand-typed table. The current edition then chains to
it by `prev_id` and gets real ▲▼ arrows on its first run.

What this deliberately does *not* do is trust the sheet's own arithmetic. The
ballots are the input; `news_rankings.consensus` computes the order, and the
import **aborts** if what it computes disagrees with the order the sheet
published. That check is the whole point of importing ballots rather than a
finished table: if our tie rule or averaging differed from the league's, this
is where it would show, instead of in a table that quietly disagrees with the
one people remember reading.

    ./venv/bin/python3 import_feb_rankings.py            # dry run, prints everything
    ./venv/bin/python3 import_feb_rankings.py --apply    # writes news.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routers.news_rankings as pr                      # noqa: E402
from routers.constants import NEWS_FILE                 # noqa: E402
from routers.storage import _save_json                  # noqa: E402
from routers.perry import TEAM_NAMES                    # noqa: E402

SHEET_CSV = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vQkKoSdWlsa8MiRFAXwl32MrZX7t"
             "qD4HZ1L-JuBTAnl1NXUdDCXW8cnI9xNhjq14IXosHmHBDasXGrt/pub"
             "?gid=870681550&single=true&output=csv")

TITLE = "2026 Post-Deadline Power Rankings"
AUTHOR = "bryn"
SERIES = "main"
# "Records updated 2/10" on the sheet — the day the edition reflects.
PUBLISHED_AT = "2026-02-10T12:00:00+00:00"
BASELINE_LABEL = "the January 2026 rankings"
# The edition this one is the predecessor of. Chained by title so the script
# says out loud which article it touched.
CHAIN_INTO = "2026 Preseason Power Rankings"

# Sheet handles → members.json names. The four omitted (AllenOJ, bryn,
# TimeToFly, Samm) match exactly.
VOTER_NAMES = {"Flash": "FlashThompson11", "GuyFawkes": "Guy Fawkes"}
# Blurb bylines are signed more loosely than the ballot columns are headed.
BLURB_NAMES = {"Sam": "Samm", "TTF": "TimeToFly", "Guy": "Guy Fawkes",
               "bryn": "bryn", "hkd": "hkd"}

TEAM_BY_NAME = {name: abbr for abbr, name in TEAM_NAMES.items()}
TEAM_BY_NAME["Los Angeles Clippers"] = "LAC"   # the sheet spells LAC out

POSITIONS = ("PG", "SG", "SF", "PF", "C")
BLOCK_RE = re.compile(r"^(T-)?(\d+)\.\s*$")
PREV_RE = re.compile(r"Previous Rank:\s*(?:T-)?(\d+)")


def fetch_rows(source: str | None) -> list[list[str]]:
    if source:
        text = Path(source).read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(SHEET_CSV, timeout=60) as resp:
            text = resp.read().decode("utf-8")
    return [r + [""] * (6 - len(r)) for r in csv.reader(io.StringIO(text))]


def parse_sheet(rows: list[list[str]]) -> list[dict]:
    """One dict per team block: rank, abbr, previous rank, ballots, blurb."""
    starts = [i for i, r in enumerate(rows) if BLOCK_RE.match(r[0].strip())]
    if len(starts) != 30:
        sys.exit(f"expected 30 team blocks, found {len(starts)}")
    blocks = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(rows)
        head = rows[i]
        name = head[1].split("(")[0].strip()
        abbr = TEAM_BY_NAME.get(name)
        if not abbr:
            sys.exit(f"row {i}: cannot map team name {name!r}")
        prev = PREV_RE.search(head[2])
        if not prev:
            sys.exit(f"row {i}: cannot read a previous rank from {head[2]!r}")
        ballots, blurb = {}, ""
        for r in rows[i + 1:end]:
            if r[4].strip() and r[4].strip() != "AVERAGE":
                ballots[r[4].strip()] = int(r[5])
            # The blurb is the one long free-text cell in the block that isn't
            # a depth-chart row.
            if r[0].strip() not in POSITIONS and len(r[1].strip()) > len(blurb):
                blurb = r[1].strip()
        blocks.append({
            "rank": int(BLOCK_RE.match(head[0].strip()).group(2)),
            "tied": bool(BLOCK_RE.match(head[0].strip()).group(1)),
            "team": abbr,
            "prev": int(prev.group(1)),
            "ballots": ballots,
            "blurb": blurb,
        })
    return blocks


def build_ballots(blocks: list[dict]) -> dict[str, list[str]]:
    handles = sorted({h for b in blocks for h in b["ballots"]})
    out = {}
    for handle in handles:
        ranked = sorted(blocks, key=lambda b: b["ballots"][handle])
        order = [b["team"] for b in ranked]
        if sorted(b["ballots"][handle] for b in blocks) != list(range(1, 31)):
            sys.exit(f"{handle}'s ballot is not a 1–30 permutation")
        out[VOTER_NAMES.get(handle, handle)] = order
    return out


def split_blurb(text: str) -> tuple[str | None, str]:
    """`"Guy: they are cooked"` → `("Guy Fawkes", "they are cooked")`."""
    head, sep, tail = text.partition(":")
    if sep and head.strip() in BLURB_NAMES:
        return BLURB_NAMES[head.strip()], tail.strip()
    return None, text


def build_article(blocks: list[dict], articles: list[dict]) -> dict:
    ballots = build_ballots(blocks)
    a = {
        "id": str(uuid.uuid4()),
        "title": TITLE,
        "body": "",
        "cover_image": None,
        "tags": ["power rankings"],
        "author": AUTHOR,
        "status": "published",
        "created_at": PUBLISHED_AT,
        "updated_at": PUBLISHED_AT,
        "submitted_at": PUBLISHED_AT,
        "published_at": PUBLISHED_AT,
        "published_by": AUTHOR,
        "comments": [],
    }
    a.update(pr.scaffold(SERIES, None))
    a["voters"] = sorted(ballots)
    a["ballots"] = {name: {"order": order, "submitted_at": PUBLISHED_AT}
                    for name, order in ballots.items()}
    for b in blocks:
        by, body = split_blurb(b["blurb"])
        if not body:
            continue
        a["blurbs"][b["team"]] = {"claimed_by": by, "body": body,
                                  "updated_at": PUBLISHED_AT}
    pr.set_baseline(a, {b["team"]: b["prev"] for b in blocks}, BASELINE_LABEL)
    a["phase"] = "blurbs"
    pr.freeze(a, articles)
    return a


def check_against_sheet(a: dict, blocks: list[dict]) -> None:
    sheet = {b["team"]: (b["rank"], b["tied"]) for b in blocks}
    bad = [(r["team"], sheet[r["team"]][0], r["rank"])
           for r in a["final"] if sheet[r["team"]][0] != r["rank"]]
    if bad:
        for team, want, got in bad:
            print(f"  {team}: sheet says {want}, the ballots say {got}")
        sys.exit("computed order disagrees with the sheet — not importing")
    ties = [(r["team"], sheet[r["team"]][1], r["tied"])
            for r in a["final"] if sheet[r["team"]][1] != r["tied"]]
    if ties:
        sys.exit(f"tie flags disagree with the sheet: {ties}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write news.json")
    ap.add_argument("--csv", help="read the sheet from a local CSV instead of the web")
    args = ap.parse_args()

    blocks = parse_sheet(fetch_rows(args.csv))
    articles = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    if any(x.get("title") == TITLE for x in articles):
        sys.exit(f"{TITLE!r} is already in news.json — nothing to do")

    a = build_article(blocks, articles)
    check_against_sheet(a, blocks)

    print(f"{a['title']} — edition {a['edition']}, {len(a['ballots'])} ballots, "
          f"{len(a['blurbs'])} blurbs, published {a['published_at'][:10]}")
    print(f"  voters: {', '.join(a['voters'])}")
    for r in a["final"]:
        by = (a["blurbs"].get(r["team"]) or {}).get("claimed_by") or "—"
        move = "new" if r["prev"] is None else f"{r['move']:+d}" if r["move"] else "–"
        print(f"  {'T-' if r['tied'] else '  '}{r['rank']:>2}  {r['team']}  "
              f"avg {r['avg']:>5}  was {r['prev']:>2} ({move:>4})  blurb by {by}")

    chain = next((x for x in articles if x.get("title") == CHAIN_INTO), None)
    if chain and not chain.get("prev_id"):
        print(f"  chaining {CHAIN_INTO!r} to this edition")
    elif chain:
        print(f"  {CHAIN_INTO!r} already has a prev_id — leaving it alone")
    else:
        print(f"  no article titled {CHAIN_INTO!r} — nothing to chain")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply.")
        return

    articles.append(a)
    if chain and not chain.get("prev_id"):
        chain["prev_id"] = a["id"]
        chain["updated_at"] = datetime.now(timezone.utc).isoformat()
    # The API's own writer: same formatting, same atomic replace, so the store
    # comes back byte-identical apart from the article this adds.
    _save_json(NEWS_FILE, articles)
    print(f"\nwrote {NEWS_FILE} — {a['id']}")


if __name__ == "__main__":
    main()

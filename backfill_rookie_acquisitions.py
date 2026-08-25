#!/usr/bin/env python3
"""Backfill the missing `sign` record for players still on their rookie deal.

`_player_acquisition_index` can't find a signing for 151 of 517 rostered
players, so § 3.8 Bird tenure and § 6.2 extension eligibility have no contract
start date to work from. This script closes the one slice of that where the
answer follows from a rule rather than from research.

**The rule, and where it stops.** The earlier ledger backfill was thorough
enough that, for a recent draftee, the absence of any record is itself the
evidence: they have never signed anything except the rookie deal they were
drafted into. So contract start = that draft. Four conditions have to hold
before that inference is allowed, and each one exists because a real player
fails it:

  drafted 2023-2025   — 2013-2022 draftees are long past a rookie deal
                        (Giannis is the obvious case). The 2026 class is
                        excluded from the other end: they are `draft-rights`
                        with no salaries, so they have no signing on record
                        for the simple reason that they have not signed.
  draft_team on file  — without it there is no team to name in the record.
  still on that team  — yang-hansen rosters at UTA having been drafted by DAL
                        with no trade on record. Writing a DAL signing would
                        make the ledger contradict the roster.
  salaries on file    — a player with no salary is not under contract.

Everything else stays out and wants a person: the 86 players who have trade
records but no signing are the Discord-resolver's job, not a rule's.

Records are written through `POST /api/transactions` with `historical: true`,
the same path the Discord backfill used — it logs for display without touching
roster, cap, or team-state, so nothing is re-applied. Each description says it
is inferred rather than posing as a recovered fact.

    ./venv/bin/python backfill_rookie_acquisitions.py             # dry run
    ./venv/bin/python backfill_rookie_acquisitions.py --apply
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routers.transactions as T  # noqa: E402
from routers.constants import DATA_DIR  # noqa: E402

API_BASE = "https://nbn.today"

# Each class's earliest recorded signing, which is the draft itself: the 2026
# draft's own `pick` transactions land on 2026-06-20/21, and these sit in the
# same window.
DRAFT_DATE = {2023: "2023-06-21", 2024: "2024-06-27", 2025: "2025-06-26"}


def rostered_teams() -> dict[str, str]:
    out: dict[str, str] = {}
    pattern = glob.glob(str(DATA_DIR / "derived" / "data" / "*-roster.csv")) or glob.glob(
        str(DATA_DIR / "*-roster.csv")
    )
    for f in pattern:
        abbr = Path(f).name.split("-roster")[0].upper()
        with open(f) as fh:
            for row in csv.DictReader(fh):
                slug = (row.get("SLUG") or "").strip()
                if slug:
                    out[slug] = abbr
    return out


def classify() -> tuple[list[dict], list[tuple[str, str]]]:
    """Split the no-record players into what the rule can answer and what it can't."""
    bios = json.loads((DATA_DIR / "player-bios.json").read_text())
    ledger = json.loads((DATA_DIR / "transactions.json").read_text())
    index = T._player_acquisition_index()
    teams = rostered_teams()
    drafted = {
        (t.get("details") or {}).get("player")
        for t in ledger
        if t.get("type") == "pick"
    }

    write, hold = [], []
    for slug in sorted(teams):
        if index.get(slug):
            continue          # has a record of some kind — not ours
        if slug in drafted:
            hold.append((slug, "unsigned draftee — a `pick` record already covers them"))
            continue
        bio = bios.get(slug) or {}
        year, team = bio.get("draft_year"), bio.get("draft_team")
        if year not in DRAFT_DATE:
            hold.append((slug, f"drafted {year} — outside the rookie-deal window"))
        elif not team:
            hold.append((slug, "no draft_team on file"))
        elif team != teams[slug]:
            hold.append((slug, f"drafted by {team}, rosters at {teams[slug]}, no trade on record"))
        elif not bio.get("salaries"):
            hold.append((slug, "no salaries on file — not under contract"))
        else:
            write.append({
                "slug": slug,
                "team": team,
                "date": DRAFT_DATE[year],
                "year": year,
                "name": bio.get("name") or slug,
            })
    return write, hold


def payload(row: dict) -> dict:
    return {
        "type": "sign",
        "date": row["date"],
        "description": (
            f"{row['name']} signed with {row['team']} out of the {row['year']} draft. "
            "Inferred: no signing record was on file, and a rookie-era draftee with no "
            "ledger history has never signed anything else."
        ),
        "historical": True,
        "details": {"player": row["slug"], "team": row["team"], "contract": {}},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    args = ap.parse_args()

    write, hold = classify()

    print(f"WRITE — {len(write)} historical `sign` records\n")
    print(f"{'player':28s} {'yr':4s} {'team':5s} date")
    for r in write:
        print(f"{r['slug']:28s} {r['year']:<4d} {r['team']:5s} {r['date']}")
    print(f"\nHOLD BACK — {len(hold)}\n")
    for slug, why in hold:
        print(f"{slug:28s} {why}")

    if not args.apply:
        print(f"\nDry run. {len(write)} records would be written. Re-run with --apply.")
        return 0

    token = os.environ.get("NBN_ADMIN_TOKEN")
    if not token:
        print("\nNBN_ADMIN_TOKEN is not set.", file=sys.stderr)
        return 1

    ok = fail = 0
    with httpx.Client() as client:
        for r in write:
            resp = client.post(
                f"{API_BASE}/api/transactions",
                json=payload(r),
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code < 300:
                ok += 1
                print(f"  ok   {r['slug']:28s} {resp.json().get('id', '')}")
            else:
                fail += 1
                print(f"  FAIL {r['slug']:28s} {resp.status_code} {resp.text[:200]}")
    print(f"\n{ok} written, {fail} failed.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

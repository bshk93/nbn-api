"""
Submit resolved historical Discord trades to nbn-api's transaction log via
POST /api/transactions (historical=true). See
docs/discord-transaction-backfill.md for full context.

POST /api/transactions has no server-side dedup for historical trades, so
this script is idempotent across reruns itself: it tracks submitted
discord_ids in a small state file, written immediately after each successful
POST (not batched at the end) so a crash mid-run can't cause a re-run to
double-post what already went through. Mirrors the monotonic-snapshot
pattern nbn-today/build/achievement-notify.js uses for the same class of
problem (idempotent-award-on-a-non-idempotent-API).

Submits from the "resolved" bucket (high-confidence, 2-team trades) plus,
optionally, a second file of human-reviewed promotions out of "flagged" —
e.g. discord-transactions-promoted.json, low-confidence player matches that
were manually checked and found correct. Kept as a separate file rather than
merged into discord-transactions-resolved.json so re-running
resolve_discord_trades.py from scratch can't silently overwrite reviewed
work. Everything else in "flagged" (multi-team trades, unresolved players,
malformed parses) still needs human resolution first — not this script's job.

Env:
  NBN_ADMIN_TOKEN   admin bearer token (required unless DRY_RUN)
  NBN_API_BASE      override API base URL (default http://127.0.0.1:8001)
  DRY_RUN=1         print what would be submitted without POSTing anything

Usage:
  venv/bin/python3 submit_discord_trades.py [--limit N]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

DATA = Path("/var/lib/nothing-but-stats")
DEFAULT_INPUT = DATA / "discord-transactions-resolved.json"
DEFAULT_PROMOTED = DATA / "discord-transactions-promoted.json"
DEFAULT_STATE = DATA / "discord-transactions-submitted.json"
API_BASE = os.environ.get("NBN_API_BASE", "http://127.0.0.1:8001")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def submit_one(client: httpx.Client, token: str, entry: dict) -> dict:
    payload = {
        "type": "trade",
        "date": entry["date"],
        "description": entry["description"],
        "historical": True,
        "details": {"transfers": entry["transfers"]},
    }
    resp = client.post(
        f"{API_BASE}/api/transactions",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--promoted", type=Path, default=DEFAULT_PROMOTED,
                         help="optional file of human-reviewed promotions from the "
                              "flagged bucket (list of resolved-shaped entries)")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--limit", type=int, default=None,
                         help="submit at most N new trades (for spot-testing)")
    args = parser.parse_args()

    token = os.environ.get("NBN_ADMIN_TOKEN")
    if not token and not DRY_RUN:
        sys.exit("NBN_ADMIN_TOKEN is not set")

    data = json.loads(args.input.read_text())
    resolved = list(data["resolved"])
    if args.promoted.exists():
        promoted = json.loads(args.promoted.read_text())
        resolved.extend(promoted)
        print(f"including {len(promoted)} human-reviewed promotions from {args.promoted}")
    state = load_state(args.state)

    pending = [e for e in resolved if e["discord_id"] not in state]
    print(f"{len(resolved)} total candidate trades, {len(state)} already recorded in "
          f"{args.state}, {len(pending)} pending")

    if args.limit is not None:
        pending = pending[:args.limit]

    if DRY_RUN:
        for e in pending:
            summary = " / ".join(e["description"].splitlines()[1:]) or e["description"]
            print(f"[DRY RUN] would submit {e['discord_id']} ({e['date']}): {summary}")
        print(f"[DRY RUN] {len(pending)} trade(s) would be submitted, 0 actually POSTed")
        return

    submitted, failed = 0, 0
    with httpx.Client() as client:
        for e in pending:
            try:
                txn = submit_one(client, token, e)
            except Exception as exc:
                failed += 1
                print(f"  FAILED {e['discord_id']} ({e['date']}): {exc}", file=sys.stderr)
                continue
            state[e["discord_id"]] = {
                "txn_id": txn.get("id"),
                "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            save_state(args.state, state)
            submitted += 1
            print(f"  OK {e['discord_id']} -> txn {txn.get('id')} ({e['date']})")

    print(f"Done: {submitted} submitted, {failed} failed, "
          f"{len(state)} total recorded in {args.state}")


if __name__ == "__main__":
    main()

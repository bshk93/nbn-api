"""
Submit resolved historical Discord fa-news signings/options to nbn-api's
transaction log via POST /api/transactions (historical=true, type=sign or
type=option). See docs/discord-transaction-backfill.md for full context and
resolve_discord_fa_signings.py for how candidates are produced.

Unlike submit_discord_trades.py, one Discord message can yield several
candidate records (e.g. one message announcing three signings), so a plain
discord_id can't be the dedup/state key -- it's built per-candidate instead
(discord_id + kind + slug + team/decision/year), stable across reruns since
resolve_discord_fa_signings.py is deterministic.

Env:
  NBN_ADMIN_TOKEN   admin bearer token (required unless DRY_RUN)
  NBN_API_BASE      override API base URL (default http://127.0.0.1:8001)
  DRY_RUN=1         print what would be submitted without POSTing anything

Usage:
  venv/bin/python3 submit_discord_fa_signings.py [--limit N]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

DATA = Path("/var/lib/nothing-but-stats")
DEFAULT_INPUT = DATA / "discord-fa-signings-resolved.json"
DEFAULT_PROMOTED = DATA / "discord-fa-signings-promoted.json"
DEFAULT_STATE = DATA / "discord-fa-signings-submitted.json"
API_BASE = os.environ.get("NBN_API_BASE", "http://127.0.0.1:8001")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def candidate_key(e: dict) -> str:
    return ":".join(str(x) for x in (
        e["discord_id"], e["kind"], e["slug"],
        e.get("team") or "", e.get("decision") or "", e.get("year") or "",
    ))


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def build_payload(e: dict) -> dict:
    if e["kind"] == "sign":
        return {
            "type": "sign",
            "date": e["date"],
            "description": e["description"],
            "historical": True,
            "details": {
                "player": e["slug"],
                "team": e["team"],
                "contract": {},
            },
        }
    if e["kind"] == "option":
        return {
            "type": "option",
            "date": e["date"],
            "description": e["description"],
            "historical": True,
            "details": {
                "player": e["slug"],
                "decision": e["decision"],
                "option_type": e["option_type"],
                "year": e["year"],
            },
        }
    raise ValueError(f"unknown candidate kind: {e['kind']!r}")


def submit_one(client: httpx.Client, token: str, entry: dict) -> dict:
    resp = client.post(
        f"{API_BASE}/api/transactions",
        json=build_payload(entry),
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
                              "flagged bucket (list of resolved-shaped candidate entries)")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--limit", type=int, default=None,
                         help="submit at most N new records (for spot-testing)")
    parser.add_argument("--kind", choices=["sign", "option"], default=None,
                         help="only submit this candidate kind")
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

    if args.kind:
        resolved = [e for e in resolved if e["kind"] == args.kind]
    pending = [e for e in resolved if candidate_key(e) not in state]
    print(f"{len(resolved)} total candidates, {len(state)} already recorded in "
          f"{args.state}, {len(pending)} pending")

    if args.limit is not None:
        pending = pending[:args.limit]

    if DRY_RUN:
        for e in pending:
            print(f"[DRY RUN] would submit {e['kind']} {candidate_key(e)} ({e['date']})")
        print(f"[DRY RUN] {len(pending)} record(s) would be submitted, 0 actually POSTed")
        return

    submitted, failed = 0, 0
    with httpx.Client() as client:
        for e in pending:
            key = candidate_key(e)
            try:
                txn = submit_one(client, token, e)
            except Exception as exc:
                failed += 1
                print(f"  FAILED {key} ({e['date']}): {exc}", file=sys.stderr)
                continue
            state[key] = {
                "txn_id": txn.get("id"),
                "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            save_state(args.state, state)
            submitted += 1
            print(f"  OK {key} -> txn {txn.get('id')} ({e['date']})")

    print(f"Done: {submitted} submitted, {failed} failed, "
          f"{len(state)} total recorded in {args.state}")


if __name__ == "__main__":
    main()

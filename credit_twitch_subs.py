#!/usr/bin/env python3
"""Pay this month's NB¥ to members who sub on Twitch. Run by `nbn-twitch-subs.timer`.

    venv/bin/python credit_twitch_subs.py              # print what would be paid
    venv/bin/python credit_twitch_subs.py --apply      # pay it
    venv/bin/python credit_twitch_subs.py --apply --month 2026-09

Twitch only answers "who is subbed right now", so this runs once a month and a
sub active on the day it runs counts for that month. Each payment's ledger ref
is `twitch:<month>:<twitch user id>`, so a second run in the same month (a
catch-up after downtime, or by hand) pays nobody twice.

Rates are `wallet.SUB_RATES`: tier 1 and Prime 300, tier 2 700, tier 3 1,700.
Prime is indistinguishable from tier 1 in the API, so it pays the same.

A gifted sub pays the gifter, not the recipient — the gifter is the one who
paid, and paying both would make gifting to an alt a way to mint.

A subscriber is matched to a member by the `twitch` login on their member
record. An unmatched subscriber is listed at the end and paid nothing; add the
login to their record and re-run the month.

The Twitch API calls are `fetch_twitch_subs.py`'s, including rewriting the
rotated refresh token into .env.
"""
import argparse
import json
import os
import sys

from fetch_twitch_subs import MEMBERS_PATH, fetch_subscriptions, refresh_access_token, _update_env_var
from routers import wallet
from routers.league_time import league_today


def plan(subs: list[dict], members: dict, month: str) -> tuple[list[dict], list[str]]:
    """The ledger lines this month's subs earn, and the logins nobody matched."""
    by_login = {info["twitch"].lower(): name for name, info in members.items() if info.get("twitch")}
    lines, unmatched = [], []
    for s in subs:
        tier = int(s["tier"]) // 1000
        if s.get("is_gift"):
            payer_login = (s.get("gifter_login") or "").lower()
            why = f"gifted to {s['user_login']}"
        else:
            payer_login = s["user_login"].lower()
            why = "sub"
        member = by_login.get(payer_login)
        if not member:
            unmatched.append(f"{payer_login or '(anonymous gifter)'} ({why}, tier {tier})")
            continue
        lines.append({
            "member": member,
            "delta": wallet.SUB_RATES[tier],
            "kind": "twitch_sub",
            "reason": f"Twitch tier {tier} {why} — {month}",
            "ref": f"twitch:{month}:{s['user_id']}",
        })
    return lines, unmatched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="pay; without it, only print")
    ap.add_argument("--month", default=league_today().strftime("%Y-%m"),
                    help="the month being paid (default: this one)")
    args = ap.parse_args()

    client_id = os.environ["TWITCH_CLIENT_ID"]
    access, new_refresh = refresh_access_token(
        client_id, os.environ["TWITCH_CLIENT_SECRET"], os.environ["TWITCH_REFRESH_TOKEN"])
    if new_refresh != os.environ["TWITCH_REFRESH_TOKEN"]:
        _update_env_var("TWITCH_REFRESH_TOKEN", new_refresh)
    subs = fetch_subscriptions(client_id, access, os.environ["TWITCH_BROADCASTER_ID"])

    members = json.loads(MEMBERS_PATH.read_text())
    lines, unmatched = plan(subs, members, args.month)
    with wallet.lock:
        todo = [ln for ln in lines if not wallet.has_ref(ln["ref"])]
        for ln in lines:
            state = "pay " if ln in todo else "paid"
            print(f"  {state} {ln['member']:24} {ln['delta']:>6,}  {ln['reason']}")
        if args.apply and todo:
            wallet.post(todo)
    print(f"\n{len(todo)} to pay, {len(lines) - len(todo)} already paid for {args.month}"
          + ("" if args.apply else " (dry run — nothing written)"))
    if unmatched:
        print(f"\nNo member has these Twitch logins, so nobody was paid for them:", file=sys.stderr)
        for u in unmatched:
            print(f"  {u}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

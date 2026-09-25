#!/usr/bin/env python3
"""Restart NB¥ from zero, once. The plan is docs/nbyen-economy.md in nbn-today.

    venv/bin/python nbyen_reset.py                            # print the plan
    venv/bin/python nbyen_reset.py --apply                    # do it
    venv/bin/python nbyen_reset.py --data-dir ~/nbs-scratch --apply   # rehearse

What it does, in order:

1. Refuses if the new ledger already has lines — this runs once — or if any bet
   is still open, since those stakes are old money.
2. Keeps the old economy: `member-balances.json` is copied to
   `member-balances-pre-reset.json`, `invest-holdings.json` to
   `invest-holdings-pre-reset.json` (and then emptied, so shares bought with old
   NB¥ can never be sold into new NB¥). `bets-ledger.json` is left exactly where
   it is, untouched; nothing writes it any more. The market is marked locked,
   so /invest shows why its buttons are off.
3. Writes the opening ledger: 1,000 to every member (`start`), and 100 NB¥ per
   dollar of every donation on record whose donor is a member (`donation`).

Twitch subs are not part of this. Run `credit_twitch_subs.py --apply` straight
after, which pays the current month the same way the monthly timer will.

The API should be stopped while this runs, or at least idle: it rewrites
`member-balances.json`, which every NB¥ write path reads.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from routers import wallet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write; without it, only print")
    ap.add_argument("--data-dir", type=Path, default=wallet.DATA_DIR,
                    help="the data directory (default: the live one)")
    args = ap.parse_args()
    d = args.data_dir.expanduser()

    wallet.LEDGER_FILE = d / wallet.LEDGER_FILE.name
    wallet.BALANCES_FILE = d / wallet.BALANCES_FILE.name

    if wallet.read_ledger():
        print(f"{wallet.LEDGER_FILE} already has lines — the reset has run. Refusing.", file=sys.stderr)
        return 1
    bets = json.loads((d / "bets.json").read_text()) if (d / "bets.json").exists() else []
    live = [b["title"] for b in bets if b["status"] in ("open", "locked")]
    if live:
        print("Settle or delete these bets first — their stakes are pre-reset NB¥:", file=sys.stderr)
        for t in live:
            print(f"  {t}", file=sys.stderr)
        return 1

    members = json.loads((d / "members.json").read_text())
    donations = json.loads((d / "donations.json").read_text()) if (d / "donations.json").exists() else []

    lines = [{"member": m, "delta": wallet.START_GRANT, "kind": "start",
              "reason": "Starting NB¥", "ref": f"start:{m}"} for m in sorted(members)]
    skipped = []
    for don in donations:
        if don["member"] not in members:
            skipped.append(don)
            continue
        lines.append({"member": don["member"], "delta": don["amount"] * wallet.NBY_PER_DOLLAR,
                      "kind": "donation", "reason": f"Donation (${don['amount']}, on record at the reset)",
                      "ref": f"donation:{don['id']}"})

    old = json.loads(wallet.BALANCES_FILE.read_text()) if wallet.BALANCES_FILE.exists() else {}
    new: dict[str, float] = {}
    for ln in lines:
        new[ln["member"]] = new.get(ln["member"], 0) + ln["delta"]

    print(f"{'member':28} {'old':>10} {'new':>8}")
    for m in sorted(set(old) | set(new), key=lambda m: (-new.get(m, 0), m.lower())):
        print(f"{m:28} {old.get(m, 0):>10,.0f} {new.get(m, 0):>8,.0f}")
    print(f"\n{len(members)} members × {wallet.START_GRANT:,} start = {len(members) * wallet.START_GRANT:,}")
    print(f"{sum(1 for l in lines if l['kind'] == 'donation')} donations = "
          f"{sum(l['delta'] for l in lines if l['kind'] == 'donation'):,}")
    print(f"total {sum(new.values()):,} NB¥, was {sum(old.values()):,.0f}")
    for don in skipped:
        print(f"  not a member, not credited: {don['member']} ${don['amount']}", file=sys.stderr)

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    keep = [(d / n, d / n.replace(".json", "-pre-reset.json"))
            for n in ("member-balances.json", "invest-holdings.json") if (d / n).exists()]
    for _, dst in keep:
        if dst.exists():
            print(f"{dst} already exists — refusing to overwrite the only copy.", file=sys.stderr)
            return 1
    for src, dst in keep:
        shutil.copy2(src, dst)
    (d / "invest-holdings.json").write_text("{}")
    (d / "invest-market.json").write_text(json.dumps(
        {"locked": True, "locked_reason": "The market is paused while NB¥ restarts.", "locked_until": None},
        indent=2))
    wallet.BALANCES_FILE.write_text("{}")
    wallet.post(lines)
    check = wallet.rebuild_balances()
    assert check == {m: float(v) for m, v in new.items()}, "balances don't match the ledger"
    print(f"\nDone. {len(lines)} ledger lines in {wallet.LEDGER_FILE}.")
    print("Next: credit_twitch_subs.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Grandfather the RFAs that predate qualifying offers (one-off, 2026-10-02).

Before § 3.1 QOs existed, a contract's trailing `RFA` tag *was* RFA status —
the team never made a decision because there was nothing to decide. Once QOs
are live, a current-season RFA tag with no QO on file reads as lapsed, which
would turn every one of those players into a UFA the moment the code deploys.

This records each of them as an extended QO instead, so nothing changes for
them: they stay RFAs, and their teams can now withdraw the offer like any
other. The amount is the § 3.1 formula's, and the hold rises to it only where
the QO is larger, exactly as a fresh extension would.

Run it **before** deploying the QO code; the old code ignores the new field.

    venv/bin/python migrate_grandfather_qos.py            # dry run
    venv/bin/python migrate_grandfather_qos.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from routers import audit
from routers.constants import CAP_LEVELS_FILE
from routers.players import load_player_bios, save_player_bios
from routers.storage import _current_league_year, _parse_dollar
from routers.transactions import _qo_amount, _qo_record

NOTE = "grandfathered: tagged RFA before qualifying offers existed (2026-10-02)"


def plan(bios: dict, season: str, cap_levels: dict) -> list[dict]:
    rows = []
    for slug, bio in sorted(bios.items()):
        if (bio.get("cap_holds") or {}).get(season) != "RFA" or _qo_record(bio, season):
            continue
        two_way = bio.get("type") == "two-way"
        amount = _qo_amount(bio, season, cap_levels)
        hold = _parse_dollar((bio.get("salaries") or {}).get(season) or "")
        rows.append({"slug": slug, "amount": amount, "two_way": two_way, "hold": hold,
                     "raises_hold": bool(amount and not two_way and amount > hold)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--date", default="2026-10-02")
    a = ap.parse_args()

    season = _current_league_year()
    cap_levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    bios = load_player_bios()
    rows = plan(bios, season, cap_levels)
    for r in rows:
        amt = "two-way" if r["two_way"] else (f"${r['amount']:,}" if r["amount"] else "unpriced")
        print(f"  {r['slug']:24} {season} QO {amt:>12}   hold ${r['hold']:,}"
              + ("  -> hold raised to the QO" if r["raises_hold"] else ""))
    print(f"{len(rows)} player(s)")
    if not a.apply or not rows:
        if rows:
            print("DRY RUN — re-run with --apply.")
        return 0

    audit.begin_request("CLI", "migrate_grandfather_qos.py")
    audit.set_actor("system")
    bios = load_player_bios()
    for r in plan(bios, season, cap_levels):
        bio = bios[r["slug"]]
        rec = {"status": "extended", "amount": r["amount"], "two_way": r["two_way"],
               "date": a.date, "txn_id": None, "note": NOTE}
        if r["raises_hold"]:
            rec["base_hold"] = r["hold"]
            bio["salaries"][season] = f"${r['amount']:,}"
        bio.setdefault("qualifying_offers", {})[season] = rec
    save_player_bios(bios)
    print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Seed the conveyance JSON store from the live flat picks CSV.

Phase 0: every row becomes a `settled(OWNER)` node, preserving pick/player/
notes/frozen. Rows that carry structured data the flat model DID capture
(`PROTECTED` or `SWAP_OWNER` set) are flagged `needs_structure` — their real
conveyance nodes come from the migration worksheet in Phase 2; seeding them as
settled would drop the threshold/swap, so the parity test excludes them.

Idempotent and read-only w.r.t. the CSV. Writes the store to the given path
(default: NBS_DATA_DIR/draft-conveyance.json). Nothing reads this store yet.

    python3 -m picks_conveyance.seed_store [--in CSV] [--out JSON] [--stdout]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("NBS_DATA_DIR", "/var/lib/nothing-but-stats"))
DEFAULT_IN = DATA_DIR / "draft-picks.csv"
DEFAULT_OUT = DATA_DIR / "draft-conveyance.json"


def _int_or_none(s):
    s = (s or "").strip()
    return int(s) if s else None


def seed_pick(row: dict) -> dict:
    protected = (row.get("PROTECTED") or "").strip()
    swap_owner = (row.get("SWAP_OWNER") or "").strip()
    needs_structure = bool(protected or swap_owner)

    node = {"type": "settled", "team": row["OWNER"]}
    pick = {
        "year": int(row["YEAR"]),
        "round": int(row["ROUND"]),
        "orig": row["ORIG"],
        "conveyance": node,
        "pick": _int_or_none(row.get("PICK")),
        "player": (row.get("PLAYER") or "").strip() or None,
        "notes": row.get("NOTES", "") or "",
        "frozen": (row.get("FROZEN", "") or "").strip().upper() == "TRUE",
        "frozen_reason": row.get("FROZEN_REASON", "") or "",
    }
    if needs_structure:
        # carry the flat structured fields so nothing is lost before Phase 2
        pick["needs_structure"] = True
        pick["_flat"] = {"protected": _int_or_none(protected),
                         "swap_owner": swap_owner or None}
    return pick


def build_store(csv_path: Path) -> dict:
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    picks = [seed_pick(r) for r in rows]
    n_struct = sum(1 for p in picks if p.get("needs_structure"))
    return {
        "version": 0,
        "source": str(csv_path),
        "picks": picks,
        "swap_groups": {},
        "binary_swaps": {},
        "meta": {
            "total": len(picks),
            "settled": len(picks) - n_struct,
            "needs_structure": n_struct,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out", dest="out", default=str(DEFAULT_OUT))
    ap.add_argument("--stdout", action="store_true",
                    help="print store to stdout instead of writing")
    ap.add_argument("--curated", action="store_true",
                    help="apply the curated worksheet nodes over the settled seed")
    args = ap.parse_args()

    store = build_store(Path(args.inp))
    if args.curated:
        from . import curated
        curated.apply_curated(store)
        # recompute top-level counts by resolved node type
        types = {}
        for p in store["picks"]:
            types[p["conveyance"]["type"]] = types.get(p["conveyance"]["type"], 0) + 1
        store["meta"].update({"by_type": types,
                              "needs_structure": sum(
                                  1 for p in store["picks"] if p.get("needs_structure"))})
    if args.stdout:
        print(json.dumps(store, indent=2))
    else:
        Path(args.out).write_text(json.dumps(store, indent=2))
        m = store["meta"]
        print(f"wrote {args.out}: {m['total']} picks "
              f"({m['settled']} settled, {m['needs_structure']} need structure)")


if __name__ == "__main__":
    main()

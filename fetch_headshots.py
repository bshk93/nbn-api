"""Warm the headshot cache for every bio with a photo — routers/headshots.py.

Fetches at the router's own per-host pace, so a full run over ~950 photos
takes about an hour; anything already cached is skipped instantly. Safe to
re-run: it only fetches what's missing.

    venv/bin/python fetch_headshots.py            # everything
    venv/bin/python fetch_headshots.py --limit 50
    venv/bin/python fetch_headshots.py --dry-run  # count what's missing
"""
import argparse

from fastapi import HTTPException

from routers import headshots
from routers.constants import HEADSHOTS_DIR, PLAYER_BIOS_FILE
from routers.storage import _load_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bios = _load_json(PLAYER_BIOS_FILE, {})
    missing = [
        slug for slug, b in bios.items()
        if (b.get("photo_url") or "").startswith(("http://", "https://"))
        and not (HEADSHOTS_DIR / headshots._cache_name(slug, b["photo_url"])).exists()
    ]
    print(f"{len(missing)} of {len(bios)} bios need a headshot fetched")
    if args.dry_run:
        return
    if args.limit:
        missing = missing[:args.limit]
    ok = failed = 0
    for i, slug in enumerate(missing, 1):
        try:
            headshots.headshot_path(slug)
            ok += 1
        except HTTPException:
            failed += 1
        if i % 25 == 0:
            print(f"  {i}/{len(missing)}  ok={ok} failed={failed}")
    print(f"done: ok={ok} failed={failed}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Migrate news.json (one file for every article) into news/{id}.json, one file
per article — see BACKLOG.md "news.json is one file for every article".

Dry run by default. Pass --apply to write changes. On --apply, the original
news.json is renamed to news.json.bak rather than deleted, so the pre-migration
state is still there if anything about the split needs re-checking.

    ./venv/bin/python3 migrate_news.py            # dry run, prints everything
    ./venv/bin/python3 migrate_news.py --apply    # writes news/{id}.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from routers.constants import NEWS_FILE, NEWS_DIR   # noqa: E402
from routers.news import _save_article               # noqa: E402
from routers.storage import _load_json                # noqa: E402

APPLY = "--apply" in sys.argv


def main() -> None:
    if not NEWS_FILE.exists():
        sys.exit(f"{NEWS_FILE} not found — nothing to migrate (already split?)")

    articles = _load_json(NEWS_FILE, [])
    print(f"{len(articles)} articles in {NEWS_FILE}")

    for a in articles:
        aid = a.get("id")
        if not aid:
            sys.exit(f"article with no id: {a.get('title')!r} — aborting, nothing written")
        target = NEWS_DIR / f"{aid}.json"
        status = "overwrite" if target.exists() else "create"
        print(f"  {status}  news/{aid}.json  {a.get('title', '')!r}")

    if not APPLY:
        print("\ndry run — nothing written. Re-run with --apply.")
        return

    for a in articles:
        _save_article(a)

    backup = NEWS_FILE.with_name(NEWS_FILE.name + ".bak")
    NEWS_FILE.rename(backup)
    print(f"\nwrote {len(articles)} files to {NEWS_DIR}")
    print(f"moved {NEWS_FILE} -> {backup}")


if __name__ == "__main__":
    main()

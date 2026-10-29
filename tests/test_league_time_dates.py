"""No civil date is taken from the UTC clock.

The host runs Etc/UTC, so the UTC date is tomorrow's for four or five hours
every evening ET. A transaction dated that way lands on the wrong league day:
Trade 53 (Brunson to WAS) was finalized at 21:44 ET on 2026-10-28 and filed as
10-29, so every page that reads the ledger by day put it after the games it
followed. Civil dates go through routers/league_time.py; instants (created_at)
stay UTC and are not what this looks for.

    venv/bin/python -m tests.test_league_time_dates
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "routers").glob("*.py")) + [ROOT / "season_clock.py"]
# A UTC "today" written as a date string, or a bare date.today() / utcnow().
PATTERNS = [
    re.compile(r"datetime\.now\(\s*timezone\.utc\s*\)\.(strftime\(\s*['\"]%Y-%m-%d['\"]\s*\)|date\(\))"),
    re.compile(r"\bdate\.today\(\)"),
    re.compile(r"\butcnow\(\)\.(strftime|date)\("),
]


def main():
    bad = []
    for f in FILES:
        if f.name == "league_time.py":
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(p.search(line) for p in PATTERNS):
                bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    if bad:
        print("UTC civil dates (use league_today_str() / league_today()):")
        print("\n".join("  " + b for b in bad))
        sys.exit(1)
    print(f"no UTC civil dates in {len(FILES)} files")
    print("all checks passed")


if __name__ == "__main__":
    main()

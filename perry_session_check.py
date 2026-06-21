"""
Cross-reference Perry Game sessions (from perry-activity.json, which records member+IP+timestamp
for every /api/perry/today and /api/perry/submit hit) against nginx's access logs to flag members
who visited roster/player/stats pages — i.e. looked something up — while a Perry session was open.

Requires sudo to read /var/log/nginx/access.log* (owned by root:adm).

Usage:
    sudo venv/bin/python3 perry_session_check.py [--date YYYY-MM-DD]

Only covers dates from when IP capture went live (2026-06-20 onward) — earlier perry-activity.json
entries have no IP field.
"""
import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path("/var/lib/nothing-but-stats")
PERRY_ACTIVITY_FILE = DATA_DIR / "perry-activity.json"
NGINX_LOG_DIR = Path("/var/log/nginx")

# Pages that suggest the player went looking something up instead of recalling it.
LOOKUP_PATH_RE = re.compile(r"^/(teams|players|stats|tradeblock|h2h|hof)/")

LOG_LINE_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+) \S+" (?P<status>\d+)'
)
LOG_TS_FMT = "%d/%b/%Y:%H:%M:%S %z"


def _today_et() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _read_log_lines():
    for path in sorted(NGINX_LOG_DIR.glob("access.log*")):
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", errors="replace") as f:
                    yield from f
            else:
                with path.open("rt", errors="replace") as f:
                    yield from f
        except PermissionError:
            sys.exit(f"Permission denied reading {path} — run with sudo.")


def build_sessions(date_str: str) -> dict:
    """member -> {ips: set, start: datetime, end: datetime, submitted: bool}"""
    activity = json.loads(PERRY_ACTIVITY_FILE.read_text()) if PERRY_ACTIVITY_FILE.exists() else {}
    events = [e for e in activity.get(date_str, []) if e["member"] != "anon"]

    sessions: dict = {}
    for e in events:
        ts = datetime.fromisoformat(e["ts"])
        s = sessions.setdefault(e["member"], {"ips": set(), "start": ts, "end": ts, "submitted": False})
        s["ips"].add(e["ip"])
        s["start"] = min(s["start"], ts)
        s["end"] = max(s["end"], ts)
        if e["event"] == "submit":
            s["submitted"] = True
    return sessions


def find_lookup_hits(sessions: dict) -> list[dict]:
    # window end: submit time, or +30min grace after last today-load if never submitted
    windows = []
    for member, s in sessions.items():
        end = s["end"] if s["submitted"] else s["end"] + timedelta(minutes=30)
        for ip in s["ips"]:
            windows.append({"member": member, "ip": ip, "start": s["start"], "end": end})

    hits = []
    for line in _read_log_lines():
        m = LOG_LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path")
        if not LOOKUP_PATH_RE.match(path):
            continue
        try:
            ts = datetime.strptime(m.group("ts"), LOG_TS_FMT)
        except ValueError:
            continue
        ip = m.group("ip")
        for w in windows:
            if w["ip"] == ip and w["start"] <= ts <= w["end"]:
                hits.append({"member": w["member"], "ip": ip, "path": path, "ts": ts.isoformat()})
                break
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (ET)")
    args = ap.parse_args()
    date_str = args.date or _today_et()

    sessions = build_sessions(date_str)
    if not sessions:
        print(f"No Perry activity recorded for {date_str} (or IP capture wasn't live yet).")
        return

    hits = find_lookup_hits(sessions)
    if not hits:
        print(f"{date_str}: no roster/player/stats page visits detected during any Perry session.")
        return

    print(f"{date_str}: {len(hits)} lookup-page hit(s) during an open Perry session:")
    for h in sorted(hits, key=lambda h: h["ts"]):
        print(f"  {h['ts']}  {h['member']:<20} {h['ip']:<15} {h['path']}")


if __name__ == "__main__":
    main()

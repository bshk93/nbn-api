#!/usr/bin/env python3
"""Every-5-minutes check that this box still has room to serve the sites.

The server is 1 core, 1.9 GB RAM and 2 GB swap, and it runs the live sites next
to Claude sessions, builds and side projects. The failure this catches is
memory exhaustion: swap fills, everything pages, and nbn.today slows to a crawl
or stops answering. Nothing else notices until a member does.

Three signals, each read straight from the kernel or the API:

  * ``MemAvailable`` below ``AVAIL_MB``
  * swap at or above ``SWAP_PCT`` *and* ``MemAvailable`` below ``SWAP_AVAIL_MB``.
    Full swap alone is normal here: idle pages (much of them the API's own)
    get parked there and the site stays fast. It only matters once RAM is
    short too, because then the next spike has nowhere to go.
  * the API taking longer than ``SLOW_SECS`` to answer (or not answering)
  * scraper traffic, read from ``TRAFFIC_LOG`` (one line per nbn.today request,
    written by nginx: ``msec status scraper ip``). Two notices, each on its own
    cooldown and neither a strike: ``BLOCKED_MIN`` refused requests in the last
    ``TRAFFIC_WINDOW`` (the scraper ran and the nginx block held), and
    ``SURGE_IPS`` distinct IPs among the requests that got through (it changed
    disguise). A daily scraper walked every player page through ~1,000 proxy
    IPs per burst from 2026-11-11 and froze the API on 2026-11-17; normal
    traffic peaked at 158 IPs in five minutes that week, its bursts at 777-1,366.
  * the data backup (``nbs-snapshot``) making no commit for ``BACKUP_STALE_HOURS``.
    The data dir changes every few minutes, so a quiet backup is a stuck one.
    It stalled for 13 days once (2026-11-03 to 2026-11-16, the mass-delete
    guard tripping on every run) and only its own journal said so.

One bad reading is noise (a build spikes and recovers), so it alerts only after
``STRIKES`` checks in a row, posts at most once per ``COOLDOWN``, and posts once
more when the box recovers. The alert names the biggest processes, since that
is the first thing anyone needs to know. State is kept outside the data dir, in
``~/.local/state/nbn/``, because the data dir is backed up and this isn't data.

Alerts go to ``DISCORD_ALERT_CHANNEL`` (inert if unset). ``--no-alert`` prints
the reading and posts nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routers import discord_transport as transport   # noqa: E402

DISCORD_ALERT_CHANNEL = os.environ.get("DISCORD_ALERT_CHANNEL", "").strip()
DISCORD_ADMIN_ID = os.environ.get("DISCORD_ADMIN_ID", "").strip()

SWAP_PCT = 90
AVAIL_MB = 150
SWAP_AVAIL_MB = 300
SLOW_SECS = 5.0
STRIKES = 2
COOLDOWN = 3600
BACKUP_STALE_HOURS = 12
BACKUP_GIT_DIR = "/var/lib/nbs-backup.git"
API_URL = "http://127.0.0.1:8001/api/build/status"
STATE = Path.home() / ".local/state/nbn/box-health.json"
TRAFFIC_LOG = Path("/var/log/nbn/traffic.log")
TRAFFIC_WINDOW = 300
TRAFFIC_TAIL_BYTES = 4 * 1024 * 1024
BLOCKED_MIN = 200
SURGE_IPS = 400
BLOCKED_COOLDOWN = 3 * 3600   # a burst runs about an hour: one notice per burst
SURGE_COOLDOWN = COOLDOWN


def meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, val = line.split(":", 1)
        out[key] = int(val.split()[0])  # kB
    return out


def api_latency() -> float | None:
    """Seconds the API took to answer, or None if it didn't."""
    start = time.monotonic()
    try:
        with urllib.request.urlopen(API_URL, timeout=SLOW_SECS * 2) as resp:
            resp.read()
    except Exception:
        return None
    return time.monotonic() - start


def api_memory_mb() -> tuple[int, int] | None:
    """The API's (resident, swapped) MB, or None if it isn't running."""
    # systemd's own record, not `pgrep -f`, which also matches any shell whose
    # command line happens to mention uvicorn.
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "nbn-api"],
                         capture_output=True, text=True).stdout.strip()
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except (OSError, ValueError):
        return None
    vals = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in status.splitlines()
            if l.startswith(("VmRSS", "VmSwap"))}
    return vals.get("VmRSS", 0), vals.get("VmSwap", 0)


def backup_age_hours() -> float | None:
    """Hours since the newest data backup commit, or None if unreadable."""
    try:
        out = subprocess.run(["git", "--git-dir", BACKUP_GIT_DIR, "log", "-1", "--format=%ct"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return (time.time() - int(out)) / 3600
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def top_processes(n: int = 5) -> list[str]:
    out = subprocess.run(["ps", "-eo", "rss,comm,args", "--sort=-rss", "--no-headers"],
                         capture_output=True, text=True).stdout.splitlines()
    rows = []
    for line in out[:n]:
        rss, comm, args = (line.split(None, 2) + ["", ""])[:3]
        rows.append(f"{int(rss) // 1024} MB  {comm}  {args[:70]}")
    return rows


def traffic(now: float, path: Path = TRAFFIC_LOG) -> tuple[int, int, int] | None:
    """(requests, blocked, distinct IPs that got through) in the last
    ``TRAFFIC_WINDOW``, or None if the log can't be read. Reads only the tail:
    a burst is ~15,000 lines in five minutes, well inside it."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TRAFFIC_TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    since = now - TRAFFIC_WINDOW
    total = blocked = 0
    ips = set()
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        try:
            if float(parts[0]) < since:
                continue
        except ValueError:
            continue
        total += 1
        if parts[2] == "1" and parts[1] == "403":
            blocked += 1
        else:
            ips.add(parts[3])
    return total, blocked, len(ips)


def traffic_notices(t: tuple[int, int, int] | None) -> dict[str, str]:
    """Which scraper notices this reading calls for, keyed by kind."""
    if t is None:
        return {}
    total, blocked, ips = t
    out = {}
    if blocked >= BLOCKED_MIN:
        out["blocked"] = (f"Scraper ran and the nginx block held: {blocked} requests refused "
                          f"in the last {TRAFFIC_WINDOW // 60} minutes.")
    if ips >= SURGE_IPS:
        out["surge"] = (f"Possible scraper getting through: {ips} different IPs in the last "
                        f"{TRAFFIC_WINDOW // 60} minutes (normal is under 160), {total} requests, "
                        f"{blocked} blocked. It may have changed user agent; see "
                        f"/etc/nginx/conf.d/nbn-scrapers.conf.")
    return out


def reading() -> tuple[list[str], str]:
    m = meminfo()
    swap_total = m.get("SwapTotal", 0)
    swap_used = swap_total - m.get("SwapFree", 0)
    swap_pct = 100 * swap_used / swap_total if swap_total else 0
    avail_mb = m["MemAvailable"] // 1024
    lat = api_latency()

    problems = []
    if swap_pct >= SWAP_PCT and avail_mb < SWAP_AVAIL_MB:
        problems.append(f"swap {swap_pct:.0f}% used ({swap_used // 1024} of {swap_total // 1024} MB)")
    if avail_mb < AVAIL_MB:
        problems.append(f"only {avail_mb} MB of RAM available")
    if lat is None:
        problems.append("the API did not answer")
    elif lat > SLOW_SECS:
        problems.append(f"the API took {lat:.1f}s to answer")
    age = backup_age_hours()
    if age is None:
        problems.append("can't read the data backup's history")
    elif age > BACKUP_STALE_HOURS:
        problems.append(f"no data backup for {age:.0f} hours: see `journalctl -u nbs-snapshot`")

    api_mem = api_memory_mb()
    summary = (f"swap {swap_pct:.0f}%, RAM available {avail_mb} MB, API "
               + ("down" if lat is None else f"{lat:.2f}s")
               + ("" if api_mem is None else f" using {api_mem[0]} MB RAM + {api_mem[1]} MB swap"))
    return problems, summary


def post(text: str) -> bool:
    if not transport.configured(DISCORD_ALERT_CHANNEL):
        return False
    # send_now, not send + flush: only a delivered alert may start the cooldown.
    return transport.send_now(DISCORD_ALERT_CHANNEL, {"content": text[:1990]},
                              max_burst=3, burst_window=3600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-alert", action="store_true", help="print the reading, post nothing")
    args = ap.parse_args()

    problems, summary = reading()
    print(summary)
    for p in problems:
        print(f"  PROBLEM  {p}")
    t = traffic(time.time())
    print("traffic: log unreadable" if t is None
          else f"traffic: {t[0]} requests, {t[1]} blocked, {t[2]} IPs through, last {TRAFFIC_WINDOW // 60} min")
    if args.no_alert:
        return 1 if problems else 0

    try:
        state = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    strikes = state.get("strikes", 0) + 1 if problems else 0
    alerting = state.get("alerting", False)
    last = state.get("last_alert", 0)
    now = time.time()

    if strikes >= STRIKES and now - last >= COOLDOWN:
        mention = f"<@{DISCORD_ADMIN_ID}> " if DISCORD_ADMIN_ID else ""
        body = "\n".join(f"• {p}" for p in problems)
        procs = "\n".join(top_processes())
        if post(f"{mention}**Server check failed** ({summary})\n{body}\n"
                f"Biggest processes:\n```\n{procs}\n```"):
            last, alerting = now, True
    elif not problems and alerting:
        if post(f"Server checks are back to normal ({summary})."):
            alerting = False

    traffic_last = state.get("traffic_last", {})
    for kind, text in traffic_notices(traffic(now)).items():
        cooldown = SURGE_COOLDOWN if kind == "surge" else BLOCKED_COOLDOWN
        if now - traffic_last.get(kind, 0) < cooldown:
            continue
        mention = f"<@{DISCORD_ADMIN_ID}> " if kind == "surge" and DISCORD_ADMIN_ID else ""
        if post(mention + text):
            traffic_last[kind] = now

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"strikes": strikes, "alerting": alerting,
                                 "last_alert": last, "traffic_last": traffic_last}))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

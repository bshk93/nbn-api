"""Regression tests for check_stats_integrity.py — the weekly check that the raw
box score files have only ever grown (dev-deploy spec, Phase 2 item 10).

What matters here is the asymmetry between the two kinds of file. The current
season's two files are appended to all week and are *expected* to differ from the
last check; every closed season's file is finished forever and must match byte for
byte. A check that only counted rows would miss an in-place edit of a closed
season entirely, which is precisely the "bad manual fix" the spec names as threat
number one.

The other load-bearing behaviour is that a violation is **not** written into the
manifest. Re-baselining on sight would report each corruption once and then treat
it as the new floor, so the second week's check would pass and the damage would
become permanent silently.

Nothing here touches the live data directory or the network.

    venv/bin/python -m tests.test_stats_integrity
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_stats_integrity as ci  # noqa: E402
import routers.discord_transport as tp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


SEASON = "25-26"
HEADER = "TEAM,DATE,PLAYER,P\n"


def write_rows(path: Path, n: int, pts: int = 20):
    path.write_text(HEADER + "".join(
        f"PHX,2026-01-{i:02d},Booker Devin,{pts}\n" for i in range(1, n + 1)))


def seed(tmp: Path, live_rows=3, closed_rows=5):
    write_rows(tmp / f"allstats-{SEASON}.csv", live_rows)
    write_rows(tmp / "allstats-playoffs-26.csv", 2)
    write_rows(tmp / "allstats-24-25.csv", closed_rows)


def run(tmp: Path, **kw) -> int:
    argv = sys.argv
    sys.argv = ["check_stats_integrity.py", "--data-dir", str(tmp), "--no-alert"] + \
               (["--accept"] if kw.get("accept") else [])
    try:
        return ci.main()
    finally:
        sys.argv = argv


def manifest(tmp: Path) -> dict:
    return json.loads((tmp / ci.MANIFEST_NAME).read_text())["files"]


ci._current_league_year = lambda: SEASON

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    seed(tmp)

    check("first run seeds and passes", run(tmp) == 0)
    m = manifest(tmp)
    check("the manifest describes every file", set(m) == {
        f"allstats-{SEASON}.csv", "allstats-playoffs-26.csv", "allstats-24-25.csv"})
    check("rows are counted without the header", m[f"allstats-{SEASON}.csv"]["rows"] == 3)

    # ── The current season growing is the normal case ─────────────────────────
    write_rows(tmp / f"allstats-{SEASON}.csv", 9)
    check("the live file growing is not a violation", run(tmp) == 0)
    check("the manifest advances to the new count", manifest(tmp)[f"allstats-{SEASON}.csv"]["rows"] == 9)

    # ── A closed season must not move at all ──────────────────────────────────
    write_rows(tmp / "allstats-24-25.csv", 5, pts=99)      # same row count, edited in place
    rc = run(tmp)
    check("an in-place edit of a closed season is a violation", rc == 1)
    check("the closed season keeps its last good entry",
          manifest(tmp)["allstats-24-25.csv"]["rows"] == 5
          and manifest(tmp)["allstats-24-25.csv"]["sha256"] != ci.scan(tmp)["allstats-24-25.csv"]["sha256"])
    check("it alerts again on the next run rather than accepting it", run(tmp) == 1)
    check("--accept re-baselines it", run(tmp, accept=True) == 0)
    check("and then the run is clean", run(tmp) == 0)

    # Appending to a closed season is a violation too — the season is over.
    write_rows(tmp / "allstats-24-25.csv", 6, pts=99)
    check("appending to a closed season is a violation", run(tmp) == 1)
    run(tmp, accept=True)

    # ── Truncation, on any file ───────────────────────────────────────────────
    write_rows(tmp / f"allstats-{SEASON}.csv", 4)
    check("the live file losing rows is a violation", run(tmp) == 1)
    check("the truncated file keeps its pre-loss row count", manifest(tmp)[f"allstats-{SEASON}.csv"]["rows"] == 9)
    write_rows(tmp / f"allstats-{SEASON}.csv", 9)
    check("restoring the rows clears it without --accept", run(tmp) == 0)

    # ── A file disappearing ───────────────────────────────────────────────────
    (tmp / "allstats-playoffs-26.csv").unlink()
    check("a vanished file is a violation", run(tmp) == 1)
    check("its entry survives so the alert repeats", "allstats-playoffs-26.csv" in manifest(tmp))

    # ── A new season's file appearing ─────────────────────────────────────────
    write_rows(tmp / "allstats-playoffs-26.csv", 2)
    run(tmp)
    write_rows(tmp / "allstats-26-27.csv", 1)
    check("a brand new file is recorded, not flagged", run(tmp) == 0)
    check("and is in the manifest", "allstats-26-27.csv" in manifest(tmp))

    # ── Row counting survives an embedded newline ─────────────────────────────
    quoted = tmp / "allstats-23-24.csv"
    quoted.write_text(HEADER + 'PHX,2026-01-01,"Booker\nDevin",20\n')
    check("a quoted embedded newline counts as one row", ci.scan(tmp)["allstats-23-24.csv"]["rows"] == 1)

# ── The July-to-October gap ───────────────────────────────────────────────────
# The stats clock rolls to the new season on July 1, but a playoff run can finish
# after it — so the newest season on disk stays live until its successor exists.
GAP = {
    "allstats-25-26.csv": {"rows": 10, "sha256": "a", "bytes": 1},
    "allstats-playoffs-26.csv": {"rows": 5, "sha256": "b", "bytes": 1},
    "allstats-24-25.csv": {"rows": 20, "sha256": "c", "bytes": 1},
}
grown = {k: dict(v) for k, v in GAP.items()}
grown["allstats-playoffs-26.csv"] = {"rows": 8, "sha256": "b2", "bytes": 2}
v, _ = ci.compare(grown, GAP, "26-27")
check("a July finals game against a 26-27 clock is not a violation", v == [], v)

stale = {k: dict(v) for k, v in GAP.items()}
stale["allstats-24-25.csv"] = {"rows": 20, "sha256": "c2", "bytes": 1}
v, _ = ci.compare(stale, GAP, "26-27")
check("the season before the newest is still frozen", len(v) == 1 and "24-25" in v[0], v)

started = {**{k: dict(v) for k, v in GAP.items()},
           "allstats-26-27.csv": {"rows": 3, "sha256": "d", "bytes": 1}}
started["allstats-playoffs-26.csv"] = {"rows": 8, "sha256": "b2", "bytes": 2}
v, _ = ci.compare(started, {**GAP, "allstats-26-27.csv": {"rows": 3, "sha256": "d", "bytes": 1}}, "26-27")
check("once the new season has a file, last season freezes", len(v) == 1 and "playoffs-26" in v[0], v)

# ── Alerting ──────────────────────────────────────────────────────────────────
SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"

ci.DISCORD_ALERT_CHANNEL = ""
check("no channel configured means nothing is posted", ci.alert(["x: LOST 5 rows"], Path("/tmp")) is False)

ci.DISCORD_ALERT_CHANNEL = "alerts-chan"
check("a configured channel gets the violation", ci.alert(["x: LOST 5 rows"], Path("/tmp")) is True)
check("the message names the file and the loss",
      SENT and "x: LOST 5 rows" in SENT[0][1]["content"], SENT[0][1]["content"][:80] if SENT else "")
check("and stays inside Discord's length limit", len(SENT[0][1]["content"]) <= 2000)

print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_stats_integrity: all pass")

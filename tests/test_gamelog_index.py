"""The gamelog index and GET /api/rosters/slugs.

`get_player_gamelog` answers from a per-file index (`_rows_by_player`) instead
of parsing every raw box score file per request. It must return exactly what
the scan did, and see a file that changed. Runs on a temp dir, no live data.

    venv/bin/python -m tests.test_gamelog_index
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import boxscores, roster_picks  # noqa: E402
from routers.storage import read_csv  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def scan(name, data_dir):
    """The gamelog as it was computed before the index: every file, every row."""
    out = []
    for path in sorted(data_dir.glob("allstats-??-??.csv")):
        season = path.stem[len("allstats-"):]
        for r in read_csv(path)[1]:
            if r.get("PLAYER", "").strip().upper() == name:
                out.append(boxscores._gamelog_row(r, season))
    for path in sorted(data_dir.glob("allstats-playoffs-??.csv")):
        end = path.stem[len("allstats-playoffs-"):]
        season = f"{str(int(end) - 1).zfill(2)[-2:]}-{end}"
        for r in read_csv(path)[1]:
            if r.get("PLAYER", "").strip().upper() == name:
                out.append(boxscores._gamelog_row(r, season))
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


HEAD = "TEAM,DATE,OPP,PLAYER,M,P,R,A,S,B,gametype"
tmp = Path(tempfile.mkdtemp())
(tmp / "allstats-24-25.csv").write_text(HEAD + "\n"
    "BOS,2024-11-01,NYK,\"Tatum, Jayson\",36,30,8,5,1,1,REG\n"
    "BOS,2024-11-01,NYK,\"BROWN, JAYLEN\",34,22,6,3,2,0,REG\n"
    "BOS,2024-11-03,MIA,\" tatum, jayson \",38,41,9,7,0,2,REG\n")
(tmp / "allstats-25-26.csv").write_text(HEAD + "\n"
    "BOS,2025-11-02,ATL,\"TATUM, JAYSON\",35,28,10,4,1,0,REG\n")
(tmp / "allstats-playoffs-25.csv").write_text(HEAD + "\n"
    "BOS,2025-04-20,ORL,\"TATUM, JAYSON\",40,33,11,6,2,1,PLAYOFF\n")
(tmp / "bos-roster.csv").write_text("SLUG\ntatum-jayson\n\nbrown-jaylen\n")

BIOS = {"tatum-jayson": {"name": "TATUM, JAYSON"}, "brown-jaylen": {"name": "Brown, Jaylen"},
        "nobody-here": {"name": "HERE, NOBODY"}}
saved = (boxscores.DATA_DIR, boxscores.player_bios_shared, roster_picks.DATA_DIR)
boxscores.DATA_DIR = tmp
boxscores.player_bios_shared = lambda: BIOS
roster_picks.DATA_DIR = tmp
boxscores._GAMELOG_INDEX.clear()
try:
    print("gamelog: the index answers what the scan answered")
    for slug, bio in BIOS.items():
        name = bio["name"].upper()
        check(slug, boxscores.get_player_gamelog(slug) == scan(name, tmp))
    log = boxscores.get_player_gamelog("tatum-jayson")
    check("regular seasons and playoffs, newest first",
          [g["date"] for g in log] == ["2025-11-02", "2025-04-20", "2024-11-03", "2024-11-01"])
    check("a player with no games is an empty list", boxscores.get_player_gamelog("nobody-here") == [])

    print("gamelog: files are parsed once, and again only when they change")
    reads = []
    real = boxscores.read_csv
    boxscores.read_csv = lambda p: (reads.append(p.name), real(p))[1]
    try:
        boxscores.get_player_gamelog("tatum-jayson")
        boxscores.get_player_gamelog("brown-jaylen")
        check("no file re-read for a second player", reads == [])
        cur = tmp / "allstats-25-26.csv"
        with cur.open("a") as f:
            f.write("BOS,2025-11-04,PHI,\"TATUM, JAYSON\",37,45,7,8,3,1,REG\n")
        st = cur.stat()
        os.utime(cur, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        log = boxscores.get_player_gamelog("tatum-jayson")
        check("an appended game appears", log[0]["date"] == "2025-11-04" and len(log) == 5)
        check("only the changed file was re-read", reads == ["allstats-25-26.csv"])
        check("still equal to the scan", log == scan("TATUM, JAYSON", tmp))
        (tmp / "allstats-playoffs-26.csv").write_text(HEAD + "\n"
            "BOS,2026-04-19,NYK,\"TATUM, JAYSON\",41,38,12,5,1,2,PLAYOFF\n")
        check("a new file is picked up", len(boxscores.get_player_gamelog("tatum-jayson")) == 6)
    finally:
        boxscores.read_csv = real

    print("GET /api/rosters/slugs")
    r = roster_picks.get_roster_slugs()
    check("all 30 teams", sorted(r) == sorted(roster_picks.VALID_TEAMS))
    check("slugs in file order, blanks dropped", r["BOS"] == ["tatum-jayson", "brown-jaylen"])
    check("a missing roster file is an empty list", r["ATL"] == [])
finally:
    boxscores.DATA_DIR, boxscores.player_bios_shared, roster_picks.DATA_DIR = saved
    boxscores._GAMELOG_INDEX.clear()

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nall passed")

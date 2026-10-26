"""GET /api/bird-rights: the § 3.8 tier on each rostered UFA/RFA hold, and how
it is known. Loaders and the tenure walk are patched, so nothing here reads
or writes the live data dir.

    venv/bin/python -m tests.test_bird_rights_endpoint
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import bird_rights as B  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


BIOS = {
    "stored": {"cap_holds": {"28-29": "UFA"}, "bird_tiers": {"28-29": "QVFA"}},
    "derived": {"cap_holds": {"27-28": "PLAYER_OPT", "28-29": "RFA"}},
    "nobody": {"cap_holds": {"28-29": "UFA"}},
    "past": {"cap_holds": {"25-26": "UFA"}},
    "elsewhere": {"cap_holds": {"28-29": "UFA"}},
}
TENURE = {
    "stored": {"tier": "EQVFA", "seasons": 2, "basis": "ledger", "evidence": "signed with MIL"},
    "derived": {"tier": "EQVFA", "seasons": 2, "basis": "draft", "evidence": "drafted by MIL"},
    "nobody": {"tier": None, "seasons": None, "basis": "unknown", "evidence": "no record"},
}


def main():
    B._rosters = lambda: {"stored": "MIL", "derived": "MIL", "nobody": "MIL", "past": "MIL", "elsewhere": "BOS"}
    B.load_player_bios = lambda: BIOS
    B._current_league_year = lambda: "26-27"
    B._bird_tenure = lambda slug, team, season, bio: TENURE.get(slug) or {
        "tier": "QVFA", "seasons": 4, "basis": "ledger", "evidence": ""}

    print("one team")
    out = B.get_bird_rights(team="mil", player=None)["players"]
    check("narrowed to MIL, case-insensitive", "elsewhere" not in out and "stored" in out)
    check("a stored tier wins and says so",
          out["stored"]["holds"]["28-29"]["tier"] == "QVFA"
          and out["stored"]["holds"]["28-29"]["source"] == "record")
    d = out["derived"]["holds"]
    check("an option year isn't a Bird hold", list(d) == ["28-29"])
    check("a derived tier carries its basis", d["28-29"]["tier"] == "EQVFA" and d["28-29"]["source"] == "draft")
    check("RFA holds count", d["28-29"]["type"] == "RFA")
    n = out["nobody"]["holds"]["28-29"]
    check("nothing on file is unknown, never Non-Bird", n["tier"] is None and n["source"] == "unknown")
    check("a hold before the current season is left out", "past" not in out)

    print("\none player")
    out = B.get_bird_rights(team=None, player="elsewhere")["players"]
    check("only that player, on his own team", list(out) == ["elsewhere"] and out["elsewhere"]["team"] == "BOS")

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

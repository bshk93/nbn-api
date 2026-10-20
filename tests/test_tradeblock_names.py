"""Trading-block names match without regard to case.

The block stores display names. Older entries carry a hand-cased spelling
("Jared McCain") where `_display_name` now gives "Jared Mccain", and an exact
match treated them as two players: GSW ended up with McCain listed twice, and a
trade would have scrubbed only one of them.

Every read and write goes through patched loaders, so nothing touches the live
data directory.

    venv/bin/python -m tests.test_tradeblock_names
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.players as pl  # noqa: E402
import routers.roster_picks as rp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


BIOS = {"mccain-jared": {"name": "MCCAIN, JARED"}, "okoro-isaac": {"name": "OKORO, ISAAC"}}
STORE: dict = {}
INFO = {"name": "Tester", "roles": ["admin"]}

rp.load_trading_block = lambda: {t: rp._normalize_team_block(v) for t, v in STORE.items()}
rp.save_trading_block = lambda data: STORE.update(data)
rp.log_write = lambda *a, **k: None
rp.read_csv = lambda path: (["SLUG"], [{"SLUG": s} for s in BIOS])
pl.load_player_bios = lambda: BIOS


def reset(*names):
    STORE.clear()
    STORE["GSW"] = {"players": [{"player": n, "notes": f"note {n}"} for n in names], "picks": []}


def gsw_names():
    return [e["player"] for e in STORE["GSW"]["players"]]


print("\nper-player add")
reset("Jared McCain")
rp.put_trading_block_player("GSW", "mccain-jared", rp.TradingBlockPlayerIn(notes="new"), INFO)
check("re-adding updates the old entry, not a second one", len(gsw_names()) == 1, gsw_names())
check("the entry takes the current spelling", gsw_names() == ["Jared Mccain"], gsw_names())

print("\nper-player remove")
reset("Jared McCain", "Isaac Okoro")
rp.delete_trading_block_player("GSW", "mccain-jared", INFO)
check("removes the differently-cased entry", gsw_names() == ["Isaac Okoro"], gsw_names())

print("\nwhole-block save")
reset()
body = rp.TeamTradeBlock(players=[{"player": "Jared McCain"}, {"player": "Isaac Okoro"},
                                  {"player": "Jared Mccain"}])
rp.put_trading_block("GSW", body, INFO)
check("keeps one entry per player, the first", gsw_names() == ["Jared McCain", "Isaac Okoro"], gsw_names())

print("\nscrub after a trade")
reset("Jared McCain", "Isaac Okoro")
pl._scrub_trading_block({"GSW": {"mccain-jared"}}, BIOS)
check("scrubs the differently-cased entry", gsw_names() == ["Isaac Okoro"], gsw_names())

print()
if FAILS:
    print(f"{len(FAILS)} failed: {FAILS}")
    sys.exit(1)
print("all passed")

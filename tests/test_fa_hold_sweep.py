"""`sweep_fa_holds` — § 3.10: a UFA/RFA hold still on a roster at opening
night is renounced automatically.

The sweep runs hourly (`sweep_fa_holds.py`). Before opening night it does
nothing. From opening night on, every player whose hold is for the current
season itself is renounced and announced like an owner's own renounce. It
leaves alone a hold for a later season (a player under contract now), two-ways
and draft rights, and an RFA with an open offer sheet.

Every write is stubbed: bios, ledger, Discord, inbox.

    venv/bin/python -m tests.test_fa_hold_sweep
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.transactions as T  # noqa: E402
import routers.season_calendar as cal  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


BIOS = {
    "ufa": {"name": "HOLD, UFA", "type": "player", "cap_holds": {"26-27": "UFA"}},
    "rfa": {"name": "HOLD, RFA", "type": "player", "cap_holds": {"26-27": "RFA"}},
    "sheet": {"name": "OPEN, SHEET", "type": "player", "cap_holds": {"26-27": "RFA"}},
    "signed": {"name": "UNDER, CONTRACT", "type": "player", "cap_holds": {"27-28": "UFA"}},
    "twoway": {"name": "TWO, WAY", "type": "two-way", "cap_holds": {"26-27": "RFA"}},
    "rights": {"name": "DRAFT, RIGHTS", "type": "draft-rights", "cap_holds": {}},
}
TEAMS = {"ufa": "MIL", "rfa": "TOR", "sheet": "TOR", "signed": "MIL", "twoway": "MIL", "rights": "MIL"}

RENOUNCED, LEDGER, POSTS, SENT = [], [], [], []


def apply_renounce(details, txn_date, info):
    RENOUNCED.append((details.player, txn_date))
    BIOS[details.player]["cap_holds"] = {}
    BIOS[details.player]["type"] = ""
    return TEAMS.pop(details.player), {"cap_holds": {}}


T.load_player_bios = lambda: BIOS
T._build_team_map = lambda: dict(TEAMS)
T._season_for_date = lambda d: "26-27"
T._open_offer_sheets = lambda: [{"player": "sheet"}]
T._apply_renounce = apply_renounce
T._append_transaction = lambda txn: LEDGER.append(txn)
T.notify_transaction = lambda txn, relay_to_roster_log=False: POSTS.append(("embed", relay_to_roster_log))
T.announce_roster_move = lambda txn: POSTS.append(("fa-news", txn["details"]["player"]))
T.inbox.notify_team = lambda team, text, link=None: SENT.append((team, text))
cal.opening_night = lambda season: "2026-10-20"


print("before opening night")
check("nothing renounced the day before", T.sweep_fa_holds("2026-10-19") == {"renounced": []})
check("no writes", not RENOUNCED and not LEDGER)

print("on opening night")
done = T.sweep_fa_holds("2026-10-20")
check("the current-season UFA and RFA holds are renounced",
      sorted(p for p, _ in RENOUNCED) == ["rfa", "ufa"])
check("an RFA with an open offer sheet is left alone", "sheet" in TEAMS)
check("a player under contract this season is left alone", "signed" in TEAMS)
check("two-ways and draft rights are left alone", "twoway" in TEAMS and "rights" in TEAMS)
check("dated opening night", all(d == "2026-10-20" for _, d in RENOUNCED))
check("one ledger entry each, by the system",
      len(LEDGER) == 2 and all(t["type"] == "renounce" and t["created_by"] == "system"
                               and t["details"]["_source"] == "auto_opening_night"
                               and "_snapshot" in t["details"] for t in LEDGER))
check("announced like an owner's renounce",
      POSTS.count(("embed", True)) == 2 and sum(1 for p in POSTS if p[0] == "fa-news") == 2)
check("each team told in its inbox", sorted(t for t, _ in SENT) == ["MIL", "TOR"])
check("returns what it did", sorted(done["renounced"]) == [("MIL", "ufa"), ("TOR", "rfa")])

print("the next hour")
check("idempotent: nothing left to renounce", T.sweep_fa_holds("2026-10-20") == {"renounced": []})

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

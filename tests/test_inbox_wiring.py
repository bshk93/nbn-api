"""Regression tests for the two new inbox call sites that don't already share
a fixture with an existing suite: `inbox.notify_team` (team-role fan-out) and
`waivers._notify_waiver_claimants` (win/lose per claim).

The other wave-2 call sites (FA offer restore/finalize/return-to-agent,
suggestion comments, offer sheets, member role/tenure changes) are covered by
manual review plus the existing suites' full-run pollution check — they share
those suites' large fixtures and adding a second, parallel fixture here would
drift from them rather than add real coverage.

    venv/bin/python -m tests.test_inbox_wiring
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.inbox as ib  # noqa: E402
import routers.waivers as w  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


CALLS = []
ib.notify_member = lambda *a, **k: CALLS.append((a, k))
# waivers.py imported notify_member as part of `from . import inbox` — it
# calls `inbox.notify_member`, which resolves this attribute at call time, so
# patching the name on the module object (not rebinding waivers' import) is
# what actually intercepts it.

MEMBERS = {
    "alice": {"roles": ["phx", "rosters"], "tenures": []},
    "bob":   {"roles": ["phx"], "tenures": []},
    "carol": {"roles": ["lal"], "tenures": []},
}
ib.load_members = lambda: MEMBERS

print("\nnotify_team")
CALLS.clear()
ib.notify_team("phx", "hello PHX", link="/x")
recipients = sorted(c[0][0] for c in CALLS)
check("delivers to every member holding the team's role", recipients == ["alice", "bob"])
check("skips a member holding a different team's role", "carol" not in recipients)

CALLS.clear()
ib.notify_team("lal", "hello LAL")
check("case-insensitive team match", [c[0][0] for c in CALLS] == ["carol"])

CALLS.clear()
ib.notify_team("mia", "nobody here")
check("a team with no current role holders notifies nobody", CALLS == [])

print("\n_notify_waiver_claimants")
w._waiver_claims_for = lambda txn_id: [
    {"team": "PHX", "created_by": "alice"},
    {"team": "LAL", "created_by": "carol"},
    {"team": "BOS", "created_by": "system"},   # excluded: system-filed
    {"team": "MIA", "created_by": None},       # excluded: no recipient
]

CALLS.clear()
w._notify_waiver_claimants("txn1", "some-player", "PHX")
by_recipient = {c[0][0]: c[0][1] for c in CALLS}
check("exactly the two real claimants are notified", set(by_recipient) == {"alice", "carol"})
check("the winner is told they were awarded the player", "awarded" in by_recipient["alice"])
check("the loser is told who won", "PHX" in by_recipient["carol"] and "lost priority" in by_recipient["carol"])

CALLS.clear()
w._notify_waiver_claimants("txn2", "some-player", None)
by_recipient = {c[0][0]: c[0][1] for c in CALLS}
check("an unclaimed resolution tells every claimant, naming no winner",
      set(by_recipient) == {"alice", "carol"} and all("not awarded" in t for t in by_recipient.values()))

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

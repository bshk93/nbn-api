"""Backfilling a two-way conversion from #fa-news.

The importer used to skip every "convert" post as "no sign or option
language", so 25 conversions (Ryan Nembhard's among them) never reached the
ledger, and his history read as a two-way traded to LAC. Covers the resolver's
two templates, the submitter's payload and its skip of a conversion the office
already entered, and the historical path in POST /api/transactions, which
must log without touching the bio.

    venv/bin/python -m tests.test_historical_convert
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

import resolve_discord_fa_signings as res  # noqa: E402
import submit_discord_fa_signings as sub  # noqa: E402
import routers.transactions as t  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


NAME_MAP = {"ryan nembhard": "nembhard-ryan", "vit krejci": "krejci-vit",
            "johnny furphy": "furphy-johnny", "jamal shead": "shead-jamal"}


def parse(content):
    return res.parse_message({"content": content, "timestamp": "2026-02-15T00:00:00Z"}, NAME_MAP, {})


print("\nresolver")
r = parse("The Toronto Raptors convert Ryan Nembhard to a 2yr min")
check("team-first conversion resolves", r["bucket"] == "resolved"
      and [(c["kind"], c["team"], c["slug"]) for c in r["candidates"]] == [("convert", "TOR", "nembhard-ryan")])
r = parse("Vit Krejci has been converted from a two way contract to a $7,898,151 2+1 TO contract with the New York Knicks")
check("player-first conversion resolves", r["bucket"] == "resolved"
      and [(c["team"], c["slug"]) for c in r["candidates"]] == [("NYK", "krejci-vit")])
r = parse("The Sacramento Kings convert Johnny Furphy and Jamal Shead to 3yr mins")
check("two players in one post give two candidates", r["bucket"] == "resolved"
      and [c["slug"] for c in r["candidates"]] == ["furphy-johnny", "shead-jamal"])
r = parse("The Toronto Raptors convert Ryan Nembhard to a 2yr min and sign Vit Krejci to a min")
check("a conversion mixed with a signing is flagged", r["bucket"] == "flagged")

print("\nsubmitter")
e = {"kind": "convert", "date": "2026-02-15", "description": "x", "slug": "nembhard-ryan", "team": "TOR"}
p = sub.build_payload(e)
check("payload is a historical convert_twoway with the team", p["type"] == "convert_twoway"
      and p["historical"] is True and p["details"] == {"player": "nembhard-ryan", "team": "TOR", "contract": {}})
ledger = [{"type": "convert_twoway", "date": "2026-02-14", "details": {"player": "nembhard-ryan", "team": "TOR"}}]
check("a conversion already in the ledger is skipped", sub.already_converted(e, ledger))
check("one months apart is not the same conversion",
      not sub.already_converted({**e, "date": "2026-04-15"}, ledger))
check("nor one with another team", not sub.already_converted({**e, "team": "LAC"}, ledger))

print("\nhistorical POST /api/transactions")
APPENDED = []
t.load_player_bios = lambda: {"nembhard-ryan": {"type": "player"}}
t._append_transaction = APPENDED.append
INFO = {"name": "tester", "roles": ["admin"]}


def post(details):
    return t.create_transaction(t.TransactionIn(type="convert_twoway", date="2026-02-15",
                                                description="d", historical=True, details=details), INFO)


out = post({"player": "nembhard-ryan", "team": "tor", "contract": {}})
check("logs a convert_twoway with the team upper-cased", out["type"] == "convert_twoway"
      and out["details"]["team"] == "TOR" and out["details"]["historical"] is True and APPENDED == [out])
for name, details in [("an unknown player is a 422", {"player": "nobody", "team": "TOR", "contract": {}}),
                      ("a missing team is a 422", {"player": "nembhard-ryan", "contract": {}})]:
    try:
        post(details)
        check(name, False)
    except HTTPException as exc:
        check(name, exc.status_code == 422)

if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nall checks passed")

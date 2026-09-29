"""TRC votes from Discord — the 👍/👎 buttons on a trc-alerts post.

A click is the whole vote, cast through `ballot_trade_request` with a fixed
note. The clicker is matched by linked Discord id and must
hold `trc`. Nothing here touches disk or Discord: members and the ballot
endpoint are stubbed.

    venv/bin/python3 tests/test_trc_discord.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

import routers.trc_discord as td  # noqa: E402
import routers.trc_notify as tn  # noqa: E402
import routers.trade_requests as tr  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(name)


td.load_members = lambda: {
    "Voter": {"discord_id": "111", "roles": ["trc"]},
    "Fan": {"discord_id": "222", "roles": ["uta"]},
}
CAST = []
RESULT = {"status": "balloting", "number": 1, "parties": ["LAL", "UTA"], "ballots": {"Voter": {"decision": "approve"}}}


def fake_ballot(request_id, body, info):
    CAST.append((request_id, body.decision, body.note, info["name"]))
    if RAISE["value"]:
        raise HTTPException(status_code=422, detail="Request is awaiting_consent, not open for a vote")
    return RESULT


RAISE = {"value": False}
tr.ballot_trade_request = fake_ballot


def click(did, custom_id):
    return td.handle_component({"member": {"user": {"id": did}}, "data": {"custom_id": custom_id}})


def text(r):
    return r["data"]["content"]


print("the buttons")
rows = tn.vote_buttons("abc123")
btns = rows[0]["components"]
check("two emoji-only buttons, 👍 then 👎", [b["emoji"]["name"] for b in btns] == ["👍", "👎"]
      and not any("label" in b for b in btns))
check("carrying the decision and the request", [b["custom_id"] for b in btns]
      == ["trc_vote:approve:abc123", "trc_vote:reject:abc123"])

print("\nwho can click")
r = click("999", "trc_vote:approve:abc123")
check("an unlinked account is told to /link", "/link" in text(r) and r["data"]["flags"] == td.EPHEMERAL)
r = click("222", "trc_vote:approve:abc123")
check("a member without trc is refused", "Only TRC members" in text(r))
check("unknown buttons are refused", "Unsupported" in text(click("111", "something:else")))

print("\na TRC member votes with one click")
r = click("111", "trc_vote:approve:abc123")
check("the click casts the vote as that member, with the Discord note",
      CAST[-1] == ("abc123", "approve", td.DISCORD_NOTE, "Voter"), CAST)
check("and says where it stands, privately", text(r) == "Approved the LAL ⇄ UTA trade — 1/3 approvals."
      and r["data"]["flags"] == td.EPHEMERAL, text(r))
click("111", "trc_vote:reject:abc123")
check("👎 casts a reject", CAST[-1][1] == "reject")
RESULT.update(status="ready_to_finalize", ballots={n: {"decision": "approve"} for n in "abc"})
r = click("111", "trc_vote:approve:abc123")
check("the third approval says it's ready", text(r).endswith("3/3 approvals. It's ready to finalize."), text(r))
RAISE["value"] = True
r = click("111", "trc_vote:reject:abc123")
check("a refused vote says why", text(r).startswith("Vote not recorded — Request is awaiting_consent"), text(r))
RAISE["value"] = False
n = len(CAST)
click("222", "trc_vote:approve:abc123")
click("999", "trc_vote:approve:abc123")
check("no vote is cast for a non-TRC or unlinked clicker", len(CAST) == n)

if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nall checks passed")

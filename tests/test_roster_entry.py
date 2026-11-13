"""The streamer's queue of roster moves to make in 2K.

- Only moves of a player count: an extension, an option, a picks-only or
  backfilled trade, an unclaimed waiver clear, a matched offer sheet and a
  voided transaction never show.
- Nothing from before SINCE shows.
- The queue is oldest first.
- PUT needs `streamer` or `updates` (or admin); anyone else 403. An id not
  in the queue is a 404. Unticking removes the tick.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_roster_entry
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.roster_entry as re_  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


STREAM_TOKEN, UPD_TOKEN, ADMIN_TOKEN, PLAIN_TOKEN = "s" * 64, "u" * 64, "d" * 64, "n" * 64
MEMBERS = {
    "Streamer": {"token": STREAM_TOKEN, "roles": ["streamer"], "tenures": []},
    "Updater":  {"token": UPD_TOKEN,    "roles": ["updates"],  "tenures": []},
    "Admin":    {"token": ADMIN_TOKEN,  "roles": ["admin"],    "tenures": []},
    "Nobody":   {"token": PLAIN_TOKEN,  "roles": [],           "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-roster-entry-test-"))
re_.ROSTER_ENTRY_FILE = TMP / "roster-entry.json"
re_.log_write = lambda info, msg: None


def txn(id_, type_, at, **details):
    return {"id": id_, "type": type_, "date": at[:10], "created_at": at,
            "description": "", "details": details}


def player_trade(id_, at, **extra):
    return txn(id_, "trade", at, transfers=[{"from_team": "MEM", "to_team": "GSW",
                                             "assets": [{"type": "player", "slug": "doe-john"}]}], **extra)


LEDGER = [
    txn("old", "sign", "2026-10-20T00:00:00Z", player="a", team="ATL"),
    txn("sign2", "sign", "2026-11-05T12:00:00Z", player="b", team="ATL"),
    txn("rel1", "release", "2026-11-05T11:00:00Z", player="c", team="ATL"),
    txn("ext", "extension", "2026-11-05T13:00:00Z", player="d", team="ATL"),
    txn("opt", "option", "2026-11-05T13:00:00Z", player="d", team="ATL"),
    player_trade("trade1", "2026-11-06T00:00:00Z"),
    player_trade("hist", "2026-11-06T00:00:00Z", historical=True),
    txn("picks", "trade", "2026-11-06T00:00:00Z",
        transfers=[{"from_team": "MEM", "to_team": "GSW", "assets": [{"type": "pick", "year": 2027}]}]),
    txn("clear", "waiver_clear", "2026-11-07T00:00:00Z", player="c", outcome="unclaimed"),
    txn("claim", "waiver_clear", "2026-11-07T00:00:00Z", player="c", outcome="claimed", claimed_by="BOS"),
    txn("matched", "offer_sheet_decision", "2026-11-07T00:00:00Z", player="e", outcome="matched"),
    txn("moved", "offer_sheet_decision", "2026-11-07T00:00:00Z", player="e", outcome="not_matched"),
    {**txn("void", "sign", "2026-11-08T00:00:00Z", player="f", team="ATL"), "voided": True},
    txn("conv", "convert_twoway", "2026-11-08T00:00:00Z", player="g", team="ATL"),
    txn("sign3", "sign", "2026-11-09T00:00:00Z", player="h", team="ATL"),
    txn("rel2", "release", "2026-11-09T00:00:00Z", player="i", team="ATL"),
]
re_._load_ledger = lambda: LEDGER

app = FastAPI()
app.include_router(re_.router)
client = TestClient(app)


def ids():
    return [i["id"] for i in client.get("/api/roster-entry").json()["items"]]


def put(token, txn_id, done):
    return client.put(f"/api/roster-entry/{txn_id}", json={"done": done},
                      headers={"Authorization": f"Bearer {token}"})


print("roster entry queue")
check("only player moves, oldest first, a release before its signing",
      ids() == ["rel1", "sign2", "trade1", "claim", "moved", "conv", "rel2", "sign3"])
check("no role is 403", put(PLAIN_TOKEN, "sign2", True).status_code == 403)
check("extension is not in the queue: 404", put(STREAM_TOKEN, "ext", True).status_code == 404)
check("before SINCE: 404", put(STREAM_TOKEN, "old", True).status_code == 404)
r = put(STREAM_TOKEN, "sign2", True)
check("streamer can tick", r.status_code == 200 and r.json()["done"]["by"] == "Streamer")
item = next(i for i in client.get("/api/roster-entry").json()["items"] if i["id"] == "sign2")
check("GET shows who ticked it", (item["done"] or {}).get("by") == "Streamer")
check("updates role can tick", put(UPD_TOKEN, "trade1", True).status_code == 200)
check("admin can untick", put(ADMIN_TOKEN, "sign2", False).status_code == 200)
item = next(i for i in client.get("/api/roster-entry").json()["items"] if i["id"] == "sign2")
check("unticked reads null", item["done"] is None)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")

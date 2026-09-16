"""`routers/tips.py` — member-to-member NB¥ tipping.

Pins the `context` field added so an article page can show tips sent
against that specific piece of content, without those tips being
indistinguishable from a plain member-to-member tip:

  * a tip with no context behaves exactly as before (GET /api/tips/{member})
  * a tip with a context shows up under GET /api/tips/context/{context}
    and NOT under a different context
  * /api/tips/context/{context} totals and lists only that context's tips,
    independent of how many other tips (contexted or not) exist
  * self-tip and unknown-member rejection are unaffected by the new field

Also pins `perform_tip_multi` / POST /api/tips/multi, added for a power
rankings edition's many credited authors (voters + blurb writers) —
"tip any or all of them":

  * each selected recipient gets the full amount, not a split of it
  * the sender is charged amount x recipient count, atomically — an
    unaffordable batch is rejected whole, never partially sent
  * duplicate recipients are deduped rather than double-charging the sender
  * the sender can't be one of the recipients, and an unknown recipient
    (anywhere in the list) rejects the whole request

Writes go to a temp directory; nothing here touches tips.json in NBS_DATA_DIR.

    venv/bin/python -m tests.test_tips
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.bets as bets  # noqa: E402
import routers.tips as tips  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


ALICE_TOKEN = "a" * 64
BOB_TOKEN   = "b" * 64
MEMBERS = {
    "Alice": {"token": ALICE_TOKEN, "roles": [], "tenures": []},
    "Bob":   {"token": BOB_TOKEN,   "roles": [], "tenures": []},
    "Carol": {"token": "c" * 64,    "roles": [], "tenures": []},
}
auth.load_members = lambda: MEMBERS
tips.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-tips-test-"))
tips.TIPS_FILE = TMP / "tips.json"
bets.BALANCES_FILE = TMP / "member-balances.json"
bets.LEDGER_FILE = TMP / "bets-ledger.json"
tips.log_write = lambda info, msg: None

app = FastAPI()
app.include_router(tips.router)
c = TestClient(app)

ALICE = {"Authorization": "Bearer " + ALICE_TOKEN}
BOB   = {"Authorization": "Bearer " + BOB_TOKEN}

# Give Alice a starting balance to tip from.
bal = bets._load_balances()
bal["Alice"] = 10_000.0
bets._save_balances(bal)

# ── plain tip, no context (existing behavior unchanged) ─────────────────────

print("plain tip")
r = c.post("/api/tips", json={"to": "Bob", "amount": 50}, headers=ALICE)
check("plain tip succeeds", r.status_code == 200)
check("no context key stored", "context" not in tips._load_json(tips.TIPS_FILE, [])[-1])

member_tips = c.get("/api/tips/Bob").json()
check("shows up on the recipient's history", len(member_tips) == 1 and member_tips[0]["from"] == "Alice")

# ── contexted tips (article page) ───────────────────────────────────────────

print("contexted tips")
r = c.post("/api/tips", json={"to": "Bob", "amount": 200, "context": "article:aaa"}, headers=ALICE)
check("contexted tip succeeds", r.status_code == 200)
c.post("/api/tips", json={"to": "Bob", "amount": 300, "context": "article:aaa", "message": "great piece"}, headers=ALICE)
c.post("/api/tips", json={"to": "Bob", "amount": 400, "context": "article:zzz"}, headers=ALICE)

ctx_a = c.get("/api/tips/context/article:aaa").json()
check("totals only this context's tips", ctx_a["total"] == 500)
check("lists only this context's tips", len(ctx_a["tips"]) == 2)
check("message included over the threshold", any(t.get("message") == "great piece" for t in ctx_a["tips"]))

ctx_z = c.get("/api/tips/context/article:zzz").json()
check("a different context is independent", ctx_z["total"] == 400 and len(ctx_z["tips"]) == 1)

ctx_none = c.get("/api/tips/context/article:nope").json()
check("an unused context is empty, not an error", ctx_none == {"total": 0, "tips": []})

check("member history includes contexted tips too", len(c.get("/api/tips/Bob").json()) == 4)
check("member totals sum across context and non-context tips",
      c.get("/api/tips/totals").json().get("Bob") == 950)

# ── rejections unaffected by the new field ──────────────────────────────────

# ── multi-recipient tips (power rankings: many credited authors) ───────────

print("multi-recipient tips")
bal = bets._load_balances()
bal["Alice"] = 10_000.0
bets._save_balances(bal)

r = c.post("/api/tips/multi", json={"to": ["Bob", "Carol"], "amount": 100, "context": "article:multi"}, headers=ALICE)
check("multi tip succeeds", r.status_code == 200)
check("returns one tip per recipient", len(r.json()["tips"]) == 2)

ctx_multi = c.get("/api/tips/context/article:multi").json()
check("both recipients recorded under the same context", ctx_multi["total"] == 200 and len(ctx_multi["tips"]) == 2)
check("each recipient got the full amount, not a split",
      all(t["amount"] == 100 for t in ctx_multi["tips"]))

alice_bal_after = bets._load_balances()["Alice"]
check("sender pays amount x recipient count", alice_bal_after == 10_000.0 - 200)

check("insufficient balance rejects the whole batch, not a partial send",
      c.post("/api/tips/multi", json={"to": ["Bob", "Carol"], "amount": 999_999}, headers=ALICE).status_code == 422)
bob_before = bets._load_balances()["Bob"]
c.post("/api/tips/multi", json={"to": ["Bob", "Bob"], "amount": 50}, headers=ALICE)
bob_after = bets._load_balances()["Bob"]
check("duplicate recipients are deduped rather than double-charged", bob_after - bob_before == 50)
check("self among recipients is rejected",
      c.post("/api/tips/multi", json={"to": ["Bob", "Alice"], "amount": 10}, headers=ALICE).status_code == 422)
check("an unknown recipient is rejected",
      c.post("/api/tips/multi", json={"to": ["Bob", "Nobody"], "amount": 10}, headers=ALICE).status_code == 404)
check("empty recipient list is rejected",
      c.post("/api/tips/multi", json={"to": [], "amount": 10}, headers=ALICE).status_code == 422)

print("rejections")
check("self-tip still rejected", c.post("/api/tips", json={"to": "Alice", "amount": 10, "context": "article:aaa"},
      headers=ALICE).status_code == 422)
check("unknown recipient still rejected", c.post("/api/tips", json={"to": "Nobody", "amount": 10},
      headers=ALICE).status_code == 404)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

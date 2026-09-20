"""Regression tests for the Trade Request Committee (TRC) pipeline —
routers.trade_requests. Spec: nbn-today/docs/trc-trade-pipeline.md.

What's worth pinning here is the set of decisions, not the CRUD around them:

  * **Proposing needs a party-team role**, consenting needs actual owner
    tenure for that team — a front-office member of the team can draft a
    proposal, but only the owner can lock the team into it.
  * **Every party consents through the same endpoint**, including the
    proposer's own team — no auto-consent for whoever clicked submit.
  * **Exactly 3 approvals** (a fixed count, not a majority of the committee's
    actual size) moves a request to `ready_to_finalize`. A reject ballot
    never auto-rejects — only `trc_head`'s own `/reject` does. Re-voting
    overwrites, it doesn't double-count.
  * **A ballot is a judgment call, not an administrative one** — `admin`
    without the `trc` role cannot cast one, even though `admin` does satisfy
    `require_role("trc_head")` for reject/finalize (same as every other
    head-power in this codebase).
  * **Finalize re-checks legality and current ownership live** — a leg that's
    moved since the request was created blocks finalize, which is the
    regression test for the gap this feature's design surfaced: legality
    checks alone never verified a team still holds what it's proposing to
    trade.
  * **A finalized/rejected/withdrawn request is terminal** — no further
    action, and a second finalize 409s rather than double-applying a trade.

Everything is patched into memory — the endpoint functions are called
directly, so nothing touches trade-requests.json in NBS_DATA_DIR and no real
validator or `apply_trade` runs.

    venv/bin/python -m tests.test_trade_requests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.trade_requests as tr  # noqa: E402
from routers.transactions import TradeTransfer, TradeAsset  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def raises(name, status, fn):
    try:
        fn()
    except HTTPException as e:
        check(f"{name} → {status}", e.status_code == status)
        return
    check(f"{name} → {status}", False)


# ── in-memory world ───────────────────────────────────────────────────────────

STORE = {"seq": 0, "items": []}
tr._load_store = lambda: STORE
tr._save_store = lambda s: None
tr.log_write = lambda info, msg: None

# is_team_owner is the one auth helper that touches disk (members.json) for
# real, so it's the one stubbed — has_role/require_role are pure over the
# passed-in info dict and safe to leave real.
OWNERS = {"phxOwner": "PHX", "bosOwner": "BOS", "gswOwner": "GSW"}
tr.is_team_owner = lambda info, team: OWNERS.get(info["name"]) == team.upper()

tr._require_trade_validatable = lambda body, ctx: None
tr._validation_ctx = lambda: {"bios": {}}
tr._build_team_map = lambda: {}
tr.load_picks = lambda: []
tr._load_conveyance_store_for_shadow_check = lambda: None

LEGAL = {"value": True}


class FakeCheck:
    def __init__(self, passed, level="error"):
        self.passed = passed
        self.level = level

    def model_dump(self):
        return {"check": "test", "passed": self.passed, "level": self.level, "message": ""}


tr._validate_trade = lambda trade, ctx: [] if LEGAL["value"] else [FakeCheck(False)]

OWNERSHIP_PROBLEMS = {"value": []}
tr._trade_leg_ownership_problems = lambda *a, **k: OWNERSHIP_PROBLEMS["value"]

APPLIED = []


def fake_apply_trade(details, date, info, description="", force=False, relay_to_roster_log=False):
    txn = {"id": f"txn{len(APPLIED) + 1}", "type": "trade", "details": details.model_dump()}
    APPLIED.append(txn)
    return txn


tr.apply_trade = fake_apply_trade


def gm(name, *roles):
    return {"name": name, "roles": list(roles)}


PHX_GM = gm("phxGM", "phx")
PHX_OWNER = gm("phxOwner", "phx")
BOS_OWNER = gm("bosOwner", "bos")
GSW_OWNER = gm("gswOwner", "gsw")
OUTSIDER = gm("outsider", "bkn")
TRC_A = gm("trcA", "trc")
TRC_B = gm("trcB", "trc")
TRC_C = gm("trcC", "trc")
TRC_HEAD = gm("trcHead", "trc_head")
ADMIN = gm("admin", "admin")


def transfer(from_team, to_team, slug):
    return TradeTransfer(from_team=from_team, to_team=to_team,
                         assets=[TradeAsset(type="player", slug=slug)])


def body_2team():
    return tr.TradeValidateInput(transfers=[
        transfer("PHX", "BOS", "player-a"),
        transfer("BOS", "PHX", "player-b"),
    ])


def body_3team():
    return tr.TradeValidateInput(transfers=[
        transfer("PHX", "BOS", "player-a"),
        transfer("BOS", "GSW", "player-b"),
        transfer("GSW", "PHX", "player-c"),
    ])


# ══ creation ══════════════════════════════════════════════════════════════════

print("\ncreation")
raises("no party-team role can't propose", 403, lambda: tr.create_trade_request(body_2team(), OUTSIDER))
req = tr.create_trade_request(body_2team(), PHX_GM)
check("starts awaiting_consent", req["status"] == "awaiting_consent")
check("parties derived from transfers", req["parties"] == ["BOS", "PHX"])
check("number assigned", req["number"] == 1)
check("neither team pre-consented", not any(c["consented"] for c in req["consents"].values()))

req3 = tr.create_trade_request(body_3team(), GSW_OWNER)
check("3-team request has all 3 parties", req3["parties"] == ["BOS", "GSW", "PHX"])

# ══ consent ════════════════════════════════════════════════════════════════════

print("\nconsent (owner tenure, not just the team role)")
rid = req["id"]
raises("a team-role holder who isn't the owner can't consent", 403,
       lambda: tr.consent_trade_request(rid, gm("phxFrontOffice", "phx")))
r = tr.consent_trade_request(rid, PHX_OWNER)
check("PHX consented", r["consents"]["PHX"]["consented"] is True)
check("still awaiting BOS", r["status"] == "awaiting_consent")
r = tr.consent_trade_request(rid, BOS_OWNER)
check("flips to balloting once every party has consented", r["status"] == "balloting")

# ══ ballots ═════════════════════════════════════════════════════════════════════

print("\nballots (3 fixed approvals, not a majority)")
raises("admin without trc can't ballot — a vote isn't administrative", 403,
       lambda: tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="fine"), ADMIN))
raises("empty note rejected — ballots judge fairness, need a reason", 422,
       lambda: tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="  "), TRC_A))
r = tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="Looks fair"), TRC_A)
check("1st approval doesn't ready it", r["status"] == "balloting")
r = tr.ballot_trade_request(rid, tr.BallotBody(decision="reject", note="Lopsided"), TRC_B)
check("a reject doesn't move the approve count", r["status"] == "balloting" and tr._approve_count(r) == 1)
r = tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="Changed my mind"), TRC_B)
check("re-voting overwrites, doesn't double-count", tr._approve_count(r) == 2)
r = tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="Fine by me"), TRC_C)
check("3rd distinct approver readies it", r["status"] == "ready_to_finalize")
r = tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="Also fine"), gm("trcD", "trc"))
check("a 4th vote doesn't change ready status", r["status"] == "ready_to_finalize")

# ══ the trc_head role gate itself ═══════════════════════════════════════════════

print("\nrole gate for reject/finalize (Depends(require_role(\"trc_head\")))")
raises("a trc-only member can't satisfy trc_head's gate", 403,
       lambda: tr.require_role("trc_head")(TRC_A))
check("trc_head satisfies its own gate", tr.require_role("trc_head")(TRC_HEAD) == TRC_HEAD)
check("admin satisfies trc_head's gate too — same as every other head-power", tr.require_role("trc_head")(ADMIN) == ADMIN)

# ══ finalize / reject ════════════════════════════════════════════════════════════

print("\nfinalize and reject")
raises("can't finalize before it's ready", 422, lambda: tr.finalize_trade_request(req3["id"], TRC_HEAD))

stale = tr.create_trade_request(body_2team(), PHX_GM)
tr.consent_trade_request(stale["id"], PHX_OWNER)
tr.consent_trade_request(stale["id"], BOS_OWNER)
for who in (TRC_A, TRC_B, TRC_C):
    tr.ballot_trade_request(stale["id"], tr.BallotBody(decision="approve", note="ok"), who)
OWNERSHIP_PROBLEMS["value"] = ["Player 'player-a' is on GSW, not PHX"]
raises("a leg that's moved since creation blocks finalize", 422,
       lambda: tr.finalize_trade_request(stale["id"], TRC_HEAD))
check("a blocked finalize doesn't apply anything", len(APPLIED) == 0)
OWNERSHIP_PROBLEMS["value"] = []

final = tr.finalize_trade_request(rid, TRC_HEAD)
check("finalize applies the trade for real", len(APPLIED) == 1)
check("status is finalized", final["status"] == "finalized")
check("txn_id recorded", final["finalized"]["txn_id"] == APPLIED[0]["id"])
raises("a second finalize 409s instead of double-applying", 409, lambda: tr.finalize_trade_request(rid, TRC_HEAD))

r = tr.reject_trade_request(req3["id"], tr.RejectBody(reason="Too lopsided"), TRC_HEAD)
check("trc_head can reject outright, no ballots needed", r["status"] == "rejected")
raises("can't act on an already-rejected request", 409,
       lambda: tr.consent_trade_request(req3["id"], GSW_OWNER))

# ══ withdraw ═══════════════════════════════════════════════════════════════════

print("\nwithdraw")
w = tr.create_trade_request(body_2team(), PHX_GM)
raises("an uninvolved member can't withdraw", 403,
       lambda: tr.withdraw_trade_request(w["id"], tr.WithdrawBody(), OUTSIDER))
r = tr.withdraw_trade_request(w["id"], tr.WithdrawBody(reason="changed our minds"), PHX_OWNER)
check("a party owner can withdraw", r["status"] == "withdrawn")
raises("can't withdraw an already-terminal request", 409,
       lambda: tr.withdraw_trade_request(w["id"], tr.WithdrawBody(), PHX_OWNER))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

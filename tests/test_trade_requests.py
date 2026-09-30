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


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra and not cond else ''}")
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
tr._trade_fact_sheet = lambda trade, ctx: {"season": "26-27", "teams": {"PHX": {"incoming_salary": 1}}}

NOTIFICATIONS = []
tr.inbox.notify_team = lambda team, text, link=None: NOTIFICATIONS.append(("team", team, text))
tr.inbox.notify_role = lambda role, text, link=None: NOTIFICATIONS.append(("role", role, text))

# trc-alerts: the real embed builders run, only the network send is captured.
ALERTS = []
TRADE_POSTS = []


def _capture(msg):
    if msg["channel"] == "trades-chan":
        TRADE_POSTS.append(msg["payload"]["content"])
    else:
        ALERTS.append((msg["channel"], msg["payload"]["embeds"][0]))


tr.trc_notify.transport._enqueue = _capture
tr.trc_notify.DISCORD_TRADES_CHANNEL = "trades-chan"
tr.trc_notify.load_player_bios = lambda: {"player-a": {"name": "ALPHA, ANDY"}, "player-b": {"name": "BRAVO, BEN"}}
LEAGUE_YEAR = {"value": "26-27"}
tr.season_clock.current_season = lambda: LEAGUE_YEAR["value"]
tr.trc_notify.transport.DISCORD_BOT_TOKEN = "test-token"
tr.trc_notify.DISCORD_TRC_CHANNEL = "trc-chan"


def alerts_during(fn):
    """fn's result and the trc-alerts titles posted while it ran. Titles name a
    trade by its teams, not a number, so several BOS ⇄ PHX requests are told
    apart by when their alerts were posted."""
    before = len(ALERTS)
    result = fn()
    return result, [e["title"] for _, e in ALERTS[before:]]

LEGAL = {"value": True}


class FakeCheck:
    def __init__(self, passed, level="error", check="test", message=""):
        self.passed = passed
        self.level = level
        self.check = check
        self.message = message

    def model_dump(self):
        return {"check": self.check, "passed": self.passed, "level": self.level, "message": self.message}


tr._validate_trade = lambda trade, ctx: [] if LEGAL["value"] else [FakeCheck(False)]

OWNERSHIP_PROBLEMS = {"value": []}
tr._trade_leg_ownership_problems = lambda *a, **k: OWNERSHIP_PROBLEMS["value"]

APPLIED = []


def fake_apply_trade(details, date, info, description="", force=False, relay_to_roster_log=False):
    txn = {"id": f"txn{len(APPLIED) + 1}", "type": "trade", "details": details.model_dump(),
           "relay_to_roster_log": relay_to_roster_log, "description": description}
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
notified_teams = [n[1] for n in NOTIFICATIONS if n[0] == "team"]
check("every party is notified, the proposer's own team included",
      "BOS" in notified_teams and "PHX" in notified_teams)
phx_text = next(n[2] for n in NOTIFICATIONS if n[:2] == ("team", "PHX"))
check("the proposer's team is told its own consent is still needed",
      "still needs your team's consent" in phx_text)

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
before_notify = len(NOTIFICATIONS)
r, vote_alerts = alerts_during(lambda: tr.consent_trade_request(rid, BOS_OWNER))
check("flips to balloting once every party has consented", r["status"] == "balloting")
new_notifications = NOTIFICATIONS[before_notify:]
check("trc and trc_head are both notified once it's ready for a vote",
      ("role", "trc") in [(n[0], n[1]) for n in new_notifications]
      and ("role", "trc_head") in [(n[0], n[1]) for n in new_notifications])

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
r, finalize_alerts = alerts_during(
    lambda: tr.ballot_trade_request(rid, tr.BallotBody(decision="approve", note="Fine by me"), TRC_C))
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
raises("can't finalize before it's ready", 422,
       lambda: tr.finalize_trade_request(req3["id"], tr.FinalizeBody(), TRC_HEAD))

stale = tr.create_trade_request(body_2team(), PHX_GM)
tr.consent_trade_request(stale["id"], PHX_OWNER)
tr.consent_trade_request(stale["id"], BOS_OWNER)
for who in (TRC_A, TRC_B, TRC_C):
    tr.ballot_trade_request(stale["id"], tr.BallotBody(decision="approve", note="ok"), who)
OWNERSHIP_PROBLEMS["value"] = ["Player 'player-a' is on GSW, not PHX"]
raises("a leg that's moved since creation blocks finalize", 422,
       lambda: tr.finalize_trade_request(stale["id"], tr.FinalizeBody(), TRC_HEAD))
check("a blocked finalize doesn't apply anything", len(APPLIED) == 0)
raises("ownership problems block finalize even with force=True — never overrideable", 422,
       lambda: tr.finalize_trade_request(
           stale["id"], tr.FinalizeBody(force=True, override_reason="ship it anyway"), TRC_HEAD))
check("the force attempt still didn't apply anything", len(APPLIED) == 0)
OWNERSHIP_PROBLEMS["value"] = []

# ── force override: illegal-but-not-stale can be pushed through deliberately ──
LEGAL["value"] = False
illegal = tr.create_trade_request(body_2team(), PHX_GM)
tr.consent_trade_request(illegal["id"], PHX_OWNER)
tr.consent_trade_request(illegal["id"], BOS_OWNER)
for who in (TRC_A, TRC_B, TRC_C):
    tr.ballot_trade_request(illegal["id"], tr.BallotBody(decision="approve", note="ok"), who)
raises("an illegal trade still 422s by default (no force)", 422,
       lambda: tr.finalize_trade_request(illegal["id"], tr.FinalizeBody(), TRC_HEAD))
raises("force=True without a reason 400s — a bare override isn't enough", 400,
       lambda: tr.finalize_trade_request(illegal["id"], tr.FinalizeBody(force=True), TRC_HEAD))
check("still nothing applied", len(APPLIED) == 0)
forced = tr.finalize_trade_request(
    illegal["id"], tr.FinalizeBody(force=True, override_reason="league approved it anyway"), TRC_HEAD)
check("force=True + a reason finalizes an illegal trade", forced["status"] == "finalized")
check("the override reason is recorded in history",
      forced["history"][-1]["override_reason"] == "league approved it anyway")
LEGAL["value"] = True

final = tr.finalize_trade_request(rid, tr.FinalizeBody(), TRC_HEAD)
check("finalize applies the trade for real", len(APPLIED) == 2)
check("status is finalized", final["status"] == "finalized")
check("txn_id recorded", final["finalized"]["txn_id"] == APPLIED[-1]["id"])
check("the #roster-log-nbn-today post isn't relayed (the #transactions post is)",
      APPLIED[-1]["relay_to_roster_log"] is False)
check("numbered at finalization: request #1, finalized second, is trade 53 (26-27 starts after 51)",
      forced["finalized"]["trade_number"] == 52 and final["finalized"]["trade_number"] == 53)
check("the ledger entry carries the same league number as #transactions",
      APPLIED[-1]["description"] == "Trade 53", APPLIED[-1]["description"])
check("the league year is recorded with the number", final["finalized"]["league_year"] == "26-27")
check("#transactions gets the league's format",
      TRADE_POSTS[-1] == "Trade 53:\nBOS receives: Andy Alpha\n\nPHX receives: Ben Bravo", TRADE_POSTS[-1:])
raises("a second finalize 409s instead of double-applying", 409,
       lambda: tr.finalize_trade_request(rid, tr.FinalizeBody(), TRC_HEAD))

r, reject_alerts = alerts_during(
    lambda: tr.reject_trade_request(req3["id"], tr.RejectBody(reason="Too lopsided"), TRC_HEAD))
check("trc_head can reject outright, no ballots needed", r["status"] == "rejected")
raises("can't act on an already-rejected request", 409,
       lambda: tr.consent_trade_request(req3["id"], GSW_OWNER))

# ══ withdraw ═══════════════════════════════════════════════════════════════════

print("\nwithdraw")
w, withdraw_alerts = alerts_during(lambda: tr.create_trade_request(body_2team(), PHX_GM))
raises("an uninvolved member can't withdraw", 403,
       lambda: tr.withdraw_trade_request(w["id"], tr.WithdrawBody(), OUTSIDER))
r, more = alerts_during(
    lambda: tr.withdraw_trade_request(w["id"], tr.WithdrawBody(reason="changed our minds"), PHX_OWNER))
withdraw_alerts += more
check("a party owner can withdraw", r["status"] == "withdrawn")
raises("can't withdraw an already-terminal request", 409,
       lambda: tr.withdraw_trade_request(w["id"], tr.WithdrawBody(), PHX_OWNER))

print()
# ══ trc-alerts ══════════════════════════════════════════════════════════════════

print("\ntrc-alerts (the committee's Discord channel)")
check("every alert goes to the trc channel", ALERTS and all(c == "trc-chan" for c, _ in ALERTS))
t1 = vote_alerts + finalize_alerts
check("one request: only ready for a vote and ready to finalize — no proposal, votes or ending",
      [t.split(" — ", 1)[1] for t in t1] == ["ready for a vote", "ready to finalize"], t1)
check("titles name the trade by its teams, with no request number",
      t1[0] == "BOS ⇄ PHX trade — ready for a vote" and "#" not in t1[0], t1[0])
agreed = next(e for _, e in ALERTS if e["title"] == t1[0])
check("the ready-for-a-vote post lists each leg", "PHX → BOS" in agreed["fields"][0]["value"]
      or "BOS → PHX" in agreed["fields"][0]["value"], agreed["fields"][0]["value"])
check("rejected and withdrawn requests post nothing",
      not reject_alerts and not withdraw_alerts, (reject_alerts, withdraw_alerts))
check("no inbox notice shows the request number",
      not any("#" in n[2] for n in NOTIFICATIONS), [n[2] for n in NOTIFICATIONS if "#" in n[2]])
print("\na trade that carries a release")
rel_body = body_2team()
rel_body.releases = {"PHX": ["player-b"]}
rel = tr.create_trade_request(rel_body, PHX_GM)
check("the release is stored on the request", rel["trade"]["releases"] == {"PHX": ["player-b"]})
tr.consent_trade_request(rel["id"], PHX_OWNER)
tr.consent_trade_request(rel["id"], BOS_OWNER)
ready = [e for c, e in ALERTS if e["title"] == "BOS ⇄ PHX trade — ready for a vote"][-1:]
check("the ready-for-a-vote post lists the release", ready and "**PHX releases**: Ben Bravo" in ready[0]["fields"][0]["value"],
      ready and ready[0]["fields"][0]["value"])
for who in (TRC_A, TRC_B, TRC_C):
    tr.ballot_trade_request(rel["id"], tr.BallotBody(decision="approve", note="ok"), who)
BAD_RELEASE = FakeCheck(False, check="trade_release_player-b", message="Ben Bravo has no real contract years left to release")
tr._validate_trade = lambda trade, ctx: [BAD_RELEASE]
raises("a release that can't happen blocks finalize", 422,
       lambda: tr.finalize_trade_request(rel["id"], tr.FinalizeBody(), TRC_HEAD))
raises("even forced", 422, lambda: tr.finalize_trade_request(
    rel["id"], tr.FinalizeBody(force=True, override_reason="push it"), TRC_HEAD))
stored = next(i for i in STORE["items"] if i["id"] == rel["id"])
check("the page gets the fact sheet, so it can show each team's money",
      tr._public_view(stored)["validation"]["fact_sheet"]["teams"]["PHX"]["incoming_salary"] == 1)
check("the page is told why", tr._public_view(stored)["validation"]["release_problems"]
      == ["Ben Bravo has no real contract years left to release"])
tr._validate_trade = lambda trade, ctx: [] if LEGAL["value"] else [FakeCheck(False)]
rel = tr.finalize_trade_request(rel["id"], tr.FinalizeBody(), TRC_HEAD)
check("#transactions gets the release line",
      TRADE_POSTS[-1] == f"Trade {rel['finalized']['trade_number']}:\nBOS receives: Andy Alpha\n\n"
                         "PHX receives: Ben Bravo\n\nPHX releases: Ben Bravo", TRADE_POSTS[-1:])

print("\ntrade numbers reset each league year")
LEAGUE_YEAR["value"] = "27-28"
nxt = tr.create_trade_request(body_2team(), PHX_GM)
tr.consent_trade_request(nxt["id"], PHX_OWNER)
tr.consent_trade_request(nxt["id"], BOS_OWNER)
for who in (TRC_A, TRC_B, TRC_C):
    tr.ballot_trade_request(nxt["id"], tr.BallotBody(decision="approve", note="ok"), who)
nxt = tr.finalize_trade_request(nxt["id"], tr.FinalizeBody(), TRC_HEAD)
check("a new league year starts at 1", nxt["finalized"]["trade_number"] == 1 and TRADE_POSTS[-1].startswith("Trade 1:\n"))
check("a pick reads the league's way", tr.trc_notify._trade_asset(
    {"type": "pick", "year": 2027, "round": 2, "orig": "ATL"}, {}) == "ATL 2027 2nd")

n = len(ALERTS)
tr.trc_notify.DISCORD_TRC_CHANNEL = ""
quiet = tr.create_trade_request(body_2team(), PHX_GM)
tr.consent_trade_request(quiet["id"], PHX_OWNER)
tr.consent_trade_request(quiet["id"], BOS_OWNER)
check("with the channel unset nothing is sent", len(ALERTS) == n)

if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

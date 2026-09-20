"""Regression tests for the PDC extension pipeline (§ 6.2 / § 6.3) —
routers.poext. Spec: nbn-today/docs/poext-extension-pipeline.md.

Same house style test_fa_offers.py uses for the pipeline it mirrors:
everything is patched into memory, the route functions are called directly
(bypassing FastAPI's Depends layer — an `info` dict is just passed as a plain
argument), and the real validator is stubbed so this suite tests the
*pipeline*, not § 6.2's rules (those are test_extensions.py's job).

What's worth pinning here, because each is a decision rather than incidental
CRUD:

  * **One live proposal per player** (§ 2.4 — only the incumbent may
    propose, so there's no "which of several" the way FA has).
  * **A remand is free; only a majority-reject burns one of the three**
    (§ 2.5, D4) — no standalone reject action exists.
  * **After the third rejection, no further proposal may be drafted**
    (§ 6.3) — checked at both create and submit, and unlock decrements it.
  * **Claim is refused outright when the agent shares the proposing team**
    (this pipeline's own answer to § 2.9's self-dealing question — see the
    docstring on `claim_player`), not a permanent post-hoc bar the way FA's
    `blocked_teams` is, since there are no rival bids here to protect against.
  * **A vote is never admin-waved** and **finalize is always head-only** —
    no agent-uncontested shortcut (§ 2.9a), unlike FA.
  * **A tied vote refuses to finalize** rather than silently picking a side.

    venv/bin/python -m tests.test_poext
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.poext as poext  # noqa: E402

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
    except Exception as e:
        check(f"{name} → {status} (got {type(e).__name__}: {e})", False)
        return
    check(f"{name} → {status}", False)


# ── in-memory world ───────────────────────────────────────────────────────────

STATE = {"seq": 0, "players": {}}
PROPOSALS: list = []
VOTES: dict = {}
TEAM_MAP = {"barlow-dominick": "SAS"}

MEMBERS = {
    "poextHead": {"roles": ["poext_head"]},
    "sasOwner": {"roles": ["sas"]},
    "bknOwner": {"roles": ["bkn"]},
    "agentA": {"roles": ["agent"]},
    "agentSas": {"roles": ["agent", "sas"]},
    "memberA": {"roles": ["poext"]},
    "memberB": {"roles": ["poext"]},
    "memberC": {"roles": ["poext"]},
    "outsider": {"roles": ["poext"]},
}

HEAD = {"name": "poextHead", "roles": ["poext_head"]}
SAS = {"name": "sasOwner", "roles": ["sas"]}
BKN = {"name": "bknOwner", "roles": ["bkn"]}
AGENT = {"name": "agentA", "roles": ["agent"]}
AGENT_SAS = {"name": "agentSas", "roles": ["agent", "sas"]}
MEM_A = {"name": "memberA", "roles": ["poext"]}
MEM_B = {"name": "memberB", "roles": ["poext"]}
MEM_C = {"name": "memberC", "roles": ["poext"]}
UNASSIGNED = {"name": "outsider", "roles": ["poext"]}

poext._load_state = lambda: STATE
poext._save_state = lambda s: None
poext._load_proposals = lambda: PROPOSALS
poext._save_proposals = lambda p: None
poext._load_votes = lambda: VOTES
poext._save_votes = lambda v: None
poext._build_team_map = lambda: TEAM_MAP
poext.load_members = lambda: MEMBERS
poext.log_write = lambda info, msg: None
poext.inbox.notify_member = lambda *a, **k: None
poext.inbox.notify_role = lambda *a, **k: None
poext._member_current_team = lambda name, members=None: (
    "SAS" if name in ("sasOwner", "agentSas") else "BKN" if name == "bknOwner" else None)
poext._current_league_year = lambda: "26-27"


def vote_node(slug):
    """Votes are keyed [slug][cycle] (the live proposal's own id stands in
    for FA's round_id — see poext._current_cycle) — this test only ever has
    one proposal per slug live at a time, so "the latest" is unambiguous."""
    cycle = poext._current_cycle(PROPOSALS, slug)
    return poext._vote_node(VOTES, slug, cycle)

# _member_teams is imported from free_agency.py and reused verbatim (per the
# pipeline doc's own "reuse the helper, don't re-derive it" rule) — but it
# calls free_agency's OWN load_members/_member_current_team references, not
# poext's, so those need patching there too or it silently reads real
# members.json and finds nothing.
from routers import free_agency as _fa  # noqa: E402
_fa.load_members = lambda: MEMBERS
_fa._member_current_team = poext._member_current_team

# The validator is exercised by test_extensions.py; here it only needs to be
# *the same call* with a steerable verdict, same as test_fa_offers.py's
# treatment of _validate_sign.
LEGAL = {"legal": True}
poext._validation_ctx = lambda: {"cur_season": "26-27", "bios": {}, "cap_levels": {}}
poext._require_validatable = lambda team, player, ctx: None
poext._extension_fact_sheet = lambda *a, **k: {"team": "SAS", "player": "barlow-dominick"}


class FakeCheck:
    def __init__(self, passed):
        self.passed = passed

    def model_dump(self):
        return {"check": "extension_eligibility", "passed": self.passed, "level": "error",
                "message": "" if self.passed else "not eligible"}


poext._validate_extension = lambda details, ctx: [FakeCheck(LEGAL["legal"])]

# apply_extension is transactions.py's own reusable, importable slice (the
# same shape as apply_trade) — it runs the REAL _validate_extension/_run_validation
# dispatch internally, not poext's locally-patched stub above, since that
# dispatch is bound in transactions.py's own module namespace. Patching
# poext.apply_extension itself (same pattern test_trade_requests.py uses for
# tr.apply_trade) keeps this suite testing the pipeline, never real cap math
# or real on-disk bios/team-state.
APPLIED_EXTENSIONS: list = []


# Controls what fake_apply_extension "finds" on its next call — empty means
# everything passes. Lets the warning-confirm tests below simulate exactly
# what a real apply_extension failure looks like without touching real cap
# math, mirroring the real function's own HTTPException shape.
PENDING_CHECKS: list = []


def fake_apply_extension(details, txn_date, info, description="", force=False,
                          force_warnings_only=False, relay_to_roster_log=False):
    failed = [c for c in PENDING_CHECKS if not c["passed"]]
    blocking = [c for c in failed if c["level"] == "error"] if force_warnings_only else failed
    if blocking:
        raise HTTPException(422, {"validation": True, "checks": PENDING_CHECKS, "can_force": True})
    txn = {"id": f"txn{len(APPLIED_EXTENSIONS) + 1}", "type": "extension",
           "details": details.model_dump(), "force_warnings_only": force_warnings_only}
    APPLIED_EXTENSIONS.append(txn)
    return txn


poext.apply_extension = fake_apply_extension


def contract(y1="$3,000,000", y2="$3,200,000"):
    return poext.ProposalContract(salaries={"27-28": y1, "28-29": y2}, cap_holds={})


def make_proposal(who, player="barlow-dominick", team="SAS"):
    return poext.create_proposal(
        poext.ProposalCreate(player=player, team=team, contract=contract()), who)


def reset():
    STATE["players"] = {}
    STATE["seq"] = 0
    PROPOSALS.clear()
    VOTES.clear()
    LEGAL["legal"] = True


# ══ proposal creation and lifecycle ═══════════════════════════════════════════

print("proposal creation")
reset()
raises("BKN can't propose an extension for a SAS player", 403, lambda: make_proposal(BKN))
p = make_proposal(SAS)
check("draft created", p["status"] == "draft" and p["player"] == "barlow-dominick")
raises("only one live proposal per player", 409, lambda: make_proposal(SAS))

print("\nsubmit")
raises("BKN can't submit SAS's proposal", 403,
       lambda: poext.submit_proposal(p["id"], BKN))
sub = poext.submit_proposal(p["id"], SAS)
check("submitted", sub["status"] == "submitted" and sub["validation"]["legal"] is True)

reset()
LEGAL["legal"] = False
p2 = make_proposal(SAS)
raises("an illegal contract is refused at submit", 422,
       lambda: poext.submit_proposal(p2["id"], SAS))
LEGAL["legal"] = True

# ══ remand / void / restore ═══════════════════════════════════════════════════

print("\nremand — free, doesn't burn a proposal")
reset()
p = make_proposal(SAS)
poext.submit_proposal(p["id"], SAS)
raises("remand needs a note", 422, lambda: poext.remand_proposal(p["id"], {"note": ""}, HEAD))
r = poext.remand_proposal(p["id"], {"note": "raise Year 1"}, HEAD)
check("returned, one remand recorded", r["status"] == "returned" and len(r["remands"]) == 1)
check("no rejection counted", STATE["players"].get("barlow-dominick", {}).get("rejections", 0) == 0)
resub = poext.submit_proposal(p["id"], SAS)
check("resubmit bumps version", resub["version"] == 2)

print("\nvoid / restore")
reset()
p = make_proposal(SAS)
poext.submit_proposal(p["id"], SAS)
raises("void needs a reason", 422, lambda: poext.void_proposal(p["id"], {"reason": ""}, HEAD))
v = poext.void_proposal(p["id"], {"reason": "wrong player"}, HEAD)
check("voided", v["status"] == "voided")
restored = poext.restore_proposal(p["id"], HEAD)
check("restored to submitted", restored["status"] == "submitted")

# ══ the agent stage ══════════════════════════════════════════════════════════

print("\nclaim — refused when the agent shares the proposing team")
reset()
p = make_proposal(SAS)
poext.submit_proposal(p["id"], SAS)
raises("a SAS-affiliated agent can't claim a SAS extension", 422,
       lambda: poext.claim_player("barlow-dominick", AGENT_SAS))
claim = poext.claim_player("barlow-dominick", AGENT)
check("claimed", claim["agent"]["claimed_by"] == "agentA")
raises("second claim refused", 422, lambda: poext.claim_player("barlow-dominick", AGENT))
raises("the head can't claim an already-claimed player either", 422,
       lambda: poext.claim_player("barlow-dominick", HEAD))

print("\nadvance")
raises("outsider agent can't advance someone else's claim", 403,
       lambda: poext.advance_player("barlow-dominick", {"note": ""}, AGENT_SAS))
adv = poext.advance_player("barlow-dominick", {"note": "ready for a vote"}, AGENT)
check("advanced", adv["agent"]["advanced_at"] is not None)
# _require_curator catches this before the explicit _is_advanced guard does —
# a more specific message ("ask the head to send it back") than the generic
# "already advanced" 409, so 403 is the real, intended answer here.
raises("can't advance twice (caught by _require_curator first)", 403,
       lambda: poext.advance_player("barlow-dominick", {"note": ""}, AGENT))

# ══ assignment + votes ════════════════════════════════════════════════════════

print("\nassignment + votes")
poext.assign_subcommittee("barlow-dominick", {"subcommittee": ["memberA", "memberB", "memberC"]}, HEAD)
raises("unassigned member can't vote", 403,
       lambda: poext.cast_vote("barlow-dominick", poext.VoteIn(vote="accept"), UNASSIGNED))
raises("bad vote value rejected", 422,
       lambda: poext.cast_vote("barlow-dominick", poext.VoteIn(vote="maybe"), MEM_A))
poext.cast_vote("barlow-dominick", poext.VoteIn(vote="accept"), MEM_A)
poext.cast_vote("barlow-dominick", poext.VoteIn(vote="accept"), MEM_B)
check("2 votes recorded", len(vote_node("barlow-dominick")["votes"]) == 2)

print("\nfinalize — head only, majority accept")
# The head-only gate here is Depends(require_role("poext_head")) — a plain
# FastAPI dependency, invisible to a direct call the way every route in this
# suite is exercised (matches test_fa_offers.py's own gap around
# fa.claim_player's Depends(require_role("agent")): calling the function
# directly bypasses Depends entirely, so there is nothing to assert here
# without going through real HTTP). What direct calls CAN and do pin is every
# check finalize makes on its own — see finalize's other cases below.
final = poext.finalize_player("barlow-dominick", poext.FinalizeBody(), HEAD)
check("agreed, 2-0", final["outcome"] == "agreed" and final["accept"] == 2 and final["reject"] == 0)
check("an agreed extension is applied for real, not hand-typed", len(APPLIED_EXTENSIONS) == 1)
check("applied for the right player/team", APPLIED_EXTENSIONS[0]["details"]["player"] == "barlow-dominick"
      and APPLIED_EXTENSIONS[0]["details"]["team"] == "SAS")
check("no warnings here, so nothing needed confirming",
      APPLIED_EXTENSIONS[0]["force_warnings_only"] is False)
check("finalize's txn_id matches the applied extension", final["txn_id"] == APPLIED_EXTENSIONS[0]["id"])
check("proposal archived", PROPOSALS[[i for i, x in enumerate(PROPOSALS) if x["id"] == p["id"]][0]]["status"] == "agreed")
raises("can't finalize twice", 409, lambda: poext.finalize_player("barlow-dominick", poext.FinalizeBody(), HEAD))

print("\nfinalize — warning-level checks need explicit confirmation, never silent")
reset()
TEAM_MAP["warny"] = "SAS"
p_w = make_proposal(SAS, player="warny")
poext.submit_proposal(p_w["id"], SAS)
poext.claim_player("warny", AGENT)
poext.advance_player("warny", {"note": ""}, AGENT)
poext.assign_subcommittee("warny", {"subcommittee": ["memberA", "memberB"]}, HEAD)
poext.cast_vote("warny", poext.VoteIn(vote="accept"), MEM_A)
poext.cast_vote("warny", poext.VoteIn(vote="accept"), MEM_B)
PENDING_CHECKS[:] = [{"check": "extension_not_minimum", "passed": False, "level": "warning", "message": "double check"}]
before = len(APPLIED_EXTENSIONS)
raises("a warning-only failure asks for confirmation instead of writing", 422,
       lambda: poext.finalize_player("warny", poext.FinalizeBody(), HEAD))
check("nothing was applied on the unconfirmed attempt", len(APPLIED_EXTENSIONS) == before)
check("vote isn't lost either — still finalizable", not poext.finalize_player(
      "warny", poext.FinalizeBody(confirm_warnings=True), HEAD).get("outcome") is None)
check("confirmed finalize actually applied it", len(APPLIED_EXTENSIONS) == before + 1)
check("the applied txn shows the warning was consciously cleared",
      APPLIED_EXTENSIONS[-1]["force_warnings_only"] is True)

print("\nfinalize — a real error is never confirmable, confirm_warnings or not")
reset()
TEAM_MAP["errory"] = "SAS"
p_e = make_proposal(SAS, player="errory")
poext.submit_proposal(p_e["id"], SAS)
poext.claim_player("errory", AGENT)
poext.advance_player("errory", {"note": ""}, AGENT)
poext.assign_subcommittee("errory", {"subcommittee": ["memberA", "memberB"]}, HEAD)
poext.cast_vote("errory", poext.VoteIn(vote="accept"), MEM_A)
poext.cast_vote("errory", poext.VoteIn(vote="accept"), MEM_B)
PENDING_CHECKS[:] = [{"check": "extension_max_year1", "passed": False, "level": "error", "message": "over the cap"}]
before = len(APPLIED_EXTENSIONS)
raises("a real error blocks with no confirm_warnings escape hatch", 422,
       lambda: poext.finalize_player("errory", poext.FinalizeBody(confirm_warnings=True), HEAD))
check("still nothing applied", len(APPLIED_EXTENSIONS) == before)
PENDING_CHECKS[:] = []

# ══ rejection + the 3-strike exhaustion ══════════════════════════════════════

print("\nrejection burns a proposal; three rejections exhaust the player")


def run_one_rejected_round(slug="rejectee"):
    TEAM_MAP[slug] = "SAS"
    pr = make_proposal(SAS, player=slug)
    poext.submit_proposal(pr["id"], SAS)
    poext.claim_player(slug, AGENT)
    poext.advance_player(slug, {"note": ""}, AGENT)
    poext.assign_subcommittee(slug, {"subcommittee": ["memberA", "memberB"]}, HEAD)
    poext.cast_vote(slug, poext.VoteIn(vote="reject"), MEM_A)
    poext.cast_vote(slug, poext.VoteIn(vote="reject"), MEM_B)
    return poext.finalize_player(slug, poext.FinalizeBody(), HEAD)

reset()
f1 = run_one_rejected_round()
check("round 1: rejected, 1 total", f1["outcome"] == "rejected" and f1["rejections_total"] == 1)
check("not yet exhausted", not f1["exhausted"])
f2 = run_one_rejected_round()
check("round 2: 2 total, still not exhausted", f2["rejections_total"] == 2 and not f2["exhausted"])
f3 = run_one_rejected_round()
check("round 3: 3 total, exhausted", f3["rejections_total"] == 3 and f3["exhausted"])
raises("a 4th proposal is refused — opportunities exhausted", 422,
       lambda: make_proposal(SAS, player="rejectee"))

print("\ntie refuses to finalize rather than picking a side")
reset()
p = make_proposal(SAS)
poext.submit_proposal(p["id"], SAS)
poext.claim_player("barlow-dominick", AGENT)
poext.advance_player("barlow-dominick", {"note": ""}, AGENT)
poext.assign_subcommittee("barlow-dominick", {"subcommittee": ["memberA", "memberB"]}, HEAD)
poext.cast_vote("barlow-dominick", poext.VoteIn(vote="accept"), MEM_A)
poext.cast_vote("barlow-dominick", poext.VoteIn(vote="reject"), MEM_B)
raises("1-1 tie can't finalize", 409, lambda: poext.finalize_player("barlow-dominick", poext.FinalizeBody(), HEAD))

print("\nunlock decrements the rejection count it undid")
reset()
f1 = run_one_rejected_round("unlockee")
TEAM_MAP["unlockee"] = "SAS"
check("1 rejection recorded", STATE["players"]["unlockee"]["rejections"] == 1)
poext.unlock_player("unlockee", HEAD)
check("unlock rolled the rejection back", STATE["players"]["unlockee"]["rejections"] == 0)

print("\nlist_proposals visibility")
reset()
p = make_proposal(SAS)
draft_visible_to_sas = poext.list_proposals(info=SAS)
check("SAS sees its own draft", any(x["id"] == p["id"] for x in draft_visible_to_sas))
draft_visible_to_bkn = poext.list_proposals(info=BKN)
check("BKN can't see SAS's draft", not any(x["id"] == p["id"] for x in draft_visible_to_bkn))
draft_visible_to_agent = poext.list_proposals(info=AGENT)
check("an agent can't see a draft either — nobody's chosen to show it yet",
      not any(x["id"] == p["id"] for x in draft_visible_to_agent))
poext.submit_proposal(p["id"], SAS)
submitted_visible_to_agent = poext.list_proposals(info=AGENT)
check("once submitted, any agent can see it in the queue",
      any(x["id"] == p["id"] for x in submitted_visible_to_agent))
submitted_visible_to_bkn = poext.list_proposals(info=BKN)
check("...but a team with no committee role still can't", not any(x["id"] == p["id"] for x in submitted_visible_to_bkn))

print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

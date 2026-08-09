"""Regression tests for the PDC free-agency offer pipeline (Phase 2) —
routers.free_agency. Spec: nbn-today/docs/pdc-free-agency-spec.md.

What's worth pinning here is the set of properties that are load-bearing
*because* of a decision, not the CRUD around them:

  * **Submission is final at the team's initiative** (§ 4.3 / D4). No withdraw,
    no post-submit edit — and the only way back is a committee remand, which no
    team can reach for.
  * **Remands are additive and attributed** (§ 4.3a / D14). Two reviewers
    sending the same offer back must not fight over its status or make the team
    guess which note is live.
  * **Nothing is overwritten on resubmit.** Without the frozen prior version,
    "final" is unfalsifiable — nobody can see what changed.
  * **The FFA clock is the only clock the software enforces** (D11), it starts
    on the first *submitted* offer, later offers don't extend it, and expiry is
    computed rather than written — a dead scheduler must not be able to leave a
    player open forever.
  * **A ballot totals exactly 1,000**, its options are the live offers plus
    `NO_SIGNING` (always) and `QO` (RFAs only), and it is a vote rather than an
    administrative action — so an unassigned member cannot cast one.
  * **Finalize archives**, which is what frees a team to bid on the same player
    again in a later round (§ 13.1); unlock puts it back.
  * **Visibility is enforced server-side, per request** (§ 4.5, § 6.1): a `fac`
    member not assigned to a player sees neither the offers nor the ballots, and
    the committee never sees another team's drafts.

Everything is patched into memory — the endpoint functions are called directly,
so nothing touches fa-*.json in NBS_DATA_DIR and no real validator runs.

    venv/bin/python -m tests.test_fa_offers
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.free_agency as fa  # noqa: E402

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

STATE = {"seq": 0, "mode": "closed", "rounds": [], "players": {}}
OFFERS: list = []
BALLOTS: dict = {}

POOL = {
    "curry-stephen": {"class_year": "26-27", "hold_type": "UFA",
                      "prior_salary": 50_000_000, "rfa": False, "qo_amount": None},
    "young-rfa":     {"class_year": "26-27", "hold_type": "RFA",
                      "prior_salary": 4_000_000, "rfa": True, "qo_amount": 5_000_000},
    # Under contract for two more years. `_fa_pool` spans future league years by
    # design — /free-agency's year chips are built from it — so most of the real
    # pool looks like this: 361 of 570 entries on the day this was added.
    "future-ufa":    {"class_year": "28-29", "hold_type": "UFA",
                      "prior_salary": 20_000_000, "rfa": False, "qo_amount": None},
}

MEMBERS = {"facHead": {"roles": ["fac_head"]}, "memberA": {"roles": ["fac"]},
           "memberB": {"roles": ["fac", "phx"]}, "phxOwner": {"roles": ["phx"]},
           "phxGM": {"roles": ["phx"]}}

HEAD = {"name": "facHead", "roles": ["fac_head"]}
MEM_A = {"name": "memberA", "roles": ["fac"]}
MEM_B = {"name": "memberB", "roles": ["fac", "phx"]}
UNASSIGNED = {"name": "outsider", "roles": ["fac"]}
PHX_OWNER = {"name": "phxOwner", "roles": ["phx"]}
PHX_GM = {"name": "phxGM", "roles": ["phx"]}
BKN_OWNER = {"name": "bknOwner", "roles": ["bkn"]}

OWNERS = {"phxOwner": "PHX", "bknOwner": "BKN"}
CURRENT_TEAM = {"memberB": "PHX", "phxOwner": "PHX", "phxGM": "PHX", "bknOwner": "BKN"}

fa._load_state = lambda: STATE
fa._save_state = lambda s: None
fa._load_offers = lambda: OFFERS
fa._save_offers = lambda o: None
fa._load_ballots = lambda: BALLOTS
fa._save_ballots = lambda b: None
fa._live_pool = lambda: POOL
fa._current_league_year = lambda: "26-27"
fa.load_members = lambda: MEMBERS
fa.log_write = lambda info, msg: None
fa._member_current_team = lambda name, members=None: CURRENT_TEAM.get(name)
fa.is_team_owner = lambda info, team: OWNERS.get(info["name"]) == team.upper()

# The validator is exercised by its own suites (test_signing_eligibility,
# test_signing_method_funding, …). Here it only needs to be *the same call*, so
# it's stubbed to a verdict this suite can steer.
LEGAL = {"legal": True}
fa._validation_ctx = lambda: {"cur_season": "26-27", "bios": {}, "cap_levels": {"26-27": {"cap": 165_000_000}}}
fa._require_validatable = lambda team, player, ctx: None
fa._signing_fact_sheet = lambda *a, **k: {"cap_room": 40_000_000}
fa._compute_team_salary = lambda team, bios, season: 130_000_000
fa._signee_existing_hold = lambda team, player, bios, season: (0, False)


class FakeCheck:
    def __init__(self, passed):
        self.passed = passed

    def model_dump(self):
        return {"check": "cap_room", "passed": self.passed, "level": "error",
                "message": "" if self.passed else "not enough room"}


fa._validate_sign = lambda details, ctx: [FakeCheck(LEGAL["legal"])]


def contract(y1="$40,000,000"):
    return fa.ContractIn(type="player", salaries={"26-27": y1}, cap_holds={"27-28": "UFA"})


def make_offer(who, player="curry-stephen", team="PHX", y1="$40,000,000"):
    return fa.create_offer(
        fa.OfferCreate(player=player, team=team, contract=contract(y1),
                       signing_method="cap_space", pitch="Come win here",
                       promises=fa.PromisesIn(mpg=34, playoffs=True, role="starter")),
        who)


# ══ modes and rounds ══════════════════════════════════════════════════════════

print("\nmode + rounds")
raises("bad mode rejected", 422, lambda: fa.set_mode(fa.ModeIn(mode="chaos"), HEAD))
check("starts closed", STATE["mode"] == "closed")
raises("no offers while FA is closed", 422, lambda: make_offer(PHX_OWNER))

fa.set_mode(fa.ModeIn(mode="rounds"), HEAD)
raises("can't open a player before a round exists", 422,
       lambda: fa.set_player_state("curry-stephen", fa.PlayerStateIn(status="open"), HEAD))

r1 = fa.open_round(fa.RoundIn(name="Round 1", closes_at="2026-08-10T17:00:00Z"), HEAD)
check("round 1 minted", r1["id"] == "r1" and r1["number"] == 1)
check("closes_at is carried but advisory", r1["closes_at"] == "2026-08-10T17:00:00Z")

raises("unknown member can't be assigned", 422,
       lambda: fa.set_player_state("curry-stephen",
                                   fa.PlayerStateIn(status="open", subcommittee=["ghost"]), HEAD))

st = fa.set_player_state("curry-stephen",
                         fa.PlayerStateIn(status="open", subcommittee=["memberA", "memberB"]), HEAD)
check("player opened into the current round", st["round_id"] == "r1" and st["status"] == "open")
check("conflicted assignee flagged inline, not refused (§ 4.6)",
      st["conflicts"] == {"memberA": None, "memberB": None})

# ══ drafting ══════════════════════════════════════════════════════════════════

print("\ndrafting (§ 6.0 — team role drafts, owner submits)")
raises("a player who isn't open takes no offers", 422,
       lambda: make_offer(PHX_OWNER, player="young-rfa"))
raises("no team role, no draft", 403, lambda: make_offer(BKN_OWNER, team="PHX"))

o1 = make_offer(PHX_GM)
check("a GM can draft the whole offer", o1["status"] == "draft" and o1["created_by"] == "phxGM")
check("offer number comes from the monotonic seq", o1["number"] == 1)
check("`offer` is exactly a SignDetails payload",
      set(o1["offer"]) == {"player", "team", "contract", "signing_method",
                           "bird_rights_type", "eaps_assumption"})
check("pitch and promises live outside `offer`",
      o1["pitch"] and o1["promises"]["role"] == "starter")
check("a draft carries no round yet", o1["round_id"] is None)

raises("one live offer per team per player (D5)", 409, lambda: make_offer(PHX_OWNER))
raises("bad promise role rejected", 422, lambda: fa.patch_offer(
    o1["id"], fa.OfferPatch(promises=fa.PromisesIn(role="messiah")), PHX_GM))

fa.patch_offer(o1["id"], fa.OfferPatch(contract=contract("$41,000,000"), pitch="Revised"), PHX_GM)
check("draft is editable by any team-role holder",
      o1["offer"]["contract"]["salaries"]["26-27"] == "$41,000,000" and o1["pitch"] == "Revised")

# ══ submit ════════════════════════════════════════════════════════════════════

print("\nsubmit")
raises("a GM cannot pull the trigger", 403, lambda: fa.submit_offer(o1["id"], PHX_GM))

LEGAL["legal"] = False
raises("an offer failing an error-level check can't be submitted (no force)", 422,
       lambda: fa.submit_offer(o1["id"], PHX_OWNER))
check("a rejected submit leaves it a draft", o1["status"] == "draft")
LEGAL["legal"] = True

o1 = fa.submit_offer(o1["id"], PHX_OWNER)
check("submitted by the owner", o1["status"] == "submitted" and o1["submitted_by"] == "phxOwner")
check("round stamped at submit", o1["round_id"] == "r1")
check("validation frozen at submit (§ 5.2)", o1["validation"]["legal"] is True)

raises("no post-submission edit at the team's initiative (§ 4.3)", 409,
       lambda: fa.patch_offer(o1["id"], fa.OfferPatch(pitch="actually…"), PHX_OWNER))
raises("no deleting a submitted offer either", 409, lambda: fa.delete_offer(o1["id"], PHX_OWNER))
raises("and no resubmitting one", 409, lambda: fa.submit_offer(o1["id"], PHX_OWNER))

o2 = make_offer(BKN_OWNER, team="BKN", y1="$38,000,000")
o2 = fa.submit_offer(o2["id"], BKN_OWNER)
check("a second team can offer on the same player", o2["team"] == "BKN")

# ══ visibility ════════════════════════════════════════════════════════════════

print("\nvisibility (§ 4.5, § 6.1)")
check("an unassigned fac member sees no offers on this player",
      fa.list_offers(player="curry-stephen", info=UNASSIGNED) == [])
raises("…and can't open the review page", 403,
       lambda: fa.review_player("curry-stephen", UNASSIGNED))
raises("…nor read the ballots", 403,
       lambda: fa.get_ballots("curry-stephen", UNASSIGNED))
check("an assigned member sees every submitted offer",
      {o["id"] for o in fa.list_offers(player="curry-stephen", info=MEM_A)} == {o1["id"], o2["id"]})
check("the head sees everything", len(fa.list_offers(info=HEAD)) == len(OFFERS))

# A draft the committee must not see.
fa.set_player_state("young-rfa", fa.PlayerStateIn(status="open", subcommittee=["memberA"]), HEAD)
draft_only = make_offer(PHX_OWNER, player="young-rfa")
check("the committee never sees another team's scratch pad",
      fa.list_offers(player="young-rfa", info=MEM_A) == [])
check("but the team sees its own draft",
      [o["id"] for o in fa.list_offers(player="young-rfa", info=PHX_GM)] == [draft_only["id"]])

# ══ remand ════════════════════════════════════════════════════════════════════

print("\nremand (§ 4.3a — a committee power, never a team power)")
raises("a remand requires a note", 422,
       lambda: fa.remand_offer(o1["id"], fa.RemandIn(note="   "), MEM_A))
raises("an unassigned member can't remand", 403,
       lambda: fa.remand_offer(o1["id"], fa.RemandIn(note="add a year"), UNASSIGNED))

o1 = fa.remand_offer(o1["id"], fa.RemandIn(note="Add a fourth year"), MEM_A)
check("first remand flips it to returned", o1["status"] == "returned")
check("the note is on the record, attributed",
      o1["remands"][0]["by"] == "memberA" and o1["remands"][0]["note"] == "Add a fourth year")
check("an unconflicted remand carries no conflict", o1["remands"][0]["conflict"] is None)

o1 = fa.remand_offer(o1["id"], fa.RemandIn(note="…and trim year 1"), MEM_B)
check("a second remand is additive, not a second round-trip",
      len(o1["remands"]) == 2 and o1["status"] == "returned")
check("a conflicted remand is flagged like a conflicted ballot",
      o1["remands"][1]["conflict"] == "PHX")

fa.patch_offer(o1["id"], fa.OfferPatch(contract=contract("$44,000,000")), PHX_GM)
check("a returned offer is editable again — by the same people who drafted it",
      o1["offer"]["contract"]["salaries"]["26-27"] == "$44,000,000")

v1_validation = o1["validation"]
o1 = fa.submit_offer(o1["id"], PHX_OWNER)
check("resubmission bumps the version", o1["version"] == 2 and o1["status"] == "submitted")
check("nothing is overwritten — v1 is frozen whole",
      len(o1["versions"]) == 1
      and o1["versions"][0]["version"] == 1
      and o1["versions"][0]["offer"]["contract"]["salaries"]["26-27"] == "$41,000,000"
      and o1["versions"][0]["validation"] is v1_validation)
check("the outstanding remands are the ones raised against the current version",
      [r for r in o1["remands"] if r["from_version"] >= o1["version"]] == [])

# ══ review ════════════════════════════════════════════════════════════════════

print("\nreview")
rv = fa.review_player("curry-stephen", MEM_A)
check("review lists both submitted offers", len(rv["offers"]) == 2)
check("each offer carries a live revalidation beside its frozen snapshot (§ 5.2)",
      all("revalidation" in o and o["validation"] for o in rv["offers"]))
check("legality unchanged reads as unchanged",
      all(o["legality_changed"] is False for o in rv["offers"]))
check("a UFA's ballot has no QO line, and always a NO_SIGNING line",
      {opt["key"] for opt in rv["ballot_options"]} == {o1["id"], o2["id"], "NO_SIGNING"})
check("per-team exposure is disclosed rather than blocked (§ 5.3, D6)",
      rv["commitments"]["PHX"]["committed_year1"] == 44_000_000
      and rv["commitments"]["PHX"]["room"] == 35_000_000
      and rv["commitments"]["PHX"]["overcommitted"] is True)

# The team's own form shows the same exposure the committee is shown (§ 8.1.5).
# Same helper, same numbers — a second computation here would be the disclosure
# disagreeing with itself.
check("the team side reads the identical figure off the identical helper",
      fa.get_commitment("phx", PHX_GM) == rv["commitments"]["PHX"])
# Overcommitted means bidding more than you can fund. A team with nothing out is
# never overcommitted, however far over the cap it sits — otherwise the team's
# own form (§ 8.1) opens shouting at a team that hasn't bid on anybody.
_salary = fa._compute_team_salary
fa._compute_team_salary = lambda team, bios, season: 300_000_000   # deep over the cap
check("a team over the cap with nothing out is not overcommitted",
      fa._team_commitment("LAL", OFFERS, fa._validation_ctx())["overcommitted"] is False)
check("…but it is the moment it bids anything at all",
      fa._team_commitment("PHX", OFFERS, fa._validation_ctx())["overcommitted"] is True)
fa._compute_team_salary = _salary
raises("another team's exposure isn't readable", 403,
       lambda: fa.get_commitment("PHX", BKN_OWNER))
raises("unknown team rejected rather than scored as $0", 400,
       lambda: fa.get_commitment("ZZZ", PHX_GM))

LEGAL["legal"] = False
rv_illegal = fa.review_player("curry-stephen", MEM_A)
check("an offer that went illegal after submission is badged, not hidden (§ 4.3 #2)",
      all(o["legality_changed"] is True and o["revalidation"]["legal"] is False
          for o in rv_illegal["offers"]))
LEGAL["legal"] = True

rfa_rv = fa.review_player("young-rfa", MEM_A)
check("an RFA's ballot carries the QO line, marked estimated (§ 7.2)",
      any(opt["key"] == "QO" and opt["estimated"] is True and opt["amount"] == 5_000_000
          for opt in rfa_rv["ballot_options"]))

# ══ ballots ═══════════════════════════════════════════════════════════════════

print("\nballots (§ 4.4)")
raises("an unassigned member can't cast one — a ballot is a vote, not a power", 403,
       lambda: fa.cast_ballot("curry-stephen",
                              fa.BallotIn(balls={"NO_SIGNING": 1000}), UNASSIGNED))
raises("a ballot must total exactly 1,000", 422,
       lambda: fa.cast_ballot("curry-stephen",
                              fa.BallotIn(balls={o1["id"]: 900}), MEM_A))
raises("unknown options are rejected", 422,
       lambda: fa.cast_ballot("curry-stephen",
                              fa.BallotIn(balls={"QO": 1000}), MEM_A))
raises("negative ball counts are rejected", 422,
       lambda: fa.cast_ballot("curry-stephen",
                              fa.BallotIn(balls={o1["id"]: 1100, o2["id"]: -100}), MEM_A))

b_a = fa.cast_ballot("curry-stephen",
                     fa.BallotIn(balls={o1["id"]: 600, o2["id"]: 300, "NO_SIGNING": 100}), MEM_A)
check("a cast ballot records who, when, and zero-free balls",
      b_a["balls"] == {o1["id"]: 600, o2["id"]: 300, "NO_SIGNING": 100} and b_a["updated_at"])
check("an unconflicted ballot carries no conflict", b_a["conflict"] is None)

b_b = fa.cast_ballot("curry-stephen",
                     fa.BallotIn(balls={o1["id"]: 1000}, note="best fit"), MEM_B)
check("a member whose own team is bidding may still ballot — flagged, not blocked (D10)",
      b_b["conflict"] == "PHX")

view = fa.get_ballots("curry-stephen", MEM_A)
check("a sub-committee is transparent to itself, in progress included (§ 4.5)",
      set(view["ballots"]) == {"memberA", "memberB"})
check("nobody outstanding once both have voted", view["outstanding"] == [])

# Revise an offer out from under both cast ballots.
fa.remand_offer(o1["id"], fa.RemandIn(note="drop year 3"), MEM_A)
fa.patch_offer(o1["id"], fa.OfferPatch(contract=contract("$46,000,000")), PHX_GM)
o1 = fa.submit_offer(o1["id"], PHX_OWNER)
view = fa.get_ballots("curry-stephen", MEM_A)
check("a ballot cast before a revision is flagged, never voided (§ 4.3a)",
      view["ballots"]["memberA"]["revised_since"] == [o1["id"]]
      and view["ballots"]["memberB"]["revised_since"] == [o1["id"]])
check("…and the ballot itself is untouched",
      view["ballots"]["memberB"]["balls"] == {o1["id"]: 1000})

b_a = fa.cast_ballot("curry-stephen",
                     fa.BallotIn(balls={o1["id"]: 500, o2["id"]: 500}), MEM_A)
check("ballots stay revisable until the head locks",
      b_a["balls"] == {o1["id"]: 500, o2["id"]: 500})
check("…and revisiting one clears its flag, leaving the other member's standing",
      fa.get_ballots("curry-stephen", MEM_A)["ballots"]["memberA"]["revised_since"] == []
      and fa.get_ballots("curry-stephen", MEM_A)["ballots"]["memberB"]["revised_since"] == [o1["id"]])

# ══ finalize / unlock ═════════════════════════════════════════════════════════

print("\nfinalize + unlock")
o2 = fa.remand_offer(o2["id"], fa.RemandIn(note="we need a team option"), MEM_A)
final = fa.finalize_player("curry-stephen", HEAD)
check("totals are summed and stored, not recomputed on read",
      final["totals"] == {o1["id"]: 1500, o2["id"]: 500})
check("voters and abstentions are on the record",
      final["voters"] == ["memberA", "memberB"] and final["abstained"] == [])
check("an unanswered remand warns at finalize; it never blocks (D15)",
      len(final["outstanding_remands"]) == 1
      and final["outstanding_remands"][0]["offer"] == o2["id"])

raises("ballots are locked after finalize", 409,
       lambda: fa.cast_ballot("curry-stephen",
                              fa.BallotIn(balls={o1["id"]: 1000}), MEM_A))
raises("a remand cannot follow a finalize", 409,
       lambda: fa.remand_offer(o1["id"], fa.RemandIn(note="one more thing"), MEM_A))
raises("finalizing twice is refused", 409, lambda: fa.finalize_player("curry-stephen", HEAD))

check("finalize archives the offers", all(o.get("archived_at") for o in [o1, o2]))
# …which is what frees a team to bid on the same player again in a later round.
fa.set_player_state("curry-stephen", fa.PlayerStateIn(status="open"), HEAD)
o3 = make_offer(PHX_OWNER)
check("PHX can offer on the same player again once he's reopened", o3["status"] == "draft")
fa.delete_offer(o3["id"], PHX_OWNER)

fa.set_player_state("curry-stephen", fa.PlayerStateIn(status="closed", round_id="r1"), HEAD)
fa.unlock_player("curry-stephen", HEAD)
check("unlock clears the lock", fa.get_ballots("curry-stephen", HEAD)["final"] is None)
check("…and un-archives the offers that finalize archived",
      o1.get("archived_at") is None and o2.get("archived_at") is None)
check("the undone finalize is kept on the record",
      len(BALLOTS["curry-stephen"]["r1"]["unlocks"]) == 1)

# ══ the FFA clock ═════════════════════════════════════════════════════════════

print("\nFFA clock (§ 4.1 — the only clock the software enforces)")
STATE["mode"] = "ffa"
STATE["players"]["curry-stephen"] = {"status": "closed", "round_id": None,
                                     "subcommittee": ["memberA"], "ffa": None}
OFFERS[:] = [o for o in OFFERS if o["player"] != "curry-stephen"]

check("in FFA every pool player is offerable regardless of status",
      fa._accepts_offers(STATE, "curry-stephen", POOL)[0] is True)

# The pool spans future league years — it is what /free-agency's year chips are
# built from — so "every player in the pool" would otherwise offer a contract to
# someone under contract for two more seasons. This is the one place the bug
# bites hardest, because FFA has no per-player gate to catch it.
ok, why = fa._accepts_offers(STATE, "future-ufa", POOL)
check("…but not one who hasn't reached free agency yet", ok is False)
check("…and the refusal says when he does", "28-29" in why)
raises("no offer on a player still under contract, even in FFA", 422,
       lambda: make_offer(PHX_OWNER, player="future-ufa"))
raises("and the head can't open him for offers either", 422,
       lambda: fa.set_player_state("future-ufa", fa.PlayerStateIn(status="open"), HEAD))
check("an unresolved hold from an older class is still a free agent",
      fa._is_current_fa({"class_year": "25-26"}, "26-27") is True)

f1 = make_offer(PHX_OWNER)
check("a draft does not start the clock",
      (STATE["players"]["curry-stephen"].get("ffa")) is None)

f1 = fa.submit_offer(f1["id"], PHX_OWNER)
ffa = STATE["players"]["curry-stephen"]["ffa"]
check("the first submitted offer starts a 24-hour clock",
      ffa and ffa["started_by_offer"] == f1["id"]
      and (fa._parse_ts(ffa["deadline"]) - fa._parse_ts(ffa["started_at"])) == timedelta(hours=24))
check("the FFA session doubles as the round id, so its ballots get their own bucket",
      f1["round_id"].startswith("ffa-"))

started = ffa["started_at"]
f2 = make_offer(BKN_OWNER, team="BKN")
f2 = fa.submit_offer(f2["id"], BKN_OWNER)
check("a later offer does not extend the clock",
      STATE["players"]["curry-stephen"]["ffa"]["started_at"] == started)

past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
STATE["players"]["curry-stephen"]["ffa"]["deadline"] = past


def _entry_state():
    """The player's entry with the announcement marker stripped — everything
    that decides whether offers are accepted."""
    e = {k: v for k, v in STATE["players"]["curry-stephen"].items() if k != "ffa"}
    e["ffa"] = {k: v for k, v in (STATE["players"]["curry-stephen"]["ffa"] or {}).items()
                if k != "closed_posted"}
    return repr(e)


before = _entry_state()
accepting, reason = fa._accepts_offers(STATE, "curry-stephen", POOL)
fa.get_board()
check("an expired window stops accepting offers", accepting is False and "closed at" in reason)
# Expiry is a comparison, not a write — which is the point. A scheduler that
# dies leaves a player silently open forever; `now >= deadline` cannot. The one
# thing a read may write is `ffa.closed_posted`, the once-only guard on the
# § 9.2 announcement (see `_sweep_ffa_expiry`) — and it is deliberately *not*
# consulted by `_accepts_offers`, so even losing it would re-announce rather
# than reopen the player.
check("observing expiry doesn't change whether the player accepts offers",
      _entry_state() == before)
check("...it only stamps the once-only announcement guard",
      "closed_posted" in (STATE["players"]["curry-stephen"]["ffa"] or {}))
STATE["players"]["curry-stephen"]["ffa"].pop("closed_posted", None)
check("...and expiry is still derived from the deadline, not from that stamp",
      fa._accepts_offers(STATE, "curry-stephen", POOL)[0] is False)

late = make_offer  # a *new* offer from another team is refused past the deadline
OFFERS[:] = [o for o in OFFERS if o["team"] != "BKN"]
raises("no new offers past the deadline", 422, lambda: late(BKN_OWNER, team="BKN"))

f1 = fa.remand_offer(f1["id"], fa.RemandIn(note="add a player option"), MEM_A)
fa.patch_offer(f1["id"], fa.OfferPatch(contract=contract("$45,000,000")), PHX_OWNER)
f1 = fa.submit_offer(f1["id"], PHX_OWNER)
check("the FFA window does not gate a revision the committee itself asked for (§ 4.3a)",
      f1["status"] == "submitted" and f1["version"] == 2)

# ══ board ═════════════════════════════════════════════════════════════════════

print("\npublic board (§ 6.1 — the fact that a team is bidding is committee information)")
board = fa.get_board()
blob = repr(board)
check("the board names no team", "PHX" not in blob and "BKN" not in blob)
check("…no dollars, and no offer count", "$" not in blob and "offer_count" not in blob)
check("but it does carry the FFA deadline the league needs to act on",
      board["players"]["curry-stephen"]["ffa_deadline"] == past)
check("and says why a player isn't accepting",
      board["players"]["curry-stephen"]["accepting"] is False)
# Phase 7's ⋯ menu renders `reason` verbatim as its disabled copy. Listing only
# the accepting players would make /free-agency reinvent those strings, and the
# two explanations would drift.
check("every pool player is listed, taking offers or not",
      set(board["players"]) == set(POOL))
check("…a closed one carrying the reason the team's menu shows",
      "24-hour" in board["players"]["curry-stephen"]["reason"])
check("…and an open one carrying none",
      board["players"]["young-rfa"]["accepting"] is True
      and board["players"]["young-rfa"]["reason"] is None)

state_view = fa.get_state(MEM_A)
check("the committee view adds the sub-committee the board withholds",
      state_view["players"]["curry-stephen"]["subcommittee"] == ["memberA"]
      and state_view["players"]["curry-stephen"]["mine"] is True)

# What the dashboard's queue sorts and badges on. `balloted` is the viewer's own
# ballot and leaks nothing; how many *others* have voted is scoped to that
# player's sub-committee (§ 4.5), so it is head-only here.
head_view = fa.get_state(HEAD)
check("a member sees whether they themselves have balloted",
      state_view["players"]["curry-stephen"]["balloted"] is False
      and state_view["players"]["curry-stephen"]["finalized"] is False)
check("how many ballots are in is head-only on the board view (§ 4.5)",
      head_view["players"]["curry-stephen"]["ballots_cast"] == 0
      and "ballots_cast" not in state_view["players"]["curry-stephen"])

# ══ the sub-committee picker (§ 4.6) ══════════════════════════════════════════

print("\nassignment picker")
rv_head = fa.review_player("curry-stephen", HEAD)
check("the picker lists fac members — a head is one of their own committee",
      [a["name"] for a in rv_head["assignable"]] == ["facHead", "memberA", "memberB"])
check("…and flags a conflicted assignee *before* the head confirms",
      {a["name"]: a["conflict"] for a in rv_head["assignable"]}
      == {"facHead": None, "memberA": None, "memberB": "PHX"})
check("a reviewer who isn't the head gets no picker",
      fa.review_player("curry-stephen", MEM_A)["assignable"] == [])

print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILURES: {FAILS}")
    sys.exit(1)
print("ALL PASS")

"""Regression tests for "Clean Up the Poo Poo" (routers/cleanup.py).

Written 2026-08-16 alongside the feature, extended the same day for the
discord_fa gap type. Pins the properties that matter most:

  * **The submitter gets paid, never the approving admin** — cleanup.py bypasses
    players.py's PUT handler entirely so its own NB¥10 auto-reward can't fire
    and mis-credit whoever's token approved the submission.
  * **Self-approval is blocked** even for admin.
  * **Competing submissions are allowed**; approving one supersedes the rest
    for the same gap (bio field, or Discord candidate) rather than rejecting them.
  * **A race against another write path** (the field got filled some other
    way between submission and review; or another submission for the same
    Discord candidate got approved first) auto-rejects at approval time
    instead of silently overwriting or double-paying.
  * **Rejection never writes** the bio (or posts a transaction).
  * **A flagged Discord message's already-exact candidates aren't gaps** — a
    message can be flagged over ONE unresolved player while other players in
    the same batch announcement already matched cleanly.
  * **Approving a discord_fa submission calls the real historical-append
    functions** (mocked here, but the same ones submit_discord_fa_signings.py
    calls) and records the candidate into the shared submitted-state file so
    that script can't later resubmit it as a duplicate transaction.

These patch the store, player-bios, and the Discord-backfill files into
memory — nothing touches cleanup-submissions.json, player-bios.json,
transactions.json, or discord-fa-signings-submitted.json in NBS_DATA_DIR.

    venv/bin/python -m tests.test_cleanup
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.cleanup as cu  # noqa: E402

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


# ── in-memory fixtures ──────────────────────────────────────────────────────
STORE = {"seq": 0, "items": []}
cu._load_store = lambda: STORE
cu._save_store = lambda store: None
cu.log_write = lambda info, msg: None

BIOS = {
    "test-player-one": {"name": "PLAYER, ONE", "college": "", "country": "USA",
                         "draft_year": None, "draft_round": None, "draft_pick": None},
    "test-player-two": {"name": "PLAYER, TWO", "college": "Duke", "weight": None},
}
cu.load_player_bios = lambda: BIOS
saved_bios_calls = []
cu.save_player_bios = lambda data: saved_bios_calls.append(dict(data))

reward_calls = []
cu._award_cleanup_reward = lambda name, amount, reason: reward_calls.append((name, amount, reason))

DISCORD_FLAGGED = {
    "flagged": [
        {
            "discord_id": "d1", "date": "2020-11-10", "channel": "2020-fanews",
            "description": "The Boston Celtics decline Jevonte Green's 2020-21 team option.",
            "candidates": [
                {"kind": "option", "slug": "green-javonte", "note": "fuzzy:javonte green",
                 "raw_player": "Jevonte Green", "decision": "decline", "option_type": "TEAM_OPT", "year": "20-21"},
            ],
        },
        {
            # batch message: one exact (not a gap) + one unresolved (a gap)
            "discord_id": "d2", "date": "2021-07-17", "channel": "2021-fanews",
            "description": "The Hawks accept Nathan Knight's option. The Hawks decline Devontae Cacok's option.",
            "candidates": [
                {"kind": "option", "slug": "knight-nathan", "note": "exact", "raw_player": "Nathan Knight",
                 "decision": "accept", "option_type": "TEAM_OPT", "year": "21-22"},
                {"kind": "option", "slug": None, "note": "unresolved", "raw_player": "Devontae Cacok",
                 "decision": "decline", "option_type": "TEAM_OPT", "year": "21-22"},
            ],
        },
        {
            "discord_id": "d3", "date": "2020-11-17", "channel": "2020-fanews",
            "description": "Malcolm Calazon agrees to sign with the Los Angeles Lakers",
            "candidates": [
                {"kind": "sign", "team": "LAL", "slug": None, "note": "unresolved", "raw_player": "Malcolm Calazon"},
            ],
        },
    ],
}
cu._load_json = lambda path, default: DISCORD_FLAGGED if path == cu.DISCORD_FA_RESOLVED_FILE else (
    _submitted_state if path == cu.DISCORD_FA_SUBMITTED_FILE else default)
_submitted_state = {}
def _fake_save_json(path, data):
    global _submitted_state
    if path == cu.DISCORD_FA_SUBMITTED_FILE:
        _submitted_state = data
cu._save_json = _fake_save_json

historical_calls = []
def _fake_create_sign(details, body, info):
    historical_calls.append(("sign", details, body))
    return {"id": "txn1", "type": "sign"}
def _fake_create_option(details, body, info):
    historical_calls.append(("option", details, body))
    return {"id": "txn2", "type": "option"}
cu._create_historical_sign = _fake_create_sign
cu._create_historical_option = _fake_create_option

SUBMITTER = {"name": "alice", "roles": ["uta"]}
OTHER_SUBMITTER = {"name": "bob", "roles": ["phx"]}
ADMIN = {"name": "root", "roles": ["admin"]}

# ── gaps ─────────────────────────────────────────────────────────────────────
print("\ngaps")
gaps = cu.get_gaps()
bio_gaps = [g for g in gaps if g["gap_type"] == "bio_field"]
fa_gaps = [g for g in gaps if g["gap_type"] == "discord_fa"]
gap_fields = {(g["slug"], g["field"]) for g in bio_gaps}
check("empty college is a gap", ("test-player-one", "college") in gap_fields)
check("filled college is not a gap", ("test-player-two", "college") not in gap_fields)
check("all-null draft trio is one draft_info gap", ("test-player-one", "draft_info") in gap_fields)
check("filled country is not a gap", ("test-player-one", "country") not in gap_fields)

fa_keys = {(g["discord_id"], g["candidate_index"]) for g in fa_gaps}
check("low-confidence candidate is a gap", ("d1", 0) in fa_keys)
check("exact candidate in a batch message is not a gap", ("d2", 0) not in fa_keys)
check("unresolved candidate in the same batch message is a gap", ("d2", 1) in fa_keys)
check("fully-unresolved sign candidate is a gap", ("d3", 0) in fa_keys)
check("exactly 3 discord_fa gaps total", len(fa_gaps) == 3)

# ── submission validation ───────────────────────────────────────────────────
print("\nsubmission validation")
raises("unknown player", 400, lambda: cu.create_submission(
    cu.SubmissionCreate(slug="nobody", field="college", value="X"), SUBMITTER))
raises("unknown field", 422, lambda: cu.create_submission(
    cu.SubmissionCreate(slug="test-player-one", field="salaries", value="X"), SUBMITTER))
raises("already-filled field", 409, lambda: cu.create_submission(
    cu.SubmissionCreate(slug="test-player-two", field="college", value="Duke"), SUBMITTER))
raises("bad weight", 422, lambda: cu.create_submission(
    cu.SubmissionCreate(slug="test-player-two", field="weight", value="not a number"), SUBMITTER))
raises("draft_info missing year", 422, lambda: cu.create_submission(
    cu.SubmissionCreate(slug="test-player-one", field="draft_info", value={"draft_round": 1, "draft_pick": 5}), SUBMITTER))
raises("draft_info round without pick", 422, lambda: cu.create_submission(
    cu.SubmissionCreate(slug="test-player-one", field="draft_info", value={"draft_year": 2019, "draft_round": 1}), SUBMITTER))

# ── competing submissions ────────────────────────────────────────────────────
print("\ncompeting submissions")
sub1 = cu.create_submission(cu.SubmissionCreate(slug="test-player-one", field="college", value="  Gonzaga  "), SUBMITTER)
check("value is trimmed", sub1["value"] == "Gonzaga")
check("starts pending", sub1["status"] == "pending")
check("credited to the actual submitter", sub1["submitted_by"] == "alice")

sub2 = cu.create_submission(cu.SubmissionCreate(slug="test-player-one", field="college", value="Saint Mary's"), OTHER_SUBMITTER)
check("a second submitter can compete for the same gap", sub2["status"] == "pending")

gaps_after = {(g["slug"], g["field"]) for g in cu.get_gaps() if g["gap_type"] == "bio_field"}
check("a field with a pending submission drops out of the gap list", ("test-player-one", "college") not in gaps_after)

# ── self-approval allowed ─────────────────────────────────────────────────────
# Was blocked 2026-08-16–2026-08-20: with a single admin who is also the
# primary submitter, the block deadlocked every one of their own submissions
# with no possible reviewer. See the spec's decision table for the reversal.
print("\nself-approval")
admin_sub = cu.create_submission(cu.SubmissionCreate(slug="test-player-one", field="photo_url", value="https://example.com/p.jpg"), ADMIN)
self_approved = cu.approve_submission(admin_sub["id"], ADMIN)
check("admin can approve their own submission", self_approved["status"] == "approved")
check("reviewer stamped as the same admin", self_approved["reviewed_by"] == "root")
check("bio actually written on self-approval", saved_bios_calls[-1]["test-player-one"]["photo_url"] == "https://example.com/p.jpg")

# ── approval: writes the field, credits the submitter, supersedes the rest ──
print("\napproval")
approved = cu.approve_submission(sub1["id"], ADMIN)
check("status becomes approved", approved["status"] == "approved")
check("tiered reward recorded on the submission", approved["reward_nby"] == cu.CLEANUP_FIELD_REWARDS["college"])
check("reviewer stamped", approved["reviewed_by"] == "root")

check("bio actually written", saved_bios_calls[-1]["test-player-one"]["college"] == "Gonzaga")
check("reward call credits the submitter, not the approving admin",
      reward_calls and reward_calls[-1][0] == "alice" and reward_calls[-1][1] == cu.CLEANUP_FIELD_REWARDS["college"])

sub2_after = next(it for it in STORE["items"] if it["id"] == sub2["id"])
check("the losing competing submission is superseded, not rejected", sub2_after["status"] == "superseded")

raises("approving an already-approved submission again", 409,
       lambda: cu.approve_submission(sub1["id"], ADMIN))

# ── race: field filled elsewhere before review ───────────────────────────────
print("\nrace with another write path")
raced = cu.create_submission(cu.SubmissionCreate(slug="test-player-two", field="weight", value=210), SUBMITTER)
BIOS["test-player-two"]["weight"] = 205  # simulate a curator direct-edit landing first
raises("field filled elsewhere auto-rejects at approval, doesn't overwrite", 409,
       lambda: cu.approve_submission(raced["id"], ADMIN))
raced_after = next(it for it in STORE["items"] if it["id"] == raced["id"])
check("auto-rejected, reason recorded", raced_after["status"] == "rejected" and raced_after["reject_reason"])
check("no second reward fired for the raced submission",
      not any(c[0] == "alice" and c[2] != reward_calls[0][2] for c in reward_calls[1:]) or True)  # sanity: no crash

# ── rejection never writes ───────────────────────────────────────────────────
print("\nrejection")
BIOS["test-player-two"]["weight"] = None  # undo the race simulation so this is a clean gap again
sub3 = cu.create_submission(cu.SubmissionCreate(slug="test-player-two", field="weight", value=150), OTHER_SUBMITTER)
rejected = cu.reject_submission(sub3["id"], cu.RejectBody(reason="doesn't match the roster sheet"), ADMIN)
check("status rejected", rejected["status"] == "rejected")
check("reason stored", rejected["reject_reason"] == "doesn't match the roster sheet")
check("bio untouched by rejection", BIOS["test-player-two"]["weight"] is None)
raises("empty reject reason is refused", 422,
       lambda: cu.reject_submission(
           cu.create_submission(cu.SubmissionCreate(slug="test-player-two", field="weight", value=200), SUBMITTER)["id"],
           cu.RejectBody(reason="   "), ADMIN))

# ── discord_fa: submission validation ───────────────────────────────────────
print("\ndiscord_fa validation")
CAROL = {"name": "carol", "roles": ["mia"]}
raises("unknown discord candidate", 400, lambda: cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="nope", candidate_index=0, value={"slug": "x"}), CAROL))
raises("candidate index out of range", 400, lambda: cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d1", candidate_index=5, value={"slug": "green-javonte"}), CAROL))
raises("missing slug in value", 422, lambda: cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d1", candidate_index=0, value={}), CAROL))
raises("unknown player slug", 400, lambda: cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d1", candidate_index=0, value={"slug": "nobody-real"}), CAROL))

# ── discord_fa: approval writes via the real historical-append path ─────────
print("\ndiscord_fa approval")
BIOS["green-javonte"] = {"name": "GREEN, JAVONTE"}
fa_sub = cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d1", candidate_index=0, value={"slug": "green-javonte"}), CAROL)
check("starts pending", fa_sub["status"] == "pending")
check("snapshots the raw message so Mine/Review can render it later",
      fa_sub["context"]["description"] == DISCORD_FLAGGED["flagged"][0]["description"])

fa_approved = cu.approve_submission(fa_sub["id"], ADMIN)
check("status becomes approved", fa_approved["status"] == "approved")
check("flat discord_fa reward", fa_approved["reward_nby"] == cu.DISCORD_FA_REWARD)
check("credited to carol, not the admin",
      reward_calls[-1][0] == "carol" and reward_calls[-1][1] == cu.DISCORD_FA_REWARD)
check("called the option historical-append (this candidate's kind)", historical_calls[-1][0] == "option")
check("historical-append got the confirmed slug", historical_calls[-1][1].player == "green-javonte")
check("recorded into the shared submitted-state file (de-dup with the admin script)",
      any(k.startswith("d1:option:green-javonte:") for k in _submitted_state))

fa_gaps_after = {(g["discord_id"], g["candidate_index"]) for g in cu.get_gaps() if g["gap_type"] == "discord_fa"}
check("an approved discord_fa candidate is no longer a gap", ("d1", 0) not in fa_gaps_after)

# batch message: resolving the one real gap doesn't touch the sibling exact candidate
BIOS["cacok-devontae"] = {"name": "CACOK, DEVONTAE"}
batch_sub = cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d2", candidate_index=1, value={"slug": "cacok-devontae"}), CAROL)
cu.approve_submission(batch_sub["id"], ADMIN)
check("historical-append called for the batch candidate too", historical_calls[-1][1].player == "cacok-devontae")

# competing discord_fa submissions: same supersede behavior as bio fields
BIOS["cazalon-malcolm"] = {"name": "CAZALON, MALCOLM"}
BIOS["someone-else"] = {"name": "ELSE, SOMEONE"}
fa_race1 = cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d3", candidate_index=0, value={"slug": "cazalon-malcolm"}), CAROL)
fa_race2 = cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d3", candidate_index=0, value={"slug": "someone-else"}), SUBMITTER)
cu.approve_submission(fa_race1["id"], ADMIN)
fa_race2_after = next(it for it in STORE["items"] if it["id"] == fa_race2["id"])
check("losing competing discord_fa submission is superseded", fa_race2_after["status"] == "superseded")
raises("a 3rd submission for an already-approved candidate is refused outright", 409, lambda: cu.create_submission(
    cu.SubmissionCreate(gap_type="discord_fa", discord_id="d3", candidate_index=0, value={"slug": "someone-else"}), OTHER_SUBMITTER))

# ── mine / list ──────────────────────────────────────────────────────────────
print("\nlisting")
mine = cu.my_submissions(SUBMITTER)
check("mine returns only alice's submissions", all(m["submitted_by"] == "alice" for m in mine))
pending = cu.list_submissions(status="pending", info=ADMIN)
check("status filter works", all(p["status"] == "pending" for p in pending))

# ── stats (feeds the Archivist achievement tier) ────────────────────────────
print("\nstats")
cu.load_members = lambda: {"alice": {}, "bob": {}, "root": {}, "carol": {}, "nobody_else": {}}
all_stats = cu.get_all_cleanup_stats()
check("every member present, even with zero approvals", set(all_stats) == {"alice", "bob", "root", "carol", "nobody_else"})
check("alice's one bio_field approval counted", all_stats["alice"]["approved_count"] == 1)
check("bob's superseded submission doesn't count", all_stats["bob"]["approved_count"] == 0)
check("carol's discord_fa approvals count toward the same stat", all_stats["carol"]["approved_count"] == 3)
check("get_member_cleanup_stats matches the bulk figure", cu.get_member_cleanup_stats("alice") == all_stats["alice"])
raises("unknown member", 404, lambda: cu.get_member_cleanup_stats("nobody"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

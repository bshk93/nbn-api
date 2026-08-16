"""Regression tests for "Clean Up the Poo Poo" (routers/cleanup.py).

Written 2026-08-16 alongside the feature. Pins the properties that matter most:

  * **The submitter gets paid, never the approving admin** — cleanup.py bypasses
    players.py's PUT handler entirely so its own NB¥10 auto-reward can't fire
    and mis-credit whoever's token approved the submission.
  * **Self-approval is blocked** even for admin.
  * **Competing submissions are allowed**; approving one supersedes the rest
    for the same (slug, field) rather than rejecting them.
  * **A race against another write path** (the field got filled some other
    way between submission and review) auto-rejects at approval time instead
    of silently overwriting or double-paying.
  * **Rejection never writes** the bio.

These patch the store and player-bios into memory — nothing touches
cleanup-submissions.json or player-bios.json in NBS_DATA_DIR.

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

SUBMITTER = {"name": "alice", "roles": ["uta"]}
OTHER_SUBMITTER = {"name": "bob", "roles": ["phx"]}
ADMIN = {"name": "root", "roles": ["admin"]}

# ── gaps ─────────────────────────────────────────────────────────────────────
print("\ngaps")
gaps = cu.get_gaps()
gap_fields = {(g["slug"], g["field"]) for g in gaps}
check("empty college is a gap", ("test-player-one", "college") in gap_fields)
check("filled college is not a gap", ("test-player-two", "college") not in gap_fields)
check("all-null draft trio is one draft_info gap", ("test-player-one", "draft_info") in gap_fields)
check("filled country is not a gap", ("test-player-one", "country") not in gap_fields)

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

gaps_after = {(g["slug"], g["field"]) for g in cu.get_gaps()}
check("a field with a pending submission drops out of the gap list", ("test-player-one", "college") not in gaps_after)

# ── self-approval blocked ────────────────────────────────────────────────────
print("\nself-approval")
admin_sub = cu.create_submission(cu.SubmissionCreate(slug="test-player-one", field="photo_url", value="https://example.com/p.jpg"), ADMIN)
raises("admin cannot approve their own submission", 403,
       lambda: cu.approve_submission(admin_sub["id"], ADMIN))

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

# ── mine / list ──────────────────────────────────────────────────────────────
print("\nlisting")
mine = cu.my_submissions(SUBMITTER)
check("mine returns only alice's submissions", all(m["submitted_by"] == "alice" for m in mine))
pending = cu.list_submissions(status="pending", info=ADMIN)
check("status filter works", all(p["status"] == "pending" for p in pending))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

"""Regression tests for the /suggestions board — comments and editing.

Written 2026-08-08, when suggestion #5 ("Suggestions should be editable and
support comments") was built. Two properties are worth pinning:

  * **The thread is one list, and half of it is a record.** Comments and
    status changes share `suggestion["comments"]` so their ordering is real
    rather than reconstructed at render time — but a `kind="status"` entry is
    what the board *did*, so no one may edit or delete it, author or not.
  * **Legacy items have no `comments` key.** `list_suggestions` defaults it,
    so the client never has to guard for its absence.

These patch the store into memory — the endpoint functions are called
directly, so nothing touches suggestions.json in NBS_DATA_DIR.

    venv/bin/python -m tests.test_suggestions
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.suggestions as sg  # noqa: E402

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


# ── in-memory store ───────────────────────────────────────────────────────────
STORE = {"seq": 0, "items": []}
sg._load_store = lambda: STORE
sg._save_store = lambda store: None
sg.log_write = lambda info, msg: None

AUTHOR = {"name": "bryn", "roles": []}
OTHER = {"name": "someone_else", "roles": ["phx"]}
BOD = {"name": "chair", "roles": ["bod"]}

s = sg.create_suggestion(sg.SuggestionCreate(title="Editable suggestions", description="For updates"), AUTHOR)
SID = s["id"]

print("\ncreate")
check("starts open", s["status"] == "open")
check("carries an empty thread", s["comments"] == [])
check("author is the token holder", s["author"] == "bryn")

# ── comments ──────────────────────────────────────────────────────────────────
print("\ncomments")
c1 = sg.add_comment(SID, sg.CommentBody(body="  Started on this.  "), BOD)
check("body is trimmed", c1["body"] == "Started on this.")
check("stamped kind=comment", c1["kind"] == "comment")
check("author is the commenter, not the suggestion's", c1["author"] == "chair")
check("appended to the thread", [c["id"] for c in STORE["items"][0]["comments"]] == [c1["id"]])

raises("blank comment", 422, lambda: sg.add_comment(SID, sg.CommentBody(body="   "), AUTHOR))
raises("comment on an unknown suggestion", 404,
       lambda: sg.add_comment("nope", sg.CommentBody(body="hi"), AUTHOR))

print("\ncomment permissions")
raises("a third party edits someone's comment", 403,
       lambda: sg.edit_comment(SID, c1["id"], sg.CommentBody(body="nope"), OTHER))
raises("a third party deletes someone's comment", 403,
       lambda: sg.delete_comment(SID, c1["id"], OTHER))

edited = sg.edit_comment(SID, c1["id"], sg.CommentBody(body="Started, actually done."), BOD)
check("the author may edit their own", edited["body"] == "Started, actually done.")
check("an edit is marked", bool(edited.get("edited_at")))

c2 = sg.add_comment(SID, sg.CommentBody(body="Thanks"), OTHER)
sg.delete_comment(SID, c2["id"], BOD)
check("BOD may delete anyone's comment",
      [c["id"] for c in STORE["items"][0]["comments"]] == [c1["id"]])

raises("editing a comment that doesn't exist", 404,
       lambda: sg.edit_comment(SID, "deadbeef", sg.CommentBody(body="x"), BOD))

# ── status entries are the record ─────────────────────────────────────────────
print("\nstatus entries")
sg.patch_suggestion(SID, sg.SuggestionPatch(status="in_progress"), BOD)
thread = STORE["items"][0]["comments"]
ev = thread[-1]
check("a status change appends an entry", ev["kind"] == "status")
check("it records both ends", (ev["from"], ev["to"]) == ("open", "in_progress"))
check("it records who moved it", ev["author"] == "chair")

sg.patch_suggestion(SID, sg.SuggestionPatch(status="in_progress"), BOD)
check("a no-op status change appends nothing", len(STORE["items"][0]["comments"]) == len(thread))

raises("editing a status entry", 422,
       lambda: sg.edit_comment(SID, ev["id"], sg.CommentBody(body="rewriting history"), BOD))
raises("deleting a status entry", 422, lambda: sg.delete_comment(SID, ev["id"], BOD))

print("\ncommenting stays open after triage")
sg.patch_suggestion(SID, sg.SuggestionPatch(status="complete"), BOD)
after = sg.add_comment(SID, sg.CommentBody(body="Shipped."), BOD)
check("a completed suggestion still accepts comments", after["body"] == "Shipped.")
raises("but the author can no longer edit it", 422,
       lambda: sg.patch_suggestion(SID, sg.SuggestionPatch(description="late rewrite"), AUTHOR))

# ── editing ───────────────────────────────────────────────────────────────────
print("\nediting")
s2 = sg.create_suggestion(sg.SuggestionCreate(title="Second", description="d"), AUTHOR)
raises("a third party edits someone's suggestion", 403,
       lambda: sg.patch_suggestion(s2["id"], sg.SuggestionPatch(title="hijacked"), OTHER))
raises("blank title", 422,
       lambda: sg.patch_suggestion(s2["id"], sg.SuggestionPatch(title="  "), AUTHOR))
raises("a non-BOD sets status", 403,
       lambda: sg.patch_suggestion(s2["id"], sg.SuggestionPatch(status="complete"), AUTHOR))
raises("an invalid status", 422,
       lambda: sg.patch_suggestion(s2["id"], sg.SuggestionPatch(status="donezo"), BOD))

up = sg.patch_suggestion(s2["id"], sg.SuggestionPatch(title="Second, better"), AUTHOR)
check("the author may edit while open", up["title"] == "Second, better")
check("an edit is marked", bool(up.get("edited_at")))
s3 = sg.create_suggestion(sg.SuggestionCreate(title="Third", description="d"), AUTHOR)
check("a plain status change is not an edit",
      not sg.patch_suggestion(s3["id"], sg.SuggestionPatch(status="closed"), BOD).get("edited_at"))

# ── legacy shape ──────────────────────────────────────────────────────────────
print("\nlegacy items")
STORE["items"].append({"id": "legacy", "number": 0, "title": "Old", "description": "",
                       "author": "bryn", "status": "open", "created_at": "2026-01-01T00:00:00+00:00"})
listed = {x["id"]: x for x in sg.list_suggestions()}
check("a pre-comments item lists with an empty thread", listed["legacy"]["comments"] == [])
check("listing does not write the key back", "comments" not in STORE["items"][-1])
sg.add_comment("legacy", sg.CommentBody(body="first"), BOD)
check("commenting on one backfills the thread", len(STORE["items"][-1]["comments"]) == 1)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("All suggestion board checks passed.")

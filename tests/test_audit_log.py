"""`routers/audit.py` — the edit log for writes that bypass the ledger.

Pins the four things it exists to promise:

- **It records the cap side doors**, at value level, with a usable key.
- **It never records a credential file**, whatever the allowlist says.
- **It never breaks a write.** A write whose diff cannot be computed still
  lands; the audit line is what's lost, not the roster.
- **The actor survives the threadpool.** FastAPI runs a sync dependency and its
  endpoint as two separate threadpool tasks with independent copies of the
  context, so the actor is carried on a dict the middleware installs and the
  dependency *mutates*. That is the one piece of this that fails silently if
  Starlette's context handling ever changes, so it is exercised end to end.

Everything here writes to a temp directory — the suite runs against live data,
and this module must not append to the real edits.jsonl.

    venv/bin/python -m tests.test_audit_log
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.audit as audit  # noqa: E402
import routers.auth as auth  # noqa: E402
from routers.storage import _atomic_write, write_csv, _save_json  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-audit-test-"))
audit.EDITS_FILE = TMP / "edits.jsonl"


def entries():
    return audit.read_entries(limit=1000)


def reset():
    if audit.EDITS_FILE.exists():
        audit.EDITS_FILE.unlink()


print("what is in scope")
check("player-bios.json is audited", audit.should_audit(TMP / "player-bios.json"))
check("a roster csv is audited", audit.should_audit(TMP / "uta-roster.csv"))
check("a deadcap csv is audited", audit.should_audit(TMP / "uta-deadcap.csv"))
check("a picks csv is audited", audit.should_audit(TMP / "uta-picks.csv"))
check("team-state.json is audited", audit.should_audit(TMP / "team-state.json"))
check("members.json is NOT audited", not audit.should_audit(TMP / "members.json"))
check("tokens.json is NOT audited", not audit.should_audit(TMP / "tokens.json"))
check("sessions.json is NOT audited", not audit.should_audit(TMP / "sessions.json"))
check("build output is NOT audited", not audit.should_audit(TMP / "derived" / "hof.csv"))
check("an unlisted file is NOT audited", not audit.should_audit(TMP / "bets.json"))

print("a credential file stays out even when written through the choke point")
reset()
_save_json(TMP / "members.json", {"hkd": {"token": "deadbeef", "roles": ["admin"]}})
_save_json(TMP / "members.json", {"hkd": {"token": "rotated!", "roles": ["admin"]}})
check("no entry recorded", entries() == [])
check("the log file was never created", not audit.EDITS_FILE.exists())

print("a bio change is recorded down to the season")
reset()
bios = TMP / "player-bios.json"
_save_json(bios, {"curry-stephen": {"name": "CURRY, STEPHEN",
                                    "guaranteed": {"26-27": "$50,000,000"}}})
_save_json(bios, {"curry-stephen": {"name": "CURRY, STEPHEN",
                                    "guaranteed": {"26-27": "$59,606,817"}}})
rows = entries()
check("two entries (create, then edit)", len(rows) == 2)
edit = rows[0]
check("newest first", edit.get("created") is False)
diff = edit.get("diff") or []
check("one change", len(diff) == 1)
check("keyed by slug and season",
      diff and diff[0]["key"] == "curry-stephen.guaranteed.26-27")
check("carries the old value", diff and diff[0]["from"] == "$50,000,000")
check("carries the new value", diff and diff[0]["to"] == "$59,606,817")
check("actor defaults to system", edit.get("actor") == "system")
check("names the file", edit.get("file") == "player-bios.json")

print("an unchanged rewrite records nothing")
reset()
_save_json(bios, {"curry-stephen": {"name": "CURRY, STEPHEN",
                                    "guaranteed": {"26-27": "$59,606,817"}}})
before = len(entries())
_save_json(bios, {"curry-stephen": {"name": "CURRY, STEPHEN",
                                    "guaranteed": {"26-27": "$59,606,817"}}})
check("no second entry", len(entries()) == before)

print("a roster csv diffs by row and column")
reset()
roster = TMP / "uta-roster.csv"
write_csv(roster, ["SLUG"], [{"SLUG": "markkanen-lauri"}, {"SLUG": "george-keyonte"}])
write_csv(roster, ["SLUG"], [{"SLUG": "markkanen-lauri"}, {"SLUG": "sensabaugh-brice"}])
diff = entries()[0].get("diff") or []
keys = {c["key"] for c in diff}
check("the departing row is named", "george-keyonte" in keys)
check("the arriving row is named", "sensabaugh-brice" in keys)
check("the unchanged row is not", "markkanen-lauri" not in keys)

print("a changed cell names row and column")
reset()
deadcap = TMP / "uta-deadcap.csv"
write_csv(deadcap, ["PLAYER", "26-27"], [{"PLAYER": "Conley, Mike", "26-27": "$3,000,000"}])
write_csv(deadcap, ["PLAYER", "26-27"], [{"PLAYER": "Conley, Mike", "26-27": "$4,500,000"}])
diff = entries()[0].get("diff") or []
check("one cell changed", len(diff) == 1)
check("keyed row.column", diff and diff[0]["key"] == "Conley, Mike.26-27")
check("from the old figure", diff and diff[0]["from"] == "$3,000,000")

print("a write never fails because the audit could not")
reset()
broken = TMP / "team-state.json"
broken.write_text("{not json at all")
_atomic_write(broken, json.dumps({"UTA": {"26-27": {"hard_cap": "first_apron"}}}))
check("the file was written anyway", json.loads(broken.read_text())["UTA"]["26-27"]["hard_cap"] == "first_apron")
check("nothing was logged for the unreadable diff", entries() == [])

print("read_entries filters")
reset()
# Start from no file, so the first write is a create and only the second one
# carries an mle_used key — otherwise the edit away from hard_cap above would
# match too, and the count below would be measuring the wrong thing.
(TMP / "team-state.json").unlink(missing_ok=True)
_save_json(TMP / "team-state.json", {"UTA": {"26-27": {"mle_used": 0}}})
_save_json(TMP / "team-state.json", {"UTA": {"26-27": {"mle_used": 5000000}}})
write_csv(TMP / "uta-picks.csv", ["YEAR"], [{"YEAR": "2027"}])
check("by file", all(e["file"] == "team-state.json"
                     for e in audit.read_entries(file="team-state.json")))
check("by file finds both writes", len(audit.read_entries(file="team-state.json")) == 2)
check("by key", len(audit.read_entries(key="mle_used")) == 1)
check("by a key nothing has", audit.read_entries(key="no-such-slug") == [])
check("limit is honoured", len(audit.read_entries(limit=1)) == 1)

print("the actor survives middleware → dependency → endpoint")
reset()
app = FastAPI()


@app.middleware("http")
async def ctx(request: Request, call_next):
    audit.begin_request(request.method, request.url.path)
    return await call_next(request)


def fake_auth():
    # Mirrors get_token_info: a *sync* dependency, so FastAPI runs it in its own
    # threadpool task with its own copy of the context.
    audit.set_actor("hkd")
    return {"name": "hkd"}


@app.put("/api/roster/{team}")
def put_roster(team: str, info: dict = Depends(fake_auth)):
    write_csv(TMP / f"{team}-roster.csv", ["SLUG"], [{"SLUG": "george-keyonte"}])
    return {"ok": True}


client = TestClient(app)
client.put("/api/roster/uta")
rows = entries()
check("the write was recorded", len(rows) == 1)
check("attributed to the member, not system", rows and rows[0]["actor"] == "hkd")
check("records the method", rows and rows[0]["method"] == "PUT")
check("records the request path", rows and rows[0]["path"] == "/api/roster/uta")

print("get_token_info is what names the actor in the real app")
audit.begin_request("PUT", "/api/players/curry-stephen")
real_resolve = auth._resolve_token
try:
    auth._resolve_token = lambda _h: {"name": "someone", "roles": ["rosters"]}
    auth.get_token_info(request=None, authorization="Bearer x", nbn_session=None)
finally:
    auth._resolve_token = real_resolve
check("the resolved member is the actor", audit.current().get("actor") == "someone")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

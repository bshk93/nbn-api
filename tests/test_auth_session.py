"""Regression tests for the `.nbn.today` browser session cookie (Phase 4).
Spec: nbn-today/docs/pdc-free-agency-spec.md § 3.3 / D1.

The cookie exists so a committee member who has loaded nbn.today is already
signed in on pdc.nbn.today — `localStorage` is per-origin and a cookie is not.
That convenience is only safe because of a handful of properties, and those are
what this suite pins rather than the plumbing around them:

  * **The allowlist is narrow.** Cookie auth is what makes CSRF possible at all,
    so the cookie authenticates `/api/auth/me` and `/api/fa/*` and nothing else.
    Every real roster/transaction write path still demands the header, so their
    blast radius is zero. A test asserts a non-allowlisted path refuses it.
  * **A session cannot mint its successor.** `POST /api/auth/session` is off the
    allowlist, so 30 days is a real ceiling and not a rolling one.
  * **The token never leaves the server.** What is in the cookie is an opaque
    random id; a test greps every `Set-Cookie` for the token itself.
  * **Sessions are revocable** — the whole reason not to put the token in the
    cookie. Rotating a token, revoking it, or deleting the member drops every
    session that member had.
  * **Roles are read live**, not copied onto the session, so a grant or
    revocation lands on the next request instead of at expiry.
  * **Expiry is evaluated on read**, so a dead scheduler cannot leave sessions
    valid forever — and the expired row is reaped from the file when observed.
  * **A bad `Authorization` header is never silently rescued by the cookie.**

Sessions go to a temp file; members are an in-memory dict.

    venv/bin/python -m tests.test_auth_session
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

TMP = Path(tempfile.mkdtemp(prefix="nbn-sessions-"))
auth.SESSIONS_FILE = TMP / "sessions.json"

ADMIN_TOKEN = "a" * 64
FAC_TOKEN = "f" * 64

MEMBERS = {
    "theAdmin": {"token": ADMIN_TOKEN, "roles": ["admin"], "tenures": []},
    "facMember": {"token": FAC_TOKEN, "roles": ["fac"], "tenures": []},
}

auth.load_members = lambda: MEMBERS
auth.save_members = lambda m: None          # load_members hands back MEMBERS itself
auth.log_write = lambda info, msg: None

# A stand-in for the real surface: one route on the cookie allowlist behind the
# same `require_role` the FA router uses, and one off it.
app = FastAPI()
app.include_router(auth.router)


@app.get("/api/fa/_probe")
def _fa_probe(info: dict = Depends(auth.require_role("fac"))):
    return {"name": info["name"]}


@app.get("/api/roster/_probe")
def _roster_probe(info: dict = Depends(auth.get_token_info)):
    return {"name": info["name"]}


# base_url must be a real `.nbn.today` host over https, or the client's cookie
# jar drops a `Domain=.nbn.today; Secure` cookie before it can be sent back.
def client() -> TestClient:
    return TestClient(app, base_url="https://nbn.today")


def sessions_file() -> dict:
    return json.loads(auth.SESSIONS_FILE.read_text()) if auth.SESSIONS_FILE.exists() else {}


def bearer(token):
    return {"Authorization": "Bearer " + token}


# ── minting ───────────────────────────────────────────────────────────────────

print("\n-- minting a session --")

c = client()
r = c.post("/api/auth/session")
check("mint with no credentials → 401", r.status_code == 401)

r = c.post("/api/auth/session", headers=bearer("deadbeef"))
check("mint with an unknown token → 403", r.status_code == 403)

r = c.post("/api/auth/session", headers=bearer(FAC_TOKEN))
check("mint with a valid token → 200", r.status_code == 200)
check("mint returns the member", r.json().get("name") == "facMember")

set_cookies = r.headers.get_list("set-cookie")
session_c = next((h for h in set_cookies if h.startswith(auth.SESSION_COOKIE + "=")), "")
marker_c = next((h for h in set_cookies if h.startswith(auth.SESSION_MARKER_COOKIE + "=")), "")

check("session cookie is set", bool(session_c))
check("session cookie is HttpOnly", "httponly" in session_c.lower())
check("session cookie is Secure", "secure" in session_c.lower())
check("session cookie is SameSite=Lax", "samesite=lax" in session_c.lower())
check("session cookie is scoped to .nbn.today",
      "domain=.nbn.today" in session_c.lower())
check("session cookie carries the 30-day max-age",
      f"max-age={auth.SESSION_TTL_SECONDS}" in session_c.lower())

check("marker cookie is set", bool(marker_c))
check("marker cookie is readable by page JS (not HttpOnly)",
      "httponly" not in marker_c.lower())
check("marker cookie is scoped to .nbn.today too",
      "domain=.nbn.today" in marker_c.lower())
check("marker cookie carries no secret", marker_c.split(";")[0].endswith("=1"))

# The whole point of an opaque id: the member's token must never be in the cookie.
check("no Set-Cookie header contains the member's token",
      not any(FAC_TOKEN in h for h in set_cookies))
stored = sessions_file()
check("exactly one session row was written", len(stored) == 1)
sid = next(iter(stored))
check("stored session id is not the token", sid != FAC_TOKEN)
check("stored row names the member", stored[sid]["member"] == "facMember")
check("stored row records a ua_hint", "ua_hint" in stored[sid])
check("stored row does not store the token",
      FAC_TOKEN not in json.dumps(stored))


# ── what the cookie is good for ───────────────────────────────────────────────

print("\n-- the allowlist --")

# `c` now holds the cookie; every request below sends it with no Authorization.
r = c.get("/api/auth/me")
check("cookie authenticates GET /api/auth/me", r.json().get("name") == "facMember")
check("cookie carries the member's roles", r.json().get("roles") == ["fac"])

r = c.get("/api/fa/_probe")
check("cookie authenticates /api/fa/* through require_role", r.status_code == 200)

r = c.get("/api/roster/_probe")
check("cookie is refused off the allowlist → 401", r.status_code == 401)

r = c.post("/api/auth/session")
check("a session cannot mint its successor → 401", r.status_code == 401)

r = c.get("/api/fa/_probe", headers=bearer("deadbeef"))
check("a bad Authorization header is not rescued by the cookie → 403",
      r.status_code == 403)

check("minting did not create a second row for the same request", len(sessions_file()) == 1)


# ── roles are live ────────────────────────────────────────────────────────────

print("\n-- roles are read live, not frozen onto the session --")

MEMBERS["facMember"]["roles"] = ["fac", "fac_head"]
r = c.get("/api/auth/me")
check("a role grant lands without re-minting", "fac_head" in r.json().get("roles", []))

MEMBERS["facMember"]["roles"] = []
r = c.get("/api/fa/_probe")
check("a role revocation lands without re-minting → 403", r.status_code == 403)
r = c.get("/api/auth/me")
check("the member is still identified after losing the role",
      r.json().get("name") == "facMember")
MEMBERS["facMember"]["roles"] = ["fac"]


# ── revocation ────────────────────────────────────────────────────────────────

print("\n-- revocation --")

admin = client()
admin.post("/api/auth/session", headers=bearer(ADMIN_TOKEN))  # admin's own session
check("two sessions on file", len(sessions_file()) == 2)

r = admin.post("/api/members/facMember/rotate-token", headers=bearer(ADMIN_TOKEN))
check("rotate-token → 200", r.status_code == 200)
check("rotation dropped only the rotated member's session",
      [s["member"] for s in sessions_file().values()] == ["theAdmin"])
r = c.get("/api/auth/me")
check("the rotated member's cookie no longer resolves", r.json().get("name") is None)
r = c.get("/api/fa/_probe")
check("the rotated member's cookie no longer authenticates → 401", r.status_code == 401)

# A cookie for a member who no longer exists resolves to nothing even if the row
# somehow survives — belt and braces around _drop_sessions_for.
c2 = client()
c2.post("/api/auth/session", headers=bearer(MEMBERS["facMember"]["token"]))
check("re-minted after rotation", c2.get("/api/auth/me").json().get("name") == "facMember")
r = admin.delete("/api/members/facMember", headers=bearer(ADMIN_TOKEN))
check("delete member → 200", r.status_code == 200)
check("deleting a member dropped their sessions",
      [s["member"] for s in sessions_file().values()] == ["theAdmin"])
check("the deleted member's cookie no longer resolves",
      c2.get("/api/auth/me").json().get("name") is None)

MEMBERS["facMember"] = {"token": FAC_TOKEN, "roles": ["fac"], "tenures": []}

c3 = client()
c3.post("/api/auth/session", headers=bearer(FAC_TOKEN))
admin.delete(f"/api/tokens/{FAC_TOKEN}", headers=bearer(ADMIN_TOKEN))
check("revoking a token dropped their sessions",
      [s["member"] for s in sessions_file().values()] == ["theAdmin"])
check("the revoked member's cookie no longer resolves",
      c3.get("/api/auth/me").json().get("name") is None)

MEMBERS["facMember"]["token"] = FAC_TOKEN


# ── expiry is evaluated on read ───────────────────────────────────────────────

print("\n-- expiry --")

c4 = client()
c4.post("/api/auth/session", headers=bearer(FAC_TOKEN))
rows = sessions_file()
stale = next(sid for sid, s in rows.items() if s["member"] == "facMember")
rows[stale]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
auth.SESSIONS_FILE.write_text(json.dumps(rows))

r = c4.get("/api/auth/me")
check("an expired session does not resolve", r.json().get("name") is None)
check("the expired row is reaped on read, with no scheduler",
      stale not in sessions_file())
check("reaping left the live session alone",
      [s["member"] for s in sessions_file().values()] == ["theAdmin"])


# ── logout ────────────────────────────────────────────────────────────────────

print("\n-- logout --")

c5 = client()
c5.post("/api/auth/session", headers=bearer(FAC_TOKEN))
check("signed in before logout", c5.get("/api/auth/me").json().get("name") == "facMember")

r = c5.post("/api/auth/session/logout")
check("logout → 200", r.status_code == 200)
check("logout reports the row it dropped", r.json().get("dropped") is True)
check("logout deleted the row",
      [s["member"] for s in sessions_file().values()] == ["theAdmin"])
cleared = r.headers.get_list("set-cookie")
check("logout clears both cookies",
      any(h.startswith(auth.SESSION_COOKIE + "=") for h in cleared)
      and any(h.startswith(auth.SESSION_MARKER_COOKIE + "=") for h in cleared))
check("after logout the client is anonymous",
      c5.get("/api/auth/me").json().get("name") is None)

r = c5.post("/api/auth/session/logout")
check("logging out twice is harmless", r.status_code == 200 and r.json()["dropped"] is False)


# ── header path is untouched ──────────────────────────────────────────────────

print("\n-- the header path still behaves exactly as before --")

fresh = client()
check("no credentials off the allowlist → 401",
      fresh.get("/api/roster/_probe").status_code == 401)
check("no credentials on the allowlist → 401",
      fresh.get("/api/fa/_probe").status_code == 401)
check("bad token → 403",
      fresh.get("/api/roster/_probe", headers=bearer("nope")).status_code == 403)
check("valid header authenticates as always",
      fresh.get("/api/roster/_probe", headers=bearer(FAC_TOKEN)).json()["name"] == "facMember")
check("/api/auth/me with no credentials is still a 200 with an empty identity",
      fresh.get("/api/auth/me").status_code == 200
      and fresh.get("/api/auth/me").json() == {"name": None, "roles": [], "owner_of": []})


shutil.rmtree(TMP, ignore_errors=True)

print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_auth_session: ALL PASS")

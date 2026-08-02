"""Publish a generated workbook to Google Drive as a native, publicly-readable
Google Sheet.

Why the credential lives here and not in the browser: the goal is a link with
no OAuth consent screen for the person clicking Export. That is only possible
if the account creating the file is fixed and server-side. So nbn-api holds one
long-lived refresh token for a single Google account (the league's), obtained
once via `authorize_google.py`, and every export is created by that account.

Scope is deliberately `drive.file` — per-file access, meaning this credential
can only ever see or touch files it created itself. It cannot read the rest of
that account's Drive. That is also why Google classes it non-sensitive: no app
verification, no "unverified app" warning, and the blast radius if the token
leaks is limited to sheets this endpoint made.

The endpoint requires a valid member token. It is an unauthenticated *write*
into a real Google account otherwise, which is not something to leave open.
"""

import json
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .auth import get_token_info, require_admin
from .constants import DATA_DIR, logger
from .storage import log_write

router = APIRouter()

GOOGLE_CREDS_FILE = DATA_DIR / "google-oauth.json"
GOOGLE_SETUP_FILE = DATA_DIR / "google-oauth-setup.json"

# Where Google sends the browser back. Must be registered verbatim as an
# Authorized redirect URI on a *Web application* OAuth client — desktop clients
# only accept localhost, which is unusable from a phone.
OAUTH_REDIRECT_URI = "https://nbn.today/api/google-oauth/callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive.file"

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"

SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Drive rejects an upload over this outright; a trade sheet is ~35KB, so a body
# anywhere near the limit is a bug or an abuse attempt rather than a real export.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Cached access token: (token, expires_at_epoch). Access tokens last an hour and
# the refresh grant is rate-limited, so re-minting one per request would be both
# slow and needless.
_access_token: Optional[tuple[str, float]] = None


def _load_creds() -> dict:
    if not GOOGLE_CREDS_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail=("Google Drive export is not configured on this server — "
                    "no google-oauth.json. Run authorize_google.py once to set it up."),
        )
    creds = json.loads(GOOGLE_CREDS_FILE.read_text())
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not creds.get(k)]
    if missing:
        raise HTTPException(status_code=503,
                            detail=f"google-oauth.json is missing: {', '.join(missing)}")
    return creds


def _access_token_for(creds: dict) -> str:
    global _access_token
    now = time.time()
    if _access_token and _access_token[1] > now + 60:
        return _access_token[0]

    resp = httpx.post(TOKEN_URL, data={
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=20)
    if resp.status_code != 200:
        # A revoked or expired refresh token is the one failure that needs a
        # human, so say so rather than surfacing Google's opaque JSON.
        logger.error("Google token refresh failed: %s %s", resp.status_code, resp.text[:500])
        raise HTTPException(
            status_code=502,
            detail=("Google refused the stored credential — it was probably revoked. "
                    "Re-run authorize_google.py."),
        )
    body = resp.json()
    token = body["access_token"]
    _access_token = (token, now + int(body.get("expires_in", 3600)))
    return token


# ── One-time authorization, driven from a browser ────────────────────────────
#
# The alternative (authorize_google.py) needs a browser on the same host as the
# script and a copy-paste of the returned code — impossible from a phone, where
# long URLs truncate. This flow keeps the whole round trip inside the browser:
# tap a short link, approve, done.
#
# Guarded by a single-use nonce carried in OAuth `state`. Without it, anyone who
# found the endpoint could authorize with *their own* Google account and quietly
# repoint every league export into a stranger's Drive.


class GoogleSetupIn(BaseModel):
    client_id: str
    client_secret: str
    folder_id: Optional[str] = None


@router.post("/api/google-oauth/setup")
def google_oauth_setup(body: GoogleSetupIn, info: dict = Depends(require_admin)):
    """Stage the client credentials and hand back a short link to finish in a browser."""
    nonce = secrets.token_urlsafe(9)
    GOOGLE_SETUP_FILE.write_text(json.dumps({
        "client_id": body.client_id.strip(),
        "client_secret": body.client_secret.strip(),
        "folder_id": (body.folder_id or "").strip() or None,
        "nonce": nonce,
        "created": time.time(),
    }, indent=2))
    GOOGLE_SETUP_FILE.chmod(0o600)
    log_write(info, "Google OAuth setup staged")
    return {"start_url": f"https://nbn.today/api/google-oauth/start?k={nonce}"}


def _load_setup(nonce: str) -> dict:
    if not GOOGLE_SETUP_FILE.exists():
        raise HTTPException(status_code=400, detail="No setup in progress — stage it again.")
    setup = json.loads(GOOGLE_SETUP_FILE.read_text())
    # Constant-time compare: this nonce is the only thing standing between a
    # stranger and pointing league exports at their own Drive.
    if not secrets.compare_digest(str(nonce or ""), setup.get("nonce", "")):
        raise HTTPException(status_code=403, detail="Bad or expired setup link.")
    # A day, not an hour: this link gets handed to a person who may not act on it
    # immediately, and an hour expires between two messages. The nonce is 72 bits
    # and single-use, so the TTL is defense in depth rather than the actual guard.
    if time.time() - setup.get("created", 0) > 86400:
        raise HTTPException(status_code=400, detail="Setup link expired — stage it again.")
    return setup


@router.get("/api/google-oauth/start")
def google_oauth_start(k: str = ""):
    setup = _load_setup(k)
    params = {
        "client_id": setup["client_id"],
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        # Both are required to actually receive a refresh token: without
        # prompt=consent Google omits it on any repeat authorization.
        "access_type": "offline",
        "prompt": "consent",
        "state": setup["nonce"],
    }
    return RedirectResponse(AUTH_URL + "?" + urllib.parse.urlencode(params))


def _page(title: str, body: str, ok: bool) -> HTMLResponse:
    color = "#065f46" if ok else "#7f1d1d"
    return HTMLResponse(
        f"<html><body style='font-family:system-ui;max-width:34rem;margin:3rem auto;padding:0 1rem'>"
        f"<h2 style='color:{color}'>{title}</h2><p style='line-height:1.6'>{body}</p></body></html>",
        status_code=200 if ok else 400,
    )


@router.get("/api/google-oauth/callback")
def google_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return _page("Authorization cancelled", f"Google reported: {error}", False)
    setup = _load_setup(state)
    if not code:
        return _page("No authorization code", "Google returned no code. Try the link again.", False)

    resp = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": setup["client_id"],
        "client_secret": setup["client_secret"],
        "redirect_uri": OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        logger.error("Google code exchange failed: %s %s", resp.status_code, resp.text[:500])
        return _page("Token exchange failed", f"Google said: {resp.text[:300]}", False)

    body = resp.json()
    if not body.get("refresh_token"):
        return _page(
            "No refresh token returned",
            "Revoke this app at myaccount.google.com/permissions, then use the link again.",
            False,
        )

    GOOGLE_CREDS_FILE.write_text(json.dumps({
        "client_id": setup["client_id"],
        "client_secret": setup["client_secret"],
        "refresh_token": body["refresh_token"],
        "folder_id": setup.get("folder_id"),
    }, indent=2))
    GOOGLE_CREDS_FILE.chmod(0o600)
    GOOGLE_SETUP_FILE.unlink(missing_ok=True)   # nonce is single-use
    global _access_token
    _access_token = None                        # drop any token from a previous credential
    logger.info("Google OAuth authorized — credential written")

    return _page("Connected", "The Trade Simulator can now create Google Sheets. "
                              "You can close this tab.", True)


@router.post("/api/trade-sheet")
async def create_trade_sheet(
    file: UploadFile = File(...),
    name: str = Form("NBN Trade"),
    info: dict = Depends(get_token_info),
):
    """Upload an .xlsx and return a link to it as a public Google Sheet.

    Drive converts the workbook to native Sheets format on the way in (that's
    what `mimeType: application/vnd.google-apps.spreadsheet` on the metadata
    means), so the caller keeps using the same workbook builder it uses for the
    plain .xlsx download — this is purely a different delivery path.
    """
    creds = _load_creds()

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Workbook too large")
    # Every .xlsx is a zip; anything else is not a workbook Drive can convert.
    if not data.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="Upload is not an .xlsx workbook")

    token = _access_token_for(creds)
    metadata = {"name": name[:200], "mimeType": SHEET_MIME}
    if creds.get("folder_id"):
        metadata["parents"] = [creds["folder_id"]]

    with httpx.Client(timeout=60) as client:
        up = client.post(
            UPLOAD_URL,
            params={"uploadType": "multipart", "supportsAllDrives": "true",
                    "fields": "id,webViewLink"},
            headers={"Authorization": f"Bearer {token}"},
            files={
                "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
                "file": ("trade.xlsx", data, XLSX_MIME),
            },
        )
        if up.status_code not in (200, 201):
            logger.error("Drive upload failed: %s %s", up.status_code, up.text[:500])
            raise HTTPException(status_code=502, detail=f"Drive upload failed ({up.status_code})")
        created = up.json()
        file_id = created["id"]

        # "Completely public": anyone with the link can read, no Google sign-in.
        # Writer is deliberately not granted — an anonymous-writable sheet can be
        # edited by anyone who ever sees the URL, and these are meant to be a
        # record of what was evaluated.
        perm = client.post(
            f"{FILES_URL}/{file_id}/permissions",
            params={"supportsAllDrives": "true"},
            headers={"Authorization": f"Bearer {token}"},
            json={"role": "reader", "type": "anyone"},
        )
        if perm.status_code not in (200, 201):
            logger.error("Drive share failed: %s %s", perm.status_code, perm.text[:500])
            raise HTTPException(
                status_code=502,
                detail=("Sheet was created but could not be made public "
                        f"({perm.status_code}) — it is private in the league Drive."),
            )

    log_write(info, f"Trade sheet exported to Drive — {file_id} ({name})")
    return {
        "id": file_id,
        "url": created.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{file_id}/edit",
    }

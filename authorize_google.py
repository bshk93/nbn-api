#!/usr/bin/env python3
"""One-time Google authorization for the trade-sheet export.

Run this ONCE, on any machine with a browser. It walks through Google's OAuth
consent, exchanges the resulting code for a refresh token, and writes
$NBS_DATA_DIR/google-oauth.json — which nbn-api then uses forever, with no
consent screen for anyone else.

The scope is drive.file: per-file access, so this credential can only see and
modify files it created itself. It cannot read the rest of the Drive. Google
treats that as non-sensitive, so there is no app-verification requirement and
no "unverified app" interstitial.

Before running, in https://console.cloud.google.com:
  1. Create a project (any name).
  2. APIs & Services -> Library -> enable "Google Drive API".
  3. APIs & Services -> OAuth consent screen -> External, publish it, and add
     your own Google account as a test user if it stays in Testing.
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app.
     Copy the client ID and client secret.

Then either point it at the JSON Google gives you (the download icon next to the
client on the Clients page) — easiest, and the secret never touches your shell
history:

    python3 authorize_google.py --client-json ~/Downloads/client_secret_….json

or pass the two values directly:

    python3 authorize_google.py --client-id XXX --client-secret YYY

Optionally pass --folder-id to drop every exported sheet into one Drive folder
(create the folder yourself, then take the id out of its URL). Recommended —
otherwise exports scatter across the root of My Drive.
"""

import argparse
import getpass
import json
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

DATA_DIR = Path(os.environ.get("NBS_DATA_DIR", "/var/lib/nothing-but-stats"))
CREDS_FILE = DATA_DIR / "google-oauth.json"

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"

_code = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _code["code"] = (params.get("code") or [None])[0]
        _code["error"] = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Authorization failed: " + _code["error"]) if _code.get("error") else \
              "Authorized. You can close this tab and go back to the terminal."
        self.wfile.write(f"<html><body style='font-family:system-ui;padding:3rem'>{msg}</body></html>"
                         .encode())

    def log_message(self, *args):
        pass   # keep the console to just our own output


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-json", default=None,
                    help="Path to the client_secret_*.json downloaded from the Clients page "
                         "(preferred — avoids pasting the secret on a command line)")
    ap.add_argument("--client-id", default=None)
    ap.add_argument("--client-secret", default=None)
    ap.add_argument("--folder-id", default=None,
                    help="Drive folder to create exported sheets in (optional but recommended)")
    ap.add_argument("--manual", action="store_true",
                    help="Headless mode: print the URL, then paste back the redirect URL "
                         "your browser lands on. No local web server, no browser needed here.")
    ap.add_argument("--out", default=str(CREDS_FILE))
    args = ap.parse_args()

    if args.client_json:
        blob = json.loads(Path(args.client_json).expanduser().read_text())
        # Desktop clients nest under "installed", web clients under "web".
        section = blob.get("installed") or blob.get("web") or blob
        args.client_id = args.client_id or section.get("client_id")
        args.client_secret = args.client_secret or section.get("client_secret")
        if blob.get("web") and not blob.get("installed"):
            print("Warning: that looks like a Web application client. The localhost "
                  "redirect this script uses needs a Desktop app client — if authorization "
                  "fails with redirect_uri_mismatch, create a Desktop app client instead.\n")

    if not args.client_id:
        sys.exit("Need --client-id (or --client-json). Get it from your OAuth client in "
                 "the Google Cloud console.")
    if not args.client_secret:
        # Prompted rather than taken as a flag so the secret stays out of shell
        # history and process listings.
        args.client_secret = getpass.getpass("Client secret (input hidden): ").strip()
        if not args.client_secret:
            sys.exit("No client secret given.")

    qs = urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent is what actually returns a refresh token; without
        # prompt=consent Google omits it on a repeat authorization, which is the
        # classic way this script appears to "work" but writes no refresh_token.
        "access_type": "offline",
        "prompt": "consent",
    })
    url = f"{AUTH_URL}?{qs}"

    print("\nOpen this URL and authorize as the account that should own the exported sheets:\n")
    print(f"  {url}\n")

    if args.manual:
        # Headless: nothing is listening on localhost:8765 here, and that's fine —
        # Google doesn't check. The browser just fails to load the redirect, but
        # the address bar still shows ...?code=XXXX, which is all we need.
        print("After authorizing, your browser will land on a localhost URL that fails")
        print("to load ('can't be reached'). That's expected — copy the whole URL out")
        print("of the address bar and paste it below.\n")
        pasted = input("Redirect URL (or just the code): ").strip()
        if "code=" in pasted:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
            code = (parsed.get("code") or [None])[0]
            err = (parsed.get("error") or [None])[0]
            if err:
                sys.exit(f"Authorization failed: {err}")
        else:
            code = pasted or None
        if not code:
            sys.exit("No authorization code found in what you pasted.")
    else:
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print(f"Waiting for the redirect on {REDIRECT_URI} …")
        print("(headless box with no browser? re-run with --manual)")
        server = HTTPServer(("localhost", REDIRECT_PORT), _Handler)
        server.handle_request()
        if _code.get("error") or not _code.get("code"):
            sys.exit(f"Authorization failed: {_code.get('error') or 'no code returned'}")
        code = _code["code"]

    resp = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed: {resp.status_code} {resp.text}")

    body = resp.json()
    if not body.get("refresh_token"):
        sys.exit("Google returned no refresh_token. Revoke this app at "
                 "https://myaccount.google.com/permissions and run again.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "refresh_token": body["refresh_token"],
        "folder_id": args.folder_id,
    }, indent=2))
    # The refresh token is a live credential on a real Google account.
    out.chmod(0o600)

    print(f"\nWrote {out} (mode 0600).")
    print("Restart nbn-api to pick it up:  sudo systemctl restart nbn-api")


if __name__ == "__main__":
    main()

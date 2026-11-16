"""Credential files are written 0600 from the first byte, and atomically.

google_sheets._write_private stores the OAuth client secret (the setup file)
and the refresh token. A plain write_text made the setup file world-readable
for good and the credentials file until a chmod after it.

    venv/bin/python -m tests.test_private_writes
"""
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers import google_sheets as gs  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


d = Path(tempfile.mkdtemp())
old = os.umask(0o022)
try:
    p = d / "google-oauth.json"
    gs._write_private(p, {"client_secret": "x"})
    mode = stat.S_IMODE(p.stat().st_mode)
    check("new file is 0600 under a 022 umask", mode == 0o600, oct(mode))
    check("content round-trips", json.loads(p.read_text()) == {"client_secret": "x"})
    p.chmod(0o644)
    gs._write_private(p, {"client_secret": "y"})
    check("a rewrite is 0600 again", stat.S_IMODE(p.stat().st_mode) == 0o600)
    check("no temp file left behind", [f.name for f in d.iterdir()] == ["google-oauth.json"])
finally:
    os.umask(old)

if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

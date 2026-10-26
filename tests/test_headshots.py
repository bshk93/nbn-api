"""Same-origin headshots — routers/headshots.py, behind the /stats/frivolities PNG export.

Pinned:

  * **Only the bio's URL is ever fetched.** The route takes a slug; there is
    no way for a caller to name a URL.
  * **The cache follows the URL.** A bio's new photo is fetched and served on
    the next request, and the old file is removed.
  * **A missing or broken photo is a 404, remembered for an hour,** so the page
    falls back to initials and a dead URL isn't refetched on every view.

The network is patched out and the cache points at a temp dir.

    venv/bin/python -m tests.test_headshots
"""
from __future__ import annotations

import inspect
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402
import routers.headshots as hs  # noqa: E402

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


def png(w, h):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


TMP = Path(tempfile.mkdtemp())
hs.HEADSHOTS_DIR = TMP
BIOS = {
    "curry-stephen": {"photo_url": "https://example.test/curry.jpg"},
    "curry-stephen-2": {"photo_url": "https://example.test/other.jpg"},
    "no-photo": {"photo_url": ""},
    "local-photo": {"photo_url": "file:///etc/passwd"},
    "broken": {"photo_url": "https://example.test/404.jpg"},
}
hs._load_json = lambda path, default: BIOS

FETCHED = []


def fake_fetch(url):
    FETCHED.append(url)
    if "404" in url:
        raise RuntimeError("HTTP 404")
    return png(180, 270)


hs._fetch = fake_fetch
hs.HOST_INTERVAL = 0

print("the route takes a slug and nothing else")
check("get_headshot's only parameter is slug",
      list(inspect.signature(hs.get_headshot).parameters) == ["slug"])

print("\nfetch and cache")
p1 = hs.headshot_path("curry-stephen")
check("fetches the bio's URL", FETCHED == ["https://example.test/curry.jpg"])
with Image.open(p1) as img:
    check("served square at SIZE", img.size == (hs.SIZE, hs.SIZE))
    check("served as JPEG", img.format == "JPEG")
hs.headshot_path("curry-stephen")
check("a second request is a cache hit", len(FETCHED) == 1)

print("\na new photo on the bio")
hs.headshot_path("curry-stephen-2")
BIOS["curry-stephen"]["photo_url"] = "https://example.test/curry-new.jpg"
p2 = hs.headshot_path("curry-stephen")
check("the new URL is fetched", FETCHED[-1] == "https://example.test/curry-new.jpg")
check("under a different cache name", p2.name != p1.name)
check("the old file is removed", not p1.exists())
check("a longer slug sharing the prefix is left alone",
      len(list(TMP.glob("curry-stephen-2-*.jpg"))) == 1)

print("\nno usable photo")
raises("an empty photo_url", 404, lambda: hs.headshot_path("no-photo"))
raises("a non-http photo_url", 404, lambda: hs.headshot_path("local-photo"))
raises("an unknown slug", 404, lambda: hs.headshot_path("nobody"))
check("none of those fetched anything", "file:///etc/passwd" not in FETCHED)

print("\na broken photo")
n = len(FETCHED)
raises("a failed fetch", 404, lambda: hs.headshot_path("broken"))
raises("asked again", 404, lambda: hs.headshot_path("broken"))
check("is fetched once, not on every view", len(FETCHED) == n + 1)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("All headshot checks passed.")

"""Same-origin player headshots — `GET /api/players/{slug}/headshot`.

Most bios' `photo_url` is hotlinked from basketball-reference, which sends no
CORS headers. An <img> can show it, but a canvas that draws it is tainted and
can't be exported, so the quadrant chart creator on /stats/frivolities had no way to
make a PNG. This serves the same picture from our own origin.

Three things it must keep doing, pinned by tests/test_headshots.py:

- **It only ever fetches the URL on the bio.** The caller names a slug, never a
  URL, so this can't be used as an open proxy.
- **The cache is keyed by the URL, not just the slug**
  (`{slug}-{sha1(url)[:10]}.jpg`), so a bio that gets a better photo serves it
  on the next request instead of the old one forever.
- **A failure is a 404, and it is remembered for an hour.** The page draws
  initials on a 404; a dead URL shouldn't be fetched again on every view.
- **Fetches to one host are paced** (`HOST_INTERVAL`). Sports-Reference jails
  a client past ~20 requests a minute, and a first view of a 20-face chart
  would otherwise be exactly that burst. `fetch_headshots.py` warms the whole
  cache once at the same pace, so a page view is nearly always a cache hit.
"""
from __future__ import annotations

import hashlib
import io
import threading
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .constants import HEADSHOTS_DIR, PLAYER_BIOS_FILE, logger
from .storage import _load_json

router = APIRouter()

SIZE = 160                  # px, square
MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 8.0
FAIL_TTL = 3600             # seconds a failed fetch is remembered
HOST_INTERVAL = 3.5         # seconds between fetches to one host (~17/min)
USER_AGENT = "nbn.today-headshots/1.0"   # a plain token: some CDNs 403 a UA containing a URL

_lock = threading.Lock()
_failed: dict[str, float] = {}   # cache path name → retry-after timestamp
_last_fetch: dict[str, float] = {}   # host → when it was last fetched from


def _pace(url: str):
    """Sleep until HOST_INTERVAL has passed since the last fetch to this host.
    Called under _lock, so fetches are serial and the pacing is real; cache
    hits never take the lock, so they don't wait behind it."""
    host = urlsplit(url).hostname or ""
    wait = _last_fetch.get(host, 0) + HOST_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_fetch[host] = time.time()


def _fetch(url: str) -> bytes:
    """Download the image. Split out so tests can patch the network away."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise ValueError("image too large")
            return bytes(buf)


def _square_jpeg(data: bytes) -> bytes:
    """Crop to a square and shrink. Headshots are portraits with the face near
    the top, so a tall image is cropped from just below its top edge rather
    than its middle (which would cut the chin off and keep the chest)."""
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    w, h = img.size
    if h > w:
        top = min(h - w, round(h * 0.06))
        img = img.crop((0, top, w, top + w))
    elif w > h:
        left = (w - h) // 2
        img = img.crop((left, 0, left + h, h))
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def _cache_name(slug: str, url: str) -> str:
    return f"{slug}-{hashlib.sha1(url.encode()).hexdigest()[:10]}.jpg"


def headshot_path(slug: str):
    """The cached file for a slug, fetching it if needed. Raises 404 when there
    is no usable photo."""
    bio = _load_json(PLAYER_BIOS_FILE, {}).get(slug)
    url = (bio or {}).get("photo_url") or ""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="No photo")

    name = _cache_name(slug, url)
    path = HEADSHOTS_DIR / name
    if path.exists():
        return path

    with _lock:
        if path.exists():
            return path
        if _failed.get(name, 0) > time.time():
            raise HTTPException(status_code=404, detail="Photo unavailable")
        _pace(url)
        try:
            jpeg = _square_jpeg(_fetch(url))
        except Exception as e:   # network, HTTP status, not an image, too big
            logger.info("headshot %s: fetch failed (%s)", slug, e)
            _failed[name] = time.time() + FAIL_TTL
            raise HTTPException(status_code=404, detail="Photo unavailable")
        HEADSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        # The old photo, if the bio's URL changed. The hash check keeps a
        # longer slug sharing this prefix ("smith-john-2") out of the sweep.
        for old in HEADSHOTS_DIR.glob(f"{slug}-*.jpg"):
            rest = old.name[len(slug) + 1:-4]
            if old.name != name and len(rest) == 10 and all(c in "0123456789abcdef" for c in rest):
                old.unlink(missing_ok=True)
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_bytes(jpeg)
        tmp.replace(path)
        _failed.pop(name, None)
    return path


@router.get("/api/players/{slug}/headshot")
def get_headshot(slug: str):
    return FileResponse(str(headshot_path(slug)), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

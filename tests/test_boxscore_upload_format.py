"""`POST /api/boxscore/upload` — what image formats are allowed into the queue.

The pending queue is parsed by the `/parse-boxscores` CLI skill, which can read
PNG, JPEG, GIF and WebP and nothing else. Before this check the upload wrote the
file under whatever extension the *filename* carried, so a phone could land a
HEIC in the queue and the failure surfaced mid-parse with the game already
registered as pending.

Two things are pinned here: the format is decided by the leading bytes rather
than the filename (a .heic named .png is still a .heic), and the stored
extension is the one we sniffed, so it always describes the file.

    venv/bin/python -m tests.test_boxscore_upload_format
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402

from routers.boxscores import _checked_image_ext, _heif_brand, _sniff_image_ext  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def rejects(name, data):
    try:
        _checked_image_ext(data, "home")
    except HTTPException as e:
        check(f"{name} — 415", e.status_code == 415)
        return e.detail
    check(f"{name} — rejected", False)
    return ""


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF87 = b"GIF87a" + b"\x00" * 64
GIF89 = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x2c\x00\x00\x00" + b"WEBP" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
AVIF = b"\x00\x00\x00\x18ftypavif" + b"\x00" * 64

print("the four parseable formats are accepted")
check("PNG", _sniff_image_ext(PNG) == "png")
check("JPEG", _sniff_image_ext(JPG) == "jpg")
check("GIF87a", _sniff_image_ext(GIF87) == "gif")
check("GIF89a", _sniff_image_ext(GIF89) == "gif")
check("WebP", _sniff_image_ext(WEBP) == "webp")

print("everything else is refused")
check("HEIC", _sniff_image_ext(HEIC) is None)
check("AVIF", _sniff_image_ext(AVIF) is None)
check("not an image", _sniff_image_ext(b"just some text") is None)
check("empty upload", _sniff_image_ext(b"") is None)
# RIFF is a container family; only the WEBP form is an image we can read.
check("RIFF that isn't WebP", _sniff_image_ext(b"RIFF\x2c\x00\x00\x00AVI ") is None)

print("the format comes from the bytes, not the filename")
# The endpoint reads the extension off the sniff, so a mislabelled upload is
# caught rather than written under a name that lies about its contents.
check("a HEIC named .png is still refused", _sniff_image_ext(HEIC) is None)
check("a PNG named .heic is still a png", _sniff_image_ext(PNG) == "png")
check("JPEG normalises to jpg, not jpeg", _checked_image_ext(JPG, "home") == "jpg")

print("the refusal names the format that arrived")
check("HEIC brand", _heif_brand(HEIC) == "HEIC")
check("AVIF brand", _heif_brand(AVIF) == "AVIF")
check("a PNG has no brand", _heif_brand(PNG) is None)

detail = rejects("HEIC", HEIC)
check("HEIC message names HEIC", "HEIC" in detail)
check("HEIC message names the accepted formats", "PNG" in detail and "WebP" in detail)
detail = rejects("AVIF", AVIF)
check("AVIF message names AVIF", "AVIF" in detail)
detail = rejects("junk", b"just some text")
check("unknown format still explains what is accepted", "PNG" in detail)
rejects("empty upload", b"")

print("a parseable upload returns its extension rather than raising")
check("PNG passes through", _checked_image_ext(PNG, "away") == "png")
check("WebP passes through", _checked_image_ext(WEBP, "away") == "webp")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

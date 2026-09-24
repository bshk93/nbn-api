"""Box score screenshots: uploaded one team-side at a time, kept 14 days after commit.

A game's screenshots live in one of two places, and which one says where the
game is in the pipeline:

- **`pending-boxscores/<id>/`** — uploaded, not yet committed. This is what
  `/parse-boxscores` reads. The images are the originals, byte for byte, because
  a parse reads digits off them and lossy compression is the last thing to risk
  there.
- **`boxscore-screenshots/<id>/`** — committed. `archive_for_game` moves the
  item here at commit time, re-encoded as WebP (a 2K box score PNG is 1-3MB;
  WebP at q90 is a tenth of that and still reads cleanly), and stamps an
  `expires_at` 14 days out. After that `sweep_expired` deletes it.

The originals used to be deleted at commit outright (see boxscore_provenance.py
for why the pixels are not kept forever). Two weeks is long enough to settle a
"that line looks wrong" question against the screen it came from, and short
enough that the folder never holds more than a fortnight of games — about
100 games, well under 100MB. Neither folder is in the data-dir backup repo.

Both folders are served publicly (`GET /api/boxscore/screenshots`): a box score
screenshot is the same information the committed box score shows.

## One item per game, one or more images per side

The Stats committee uploads a screenshot per team, and sometimes two members
each upload one side, so an item is created by whichever side arrives first
and the other side is added to it. A side can carry more than one image (a
long bench that needs a second screenshot). The item's `home_team`/`away_team`
are fixed by the first upload; later uploads are matched to it by date and the
two teams *unordered*, and assigned a side by team, never by the caller's
idea of home and away — so a caller that has the sides swapped still files the
image under the right team.

meta.json:

    {"id", "date", "home_team", "away_team", "season", "game_type",
     "game_num", "round_num", "uploaded_by", "uploaded_at",   # first upload
     "images": {"home": [{"file", "by", "at"}], "away": [...]},
     "rewarded": ["home", "away"]}

`rewarded` records which sides have already paid the upload reward, so
removing and re-adding a screenshot does not pay twice.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .constants import PENDING_BOXSCORES_DIR, KEPT_BOXSCORES_DIR, logger

KEEP_DAYS = 14
MAX_IMAGES_PER_SIDE = 4
MAX_IMAGE_BYTES = 15 * 1024 * 1024
SIDES = ("home", "away")

# Upload, remove, archive and sweep all read-modify-write meta.json or move
# whole directories; one lock keeps two members pasting the two sides of the
# same game from each creating their own item.
shots_lock = threading.Lock()

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_FILE_RE = re.compile(r"^(home|away)(-\d+)?\.(png|jpg|gif|webp)$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _teams(a: str, b: str) -> frozenset:
    return frozenset({(a or "").upper(), (b or "").upper()})


def _read_meta(item_dir: Path) -> Optional[dict]:
    try:
        return json.loads((item_dir / "meta.json").read_text())
    except Exception:
        return None


def _write_meta(item_dir: Path, meta: dict) -> None:
    (item_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def images_of(meta: dict) -> dict:
    """`{"home": [...], "away": [...]}` for any item, including one written before
    per-side uploads existed (one `home_image`/`away_image` filename each)."""
    imgs = meta.get("images")
    if isinstance(imgs, dict):
        return {s: list(imgs.get(s) or []) for s in SIDES}
    out = {}
    for s in SIDES:
        f = meta.get(f"{s}_image")
        out[s] = [{"file": f, "by": meta.get("uploaded_by"), "at": meta.get("uploaded_at")}] if f else []
    return out


def is_ready(meta: dict) -> bool:
    """Both sides have at least one screenshot — the game can be parsed."""
    imgs = images_of(meta)
    return all(imgs[s] for s in SIDES)


def _items(root: Path) -> list[tuple[Path, dict]]:
    if not root.exists():
        return []
    out = []
    for d in root.iterdir():
        if d.is_dir():
            meta = _read_meta(d)
            if meta:
                out.append((d, meta))
    return out


def find_pending(date: str, team_a: str, team_b: str) -> Optional[tuple[Path, dict]]:
    """The pending item for this game, matched on date and teams unordered."""
    want = _teams(team_a, team_b)
    for d, meta in _items(PENDING_BOXSCORES_DIR):
        if meta.get("date") == date and _teams(meta.get("home_team"), meta.get("away_team")) == want:
            return d, meta
    return None


def new_item(*, date, home_team, away_team, season, game_type, game_num, round_num,
             uploaded_by) -> tuple[Path, dict]:
    item_id = uuid.uuid4().hex[:8]
    item_dir = PENDING_BOXSCORES_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": item_id,
        "date": date,
        "home_team": home_team,
        "away_team": away_team,
        "season": season,
        "game_type": game_type,
        "game_num": game_num,
        "round_num": round_num,
        "uploaded_by": uploaded_by,
        "uploaded_at": _now().isoformat(),
        "images": {"home": [], "away": []},
        "rewarded": [],
    }
    _write_meta(item_dir, meta)
    return item_dir, meta


def add_image(item_dir: Path, meta: dict, side: str, data: bytes, ext: str, by: str) -> str:
    """Write one image onto `side` and record it. Returns the stored filename.
    The caller holds `shots_lock` and has already sniffed `ext`."""
    imgs = images_of(meta)
    used = {int(m.group(1)) for i in imgs[side]
            if (m := re.match(rf"^{side}-(\d+)\.", i["file"] or ""))}
    n = max(used, default=0) + 1
    fname = f"{side}-{n}.{ext}"
    (item_dir / fname).write_bytes(data)
    imgs[side].append({"file": fname, "by": by, "at": _now().isoformat()})
    meta["images"] = imgs
    meta.pop("home_image", None)
    meta.pop("away_image", None)
    _write_meta(item_dir, meta)
    return fname


def remove_image(item_id: str, fname: str) -> Optional[dict]:
    """Remove one pending image. Returns the item's meta afterwards, or None if
    that was its last image and the whole item was removed. Raises KeyError
    when the item or file isn't there. The caller holds `shots_lock`."""
    m = _FILE_RE.match(fname or "")
    if not _ID_RE.match(item_id or "") or not m:
        raise KeyError(fname)
    item_dir = PENDING_BOXSCORES_DIR / item_id
    meta = _read_meta(item_dir)
    if not meta:
        raise KeyError(item_id)
    imgs = images_of(meta)
    side = m.group(1)
    if not any(i["file"] == fname for i in imgs[side]):
        raise KeyError(fname)
    imgs[side] = [i for i in imgs[side] if i["file"] != fname]
    (item_dir / fname).unlink(missing_ok=True)
    if not any(imgs[s] for s in SIDES):
        shutil.rmtree(item_dir, ignore_errors=True)
        return None
    meta["images"] = imgs
    _write_meta(item_dir, meta)
    return meta


def _to_webp(src: Path, dst: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(src) as im:
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.save(dst, "WEBP", quality=90, method=4)
        return True
    except Exception as exc:
        logger.warning("WebP conversion failed for %s: %s", src, exc)
        return False


def archive_for_game(date: str, home_team: str, away_team: str) -> None:
    """Move a committed game's screenshots to the kept folder, compressed, with
    a 14-day expiry. Called from the commit route after the rows are written.
    Never raises: keeping a screenshot must never be why a commit fails."""
    try:
        with shots_lock:
            found = find_pending(date, home_team, away_team)
            if not found:
                return
            src_dir, meta = found
            dst_dir = KEPT_BOXSCORES_DIR / meta["id"]
            dst_dir.mkdir(parents=True, exist_ok=True)
            imgs = images_of(meta)
            for side in SIDES:
                for img in imgs[side]:
                    src = src_dir / img["file"]
                    if not src.exists():
                        continue
                    webp_name = Path(img["file"]).with_suffix(".webp").name
                    if _to_webp(src, dst_dir / webp_name):
                        img["file"] = webp_name
                    else:
                        shutil.copy2(src, dst_dir / img["file"])
            now = _now()
            meta["images"] = imgs
            meta.pop("home_image", None)
            meta.pop("away_image", None)
            meta["committed_at"] = now.isoformat()
            meta["expires_at"] = (now + timedelta(days=KEEP_DAYS)).isoformat()
            _write_meta(dst_dir, meta)
            shutil.rmtree(src_dir, ignore_errors=True)
        sweep_expired()
    except Exception as exc:
        logger.warning("Archiving screenshots failed for %s vs %s on %s: %s",
                       home_team, away_team, date, exc)


def sweep_expired() -> int:
    """Delete kept screenshots past their expiry. Run on commit and whenever the
    list is read, rather than on a timer: nothing new is kept without a commit,
    and the Stats dashboard reads the list on every load. Returns how many
    games' screenshots were removed. Never raises."""
    removed = 0
    try:
        now = _now().isoformat()
        with shots_lock:
            for d, meta in _items(KEPT_BOXSCORES_DIR):
                if (meta.get("expires_at") or "") < now:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
    except Exception as exc:
        logger.warning("Screenshot sweep failed: %s", exc)
    return removed


def _public(meta: dict, status: str) -> dict:
    imgs = images_of(meta)
    base = f"/api/boxscore/screenshots/{meta['id']}"
    return {
        "id": meta["id"],
        "status": status,
        "date": meta.get("date"),
        "home_team": meta.get("home_team"),
        "away_team": meta.get("away_team"),
        "season": meta.get("season"),
        "game_type": meta.get("game_type"),
        "ready": is_ready(meta),
        "committed_at": meta.get("committed_at"),
        "expires_at": meta.get("expires_at"),
        "images": {s: [{**i, "url": f"{base}/{i['file']}"} for i in imgs[s]] for s in SIDES},
    }


def list_all(season: Optional[str] = None, date: Optional[str] = None) -> list[dict]:
    """Every pending and kept item, optionally narrowed to a season or a date."""
    sweep_expired()
    out = []
    for root, status in ((PENDING_BOXSCORES_DIR, "pending"), (KEPT_BOXSCORES_DIR, "committed")):
        for _, meta in _items(root):
            if season and meta.get("season") != season:
                continue
            if date and meta.get("date") != date:
                continue
            out.append(_public(meta, status))
    out.sort(key=lambda m: (m["date"] or "", m["home_team"] or ""))
    return out


def image_path(item_id: str, fname: str) -> Optional[Path]:
    """Path to a screenshot in either folder, or None. Both parts are checked
    against strict patterns first, so nothing from the URL reaches the path
    unvalidated."""
    if not _ID_RE.match(item_id or "") or not _FILE_RE.match(fname or ""):
        return None
    for root in (PENDING_BOXSCORES_DIR, KEPT_BOXSCORES_DIR):
        p = root / item_id / fname
        if p.is_file():
            return p
    return None

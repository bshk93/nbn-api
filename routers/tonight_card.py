"""`GET /api/tonight/card.png` — the "Tonight" graphic as one 1200x675 image.

Discord can't run the homepage, so the 11am #leaguetalk post (`post_tonight.py`)
embeds this instead. It draws exactly what `GET /api/tonight` answers
(`routers/tonight.py`) and adds no rule of its own: the featured game, its two
stars and the other games all come from that one answer.

Conventions carried over from nbn-today/build/og_cards.py:

* **A logo is cropped to its alpha bbox before it is fitted**, since the files
  in logos/ carry very different amounts of transparent padding.
* **A black mark gets a light plate under it**, or it vanishes on the dark
  strip. Utah no longer needs one: the site now has a white
  `logo-uta-dark.png`, which `default_logo` prefers. The plate stays for any
  black mark without a dark version. Every other logo gets a soft light halo in its own shape, which
  is enough for a black outline (SAS, BKN) to read.

`render(data, headshot=, logo=)` takes its two image loaders as arguments, so
the tests and a local preview can draw a card with no data directory. The
route caches each rendered card on disk under `tonight-cards/`, keyed by a
hash of the answer, so a card is drawn once per change, not per Discord fetch.
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import date as _date
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .constants import DATA_DIR
from .tonight import SITE_DIR, _check_date, _team_colors, last_name, tonight

router = APIRouter()

W, H = 1200, 675
STRIP_H = 118
CARD_DIR = DATA_DIR / "tonight-cards"
KEEP_CARDS = 60

WHITE = (255, 255, 255)
GOLD = (255, 214, 10)
LIVE = (255, 45, 85)
NAVY = (13, 17, 28)

FONT_DIR = SITE_DIR / "fonts"
_FALLBACK_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_fonts: dict = {}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """`display` is the site's Barlow Condensed, `text` its Inter. Both ship
    as woff2, which FreeType reads; DejaVu stands in if they can't be read."""
    key = (kind, size)
    if key in _fonts:
        return _fonts[key]
    files = {"display": ["BarlowCondensed-700-latin.woff2"],
             "text": ["InterVariable.subset.woff2"]}[kind]
    font = None
    for f in files:
        try:
            font = ImageFont.truetype(str(FONT_DIR / f), size)
            if kind == "text":
                try:
                    font.set_variation_by_axes([650])
                except Exception:  # noqa: BLE001 — not variable: use as is
                    pass
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.truetype(_FALLBACK_BOLD if kind == "display" else _FALLBACK, size)
        except OSError:
            font = ImageFont.load_default(size)
    _fonts[key] = font
    return font


def _hex(c: Optional[str], default=(40, 50, 70)) -> tuple:
    c = (c or "").lstrip("#")
    if len(c) != 6:
        return default
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _lift(rgb, minimum=70) -> tuple:
    """A near-black primary (BKN) would leave half the card black; lift it."""
    lum = _lum(rgb)
    if lum >= minimum:
        return rgb
    k = minimum - lum
    return tuple(min(255, int(v + k)) for v in rgb)


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ── Image loaders (swappable for tests) ──────────────────────────────────────

def default_headshot(slug: str) -> Optional[Image.Image]:
    from .headshots import headshot_path
    try:
        return Image.open(headshot_path(slug)).convert("RGB")
    except (HTTPException, OSError):
        return None


def default_logo(team: str) -> Optional[Image.Image]:
    """The logo for a dark background: `logo-{abbr}-dark.png` where the site
    has one (Utah's white mark; nbn-today build/make_logo_thumbs.py), else the
    usual `logo-{abbr}.png`. Everything this card draws a logo on is dark."""
    logos = SITE_DIR / "logos"
    for p in (logos / f"logo-{team.lower()}-dark.png", logos / f"logo-{team.lower()}.png"):
        try:
            return Image.open(p).convert("RGBA")
        except OSError:
            continue
    return None


# ── Drawing ──────────────────────────────────────────────────────────────────

def _background(c1, c2) -> Image.Image:
    """A diagonal split in the two teams' colours, a soft blend at the seam,
    a faint stripe texture and a dark fade to the bottom."""
    small = Image.new("RGB", (W // 4, H // 4))
    px = small.load()
    sw, sh = small.size
    for y in range(sh):
        for x in range(sw):
            # Position across a 115° diagonal, 0 at the left edge, 1 at the right.
            t = (x + (sh - y) * 0.45) / (sw + sh * 0.45)
            k = min(1.0, max(0.0, (t - 0.44) / 0.12))
            px[x, y] = _mix(c1, c2, k)
    bg = small.resize((W, H), Image.BICUBIC)
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for x in range(-H, W, 26):
        d.line([(x, H), (x + int(H * 0.47), 0)], fill=(255, 255, 255, 14), width=2)
    for y in range(H):
        a = int(max(0, (y - H * 0.35) / (H * 0.65)) * 150)
        d.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    bg = bg.convert("RGBA")
    bg.alpha_composite(over)
    return bg


def _fit_logo(img: Optional[Image.Image], box: int) -> Optional[Image.Image]:
    if img is None:
        return None
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    img = img.copy()
    img.thumbnail((box, box), Image.LANCZOS)
    return img


def _is_dark(img: Image.Image) -> bool:
    """Genuinely black line art (UTA). A dark-red or navy mark reads fine on
    the strip once it has the halo `_paste_logo` gives every logo."""
    a = img.getchannel("A").point(lambda v: 255 if v > 128 else 0)
    lum = img.convert("L")
    hist = lum.histogram(mask=a)
    n = sum(hist)
    if not n:
        return False
    return sum(i * c for i, c in enumerate(hist)) / n < 30


def _paste_logo(canvas: Image.Image, img: Optional[Image.Image], cx: int, cy: int, box: int,
                plate: bool = True):
    img = _fit_logo(img, box)
    if img is None:
        return
    x, y = cx - img.width // 2, cy - img.height // 2
    if plate and _is_dark(img):
        d = ImageDraw.Draw(canvas)
        r = box // 2 + 6
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(235, 238, 243, 255))
    else:
        # A soft light halo in the logo's own shape, so a black outline
        # (SAS, BKN) still separates from the dark strip.
        pad = 12
        halo = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        sil = Image.new("RGBA", img.size, (255, 255, 255, 0))
        sil.putalpha(img.getchannel("A").point(lambda v: 110 if v > 40 else 0))
        halo.alpha_composite(sil, (pad, pad))
        halo = halo.filter(ImageFilter.GaussianBlur(4))
        canvas.alpha_composite(halo, (x - pad, y - pad))
    canvas.alpha_composite(img, (x, y))


def _face(canvas: Image.Image, img: Optional[Image.Image], cx: int, cy: int, r: int,
          ring, initials: str):
    """A circular headshot with a glowing team-colour ring; initials if none."""
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - r - 16, cy - r - 16, cx + r + 16, cy + r + 16], fill=ring + (170,))
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(18)))
    d = ImageDraw.Draw(canvas)
    d.ellipse([cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7], fill=ring + (255,))
    d.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2], fill=(255, 255, 255, 255))
    if img is not None:
        size = 2 * r
        face = img.resize((size, size), Image.LANCZOS).convert("RGBA")
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size * 4, size * 4], fill=255)
        mask = mask.resize((size, size), Image.LANCZOS)
        canvas.paste(face, (cx - r, cy - r), mask)
    else:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(22, 26, 36, 255))
        f = _font("display", int(r * 0.9))
        d.text((cx, cy), initials, font=f, fill=WHITE, anchor="mm")


def _text(d, xy, s, font, fill=WHITE, anchor="la", shadow=True):
    if shadow:
        x, y = xy
        d.text((x + 2, y + 3), s, font=font, fill=(0, 0, 0, 120), anchor=anchor)
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _fit(d, s, kind, size, max_w):
    f = _font(kind, size)
    while size > 12 and d.textlength(s, font=f) > max_w:
        size -= 2
        f = _font(kind, size)
    return f


def _initials(name: str) -> str:
    parts = [p for p in name.split(" ") if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def render(data: dict, headshot: Callable[[str], Optional[Image.Image]] = default_headshot,
           logo: Callable[[str], Optional[Image.Image]] = default_logo,
           colors: Optional[dict] = None) -> bytes:
    games = data.get("games") or []
    feat = next((g for g in games if g.get("featured")), games[0] if games else None)
    theme = data.get("theme") or {}
    c1 = _lift(_hex((theme.get("away") or {}).get("primary")))
    c2 = _lift(_hex((theme.get("home") or {}).get("primary"), (120, 20, 40)))
    canvas = _background(c1, c2)
    d = ImageDraw.Draw(canvas)

    day = _date.fromisoformat(data["date"])
    n = data.get("count", len(games))

    # Left: the count.
    _text(d, (56, 22), str(n), _font("display", 230))
    _text(d, (60, 262), "GAMES" if n != 1 else "GAME", _font("display", 84))
    _text(d, (60, 336), "TONIGHT", _font("display", 84))
    _text(d, (62, 432), f"{day.strftime('%a').upper()} {day.month}/{day.day}/{day.year % 100:02d}",
          _font("display", 30), fill=(255, 255, 255, 230))

    if feat:
        # Right: the featured matchup.
        left, right = 430, W - 40
        mid = (left + right) // 2
        # No tip time anywhere: times mean nothing in NBN, where a game is
        # played when it's streamed or simmed.
        label = "GAME OF THE NIGHT"
        lf = _font("display", 28)
        lw = d.textlength(label, font=lf)
        x0 = mid - lw / 2
        if feat.get("stream"):
            # The dot is drawn: Barlow has no U+25CF.
            pill = "STREAM"
            pw = d.textlength(pill, font=lf) + 50
            x0 = mid - (lw + pw + 14) / 2
            d.rounded_rectangle([x0, 34, x0 + pw, 74], radius=6, fill=LIVE + (255,))
            d.ellipse([x0 + 14, 47, x0 + 28, 61], fill=WHITE)
            d.text((x0 + 36, 54), pill, font=lf, fill=WHITE, anchor="lm")
            x0 += pw + 14
        _text(d, (x0, 54), label, lf, anchor="lm")

        r = 96
        sides = [("away", left + 150), ("home", right - 150)]
        for side, cx in sides:
            team = feat[side]
            star = (feat.get("stars") or {}).get(side)
            col = _hex(((theme.get(side) or {}).get("accent")), WHITE)
            if _lum(col) < 60:
                col = (225, 228, 235)
            img = headshot(star["slug"]) if star else None
            _face(canvas, img, cx, 200, r, col, _initials(star["name"]) if star else team)
            d = ImageDraw.Draw(canvas)
            nm = last_name(star["name"]).upper() if star else team
            _text(d, (cx, 322), nm, _fit(d, nm, "display", 48, 300), anchor="mt")
            if star and star.get("last5"):
                l5 = star["last5"]
                line = f"{l5['pts']:.1f} PPG · {l5['reb']:.1f} RPG · {l5['ast']:.1f} APG"
                sub = f"LAST {l5['gp']}"
            elif star and star.get("ovr"):
                line, sub = f"{star['ovr']} OVR", "TOP RATED"
            else:
                line, sub = "", ""
            _text(d, (cx, 376), line, _font("text", 22), anchor="mt")
            _text(d, (cx, 406), sub, _font("display", 18), fill=(255, 255, 255, 200), anchor="mt")
            rec = (feat.get("records") or {}).get(side, "")
            tag = f"{team}  {rec}"
            tf = _font("display", 32)
            tw = d.textlength(tag, font=tf)
            _paste_logo(canvas, logo(team), int(cx - tw / 2 - 24), 452, 40)
            d = ImageDraw.Draw(canvas)
            _text(d, (cx + 4, 452), tag, tf, anchor="mm")

        _text(d, (mid, 196), "VS", _font("display", 96), anchor="mm")
        _text(d, (mid, 262), f"{feat['away']} @ {feat['home']}", _font("display", 26),
              fill=(255, 255, 255, 220), anchor="mm")

        # Storyline under the matchup: the first that the card doesn't already
        # say (the stream pill, and the stars' last-5 lines under their faces).
        story = next((s for s in feat.get("storylines") or []
                      if not s.startswith("Live on") and "over his last" not in s), "")
        if story:
            sf = _fit(d, story.upper(), "display", 28, right - left)
            _text(d, (mid, 508), story.upper(), sf, fill=GOLD + (255,), anchor="mm")

    # Bottom strip: every other game.
    strip = Image.new("RGBA", (W, STRIP_H), (8, 10, 16, 215))
    canvas.alpha_composite(strip, (0, H - STRIP_H))
    d = ImageDraw.Draw(canvas)
    d.line([(0, H - STRIP_H), (W, H - STRIP_H)], fill=GOLD + (255,), width=3)
    rest = [g for g in games if g is not feat]
    if rest:
        # Each game is its own tile, with a gap between tiles and a top edge
        # in the two teams' colours (their chart colours, which are picked to
        # read on a dark page), so nine pairs of logos don't run together.
        colors = _team_colors() if colors is None else colors
        cols = min(len(rest), 9)
        gap = 10
        cw = (W - 40) / cols
        y0, y1 = H - STRIP_H + 16, H - 12
        for i, g in enumerate(rest[:cols]):
            x0 = int(20 + cw * i + gap / 2)
            x1 = int(20 + cw * (i + 1) - gap / 2)
            mid = (x0 + x1) // 2
            d.rounded_rectangle([x0, y0, x1, y1], radius=4, fill=(30, 35, 48, 255))
            for side, (a, b) in (("away", (x0, mid)), ("home", (mid, x1))):
                c = _hex(((colors.get(g[side]) or {}).get("chart")), (120, 130, 150))
                d.rectangle([a, y0, b, y0 + 5], fill=_lift(c, 90) + (255,))
            lx = (x1 - x0) * 0.27
            for team, cx in ((g["away"], int(x0 + lx)), (g["home"], int(x1 - lx))):
                _paste_logo(canvas, logo(team), cx, y0 + 38, 40)
                d = ImageDraw.Draw(canvas)
                d.text((cx, y1 - 13), team, font=_font("display", 18), fill=(255, 255, 255, 230), anchor="mm")
            d.text((mid, y0 + 38), "@", font=_font("display", 20), fill=(255, 255, 255, 150), anchor="mm")
    else:
        d.text((W // 2, H - STRIP_H // 2), "NBN.TODAY", font=_font("display", 40), fill=WHITE, anchor="mm")

    # The NBN crest, top right.
    crest = None
    try:
        crest = Image.open(SITE_DIR / "logo.png").convert("RGBA")
    except OSError:
        pass
    if crest is not None:
        crest.thumbnail((54, 54), Image.LANCZOS)
        canvas.alpha_composite(crest, (W - crest.width - 18, 16))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def card_hash(data: dict) -> str:
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]


def card_path(data: dict) -> Path:
    """The card for this answer, drawn once and kept on disk."""
    path = CARD_DIR / f"{data['date']}-{card_hash(data)}.png"
    if path.exists():
        return path
    png = render(data)
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(png)
    tmp.replace(path)
    old = sorted(CARD_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for p in old[:-KEEP_CARDS]:
        p.unlink(missing_ok=True)
    return path


@router.get("/api/tonight/card.png")
def get_tonight_card(date: str = Query(default=""), v: str = Query(default="")):
    """`v` is ignored: the Discord post adds the answer's hash so a changed
    card is a new URL, and Discord's image cache can't show yesterday's."""
    data = tonight(_check_date(date) if date else None)
    if not data["games"]:
        raise HTTPException(status_code=404, detail="No games that day")
    return Response(card_path(data).read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


# ── The 11am post to #leaguetalk (`post_tonight.py`, `nbn-tonight.timer`) ────

SITE_URL = "https://nbn.today"
POSTED_FILE = DATA_DIR / "tonight-posted.json"


def _leaguetalk_channel() -> str:
    from .stream_notify import DISCORD_LEAGUETALK_CHANNEL
    return DISCORD_LEAGUETALK_CHANNEL


def build_post(data: dict) -> dict:
    """The message: one line naming the night and its featured game, and an
    embed whose image is the card. The image is a URL, not an upload, so the
    shared transport stays JSON; `v=` is the answer's hash, so Discord never
    serves a cached older card for the same date."""
    day = _date.fromisoformat(data["date"])
    games = data.get("games") or []
    feat = next((g for g in games if g.get("featured")), None)
    n = data.get("count", len(games))
    when = f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}"
    content = f"\U0001F3C0 **{n} game{'s' if n != 1 else ''} tonight** · {when}"
    embed = {
        "title": f"Tonight in the NBN · {day.strftime('%b')} {day.day}",
        "url": f"{SITE_URL}/",
        "image": {"url": f"{SITE_URL}/api/tonight/card.png?date={data['date']}&v={card_hash(data)}"},
        "footer": {"text": "nbn.today"},
    }
    if feat:
        lines = [f"**Game of the night:** {feat['away']} @ {feat['home']}"
                 + (" · \U0001F534 stream" if feat.get("stream") else "")]
        lines += [f"• {s}" for s in feat.get("storylines") or [] if not s.startswith("Live on")][:4]
        embed["description"] = "\n".join(lines)
        color = ((data.get("theme") or {}).get("home") or {}).get("primary")
        if color:
            embed["color"] = int(color.lstrip("#"), 16)
    return {"content": content, "embeds": [embed], "allowed_mentions": {"parse": []}}


def post_tonight(day: Optional[str] = None, *, dry_run: bool = False) -> dict:
    """Post `day`'s card once. Nothing on a day with no games, and nothing a
    second time for a date already posted (`tonight-posted.json`), so the
    timer's `Persistent=true` catch-up can never double-post."""
    from datetime import datetime, timezone
    from . import discord_transport as transport
    from .storage import _load_json, _save_json

    data = tonight(day)
    day = data["date"]
    if not data["games"]:
        return {"date": day, "posted": False, "reason": "no games"}
    posted = _load_json(POSTED_FILE, {})
    if day in posted and not dry_run:
        return {"date": day, "posted": False, "reason": "already posted"}
    payload = build_post(data)
    if dry_run:
        return {"date": day, "posted": False, "reason": "dry run", "payload": payload}
    channel = _leaguetalk_channel()
    if not transport.configured(channel):
        return {"date": day, "posted": False, "reason": "DISCORD_LEAGUETALK_CHANNEL or bot token unset"}
    card_path(data)   # drawn now, so Discord's fetch of the image is instant
    if not transport.send(channel, payload, max_burst=2, burst_window=3600):
        return {"date": day, "posted": False, "reason": "refused by the transport"}
    if not transport.flush(60):
        return {"date": day, "posted": False, "reason": "send did not finish"}
    posted[day] = {"posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "card": card_hash(data)}
    # A season of dates is ~200 small rows; keep the last 400.
    _save_json(POSTED_FILE, dict(sorted(posted.items())[-400:]))
    return {"date": day, "posted": True}

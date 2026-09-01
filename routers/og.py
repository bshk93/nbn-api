"""Open Graph heads for the pages that build themselves in the browser.

Four pages on the site are one shell serving many items — an article, a player,
a proposal, a member — with the item fetched from the API after load:

    /news/view/?id=…      /players/?p=…      /proposals/view/?id=…   /members/{name}

No link unfurler runs JavaScript, so all of them fetch the shell and see the
placeholder tags nbn-today/build/og_tags.py bakes into it. Every article
unfurled in Discord as the same "Article — NBN News" card; every player as the
same "Players — NBN" one. This module renders the head each of those items
should have had.

**It only gets used because nginx routes to it.** /etc/nginx/sites-enabled/nbn.today
carries a `$nbn_unfurler` map over `$http_user_agent` and rewrites the four
paths to internal locations that proxy here; every other visitor gets the real
page, untouched. The map is **social unfurlers only, deliberately not search
engines** — these endpoints answer with a head-only stub, and nothing that
indexes pages should ever be handed one.

Three rules hold across all four, and `tests/test_og.py` pins them:

  * **Nothing unpublished, ever.** A draft article, a draft proposal, an unknown
    id — all fall back to the page's own static card. A crawler that gets a 404
    shows no card at all, which is worse than a plain one, and a draft headline
    is not for a crawler either way.
  * **The fallback is read from the page, not restated here.** `_static_head`
    lifts the NBN:og block straight out of the shell on disk, so the generic
    card stays whatever og_tags.py last wrote and can't drift from it.
  * **An image that can't be made absolute is dropped.** Discord ignores a
    relative og:image and shows no picture at all, so the default card is the
    better answer.
"""
import csv
import html
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from .constants import DERIVED_DIR, logger
from .storage import read_csv
from .auth import load_members
from .players import load_player_bios, load_ovr, _display_name, _build_team_map
from .news import _load_article, _md_excerpt, _article_teaser
from .proposals import load_proposals
from .discord import TEAM_NAMES

router = APIRouter()

SITE = "https://nbn.today"
# The docroot is a symlink to the live checkout; _static_head reads the shells
# out of it so the fallback card is whatever og_tags.py last wrote.
SITE_DIR = Path(os.environ.get("NBN_SITE_DIR", "/var/www/nbn.today"))
DEFAULT_IMAGE = f"{SITE}/og-default.png"
TAGLINE = "Nothing But Net — fantasy basketball GM simulation league"

_OG_BLOCK_RE = re.compile(r"<!-- NBN:og -->(.*?)<!-- /NBN:og -->", re.S)
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]*)">')


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _abs_url(url: Optional[str]) -> Optional[str]:
    """Absolute https URL for an image, or None if it can't be made one.

    Cover images, headshots and avatars are all supplied by members or scraped:
    an absolute URL, a site-relative path, or something unusable."""
    u = (url or "").strip()
    if u.startswith(("http://", "https://")):
        return u
    if u.startswith("/"):
        return SITE + u
    return None


def _document(*, title: str, desc: str, url: str, image: str, image_alt: str,
              og_type: str = "website", extra: Optional[list[str]] = None) -> str:
    tags = [
        f'<meta name="description" content="{_esc(desc)}">',
        f'<meta property="og:type" content="{og_type}">',
        '<meta property="og:site_name" content="NBN">',
        f'<meta property="og:title" content="{_esc(title)}">',
        f'<meta property="og:description" content="{_esc(desc)}">',
        f'<meta property="og:url" content="{_esc(url)}">',
        f'<meta property="og:image" content="{_esc(image)}">',
    ]
    if image == DEFAULT_IMAGE:
        # Only the default card has known dimensions. A member's cover image or
        # a scraped headshot is whatever it is, and declaring a size we didn't
        # measure crops the preview.
        tags += ['<meta property="og:image:width" content="1200">',
                 '<meta property="og:image:height" content="630">']
    tags += [
        f'<meta property="og:image:alt" content="{_esc(image_alt)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        '<meta name="theme-color" content="#111827">',
    ]
    tags += extra or []
    head = "\n  ".join(tags)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '  <meta charset="UTF-8">\n'
        f"  <title>{_esc(title)}</title>\n"
        f'  <link rel="canonical" href="{_esc(url)}">\n'
        f"  {head}\n"
        "</head>\n<body>\n"
        f"  <h1>{_esc(title)}</h1>\n"
        f"  <p>{_esc(desc)}</p>\n"
        f'  <p><a href="{_esc(url)}">View this on NBN</a></p>\n'
        "</body>\n</html>\n"
    )


def _static_head(page_path: str) -> str:
    """The shell's own card, lifted from disk — what an unfurler sees today.

    Used whenever there is no item to render (no id, unknown id, unpublished).
    Reading it back out of the page rather than restating it here is the point:
    the generic card is og_tags.py's to define, and this can't drift from it."""
    shell = SITE_DIR / page_path.strip("/") / "index.html"
    try:
        text = shell.read_text()
        block = _OG_BLOCK_RE.search(text)
        if block:
            title = (_TITLE_RE.search(text) or [None, "NBN"])[1]
            desc = (_DESC_RE.search(block.group(1)) or [None, ""])[1]
            return (
                "<!DOCTYPE html>\n"
                '<html lang="en">\n<head>\n'
                '  <meta charset="UTF-8">\n'
                f"  <title>{_esc(title)}</title>\n"
                f"{block.group(1).rstrip()}\n"
                "</head>\n<body>\n"
                f"  <h1>{_esc(title)}</h1>\n"
                f"  <p>{_esc(html.unescape(desc))}</p>\n"
                f'  <p><a href="{SITE}{page_path}">View this on NBN</a></p>\n'
                "</body>\n</html>\n"
            )
        logger.warning("og: no NBN:og block in %s", shell)
    except OSError as exc:
        logger.warning("og: can't read %s — %s", shell, exc)
    return _document(title="NBN", desc=TAGLINE, url=f"{SITE}{page_path}",
                     image=DEFAULT_IMAGE, image_alt=TAGLINE)


# ── News ──────────────────────────────────────────────────────────────────────

def news_head(article_id: str) -> str:
    a = _load_article(article_id) if article_id else None
    if not a or a.get("status") != "published":
        return _static_head("/news/view/")

    title = (a.get("title") or "Untitled").strip()
    desc = _article_teaser(a, limit=200) or "Read the full story on NBN."
    author = a.get("author")
    extra = []
    if author:
        extra.append(f'<meta property="article:author" content="{_esc(author)}">')
    if a.get("published_at"):
        extra.append(f'<meta property="article:published_time" content="{_esc(a["published_at"])}">')
    extra += [f'<meta property="article:tag" content="{_esc(t)}">' for t in a.get("tags") or []]
    return _document(
        title=f"{title} — NBN News",
        desc=f"By {author}. {desc}" if author else desc,
        url=f"{SITE}/news/view/?id={a['id']}",
        image=_abs_url(a.get("cover_image")) or DEFAULT_IMAGE,
        image_alt=title,
        og_type="article",
        extra=extra,
    )


# ── Players ───────────────────────────────────────────────────────────────────

def _age(dob: Optional[str]) -> Optional[int]:
    try:
        born = date.fromisoformat(dob)
    except (TypeError, ValueError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _career_line(slug: str) -> Optional[str]:
    """'25.4 PPG, 5.1 RPG, 6.3 APG over 412 games' from the build's season totals.

    player_seasons.csv is ~1MB and this scans it, which is fine for a request
    that only ever comes from an unfurler. It is build output: a player with no
    NBN games simply isn't in it, and neither is one whose games predate a
    rebuild, so every part of this is optional."""
    path = DERIVED_DIR / "players" / "player_seasons.csv"
    g = pts = reb = ast = 0
    try:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("SLUG") != slug:
                    continue
                try:
                    g += int(row["G"] or 0)
                    pts += int(row["PTS"] or 0)
                    reb += int(row["REB"] or 0)
                    ast += int(row["AST"] or 0)
                except (KeyError, ValueError):
                    continue
    except OSError:
        return None
    if not g:
        return None
    return (f"{pts / g:.1f} PPG, {reb / g:.1f} RPG, {ast / g:.1f} APG "
            f"over {g} game{'s' if g != 1 else ''}")


def player_head(slug: str) -> str:
    bios = load_player_bios() if slug else {}
    bio = bios.get(slug)
    if not bio:
        return _static_head("/players/")

    name = _display_name(bio.get("name", "")) or slug
    bits = []
    if bio.get("pos"):
        bits.append("/".join(bio["pos"]))
    if bio.get("height"):
        bits.append(bio["height"])
    age = _age(bio.get("dob"))
    if age is not None:
        bits.append(f"{age} years old")
    team = _build_team_map().get(slug)
    if team:
        bits.append(TEAM_NAMES.get(team, team))
    ovr = (load_ovr().get(slug) or [{}])[-1].get("ovr")
    if ovr:
        bits.append(f"{ovr} OVR")

    desc = " · ".join(bits)
    career = _career_line(slug)
    if career:
        desc = f"{desc} — {career}." if desc else f"{career}."
    return _document(
        title=f"{name} — NBN",
        desc=desc or f"{name} on NBN: career stats, contract, ratings, awards and game logs.",
        url=f"{SITE}/players/?p={slug}",
        image=_abs_url(bio.get("photo_url")) or DEFAULT_IMAGE,
        image_alt=name,
        og_type="profile",
    )


# ── Proposals ─────────────────────────────────────────────────────────────────

_PROPOSAL_STATE = {
    "submitted": "Awaiting a vote",
    "voting": "Voting open",
    "closed": "Voting closed",
}


def proposal_head(proposal_id: str) -> str:
    proposals = load_proposals() if proposal_id else []
    p = next((x for x in proposals if x.get("id") == proposal_id), None)
    # A draft is visible to its author alone (GET /api/proposals/{id} 403s for
    # everyone else), and an unfurler is nobody.
    if not p or p.get("status", "draft") == "draft":
        return _static_head("/proposals/view/")

    title = (p.get("title") or "Untitled").strip()
    number = p.get("number")
    heading = f"Proposal {number}: {title}" if number else title

    # Only what the API shows an unauthenticated caller: a live tally is
    # privileged (`live_results` in _proposal_view), so the state line stops at
    # the outcome, which is public once a proposal is decided.
    state = _PROPOSAL_STATE.get(p.get("status"), "")
    if p.get("outcome"):
        state = p["outcome"].capitalize()
    lead = " · ".join(x for x in (state, f"by {p['author']}" if p.get("author") else "") if x)
    body = _md_excerpt(p.get("body", ""), limit=200)
    return _document(
        title=f"{heading} — NBN",
        desc=f"{lead}. {body}" if body else f"{lead}.",
        url=f"{SITE}/proposals/view/?id={p['id']}",
        image=DEFAULT_IMAGE,
        image_alt=TAGLINE,
        og_type="article",
    )


# ── Members ───────────────────────────────────────────────────────────────────

def _owner_record(name: str) -> Optional[dict]:
    """That member's row in the build's owner_stats.csv, if they have one.

    Keyed by the same member name — build/sync_owners.py generates owners.csv
    from members.json tenures, so the two agree by construction. A member who
    has never owned a team has no row, which is not an error."""
    path = DERIVED_DIR / "data" / "owner_stats.csv"
    try:
        _, rows = read_csv(path)
    except OSError:
        return None
    return next((r for r in rows if r.get("owner") == name), None)


def member_head(name: str) -> str:
    name = (name or "").strip()
    members = load_members() if name else {}
    m = members.get(name)
    if m is None:
        # Member names are the URL, so try a case-insensitive match before
        # giving up — /members/Bryn and /members/bryn are the same person.
        match = next((k for k in members if k.lower() == name.lower()), None)
        if match is None:
            return _static_head("/members/profile/")
        name, m = match, members[match]

    tenures = m.get("tenures") or []
    current = next((t for t in tenures if not t.get("end")), None)
    bits = []
    if current and current.get("team"):
        role = current.get("position") or "owner"
        bits.append(f"{role.capitalize()} of the {TEAM_NAMES.get(current['team'], current['team'])}")
    elif tenures:
        teams = [t["team"] for t in tenures if t.get("team")]
        if teams:
            bits.append("Formerly " + ", ".join(dict.fromkeys(teams)))

    rec = _owner_record(name)
    if rec:
        seasons = rec.get("seasons")
        if seasons:
            bits.append(f"{seasons} season{'s' if seasons != '1' else ''}")
        if rec.get("reg_w") and rec.get("reg_l"):
            bits.append(f"{rec['reg_w']}-{rec['reg_l']}")
        rings = rec.get("championships")
        if rings and rings not in ("0", ""):
            bits.append(f"{rings} championship{'s' if rings != '1' else ''}")

    avatar = f"{SITE}/api/members/{name}/avatar" if m.get("has_avatar") else None
    return _document(
        title=f"{name} — NBN",
        desc=" · ".join(bits) or f"{name}'s NBN profile — tenures, achievements and league history.",
        url=f"{SITE}/members/{name}",
        image=_abs_url(avatar) or DEFAULT_IMAGE,
        image_alt=name,
        og_type="profile",
    )


# ── Routes ────────────────────────────────────────────────────────────────────

_HEADERS = {"Cache-Control": "public, max-age=300"}


@router.get("/api/og/news", response_class=HTMLResponse)
def og_news(id: str = Query("")):
    return HTMLResponse(news_head(id), headers=_HEADERS)


@router.get("/api/og/player", response_class=HTMLResponse)
def og_player(p: str = Query("")):
    return HTMLResponse(player_head(p), headers=_HEADERS)


@router.get("/api/og/proposal", response_class=HTMLResponse)
def og_proposal(id: str = Query("")):
    return HTMLResponse(proposal_head(id), headers=_HEADERS)


@router.get("/api/og/member", response_class=HTMLResponse)
def og_member(name: str = Query("")):
    return HTMLResponse(member_head(name), headers=_HEADERS)

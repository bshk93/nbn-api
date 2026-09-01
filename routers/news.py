import json
import os
import re
import secrets
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .constants import NEWS_DIR, DATA_DIR, DERIVED_DIR, logger
from .storage import _save_json, log_write, read_csv
from .auth import get_token_info, has_role, require_role, _resolve_token, load_members
from .players import load_player_bios, load_ovr
from .league_time import league_today
from . import inbox
from . import news_rankings as pr

router = APIRouter()

_news_lock = threading.Lock()

# When True, only editors (curator/BOD/admin) may publish; authors must submit
# their drafts to the queue for editorial approval. When False (current policy),
# authors may publish their own articles directly with no approval step. The
# submit/queue/reject machinery is left fully intact either way, so flipping this
# flag back to True restores BOD approval without any further code changes.
REQUIRE_PUBLISH_APPROVAL = False

MAX_TAGS = 6
MAX_TAG_LEN = 24

NEWS_WEBHOOK = os.environ.get("NEWS_WEBHOOK", "")


def _md_excerpt(body: str, limit: int = 280) -> str:
    """Strip the heaviest markdown markup and collapse to a short plain-text teaser."""
    text = body or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)       # links → label
    text = re.sub(r"[`*_#>~]", "", text)                       # inline/heading marks
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _article_teaser(a: dict, limit: int = 280) -> str:
    """Short plain-text teaser for a Discord embed or a link preview.

    A power-rankings edition can publish with no intro at all — the table *is*
    the article — so rather than fall back to a generic line it teases its own
    top five, which is what a reader wants from the unfurl anyway.
    """
    text = _md_excerpt(a.get("body", ""), limit)
    if text:
        return text
    if pr.is_ranking(a) and a.get("final"):
        return " · ".join(f"{'T-' if r.get('tied') else ''}{r['rank']}. {r['team']}"
                          for r in a["final"][:5])
    return ""


def _announce_published(article: dict) -> None:
    """Fire-and-forget Discord webhook announcing a freshly published article."""
    url = f"https://nbn.today/news/view/?id={article['id']}"
    desc = _article_teaser(article)
    fields = []
    if pr.is_ranking(article) and article.get("final"):
        rows = article["final"]
        fields.append({
            "name": f"Edition #{article.get('edition')}" if article.get("edition") else "Consensus",
            "value": f"Average of {rows[0]['votes']} ballot"
                     f"{'' if rows[0]['votes'] == 1 else 's'}"
                     + (f" · {len(article.get('voters') or [])} voters invited" if article.get("voters") else ""),
            "inline": False,
        })
    tags = article.get("tags") or []
    if tags:
        fields.append({"name": "Tags", "value": ", ".join(tags), "inline": False})
    embed = {
        "title": article.get("title") or "Untitled",
        "url": url,
        "color": 0x3b82f6,
        "description": desc or "Read the full story on NBN.",
        "author": {"name": f"By {article.get('author', 'NBN')}"},
        "fields": fields,
        "footer": {"text": "Nothing But Net · nbn.today/news"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "username": "NBN Newsroom",
        "avatar_url": "https://nbn.today/logo.png",
        "embeds": [embed],
    }
    try:
        httpx.post(NEWS_WEBHOOK, json=payload, timeout=10)
    except Exception as exc:
        logger.warning("News Discord webhook failed: %s", exc)


def _notify_published(a: dict, actor: str) -> None:
    """Tell everyone credited on a piece that it is live — everyone but whoever
    just clicked publish, who does not need to be told what they did."""
    link = f"/news/view/?id={a['id']}"
    title = a.get("title") or "Untitled"
    author = a.get("author")
    for name in _credited(a):
        if name == actor:
            continue
        if name == author:
            text = f"{actor} published your article \"{title}\""
        else:
            text = f"The rankings you contributed to are live: \"{title}\""
        inbox.notify_member(name, text, link=link)


# One file per article (news/{id}.json), not one file for the whole archive —
# so a write to one article never touches another, and each gets its own diff
# in the backup repo. `load_articles` is still a directory scan and stays for
# the handful of things that are genuinely archive-wide (the list endpoint,
# series numbering, a previous edition's frozen `final`); everything else
# reads or writes exactly the one file it needs via _load_article/_save_article.
# NEWS_DIR.glob on a directory that doesn't exist yet just yields nothing, so
# only the writer needs to create it — same lazy-mkdir convention as
# AVATARS_DIR in auth.py.


def load_articles() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(NEWS_DIR.glob("*.json"))]


def _article_path(article_id: str) -> Path:
    return NEWS_DIR / f"{article_id}.json"


def _load_article(article_id: str) -> Optional[dict]:
    path = _article_path(article_id)
    return json.loads(path.read_text()) if path.exists() else None


def _save_article(a: dict):
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    _save_json(_article_path(a["id"]), a)


def _delete_article(article_id: str):
    _article_path(article_id).unlink(missing_ok=True)


def _can_publish(info: Optional[dict]) -> bool:
    """Editorial rights: curator or BOD (BOD implies curator via ROLE_IMPLIES) or
    admin. These roles may publish anyone's article and manage the queue."""
    if not info:
        return False
    return has_role(info, "curator") or has_role(info, "admin")


def _may_publish_article(a: dict, info: Optional[dict]) -> bool:
    """Who may move this specific article to 'published'. Editors always may.
    When approval isn't required, the author may also publish their own work."""
    if not info:
        return False
    if _can_publish(info):
        return True
    if not REQUIRE_PUBLISH_APPROVAL and a.get("author") == info.get("name"):
        return True
    return False


def _clean_tags(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags or []:
        t = str(t).strip()
        if not t:
            continue
        if len(t) > MAX_TAG_LEN:
            t = t[:MAX_TAG_LEN].strip()
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def _article_view(a: dict, viewer: Optional[str] = None) -> dict:
    """Public-facing copy. Drops nothing sensitive (no private fields) but normalises
    the comment list to a count for list views; full comments are returned by the
    single-article endpoint via include_comments.

    List views never carry ballots — they would be dead weight on every card and,
    while voting is blind, a leak. A ranking card gets its phase and a progress
    count instead; the ballots live on the detail endpoint."""
    out = {k: v for k, v in a.items() if k not in ("comments", "ballots", "blurbs")}
    out["comment_count"] = len(a.get("comments", []))
    if pr.is_ranking(a):
        out["ballot_progress"] = {
            "submitted": len(pr.submitted_ballots(a)),
            "voters": len(a.get("voters") or []),
        }
        out["viewer_is_voter"] = pr.is_voter(a, viewer)
    return out


def _article_detail(a: dict, info: Optional[dict] = None) -> dict:
    """Full article for the single-article endpoint. A ranking goes through
    `pr.redact` so a blind ballot stays blind; the previous edition (for movement
    arrows) is a one-file lookup by `prev_id`, not a scan of the whole archive."""
    if pr.is_ranking(a):
        viewer = info["name"] if info else None
        prev = _load_article(a["prev_id"]) if a.get("prev_id") else None
        out = pr.redact(a, viewer, _is_editor_of(a, info), (prev or {}).get("final"))
    else:
        out = dict(a)
    out["comment_count"] = len(a.get("comments", []))
    return out


def _is_editor_of(a: dict, info: Optional[dict]) -> bool:
    """Who may run this ranking: its author, plus the standing editors. The
    author is the one who called the vote, so they hold the phase controls,
    the invite list and blurb approval on their own article."""
    if not info:
        return False
    return _can_publish(info) or a.get("author") == info.get("name")


def _credited(a: dict) -> list[str]:
    """Everyone the published piece credits by name, author first.

    A plain article is one byline. A ranking is a group piece — the members
    whose ballots made the order and the members whose blurbs are printed under
    their names are as much its authors as whoever called the vote — so they
    are told when it goes live too. Deduped, order preserved.
    """
    names = [a.get("author")]
    if pr.is_ranking(a):
        names += list(pr.submitted_ballots(a).keys())
        names += [b.get("claimed_by") for b in (a.get("blurbs") or {}).values()
                  if (b.get("body") or "").strip()]
    out: list[str] = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


# ── Pydantic models ───────────────────────────────────────────────────────────

class ArticleCreate(BaseModel):
    title: str
    body: str = ""
    cover_image: Optional[str] = None
    tags: list[str] = []
    # Article type. Omitted or "custom" is the ordinary written article; the
    # power-rankings type adds the ballot block (see routers/news_rankings.py).
    type: Optional[str] = None
    series_id: Optional[str] = None
    prev_id: Optional[str] = None


class VotersIn(BaseModel):
    voters: list[str]


class PhaseIn(BaseModel):
    phase: str


class BallotIn(BaseModel):
    order: list[str]


class BaselineIn(BaseModel):
    # {ABBR: rank}; empty clears the baseline.
    ranks: dict[str, int] = {}
    label: Optional[str] = None


class BlurbIn(BaseModel):
    body: Optional[str] = None


class ArticlePatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    cover_image: Optional[str] = None
    tags: Optional[list[str]] = None


class PublishIn(BaseModel):
    # Optional override for the published date (YYYY-MM-DD). Defaults to today.
    publish_date: Optional[str] = None


class CommentCreate(BaseModel):
    body: str


# ── Editorial helpers ──────────────────────────────────────────────────────────

def _can_edit(a: dict, info: dict) -> bool:
    # Editors (curator/bod/admin) can edit anything; the author can always edit
    # their own article, including after it's published. Editing never changes
    # status (PATCH only touches title/body/cover/tags), so a published piece
    # stays published — it just gets the author's revisions live.
    if _can_publish(info):
        return True
    return a.get("author") == info["name"]


# ── Routes ──────────────────────────────────────────────────────────────────────

@router.get("/api/news")
def list_news(authorization: Optional[str] = Header(None)):
    info = _resolve_token(authorization)
    viewer = info["name"] if info else None
    editor = _can_publish(info)
    articles = load_articles()

    result = {"articles": [], "queue": [], "drafts": [], "ballots": []}
    for a in articles:
        status = a.get("status", "draft")
        if status == "published":
            result["articles"].append(_article_view(a, viewer))
        elif status == "submitted":
            if editor:
                result["queue"].append(_article_view(a, viewer))
            if viewer and a.get("author") == viewer:
                result["drafts"].append(_article_view(a, viewer))
        elif status == "draft":
            if viewer and a.get("author") == viewer:
                result["drafts"].append(_article_view(a, viewer))
        # An unpublished ranking a viewer is invited to vote on isn't their draft
        # and isn't in the editorial queue, so it gets its own bucket — otherwise
        # a voter has no way to find the ballot they were invited to.
        if (status != "published" and pr.is_ranking(a)
                and pr.is_voter(a, viewer) and a.get("author") != viewer):
            result["ballots"].append(_article_view(a, viewer))

    result["articles"].sort(key=lambda x: x.get("published_at") or "", reverse=True)
    result["queue"].sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
    result["drafts"].sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    result["ballots"].sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return result


@router.post("/api/news")
def create_article(body: ArticleCreate, info: dict = Depends(get_token_info)):
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title is required")
    now = datetime.now(timezone.utc).isoformat()
    article = {
        "id": str(uuid.uuid4()),
        "title": body.title.strip(),
        "body": body.body,
        "cover_image": (body.cover_image or "").strip() or None,
        "tags": _clean_tags(body.tags),
        "author": info["name"],
        "status": "draft",
        "created_at": now,
        "updated_at": now,
        "submitted_at": None,
        "published_at": None,
        "published_by": None,
        "comments": [],
    }
    if body.type == pr.PR_TYPE:
        article.update(pr.scaffold(body.series_id, body.prev_id))
    elif body.type not in (None, "", "custom"):
        raise HTTPException(status_code=422, detail=f"unknown article type: {body.type}")
    with _news_lock:
        _save_article(article)
    log_write(info, f"POST news — {article['id']!r} {body.title!r}")
    return _article_detail(article, info)


@router.get("/api/news/{article_id}")
def get_article(article_id: str, authorization: Optional[str] = Header(None)):
    info = _resolve_token(authorization)
    a = _load_article(article_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Article not found")
    viewer = info["name"] if info else None
    if a.get("status") != "published":
        is_author = viewer and a.get("author") == viewer
        # An invited voter has to be able to open the draft they are ranking.
        if not is_author and not _can_publish(info) and not pr.is_voter(a, viewer):
            raise HTTPException(status_code=403, detail="Not authorized to view this article")
    return _article_detail(a, info)


@router.patch("/api/news/{article_id}")
def patch_article(article_id: str, body: ArticlePatch, info: dict = Depends(get_token_info)):
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if not _can_edit(a, info):
            raise HTTPException(status_code=403, detail="Cannot edit this article")
        if body.title is not None:
            if not body.title.strip():
                raise HTTPException(status_code=422, detail="title cannot be empty")
            a["title"] = body.title.strip()
        if body.body is not None:
            a["body"] = body.body
        if body.cover_image is not None:
            a["cover_image"] = body.cover_image.strip() or None
        if body.tags is not None:
            a["tags"] = _clean_tags(body.tags)
        a["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_article(a)
    log_write(info, f"PATCH news/{article_id}")
    return _article_detail(a, info)


@router.post("/api/news/{article_id}/submit")
def submit_article(article_id: str, info: dict = Depends(get_token_info)):
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if a.get("author") != info["name"] and not _can_publish(info):
            raise HTTPException(status_code=403, detail="Only the author can submit this article")
        if a.get("status") != "draft":
            raise HTTPException(status_code=422, detail="Only drafts can be submitted")
        if not (a.get("body") or "").strip():
            raise HTTPException(status_code=422, detail="Cannot submit an empty article")
        now = datetime.now(timezone.utc).isoformat()
        a["status"] = "submitted"
        a["submitted_at"] = now
        a["updated_at"] = now
        _save_article(a)
    log_write(info, f"POST news/{article_id}/submit")
    return _article_detail(a, info)


# A team's whole active roster by current OVR, frozen onto a power-rankings
# article's `final` rows at publish time (see publish_article below) rather
# than fetched live on every page view -- mirrors the client-side computation
# news/view/index.html still does for an unpublished ballot preview (where
# there's no frozen `final` yet to read), but captured once so a published
# edition can't silently reflect a roster that has since been traded around.
#
# The page shows the top few as a strip and the rest inside the expandable
# team panel, so the whole list is frozen rather than a top-N slice -- the
# panel is a claim about the roster the ranking was voted on, and slicing here
# would have left it live-fetching the tail under a months-old ranking. Every
# field is one the panel reads: `pos` and `ovr` also feed the projected
# starting five (computeStartingFive, teams/lineup.js), and `age` is the age
# on publication day, not today's.
#
# Editions published before this existed carry a 6-entry `roster_strip`
# instead; the page falls back to it and simply has a shorter panel.
def _bio_age(dob: str, today: date) -> Optional[int]:
    if not dob:
        return None
    try:
        d = date.fromisoformat(dob)
    except ValueError:
        return None
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


def _team_roster_snapshot(team: str, bios: dict, ovr_current: dict) -> list[dict]:
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        return []
    _, rows = read_csv(path)
    today = league_today()
    players = []
    for row in rows:
        slug = row.get("SLUG", "").strip()
        if not slug:
            continue
        bio = bios.get(slug, {})
        if bio.get("type") == "dead":
            continue
        last, _, first = bio.get("name", "").partition(",")
        name = last.strip().title()
        if not name:
            continue
        players.append({
            "slug": slug,
            "name": name,
            "first": first.strip().title(),
            "ovr": ovr_current.get(slug, ""),
            "photo": bio.get("photo_url", ""),
            "pos": bio.get("pos") or [],
            "age": _bio_age(bio.get("dob", ""), today),
            "type": bio.get("type", ""),
        })
    players.sort(key=lambda p: p["ovr"] or 0, reverse=True)
    return players


# The team's most recent completed season out of standings-history.csv, frozen
# alongside the roster for the same reason: it is the record the ranking was
# argued over. "Most recent" is the last season that has a row for this team,
# not the current league season -- an edition published in October is arguing
# about a season with no rows yet.
def _team_season_line(team: str, standings: list[dict]) -> Optional[dict]:
    rows = [r for r in standings if (r.get("TEAM") or "").upper() == team.upper()]
    if not rows:
        return None
    row = max(rows, key=lambda r: r.get("SEASON") or "")
    return {
        "season": row.get("SEASON", ""),
        "w": row.get("W", ""),
        "l": row.get("L", ""),
        "pct": row.get("PCT", ""),
        "seed": row.get("SEED", ""),
        "off_rtg": row.get("OFF_RTG", ""),
        "def_rtg": row.get("DEF_RTG", ""),
        "diff": row.get("DIFF", ""),
        "playoff_result": row.get("PLAYOFF_RESULT", ""),
    }


@router.post("/api/news/{article_id}/publish")
def publish_article(article_id: str, body: PublishIn, info: dict = Depends(get_token_info)):
    published_at = None
    if body.publish_date:
        try:
            d = datetime.strptime(body.publish_date, "%Y-%m-%d")
            published_at = d.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            raise HTTPException(status_code=422, detail="publish_date must be YYYY-MM-DD")
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if not _may_publish_article(a, info):
            raise HTTPException(status_code=403, detail="Not authorized to publish this article")
        # A ranking's article *is* the table, so it may publish without an intro;
        # what it cannot publish without is ballots, which `pr.freeze` enforces.
        if not (a.get("body") or "").strip() and not pr.is_ranking(a):
            raise HTTPException(status_code=422, detail="Cannot publish an empty article")
        if a.get("status") not in ("submitted", "draft"):
            raise HTTPException(status_code=422, detail="Only queued or draft articles can be published")
        if pr.is_ranking(a):
            # freeze needs the whole archive — series numbering and the
            # previous edition's final both come from a cross-article scan —
            # so this is the one publish step that still reads the directory.
            articles = load_articles()
            try:
                pr.freeze(a, articles)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            bios = load_player_bios()
            ovr_history = load_ovr()
            ovr_current = {slug: entries[-1]["ovr"] for slug, entries in ovr_history.items() if entries}
            standings_path = DERIVED_DIR / "standings" / "standings-history.csv"
            _, standings = read_csv(standings_path) if standings_path.exists() else ("", [])
            for row in a["final"]:
                row["roster"] = _team_roster_snapshot(row["team"], bios, ovr_current)
                row["season_line"] = _team_season_line(row["team"], standings)
        now = datetime.now(timezone.utc).isoformat()
        a["status"] = "published"
        a["published_at"] = published_at or a.get("published_at") or now
        a["published_by"] = info["name"]
        a["updated_at"] = now
        _save_article(a)
    log_write(info, f"POST news/{article_id}/publish — by {info['name']}")
    _notify_published(a, info["name"])
    threading.Thread(target=_announce_published, args=(dict(a),), daemon=True).start()
    return _article_detail(a, info)


@router.post("/api/news/{article_id}/unpublish")
def unpublish_article(article_id: str, info: dict = Depends(require_role("curator"))):
    """Pull a published article back into the submission queue."""
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if a.get("status") != "published":
            raise HTTPException(status_code=422, detail="Article is not published")
        a["status"] = "submitted"
        a["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_article(a)
    log_write(info, f"POST news/{article_id}/unpublish")
    return _article_detail(a, info)


@router.post("/api/news/{article_id}/reject")
def reject_article(article_id: str, info: dict = Depends(require_role("curator"))):
    """Send a queued article back to its author as a draft."""
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if a.get("status") != "submitted":
            raise HTTPException(status_code=422, detail="Only queued articles can be rejected")
        a["status"] = "draft"
        a["submitted_at"] = None
        a["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_article(a)
    log_write(info, f"POST news/{article_id}/reject")
    return _article_detail(a, info)


@router.post("/api/news/{article_id}/comments")
def add_comment(article_id: str, body: CommentCreate, info: dict = Depends(get_token_info)):
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Comment body is required")
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if a.get("status") != "published":
            raise HTTPException(status_code=422, detail="Can only comment on published articles")
        comment = {
            "id": secrets.token_hex(8),
            "author": info["name"],
            "body": body.body.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        a.setdefault("comments", []).append(comment)
        _save_article(a)
    log_write(info, f"POST news/{article_id}/comments — {info['name']}")
    return comment


@router.delete("/api/news/{article_id}/comments/{comment_id}")
def delete_comment(article_id: str, comment_id: str, info: dict = Depends(get_token_info)):
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        comments = a.get("comments", [])
        comment = next((c for c in comments if c["id"] == comment_id), None)
        if not comment:
            raise HTTPException(status_code=404, detail="Comment not found")
        if comment["author"] != info["name"] and not _can_publish(info):
            raise HTTPException(status_code=403, detail="Not authorized to delete this comment")
        a["comments"] = [c for c in comments if c["id"] != comment_id]
        _save_article(a)
    log_write(info, f"DELETE news/{article_id}/comments/{comment_id}")
    return {"ok": True}


@router.delete("/api/news/{article_id}")
def delete_article(article_id: str, info: dict = Depends(get_token_info)):
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        is_author = a.get("author") == info["name"]
        if not is_author and not _can_publish(info):
            raise HTTPException(status_code=403, detail="Not authorized to delete this article")
        # Authors may only delete their own drafts; editors can delete anything.
        if a.get("status") != "draft" and not _can_publish(info):
            raise HTTPException(status_code=422, detail="Only editors can delete submitted or published articles")
        _delete_article(article_id)
    log_write(info, f"DELETE news/{article_id}")
    return {"ok": True}


@router.get("/api/news/rankings/series")
def list_ranking_series(authorization: Optional[str] = Header(None)):
    """Published editions grouped by series — what the 'previous edition' picker
    on the compose page reads, and where a series' rank history comes from."""
    articles = load_articles()
    out: dict[str, list] = {}
    for a in articles:
        if not pr.is_ranking(a) or a.get("status") != "published":
            continue
        out.setdefault(a.get("series_id") or "main", []).append({
            "id": a["id"], "title": a.get("title"), "edition": a.get("edition"),
            "published_at": a.get("published_at"), "author": a.get("author"),
        })
    for rows in out.values():
        rows.sort(key=lambda x: (x.get("edition") or 0, x.get("published_at") or ""))
    return out


# ── Power-rankings routes ─────────────────────────────────────────────────────
# The ballot phase, which sits between draft and publish on a power-rankings
# article. All the rules live in routers/news_rankings.py; these routes are the
# lock, the auth check and the ValueError → 422 translation, nothing more.

def _mutate_ranking(article_id: str, info: dict, editor_only: bool, fn):
    """Run `fn(article, is_editor)` against one ranking under the news lock.

    Every ranking write is the same four steps — find it, confirm it is a
    ranking, check who is asking, translate the rule module's ValueError into a
    422 — so they are written once here rather than six times below.
    """
    with _news_lock:
        a = _load_article(article_id)
        if a is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if not pr.is_ranking(a):
            raise HTTPException(status_code=422, detail="Not a power-rankings article")
        editor = _is_editor_of(a, info)
        if editor_only and not editor:
            raise HTTPException(status_code=403,
                                detail="Only the author or an editor can do this")
        try:
            fn(a, editor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        a["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_article(a)
    return _article_detail(a, info)


@router.put("/api/news/{article_id}/rankings/voters")
def set_ranking_voters(article_id: str, body: VotersIn,
                       info: dict = Depends(get_token_info)):
    """Replace the invite list. Names must exist in members.json.

    A newly invited member is told so in their inbox. Only the *new* names —
    the list is replaced wholesale on every save, so re-notifying the whole
    list would mean a fresh message for everyone each time one name is added."""
    known = set(load_members().keys())
    invited: list[str] = []
    ctx: dict = {}

    def go(a, editor):
        before = set(a.get("voters") or [])
        pr.set_voters(a, body.voters, known)
        invited.extend(n for n in a["voters"] if n not in before)
        ctx["title"] = a.get("title") or "Untitled"
        ctx["open"] = a.get("phase") == "voting"

    out = _mutate_ranking(article_id, info, True, go)
    log_write(info, f"PUT news/{article_id}/rankings/voters — {len(body.voters)} voters")
    tail = (" — the ballot is open" if ctx.get("open")
            else " — you'll be told when the ballot opens")
    for name in invited:
        if name == info["name"]:
            continue
        inbox.notify_member(
            name,
            f"{info['name']} invited you to rank teams for \"{ctx['title']}\"{tail}",
            link=f"/news/rankings/?id={article_id}")
    return out


@router.post("/api/news/{article_id}/rankings/phase")
def set_ranking_phase(article_id: str, body: PhaseIn,
                      info: dict = Depends(get_token_info)):
    """Move the ranking along: setup → voting → blurbs → final.

    Opening the ballot notifies the voters who still owe one — an invite lands
    during `setup`, when there is nothing yet to fill in, so this is the message
    that actually sends someone to their ballot. It covers the reopen case
    (blurbs → voting) the same way, which is what that move is for: chasing the
    stragglers, and only them.

    Blurbs open at `voting` too, so the message says so — the whole point of
    opening them early is that nobody waits for the vote to close to start
    writing, and they will not know unless they are told."""
    pending: list[str] = []
    ctx: dict = {}

    def go(a, editor):
        was = a.get("phase")
        pr.set_phase(a, body.phase)
        # `was != "voting"` matters: posting the phase a ranking is already in
        # is a no-op, and must not fire a second round of chasing messages.
        if a.get("phase") == "voting" and was != "voting":
            done = pr.submitted_ballots(a)
            pending.extend(n for n in (a.get("voters") or []) if n not in done)
        ctx["title"] = a.get("title") or "Untitled"

    out = _mutate_ranking(article_id, info, True, go)
    log_write(info, f"POST news/{article_id}/rankings/phase — {body.phase}")
    for name in pending:
        if name == info["name"]:
            continue
        inbox.notify_member(
            name, f"The ballot is open — rank all 30 teams for \"{ctx['title']}\""
                  " (you can claim and write blurbs now too)",
            link=f"/news/rankings/?id={article_id}")
    return out


@router.put("/api/news/{article_id}/rankings/ballot")
def put_ranking_ballot(article_id: str, body: BallotIn,
                       info: dict = Depends(get_token_info)):
    """Submit or replace your own ballot. Only while voting is open, and only
    if you were invited — an editor has no privileged path to a ballot here,
    because a ballot is a personal opinion and not an editorial act."""
    def go(a, editor):
        pr.set_ballot(a, info["name"], body.order)

    out = _mutate_ranking(article_id, info, False, go)
    log_write(info, f"PUT news/{article_id}/rankings/ballot — {info['name']}")
    return out


@router.put("/api/news/{article_id}/rankings/ballot/draft")
def put_ranking_draft(article_id: str, body: BallotIn,
                      info: dict = Depends(get_token_info)):
    """Autosave your working order without submitting it.

    Same auth as the ballot itself — your own, only while voting is open. It is
    deliberately *not* written to the audit log: the page calls this on a
    debounce as someone drags, and thirty rows of "saved a draft" would bury
    the writes that matter.
    """
    def go(a, editor):
        pr.save_draft(a, info["name"], body.order)

    return _mutate_ranking(article_id, info, False, go)


@router.put("/api/news/{article_id}/rankings/baseline")
def put_ranking_baseline(article_id: str, body: BaselineIn,
                         info: dict = Depends(get_token_info)):
    """Set the outside ranking a first edition moves against — a league sheet
    from before this existed, say. Editors only, and it does nothing to an
    edition that already has a `prev_id` or that has been published: movement
    is frozen into `final` at publish, so this is a pre-publish decision."""
    def go(a, editor):
        pr.set_baseline(a, body.ranks, body.label)

    out = _mutate_ranking(article_id, info, True, go)
    log_write(info, f"PUT news/{article_id}/rankings/baseline — {len(body.ranks)} teams")
    return out


@router.post("/api/news/{article_id}/rankings/blurbs/{team}/claim")
def claim_ranking_blurb(article_id: str, team: str,
                        info: dict = Depends(get_token_info)):
    def go(a, editor):
        pr.claim_blurb(a, team, info["name"], editor)

    out = _mutate_ranking(article_id, info, False, go)
    log_write(info, f"POST news/{article_id}/rankings/blurbs/{team}/claim — {info['name']}")
    return out


@router.delete("/api/news/{article_id}/rankings/blurbs/{team}/claim")
def release_ranking_blurb(article_id: str, team: str,
                          info: dict = Depends(get_token_info)):
    def go(a, editor):
        pr.release_blurb(a, team, info["name"], editor)

    out = _mutate_ranking(article_id, info, False, go)
    log_write(info, f"DELETE news/{article_id}/rankings/blurbs/{team}/claim")
    return out


@router.put("/api/news/{article_id}/rankings/blurbs/{team}")
def put_ranking_blurb(article_id: str, team: str, body: BlurbIn,
                      info: dict = Depends(get_token_info)):
    """Write a blurb — the claimer, or an editor."""
    def go(a, editor):
        pr.set_blurb(a, team, info["name"], body.body, editor)

    out = _mutate_ranking(article_id, info, False, go)
    log_write(info, f"PUT news/{article_id}/rankings/blurbs/{team} — {info['name']}")
    return out

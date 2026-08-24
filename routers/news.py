import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .constants import NEWS_FILE, logger
from .storage import _load_json, _save_json, log_write
from .auth import get_token_info, has_role, require_role, _resolve_token

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

NEWS_WEBHOOK = (
    "https://discord.com/api/webhooks/1518995213979488406/"
    "Uc1p3aAUOWhIkO7ToP26P02B0NJenZXQ06uDQwAKyXlPImV6J5ZbU6664wHc2UaE3srC"
)


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


def _announce_published(article: dict) -> None:
    """Fire-and-forget Discord webhook announcing a freshly published article."""
    url = f"https://nbn.today/news/view/?id={article['id']}"
    desc = _md_excerpt(article.get("body", ""))
    fields = []
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


def load_articles() -> list[dict]:
    return _load_json(NEWS_FILE, [])


def save_articles(articles: list[dict]):
    _save_json(NEWS_FILE, articles)


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
    single-article endpoint via include_comments."""
    out = {k: v for k, v in a.items() if k != "comments"}
    out["comment_count"] = len(a.get("comments", []))
    return out


def _article_detail(a: dict) -> dict:
    out = dict(a)
    out["comment_count"] = len(a.get("comments", []))
    return out


# ── Pydantic models ───────────────────────────────────────────────────────────

class ArticleCreate(BaseModel):
    title: str
    body: str = ""
    cover_image: Optional[str] = None
    tags: list[str] = []


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

def _find(articles: list[dict], article_id: str):
    idx = next((i for i, a in enumerate(articles) if a["id"] == article_id), None)
    return idx


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

    result = {"articles": [], "queue": [], "drafts": []}
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

    result["articles"].sort(key=lambda x: x.get("published_at") or "", reverse=True)
    result["queue"].sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
    result["drafts"].sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
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
    with _news_lock:
        articles = load_articles()
        articles.append(article)
        save_articles(articles)
    log_write(info, f"POST news — {article['id']!r} {body.title!r}")
    return _article_detail(article)


@router.get("/api/news/{article_id}")
def get_article(article_id: str, authorization: Optional[str] = Header(None)):
    info = _resolve_token(authorization)
    articles = load_articles()
    idx = _find(articles, article_id)
    if idx is None:
        raise HTTPException(status_code=404, detail="Article not found")
    a = articles[idx]
    if a.get("status") != "published":
        is_author = info and a.get("author") == info["name"]
        if not is_author and not _can_publish(info):
            raise HTTPException(status_code=403, detail="Not authorized to view this article")
    return _article_detail(a)


@router.patch("/api/news/{article_id}")
def patch_article(article_id: str, body: ArticlePatch, info: dict = Depends(get_token_info)):
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
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
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"PATCH news/{article_id}")
    return _article_detail(a)


@router.post("/api/news/{article_id}/submit")
def submit_article(article_id: str, info: dict = Depends(get_token_info)):
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
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
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"POST news/{article_id}/submit")
    return _article_detail(a)


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
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
        if not _may_publish_article(a, info):
            raise HTTPException(status_code=403, detail="Not authorized to publish this article")
        if not (a.get("body") or "").strip():
            raise HTTPException(status_code=422, detail="Cannot publish an empty article")
        if a.get("status") not in ("submitted", "draft"):
            raise HTTPException(status_code=422, detail="Only queued or draft articles can be published")
        now = datetime.now(timezone.utc).isoformat()
        a["status"] = "published"
        a["published_at"] = published_at or a.get("published_at") or now
        a["published_by"] = info["name"]
        a["updated_at"] = now
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"POST news/{article_id}/publish — by {info['name']}")
    threading.Thread(target=_announce_published, args=(dict(a),), daemon=True).start()
    return _article_detail(a)


@router.post("/api/news/{article_id}/unpublish")
def unpublish_article(article_id: str, info: dict = Depends(require_role("curator"))):
    """Pull a published article back into the submission queue."""
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
        if a.get("status") != "published":
            raise HTTPException(status_code=422, detail="Article is not published")
        a["status"] = "submitted"
        a["updated_at"] = datetime.now(timezone.utc).isoformat()
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"POST news/{article_id}/unpublish")
    return _article_detail(a)


@router.post("/api/news/{article_id}/reject")
def reject_article(article_id: str, info: dict = Depends(require_role("curator"))):
    """Send a queued article back to its author as a draft."""
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
        if a.get("status") != "submitted":
            raise HTTPException(status_code=422, detail="Only queued articles can be rejected")
        a["status"] = "draft"
        a["submitted_at"] = None
        a["updated_at"] = datetime.now(timezone.utc).isoformat()
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"POST news/{article_id}/reject")
    return _article_detail(a)


@router.post("/api/news/{article_id}/comments")
def add_comment(article_id: str, body: CommentCreate, info: dict = Depends(get_token_info)):
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Comment body is required")
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
        if a.get("status") != "published":
            raise HTTPException(status_code=422, detail="Can only comment on published articles")
        comment = {
            "id": secrets.token_hex(8),
            "author": info["name"],
            "body": body.body.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        a.setdefault("comments", []).append(comment)
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"POST news/{article_id}/comments — {info['name']}")
    return comment


@router.delete("/api/news/{article_id}/comments/{comment_id}")
def delete_comment(article_id: str, comment_id: str, info: dict = Depends(get_token_info)):
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
        comments = a.get("comments", [])
        comment = next((c for c in comments if c["id"] == comment_id), None)
        if not comment:
            raise HTTPException(status_code=404, detail="Comment not found")
        if comment["author"] != info["name"] and not _can_publish(info):
            raise HTTPException(status_code=403, detail="Not authorized to delete this comment")
        a["comments"] = [c for c in comments if c["id"] != comment_id]
        articles[idx] = a
        save_articles(articles)
    log_write(info, f"DELETE news/{article_id}/comments/{comment_id}")
    return {"ok": True}


@router.delete("/api/news/{article_id}")
def delete_article(article_id: str, info: dict = Depends(get_token_info)):
    with _news_lock:
        articles = load_articles()
        idx = _find(articles, article_id)
        if idx is None:
            raise HTTPException(status_code=404, detail="Article not found")
        a = articles[idx]
        is_author = a.get("author") == info["name"]
        if not is_author and not _can_publish(info):
            raise HTTPException(status_code=403, detail="Not authorized to delete this article")
        # Authors may only delete their own drafts; editors can delete anything.
        if a.get("status") != "draft" and not _can_publish(info):
            raise HTTPException(status_code=422, detail="Only editors can delete submitted or published articles")
        articles.pop(idx)
        save_articles(articles)
    log_write(info, f"DELETE news/{article_id}")
    return {"ok": True}

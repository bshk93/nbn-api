"""League lore — the storylines, sagas and season notes NBN Today writers draw on.

The lore was compiled from the whole #leaguetalk history (2020 on) and lives as
plain markdown files in `$DATA_DIR/lore/`, one file per document:

    lore.md      the storyline digest — read this first
    sagas.md     the big stories, told in full with every quote
    2020.md …    the season-by-season working notes behind both

A document's name is its file stem (`lore`, `sagas`, `2021-22`). Writers — human
or agent — read it here instead of needing shell access to the box:

    GET  /api/lore                 the documents and their section headings
    GET  /api/lore/search?q=…      the paragraphs that mention every term
    GET  /api/lore/{name}          one document's markdown
    PUT  /api/lore/{name}          replace one (curator or bod)

Reads need a member token. Nothing here filters content: the banned members and
the old scandals are the point of the lore, and writers are meant to use them.
History is kept by the data directory's git backup, not here.
"""

import re
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import get_token_info, require_any_role
from .constants import DATA_DIR
from .storage import _atomic_write, log_write

router = APIRouter()

LORE_DIR = DATA_DIR / "lore"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_BYTES = 2_000_000
MAX_RESULTS = 100

# Search ranks the digest above the full sagas above the raw season notes, so
# the first hits are the ones written to be read.
_DOC_RANK = {"lore": 0, "sagas": 1}

_write_lock = threading.Lock()


def _path(name: str):
    if not NAME_RE.match(name):
        raise HTTPException(status_code=422, detail="name must be lowercase letters, digits and dashes")
    return LORE_DIR / f"{name}.md"


def _doc_names() -> list[str]:
    if not LORE_DIR.is_dir():
        return []
    names = [p.stem for p in LORE_DIR.glob("*.md") if NAME_RE.match(p.stem)]
    return sorted(names, key=lambda n: (_DOC_RANK.get(n, 2), n))


def _title(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return name


def _updated_at(path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _blocks(text: str) -> list[dict]:
    """Split markdown into searchable blocks, each carrying the heading it sits
    under. A block is a top-level list item with its continuation lines, or a
    paragraph, or a table — the unit a writer would quote from."""
    blocks: list[dict] = []
    heading = ""
    current: list[str] = []

    def flush():
        if current:
            body = "\n".join(current).strip()
            if body:
                blocks.append({"section": heading, "text": body})
            current.clear()

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
        elif not line.strip():
            flush()
        elif line.startswith(("- ", "* ")) or (line.startswith("|") and current
                                                and not current[-1].startswith("|")):
            # A new top-level item starts a new block; a table row continues a table.
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()
    return blocks


@router.get("/api/lore")
def list_lore(_: dict = Depends(get_token_info)):
    docs = []
    for name in _doc_names():
        path = LORE_DIR / f"{name}.md"
        text = path.read_text()
        docs.append({
            "name": name,
            "title": _title(text, name),
            "bytes": len(text.encode()),
            "updated_at": _updated_at(path),
            "sections": [line.lstrip("#").strip() for line in text.splitlines()
                         if line.startswith("## ")],
        })
    return {"docs": docs}


@router.get("/api/lore/search")
def search_lore(q: str = Query(..., min_length=2),
                limit: int = Query(25, ge=1, le=MAX_RESULTS),
                doc: Optional[str] = None,
                _: dict = Depends(get_token_info)):
    """Every block that contains all the query's words (case-insensitive),
    best documents first, then most mentions first."""
    terms = [t for t in q.lower().split() if t]
    names = [doc] if doc else _doc_names()
    hits = []
    for name in names:
        path = _path(name)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"No lore document {name!r}")
        for i, block in enumerate(_blocks(path.read_text())):
            low = block["text"].lower()
            if all(t in low for t in terms):
                score = sum(low.count(t) for t in terms)
                hits.append((_DOC_RANK.get(name, 2), -score, name, i, block))
    hits.sort(key=lambda h: h[:4])
    return {
        "q": q,
        "total": len(hits),
        "results": [{"doc": name, "section": block["section"], "text": block["text"]}
                    for _, _, name, _, block in hits[:limit]],
    }


@router.get("/api/lore/{name}")
def get_lore(name: str, _: dict = Depends(get_token_info)):
    path = _path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No lore document {name!r}")
    text = path.read_text()
    return {"name": name, "title": _title(text, name), "updated_at": _updated_at(path),
            "markdown": text}


class LoreIn(BaseModel):
    markdown: str


@router.put("/api/lore/{name}")
def put_lore(name: str, body: LoreIn, info: dict = Depends(require_any_role("curator", "bod"))):
    path = _path(name)
    text = body.markdown
    if not text.strip():
        raise HTTPException(status_code=422, detail="markdown is empty")
    if len(text.encode()) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"over {MAX_BYTES} bytes")
    if not text.endswith("\n"):
        text += "\n"
    with _write_lock:
        LORE_DIR.mkdir(exist_ok=True)
        existed = path.exists()
        _atomic_write(path, text)
    log_write(info, f"{'replaced' if existed else 'created'} lore/{name}.md ({len(text.encode())} bytes)")
    return {"name": name, "title": _title(text, name), "updated_at": _updated_at(path),
            "created": not existed}

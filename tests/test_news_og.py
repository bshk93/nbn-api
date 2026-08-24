"""Regression tests for the link-preview stub behind /news/view/ (GET /api/og/news).

/news/view/ renders client-side, so an unfurler that fetches it sees only the
static placeholder tags and every article gets the same card. nginx sends known
unfurlers here instead. What must keep being true:

  * a published article's own title, excerpt, author and cover image reach the
    tags — that is the entire point;
  * a draft, a submitted-but-unpublished article, an unknown id and a missing
    id all fall back to the generic card, never leaking an unpublished title;
  * a cover image that can't be made absolute is dropped rather than emitted —
    Discord ignores a relative og:image and would show no picture at all;
  * every value is escaped, since titles and tags are member-supplied.

    venv/bin/python -m tests.test_news_og
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.news as news  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def meta(doc: str, prop: str) -> str | None:
    m = re.search(rf'<meta property="{re.escape(prop)}" content="([^"]*)">', doc)
    return m.group(1) if m else None


ARTICLES = [
    {"id": "a1", "status": "published", "title": "Hawks fleece the Kings",
     "body": "# Deadline\n\nThe **Hawks** landed [a star](/players/?p=x) for two firsts.",
     "author": "bryn", "tags": ["Trades", "ATL"], "cover_image": "/img/hawks.png",
     "published_at": "2026-08-24T12:00:00+00:00"},
    {"id": "a2", "status": "draft", "title": "Secret scoop nobody should see",
     "body": "Not published yet.", "author": "bryn", "tags": [], "cover_image": None},
    {"id": "a3", "status": "submitted", "title": "Also not public",
     "body": "In the queue.", "author": "bryn", "tags": [], "cover_image": None},
    {"id": "a4", "status": "published", "title": 'Quote "trouble" & <script>',
     "body": "Body text.", "author": "bryn", "tags": [], "cover_image": "img/relative.png"},
]

news.load_articles = lambda: [dict(a) for a in ARTICLES]

print("published article")
doc = news.news_og_html("a1")
check("title carries the headline", meta(doc, "og:title") == "Hawks fleece the Kings — NBN News")
check("og:type is article", meta(doc, "og:type") == "article")
check("url keeps the id", meta(doc, "og:url") == "https://nbn.today/news/view/?id=a1")
check("description leads with the byline", meta(doc, "og:description").startswith("By bryn. "))
check("description is the body, demarkdowned",
      "Deadline The Hawks landed a star for two firsts." in meta(doc, "og:description"))
check("cover image is made absolute", meta(doc, "og:image") == "https://nbn.today/img/hawks.png")
check("a real cover image ships no invented dimensions", meta(doc, "og:image:width") is None)
check("author tag", meta(doc, "article:author") == "bryn")
check("published time tag", meta(doc, "article:published_time") == "2026-08-24T12:00:00+00:00")
check("both tags emitted", doc.count('property="article:tag"') == 2)
check("canonical points at the article", 'rel="canonical" href="https://nbn.today/news/view/?id=a1"' in doc)

print("\nunpublished and unknown fall back")
for name, aid in [("draft", "a2"), ("submitted", "a3"), ("unknown id", "zz"), ("no id", "")]:
    doc = news.news_og_html(aid)
    check(f"{name} → generic title", meta(doc, "og:title") == "Article — NBN News")
    check(f"{name} → generic description", meta(doc, "og:description") == "An article from NBN News.")
    check(f"{name} → default card", meta(doc, "og:image") == "https://nbn.today/og-default.png")
    check(f"{name} → og:type website", meta(doc, "og:type") == "website")
    check(f"{name} → idless url", meta(doc, "og:url") == "https://nbn.today/news/view/")
doc = news.news_og_html("a2")
check("a draft's title never appears", "Secret scoop" not in doc)
doc = news.news_og_html("a3")
check("a submitted article's title never appears", "Also not public" not in doc)

print("\ndefault card declares its known dimensions")
doc = news.news_og_html("")
check("width", meta(doc, "og:image:width") == "1200")
check("height", meta(doc, "og:image:height") == "630")

print("\nhostile input")
doc = news.news_og_html("a4")
check("quotes and angle brackets escaped",
      meta(doc, "og:title") == "Quote &quot;trouble&quot; &amp; &lt;script&gt; — NBN News")
check("no raw script tag anywhere in the document", "<script>" not in doc)
check("an unresolvable cover image falls back to the default card",
      meta(doc, "og:image") == "https://nbn.today/og-default.png")

print("\nurl helper")
check("absolute url passes through", news._og_abs("https://i.imgur.com/x.png") == "https://i.imgur.com/x.png")
check("site-relative gets the host", news._og_abs("/og/team-atl.png") == "https://nbn.today/og/team-atl.png")
check("bare filename is rejected", news._og_abs("x.png") is None)
check("empty is rejected", news._og_abs("") is None)
check("None is rejected", news._og_abs(None) is None)

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

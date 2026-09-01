"""Regression tests for the link-preview heads behind the client-side pages
(GET /api/og/{news,player,proposal,member} — routers/og.py).

Four pages are one shell serving many items, so an unfurler that fetches one
sees only the shell's placeholder tags and every item gets the same card. nginx
sends known unfurlers to these endpoints instead. What must keep being true:

  * the item's own title, description and image reach the tags — the point;
  * nothing unpublished leaks: a draft article, a draft proposal, an unknown id
    and a missing id all fall back to the page's own static card;
  * the fallback is lifted from the shell on disk, not restated in Python, so
    it can't drift from what build/og_tags.py wrote;
  * an image that can't be made absolute is dropped rather than emitted —
    Discord ignores a relative og:image and would show no picture at all;
  * a proposal card never shows a live tally, which is privileged;
  * everything is escaped, since titles, tags and member names are user input.

    venv/bin/python -m tests.test_og
"""
from __future__ import annotations

import html
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.og as og  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def meta(doc: str, prop: str) -> str | None:
    m = re.search(rf'<meta property="{re.escape(prop)}" content="([^"]*)">', doc)
    return m.group(1) if m else None


# ── A fake docroot, so the static fallback is exercised for real ──────────────

SITE_DIR = Path(tempfile.mkdtemp())
og.SITE_DIR = SITE_DIR

SHELLS = {
    "news/view": ("Article — NBN News", "An article from NBN News."),
    "players": ("Players — NBN", "Every player in NBN — career stats and contracts."),
    "proposals/view": ("Proposal — NBN", "A rule proposal before the NBN league."),
    "members/profile": ("Member — NBN", "An NBN member’s tenures and league history."),
}
for path, (title, desc) in SHELLS.items():
    d = SITE_DIR / path
    d.mkdir(parents=True)
    (d / "index.html").write_text(
        f"<!DOCTYPE html>\n<html>\n<head>\n  <title>{title}</title>\n"
        "  <!-- NBN:og -->\n"
        f'  <meta name="description" content="{desc}">\n'
        '  <meta property="og:type" content="website">\n'
        f'  <meta property="og:title" content="{title}">\n'
        f'  <meta property="og:description" content="{desc}">\n'
        '  <meta property="og:image" content="https://nbn.today/og-default.png">\n'
        "  <!-- /NBN:og -->\n</head>\n<body></body>\n</html>\n")

print("static fallback comes from the shell on disk")
doc = og._static_head("/players/")
check("title", "<title>Players — NBN</title>" in doc)
check("og:title is the shell's", meta(doc, "og:title") == "Players — NBN")
check("og:description is the shell's",
      meta(doc, "og:description") == "Every player in NBN — career stats and contracts.")
check("a missing shell still yields a card, not an exception",
      meta(og._static_head("/nope/"), "og:title") == "NBN")

# ── News ──────────────────────────────────────────────────────────────────────

ARTICLES = [
    {"id": "a1", "status": "published", "title": "Hawks fleece the Kings",
     "body": "# Deadline\n\nThe **Hawks** landed [a star](/players/?p=x) for two firsts.",
     "author": "bryn", "tags": ["Trades", "ATL"], "cover_image": "/img/hawks.png",
     "published_at": "2026-08-24T12:00:00+00:00"},
    {"id": "a2", "status": "draft", "title": "Secret scoop nobody should see",
     "body": "Not published.", "author": "bryn", "tags": [], "cover_image": None},
    {"id": "a3", "status": "submitted", "title": "Also not public",
     "body": "In the queue.", "author": "bryn", "tags": [], "cover_image": None},
    {"id": "a4", "status": "published", "title": 'Quote "trouble" & <script>',
     "body": "Body.", "author": "bryn", "tags": [], "cover_image": "img/relative.png"},
]
og._load_article = lambda aid: next((dict(a) for a in ARTICLES if a["id"] == aid), None)

print("\nnews — published article")
doc = og.news_head("a1")
check("headline", meta(doc, "og:title") == "Hawks fleece the Kings — NBN News")
check("og:type article", meta(doc, "og:type") == "article")
check("url keeps the id", meta(doc, "og:url") == "https://nbn.today/news/view/?id=a1")
check("byline leads the description", meta(doc, "og:description").startswith("By bryn. "))
check("body is demarkdowned into the description",
      "Deadline The Hawks landed a star for two firsts." in meta(doc, "og:description"))
check("cover image made absolute", meta(doc, "og:image") == "https://nbn.today/img/hawks.png")
check("no invented dimensions on a real cover", meta(doc, "og:image:width") is None)
check("article:author", meta(doc, "article:author") == "bryn")
check("article:published_time", meta(doc, "article:published_time") == "2026-08-24T12:00:00+00:00")
check("one article:tag each", doc.count('property="article:tag"') == 2)
check("canonical", 'rel="canonical" href="https://nbn.today/news/view/?id=a1"' in doc)

print("\nnews — nothing unpublished leaks")
for name, aid in [("draft", "a2"), ("submitted", "a3"), ("unknown id", "zz"), ("no id", "")]:
    doc = og.news_head(aid)
    check(f"{name} → the shell's card", meta(doc, "og:title") == "Article — NBN News")
check("a draft's headline appears nowhere", "Secret scoop" not in og.news_head("a2"))
check("a submitted headline appears nowhere", "Also not public" not in og.news_head("a3"))

print("\nnews — hostile input")
doc = og.news_head("a4")
check("quotes and brackets escaped",
      meta(doc, "og:title") == "Quote &quot;trouble&quot; &amp; &lt;script&gt; — NBN News")
check("no raw script tag in the document", "<script>" not in doc)
check("unresolvable cover falls back to the default card",
      meta(doc, "og:image") == "https://nbn.today/og-default.png")
check("the default card declares its known size", meta(doc, "og:image:width") == "1200")

# ── Players ───────────────────────────────────────────────────────────────────

BIOS = {
    "curry-stephen": {"name": "CURRY, STEPHEN", "pos": ["PG", "SG"], "dob": "1988-03-14",
                      "height": "6'2\"", "photo_url": "https://cdn.nba.com/curry.png"},
    "nobody-jim": {"name": "NOBODY, JIM", "pos": [], "dob": None, "photo_url": ""},
}
og.load_player_bios = lambda: dict(BIOS)
og.load_ovr = lambda: {"curry-stephen": [{"date": "2026-01-01", "ovr": 90},
                                         {"date": "2026-08-01", "ovr": 96}]}
og._build_team_map = lambda: {"curry-stephen": "GSW"}
og._career_line = lambda slug: "25.4 PPG, 5.1 RPG, 6.3 APG over 412 games" if slug == "curry-stephen" else None

print("\nplayer")
doc = og.player_head("curry-stephen")
check("name is flipped to first-last", meta(doc, "og:title") == "Stephen Curry — NBN")
check("url keeps the slug", meta(doc, "og:url") == "https://nbn.today/players/?p=curry-stephen")
desc = meta(doc, "og:description")
check("positions", desc.startswith("PG/SG · "))
check("team is spelled out", "Golden State Warriors" in desc)
check("current OVR is the last entry, not the first", "96 OVR" in desc and "90 OVR" not in desc)
check("career line", desc.endswith("25.4 PPG, 5.1 RPG, 6.3 APG over 412 games."))
check("headshot is the card", meta(doc, "og:image") == "https://cdn.nba.com/curry.png")
check("og:type profile", meta(doc, "og:type") == "profile")

doc = og.player_head("nobody-jim")
check("a bare bio still gets a sentence", meta(doc, "og:description").startswith("Jim Nobody on NBN"))
check("and the default card", meta(doc, "og:image") == "https://nbn.today/og-default.png")
check("unknown slug → the shell's card", meta(og.player_head("who-what"), "og:title") == "Players — NBN")
check("no slug → the shell's card", meta(og.player_head(""), "og:title") == "Players — NBN")

# ── Proposals ─────────────────────────────────────────────────────────────────

PROPOSALS = [
    {"id": "p1", "number": 12, "title": "Adopt the new lottery rules", "status": "voting",
     "author": "bryn", "body": "SUMMARY\n\n- Adopt the **new** draft rules.", "outcome": None,
     "votes": {"a": "for", "b": "against"}},
    {"id": "p2", "number": 9, "title": "Raise the roster floor", "status": "closed",
     "author": "kim", "body": "Body.", "outcome": "passed", "votes": {"a": "for"}},
    {"id": "p3", "number": None, "title": "Half-finished idea", "status": "draft",
     "author": "bryn", "body": "Body.", "outcome": None, "votes": {}},
]
og.load_proposals = lambda: [dict(p) for p in PROPOSALS]

print("\nproposal")
doc = og.proposal_head("p1")
check("number is in the title", meta(doc, "og:title") == "Proposal 12: Adopt the new lottery rules — NBN")
check("state and author lead", meta(doc, "og:description").startswith("Voting open · by bryn."))
check("body follows", "Adopt the new draft rules." in meta(doc, "og:description"))
check("no live tally in the card", "for" not in meta(doc, "og:description").split("·")[0])
check("url keeps the id", meta(doc, "og:url") == "https://nbn.today/proposals/view/?id=p1")
doc = og.proposal_head("p2")
check("a decided proposal shows its outcome", meta(doc, "og:description").startswith("Passed · by kim."))
check("draft → the shell's card", meta(og.proposal_head("p3"), "og:title") == "Proposal — NBN")
check("a draft's title appears nowhere", "Half-finished" not in og.proposal_head("p3"))
check("unknown id → the shell's card", meta(og.proposal_head("zz"), "og:title") == "Proposal — NBN")

# ── Members ───────────────────────────────────────────────────────────────────

MEMBERS = {
    "bryn": {"roles": ["admin"], "has_avatar": True,
             "tenures": [{"team": "ORL", "start": "2020-07-01", "end": "2022-07-25", "position": "owner"},
                         {"team": "GSW", "start": "2022-07-25", "end": None, "position": "owner"}]},
    "AJGoh": {"roles": [], "has_avatar": False,
              "tenures": [{"team": "ORL", "start": "2020-07-01", "end": "2022-07-25", "position": "owner"}]},
    "newbie": {"roles": [], "has_avatar": False, "tenures": []},
}
og.load_members = lambda: {k: dict(v) for k, v in MEMBERS.items()}
og._owner_record = lambda name: {"owner": "bryn", "seasons": "6", "reg_w": "210",
                                 "reg_l": "182", "championships": "1"} if name == "bryn" else None

print("\nmember")
doc = og.member_head("bryn")
check("title", meta(doc, "og:title") == "bryn — NBN")
desc = meta(doc, "og:description")
check("current team, spelled out", desc.startswith("Owner of the Golden State Warriors"))
check("seasons and record", "6 seasons · 210-182" in desc)
check("championships pluralised for one", "1 championship" in desc and "1 championships" not in desc)
check("avatar is the card", meta(doc, "og:image") == "https://nbn.today/api/members/bryn/avatar")
check("url is path-based", meta(doc, "og:url") == "https://nbn.today/members/bryn")

doc = og.member_head("AJGoh")
check("a former owner reads as former", meta(doc, "og:description") == "Formerly ORL")
check("no avatar → default card", meta(doc, "og:image") == "https://nbn.today/og-default.png")
check("a member with no tenure still gets a sentence",
      html.unescape(meta(og.member_head("newbie"), "og:description")).startswith("newbie's NBN profile"))
check("name matching is case-insensitive", meta(og.member_head("BRYN"), "og:title") == "bryn — NBN")
check("unknown member → the shell's card", meta(og.member_head("ghost"), "og:title") == "Member — NBN")
check("no name → the shell's card", meta(og.member_head(""), "og:title") == "Member — NBN")

# ── The URL helper ────────────────────────────────────────────────────────────

print("\nurl helper")
check("absolute passes through", og._abs_url("https://i.imgur.com/x.png") == "https://i.imgur.com/x.png")
check("site-relative gets the host", og._abs_url("/og/team-atl.png") == "https://nbn.today/og/team-atl.png")
check("bare filename rejected", og._abs_url("x.png") is None)
check("empty rejected", og._abs_url("") is None)
check("None rejected", og._abs_url(None) is None)

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nall checks passed")

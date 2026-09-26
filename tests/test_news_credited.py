"""`_credited` / the `credited` field on `_article_detail` (routers/news.py).

Power rankings can have several credited authors — whoever called the vote,
whoever submitted a ballot, and whoever wrote a blurb — so an article page
can offer "tip any or all of them" instead of just the single top-level
`author`. Pins two things:

  * a plain article credits just its author; a ranking credits the author,
    then submitted voters, then blurb writers with a non-empty body —
    deduped, order preserved, unclaimed/empty blurbs excluded
  * `credited` is present on a published article's detail view and
    deliberately empty pre-publish, so the field never leaks who has
    submitted a ballot while voting is still meant to be blind

    venv/bin/python -m tests.test_news_credited
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.news as news  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("_credited")

plain = {"author": "alice"}
check("plain article credits just its author", news._credited(plain) == ["alice"])

ranking = {
    "type": "power_rankings",
    "author": "alice",
    "ballots": {
        "alice": {"order": ["BOS"], "submitted_at": "2026-09-01"},
        "bob":   {"order": ["LAL"], "submitted_at": "2026-09-01"},
        "carol": {"order": [], "submitted_at": None},  # never submitted
    },
    "blurbs": {
        "BOS": {"claimed_by": "bob", "body": "Still the class of the East."},
        "LAL": {"claimed_by": "dave", "body": ""},       # claimed, never written
        "MIA": {"claimed_by": None, "body": ""},         # never claimed
    },
}
credited = news._credited(ranking)
check("author first", credited[0] == "alice")
check("submitted voters included", "bob" in credited and "carol" not in credited)
check("blurb writer with real body included", credited.count("bob") == 1)  # deduped, not double-listed
check("claimed-but-empty blurb excludes dave", "dave" not in credited)
check("unclaimed blurb credits nobody extra", len(credited) == 2)

print("\n_article_detail gating")

published = {**ranking, "id": "r1", "status": "published", "comments": [],
             "voters": ["alice", "bob"], "phase": "final", "prev_id": None, "final": []}
news._load_article = lambda article_id: None
news.pr.redact = lambda a, viewer, is_editor, baseline: dict(a)
out = news._article_detail(published, info=None)
check("published ranking exposes credited", out["credited"] == ["alice", "bob"])

voting = {**published, "status": "submitted", "phase": "voting"}
out2 = news._article_detail(voting, info=None)
check("unpublished ranking's credited is empty, not a leak of who has voted", out2["credited"] == [])

plain_article = {"id": "p1", "author": "alice", "status": "published", "comments": []}
out3 = news._article_detail(plain_article, info=None)
check("a published plain article still credits its author", out3["credited"] == ["alice"])

print("\n_byline / _article_view")

check("one name is just the name", news._byline(plain) == "alice")
three = {**ranking, "blurbs": {"LAL": {"claimed_by": "dave", "body": "Rebuilding."}}}
check("three names: commas, then 'and'", news._byline(three) == "alice, bob and dave")
out4 = news._article_view(published, None)
check("list view carries credited once published", out4["credited"] == ["alice", "bob"])
out5 = news._article_view(voting, None)
check("list view hides credited before publish", out5["credited"] == [])
check("byline of the published ranking", news._byline(published) == "alice and bob")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")

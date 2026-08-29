"""End-to-end route tests for the power-rankings article type (routers/news.py).

tests/test_news_rankings.py pins the rules; this pins the *wiring* — the auth
gates, the redaction on the read path, the list buckets and the publish
handoff — by calling the real route functions against an in-memory article
store. That layer is where the bugs actually were: the first version of this
suite caught a publish path that would freeze a half-collected ranking and, in
the same move, make ballots public that had been cast on the promise of staying
blind until the author closed the vote.

The cases worth knowing about:

  * **The author is a voter too.** alice authors the ranking and votes in it, so
    every "can the author see it" check here is the real one, not a stand-in.
  * **A curator is an editor but not a spectator** — they can run the ranking
    and still can't read a ballot while voting is open.
  * **An invited voter can open an unpublished article**, which the plain news
    rules would otherwise 403; that carve-out is what lets a ballot be found.
  * **Publishing freezes**, and a ballot cast afterwards must not move a
    published order. That covers the *roster* each row carries as much as the
    order: the expandable team panel renders frozen data only, so publish has
    to stamp the whole roster rather than the six the strip shows.
  * **Plain articles are untouched** by any of it.

    venv/bin/python -m tests.test_news_rankings_routes
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.news as news  # noqa: E402


STORE = []
news.load_articles = lambda: STORE
def _save(arts):
    global STORE
    STORE = arts
news.save_articles = _save
news.log_write = lambda info, msg: None
news.load_members = lambda: {"alice": {}, "bob": {}, "carol": {}, "dave": {}}
news._announce_published = lambda a: None

# The inbox is stubbed rather than exercised: these routes only decide *who*
# hears about a ranking and when, and a real notify_member would write to the
# live inbox file from a test run.
INBOX = []
news.inbox.notify_member = lambda name, text, link=None: INBOX.append((name, text, link))
def inbox_since(mark): return INBOX[mark:]
def recipients(mark): return sorted(n for n, _t, _l in INBOX[mark:])

ALICE = {"name": "alice", "roles": []}     # author, no editorial role
BOB   = {"name": "bob",   "roles": []}
CAROL = {"name": "carol", "roles": []}
DAVE  = {"name": "dave",  "roles": []}     # not invited
CURATOR = {"name": "zed", "roles": ["curator"]}

news._resolve_token = lambda auth: {"alice": ALICE, "bob": BOB, "carol": CAROL,
                                    "dave": DAVE, "zed": CURATOR}.get(((auth or "").split() or [""])[-1])

FAILS = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond: FAILS.append(name)

def http(name, status, fn):
    try:
        fn()
    except HTTPException as e:
        check(f"{name} → {status}", e.status_code == status)
        if e.status_code != status: print("      got:", e.status_code, e.detail)
        return
    check(f"{name} → {status}", False)

TEAMS30 = sorted(news.pr.VALID_TEAMS)
def ballot(front): return front + [t for t in TEAMS30 if t not in front]

print("\ncreate")
a = news.create_article(news.ArticleCreate(
    title="Week 1 Power Rankings", body="", type="power_rankings", series_id="main"), ALICE)
aid = a["id"]
check("created as a power-rankings draft", a["type"] == "power_rankings" and a["status"] == "draft")
check("starts in setup", a["phase"] == "setup")
http("an unknown type", 422, lambda: news.create_article(
    news.ArticleCreate(title="x", type="mystery"), ALICE))

print("\nvoters")
http("a non-author inviting", 403,
     lambda: news.set_ranking_voters(aid, news.VotersIn(voters=["bob"]), BOB))
http("inviting a stranger", 422,
     lambda: news.set_ranking_voters(aid, news.VotersIn(voters=["nobody"]), ALICE))
mark = len(INBOX)
a = news.set_ranking_voters(aid, news.VotersIn(voters=["alice", "bob", "carol"]), ALICE)
check("voters saved", a["voters"] == ["alice", "bob", "carol"])
check("the two invitees get an inbox message, the inviting author does not",
      recipients(mark) == ["bob", "carol"])
check("the invite links to the ballot workspace",
      all(l == f"/news/rankings/?id={aid}" for _n, _t, l in inbox_since(mark)))
check("and says who invited them, to what",
      all("alice invited you" in t and "Week 1 Power Rankings" in t
          for _n, t, _l in inbox_since(mark)))

mark = len(INBOX)
news.set_ranking_voters(aid, news.VotersIn(voters=["alice", "bob", "carol"]), ALICE)
check("re-saving the same list notifies nobody twice", inbox_since(mark) == [])

print("\nvoting")
http("voting before it opens", 422,
     lambda: news.put_ranking_ballot(aid, news.BallotIn(order=ballot(["BOS"])), BOB))
mark = len(INBOX)
a = news.set_ranking_phase(aid, news.PhaseIn(phase="voting"), ALICE)
check("phase is voting", a["phase"] == "voting")
check("opening the ballot chases the voters who owe one", recipients(mark) == ["bob", "carol"])
check("the chase says the ballot is open",
      all("ballot is open" in t for _n, t, _l in inbox_since(mark)))
mark = len(INBOX)
news.set_ranking_phase(aid, news.PhaseIn(phase="voting"), ALICE)
check("re-posting the phase it is already in chases nobody", inbox_since(mark) == [])
http("an uninvited member voting", 422,
     lambda: news.put_ranking_ballot(aid, news.BallotIn(order=ballot(["BOS"])), DAVE))

# a draft: stored, returned to its own author, and counted by nobody
http("an uninvited member saving a draft", 422,
     lambda: news.put_ranking_draft(aid, news.BallotIn(order=ballot(["BOS"])), DAVE))
d = news.put_ranking_draft(aid, news.BallotIn(order=ballot(["MIA"])), CAROL)
check("carol's draft comes back to her", d["ballots"]["carol"]["order"][0] == "MIA")
check("a draft is not a submitted ballot", d["ballots"]["carol"]["submitted_at"] is None)
check("a draft leaves her outstanding", d["ballot_progress"]["pending"] == ["alice", "bob", "carol"])
check("the author is told she has started",
      news.get_article(aid, "Bearer alice")["ballot_progress"]["started"] == ["carol"])
check("a draft is blind to the other voters",
      "order" not in news.get_article(aid, "Bearer bob")["ballots"]["carol"])
http("a short ballot", 422,
     lambda: news.put_ranking_ballot(aid, news.BallotIn(order=TEAMS30[:-1]), BOB))
news.put_ranking_ballot(aid, news.BallotIn(order=ballot(["BOS", "OKC"])), ALICE)
a = news.put_ranking_ballot(aid, news.BallotIn(order=ballot(["OKC", "BOS"])), BOB)
check("bob sees his own ballot", a["ballots"]["bob"]["order"][0] == "OKC")
check("bob cannot see alice's", "order" not in a["ballots"]["alice"])

# Put carol back to having nothing at all: the rest of the suite is written
# around her being the one voter still outstanding. (The draft → submitted
# transition itself is pinned in tests/test_news_rankings.py, where it is a
# rule rather than wiring.)
STORE[0]["ballots"].pop("carol")

print("\nreading while blind")
seen_alice = news.get_article(aid, "Bearer alice")
check("an invited voter can open the draft", seen_alice["id"] == aid)

print("\nblind for the author too")
# alice is the author AND a voter — the case the rule exists for
a_view = news.get_article(aid, "Bearer alice")
check("the author reads bob's ballot", a_view["ballots"]["bob"]["order"][0] == "OKC")
check("the author can preview the running order", len(a_view["consensus"]) == 30)
check("marked provisional, since it is still being collected", a_view["provisional"] is True)
check("a voter still gets no consensus",
      news.get_article(aid, "Bearer bob")["consensus"] is None)
# A curator is an editor here, and can close voting themselves, so the same
# reasoning applies to them as to the author.
check("an editor who is not the author sees it too",
      len(news.get_article(aid, "Bearer zed")["consensus"]) == 30)
check("the author sees who is outstanding", a_view["ballot_progress"]["pending"] == ["carol"])
http("an outsider reading the draft", 403, lambda: news.get_article(aid, "Bearer dave"))
check("a curator can read it", news.get_article(aid, "Bearer zed")["id"] == aid)
check("and so does a curator, who can close the vote just as easily",
      news.get_article(aid, "Bearer zed")["ballots"]["alice"]["order"][0] == "BOS")

print("\nlist view")
lst = news.list_news("Bearer bob")
check("bob's invited ranking shows in his ballots bucket",
      [x["id"] for x in lst["ballots"]] == [aid])
check("the list view carries no ballots at all", "ballots" not in lst["ballots"][0])
check("dave sees no ballots bucket", news.list_news("Bearer dave")["ballots"] == [])

print("\nclose and blurbs")
http("publishing mid-vote", 422,
     lambda: news.publish_article(aid, news.PublishIn(), ALICE))
a = news.set_ranking_phase(aid, news.PhaseIn(phase="blurbs"), ALICE)
check("ballots open up once voting closes", a["ballots"]["alice"]["order"][0] == "BOS")
check("and the consensus appears", len(a["consensus"]) == 30)
news.claim_ranking_blurb(aid, "BOS", BOB)
http("carol claiming a taken team", 422, lambda: news.claim_ranking_blurb(aid, "BOS", CAROL))
http("dave claiming anything", 422, lambda: news.claim_ranking_blurb(aid, "DEN", DAVE))
a = news.put_ranking_blurb(aid, "BOS", news.BlurbIn(body="Still the class of the East."), BOB)
check("the claimer's blurb is stored", a["blurbs"]["BOS"]["body"].startswith("Still"))
http("bob approving his own", 422,
     lambda: news.put_ranking_blurb(aid, "BOS", news.BlurbIn(approved=True), BOB))
a = news.put_ranking_blurb(aid, "BOS", news.BlurbIn(approved=True), ALICE)
check("the author approves", a["blurbs"]["BOS"]["approved"] is True)

print("\npublish")
mark = len(INBOX)
a = news.publish_article(aid, news.PublishIn(), ALICE)
check("published with an empty intro", a["status"] == "published")
check("the contributors hear it went live; the author who published does not",
      recipients(mark) == ["bob"])
check("and the message links to the published piece",
      inbox_since(mark)[0][2] == f"/news/view/?id={aid}")
check("edition numbered", a["edition"] == 1)
check("order frozen", len(a["final"]) == 30)
frozen = [r["team"] for r in a["final"]]
check("first edition has no movement", all(r["prev"] is None for r in a["final"]))
check("published article is public", news.get_article(aid, None)["id"] == aid)
check("its ballots are public too", news.get_article(aid, None)["ballots"]["alice"]["order"][0] == "BOS")

# The team panel reads only what publish froze, so what publish froze is the
# whole contract. A top-N slice here would have left the panel live-fetching
# the tail of the roster under a months-old ranking, which is the one thing
# freezing the strip existed to prevent.
bos = [r for r in a["final"] if r["team"] == "BOS"][0]
check("publish freezes the whole roster, not a top-N slice", len(bos["roster"]) > 6)
# `ovr` is "" for anyone with no rating history yet, so this sorts the way the
# snapshot does (unrated last) rather than comparing "" against an int.
_ovrs = [pl["ovr"] or 0 for pl in bos["roster"]]
check("in OVR order", _ovrs == sorted(_ovrs, reverse=True))
check("carrying what the panel and the lineup math need",
      all({"slug", "name", "ovr", "pos", "age"} <= set(p) for p in bos["roster"]))
check("and the record the ranking was argued over",
      (bos["season_line"] or {}).get("w") not in (None, ""))

print("\nedition 2 movement")
b = news.create_article(news.ArticleCreate(
    title="Week 2", type="power_rankings", series_id="main", prev_id=aid), ALICE)
bid = b["id"]
news.set_ranking_voters(bid, news.VotersIn(voters=["alice"]), ALICE)
news.set_ranking_phase(bid, news.PhaseIn(phase="voting"), ALICE)
news.put_ranking_ballot(bid, news.BallotIn(order=list(reversed(frozen))), ALICE)
news.set_ranking_phase(bid, news.PhaseIn(phase="blurbs"), ALICE)
b = news.publish_article(bid, news.PublishIn(), ALICE)
check("edition 2 numbered", b["edition"] == 2)
top = b["final"][0]
check("the team that was last is now first with a big climb",
      top["team"] == frozen[-1] and top["move"] == 29)

print("\nfreeze holds")
news.set_ranking_phase(aid, news.PhaseIn(phase="blurbs"), ALICE)
news.set_ranking_phase(aid, news.PhaseIn(phase="voting"), ALICE)
news.put_ranking_ballot(aid, news.BallotIn(order=ballot(["WAS", "UTA"])), CAROL)
after = news.get_article(aid, None)
check("a late ballot does not move the published order",
      [r["team"] for r in after["final"]] == frozen)

print("\nlink previews")
# An edition can publish with no intro, so the Discord embed and the link
# preview both have to fall back to the table rather than a generic line.
pub = [x for x in STORE if x["id"] == aid][0]
check("an intro-less edition teases its own top five",
      len(news._article_teaser(pub).split(" · ")) == 5)
check("and a shared rank teases as a tie — alice and bob split BOS/OKC 1-2",
      news._article_teaser(pub).startswith("T-1. BOS · T-1. OKC · 3. "))
check("a written intro still wins",
      news._article_teaser({"body": "Real prose.", "type": "power_rankings"}) == "Real prose.")

print("\nbaseline")
b = news.create_article(news.ArticleCreate(
    title="Import: the old sheet", body="", type="power_rankings", series_id="sheet"), ALICE)
bid = b["id"]
SHEET = {t: i + 1 for i, t in enumerate(ballot(["OKC", "BOS"]))}
http("a non-editor setting the baseline", 403,
     lambda: news.put_ranking_baseline(bid, news.BaselineIn(ranks=SHEET, label="the sheet"), BOB))
http("a baseline missing teams", 422,
     lambda: news.put_ranking_baseline(bid, news.BaselineIn(ranks={"BOS": 1}, label="the sheet"), ALICE))
b = news.put_ranking_baseline(bid, news.BaselineIn(ranks=SHEET, label="the January sheet"), ALICE)
check("the baseline is stored with its label", b["baseline"]["label"] == "the January sheet")
news.set_ranking_voters(bid, news.VotersIn(voters=["alice", "bob"]), ALICE)
news.set_ranking_phase(bid, news.PhaseIn(phase="voting"), ALICE)
check("a voter's ballot is seeded from the baseline",
      news.get_article(bid, "Bearer bob")["seed_order"][0] == "OKC")
news.put_ranking_ballot(bid, news.BallotIn(order=ballot(["BOS", "OKC"])), ALICE)
news.put_ranking_ballot(bid, news.BallotIn(order=ballot(["BOS", "OKC"])), BOB)
news.set_ranking_phase(bid, news.PhaseIn(phase="blurbs"), ALICE)
b = news.publish_article(bid, news.PublishIn(), ALICE)
first = {r["team"]: r for r in b["final"]}
check("a first edition with a baseline still moves", first["BOS"]["prev"] == SHEET["BOS"])
check("and the climb is measured against it",
      first["BOS"]["move"] == SHEET["BOS"] - first["BOS"]["rank"] > 0)

print("\nnon-ranking articles unaffected")
plain = news.create_article(news.ArticleCreate(title="A column", body="Words."), ALICE)
check("plain article has no ranking fields", "phase" not in plain and "ballots" not in plain)
http("ranking routes refuse a plain article", 422,
     lambda: news.set_ranking_phase(plain["id"], news.PhaseIn(phase="voting"), ALICE))
mark = len(INBOX)
pub = news.publish_article(plain["id"], news.PublishIn(), ALICE)
check("a plain article still publishes", pub["status"] == "published")
check("publishing your own article does not notify you", inbox_since(mark) == [])

plain2 = news.create_article(news.ArticleCreate(title="Another column", body="Words."), BOB)
mark = len(INBOX)
news.publish_article(plain2["id"], news.PublishIn(), CURATOR)
check("an editor publishing someone else's article notifies its author",
      recipients(mark) == ["bob"] and "published your article" in inbox_since(mark)[0][1])

print()
print(f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)

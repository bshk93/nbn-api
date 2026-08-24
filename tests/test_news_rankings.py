"""Regression tests for the power-rankings article type (routers/news_rankings.py).

Written 2026-08-24 alongside the feature. The module is pure by design — no
FastAPI, no file I/O — so everything the league actually argued about is
testable directly here, and that is what this suite pins:

  * **Pure average, no override.** The order is the mean rank and nothing else.
    Teams that draw level share a rank and the next rank skips them (T-13,
    T-13, 15) — nothing hidden breaks the tie, so no tie hands the author a
    thumb on the scale.
  * **Blind until close.** While `phase == "voting"` nobody reads anyone else's
    ballot — *including the author*, who is usually a voter and would otherwise
    be the one person in the league who gets to rank last with full
    information. There is no live consensus in that phase either, since it is
    made of the ballots it would leak.
  * **Freeze at publish.** A published edition's order is stored, not
    recomputed. Editing a ballot afterwards must not silently restate a ranking
    that has already gone out to Discord.
  * **Dropping a voter drops their ballot**, so a removed voter cannot keep
    counting toward the average from beyond the invite list.
  * **Blurbs are first-come**, the claimer writes, and only an editor approves.

    venv/bin/python -m tests.test_news_rankings
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.news_rankings as pr  # noqa: E402
from routers.constants import VALID_TEAMS  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def refuses(name, fn, needle=None):
    try:
        fn()
    except ValueError as e:
        ok = needle is None or needle.lower() in str(e).lower()
        check(f"{name} refused" + (f" ({needle!r})" if needle else ""), ok)
        return
    check(f"{name} refused", False)


TEAMS = sorted(VALID_TEAMS)


def ballot_from(front: list[str]) -> list[str]:
    """A full 30-team ballot with `front` on top and the rest in alpha order."""
    return front + [t for t in TEAMS if t not in front]


def new_article(voters=("alice", "bob", "carol"), author="alice"):
    a = {"id": "art-1", "author": author, "status": "draft"}
    a.update(pr.scaffold("main", None))
    a["voters"] = list(voters)
    return a


# ── ballot validation ────────────────────────────────────────────────────────
print("\nballot validation")
a = new_article()
pr.set_phase(a, "voting")
check("a full permutation is accepted", pr.validate_order(ballot_from(["BOS"])) [0] == "BOS")
check("lowercase input is normalised", pr.validate_order([t.lower() for t in TEAMS])[0] == TEAMS[0])
refuses("a 29-team ballot", lambda: pr.validate_order(TEAMS[:-1]), "all 30")
refuses("a duplicated team", lambda: pr.validate_order(TEAMS[:-1] + [TEAMS[0]]), "duplicated")
refuses("a non-list ballot", lambda: pr.validate_order("BOS"), "list")
refuses("a stranger's ballot", lambda: pr.set_ballot(a, "dave", ballot_from(["BOS"])), "not on this ranking")

# ── consensus is the plain mean ──────────────────────────────────────────────
print("\nconsensus")
a = new_article()
pr.set_phase(a, "voting")
pr.set_ballot(a, "alice", ballot_from(["BOS", "OKC", "DEN"]))
pr.set_ballot(a, "bob",   ballot_from(["OKC", "BOS", "DEN"]))
rows = pr.consensus(a)
by = {r["team"]: r for r in rows}
check("BOS averages (1+2)/2 = 1.5", by["BOS"]["avg"] == 1.5)
check("OKC averages (2+1)/2 = 1.5", by["OKC"]["avg"] == 1.5)
check("DEN is 3rd on both ballots", by["DEN"]["avg"] == 3.0 and by["DEN"]["rank"] == 3)
check("a 1.5/1.5 tie is a shared rank, not a hidden tiebreak",
      by["BOS"]["rank"] == 1 and by["OKC"]["rank"] == 1)
check("both sides of the tie are flagged tied", by["BOS"]["tied"] and by["OKC"]["tied"])
check("first-place votes are reported but no longer break the tie",
      by["BOS"]["firsts"] == 1 and by["OKC"]["firsts"] == 1)
check("the rank after a 2-way tie for 1st is 3, not 2", by["DEN"]["rank"] == 3)
check("an untied team is not flagged tied", by["DEN"]["tied"] is False)
check("hi/lo carry the ballot spread", by["BOS"]["hi"] == 1 and by["BOS"]["lo"] == 2)
check("30 teams are ranked", len(rows) == 30 and rows[-1]["rank"] <= 30)
check("votes counts only submitted ballots", by["BOS"]["votes"] == 2)

# a third ballot that buries BOS moves it purely by the average
pr.set_ballot(a, "carol", ballot_from(["OKC", "DEN"] + [t for t in TEAMS if t not in ("OKC", "DEN", "BOS")]))
by = {r["team"]: r for r in pr.consensus(a)}
check("burying a team drags its mean down", by["BOS"]["avg"] > by["OKC"]["avg"])
check("OKC now leads on the mean alone", by["OKC"]["rank"] == 1)

# ── blind until close ────────────────────────────────────────────────────────
print("\nblind until close")
a = new_article()
pr.set_phase(a, "voting")
pr.set_ballot(a, "alice", ballot_from(["BOS"]))
pr.set_ballot(a, "bob", ballot_from(["OKC"]))

seen = pr.redact(a, "bob", is_editor=False)
check("a voter sees their own order", seen["ballots"]["bob"]["order"][0] == "OKC")
check("a voter cannot see another's order", "order" not in seen["ballots"]["alice"])
check("a voter can see that another has submitted", seen["ballots"]["alice"]["submitted_at"])
check("no live consensus while blind", seen["consensus"] is None)

# alice is both the author and a voter — the case the rule exists for
seen_author = pr.redact(a, "alice", is_editor=True)
check("the author cannot see another's order either",
      "order" not in seen_author["ballots"]["bob"])
check("the author still gets no live consensus", seen_author["consensus"] is None)
check("the author does see who is outstanding",
      seen_author["ballot_progress"]["pending"] == ["carol"])
check("the author sees who has submitted",
      seen_author["ballot_progress"]["submitted"] == ["alice", "bob"])

pr.set_phase(a, "blurbs")
opened = pr.redact(a, "bob", is_editor=False)
check("closing voting opens every ballot", opened["ballots"]["alice"]["order"][0] == "BOS")
# Both ballots put ATL 2nd (it leads the alpha tail), so it wins the mean —
# which is the point: the average decides, not who anyone put first.
check("and the consensus appears, ranking all 30",
      len(opened["consensus"]) == 30 and opened["consensus"][0]["rank"] == 1)
check("the mean, not the first-place votes, picks the leader",
      opened["consensus"][0]["team"] == "ATL" and opened["consensus"][0]["firsts"] == 0)

# ── voters ───────────────────────────────────────────────────────────────────
print("\nvoter list")
a = new_article()
pr.set_phase(a, "voting")
pr.set_ballot(a, "alice", ballot_from(["BOS"]))
pr.set_ballot(a, "bob", ballot_from(["OKC"]))
pr.set_voters(a, ["alice", "carol"], {"alice", "bob", "carol"})
check("dropping a voter drops their ballot", "bob" not in a["ballots"])
check("the remaining ballot survives", "alice" in a["ballots"])
check("a dropped voter stops counting", pr.consensus(a)[0]["votes"] == 1)
refuses("an unknown invitee", lambda: pr.set_voters(a, ["nobody"], {"alice"}), "unknown member")
pr.set_voters(a, ["alice", "alice", "carol"], {"alice", "carol"})
check("a duplicated invite collapses", a["voters"] == ["alice", "carol"])

# ── phases ───────────────────────────────────────────────────────────────────
print("\nphases")
a = new_article(voters=[])
refuses("opening a ballot with no voters", lambda: pr.set_phase(a, "voting"), "at least one voter")
a = new_article()
refuses("skipping straight to blurbs", lambda: pr.set_phase(a, "blurbs"), "cannot move")
pr.set_phase(a, "voting")
refuses("closing with no ballots in", lambda: pr.set_phase(a, "blurbs"), "no ballots")
pr.set_ballot(a, "alice", ballot_from(["BOS"]))
pr.set_phase(a, "blurbs")
check("blurbs phase reached", a["phase"] == "blurbs")
pr.set_phase(a, "voting")
check("voting can be reopened for a straggler", a["phase"] == "voting")
check("reopening keeps the ballots already cast", "alice" in a["ballots"])
refuses("an invented phase", lambda: pr.set_phase(a, "counting"), "must be one of")

# ── blurbs ───────────────────────────────────────────────────────────────────
print("\nblurbs")
a = new_article()
pr.set_phase(a, "voting")
pr.set_ballot(a, "alice", ballot_from(["BOS"]))
refuses("claiming before blurbs open", lambda: pr.claim_blurb(a, "BOS", "bob", False), "not open")
pr.set_phase(a, "blurbs")
pr.claim_blurb(a, "BOS", "bob", False)
check("a voter can claim a team", a["blurbs"]["BOS"]["claimed_by"] == "bob")
refuses("a second voter claiming the same team",
        lambda: pr.claim_blurb(a, "BOS", "carol", False), "already claimed")
refuses("a non-voter claiming", lambda: pr.claim_blurb(a, "DEN", "dave", False), "only invited voters")
refuses("claiming a team that does not exist",
        lambda: pr.claim_blurb(a, "XYZ", "bob", False), "unknown team")
pr.claim_blurb(a, "BOS", "alice", True)
check("an editor can reassign a claim", a["blurbs"]["BOS"]["claimed_by"] == "alice")

pr.claim_blurb(a, "DEN", "carol", False)
refuses("writing someone else's claimed blurb",
        lambda: pr.set_blurb(a, "DEN", "bob", "not mine", None, False), "claimed by carol")
pr.set_blurb(a, "DEN", "carol", "Jokic is still Jokic.", None, False)
check("the claimer writes their blurb", a["blurbs"]["DEN"]["body"] == "Jokic is still Jokic.")
check("writing is not approving", a["blurbs"]["DEN"]["approved"] is False)
refuses("a voter approving their own blurb",
        lambda: pr.set_blurb(a, "DEN", "carol", None, True, False), "only the author")
pr.set_blurb(a, "DEN", "alice", None, True, True)
check("an editor approves", a["blurbs"]["DEN"]["approved"] is True)
pr.set_blurb(a, "DEN", "alice", "Editor's cut.", None, True)
check("an editor can rewrite any blurb", a["blurbs"]["DEN"]["body"] == "Editor's cut.")
refuses("an over-long blurb",
        lambda: pr.set_blurb(a, "MIA", "alice", "x" * (pr.MAX_BLURB_LEN + 1), None, True), "limited to")
pr.set_blurb(a, "MIA", "alice", "Heat culture.", None, True)
check("an editor writing an unclaimed team claims it for themselves",
      a["blurbs"]["MIA"]["claimed_by"] == "alice")
prog = pr.blurb_progress(a)
check("progress counts what is written", prog["written"] == 2)
check("progress counts what is approved", prog["approved"] == 1)
check("progress lists what is still unwritten", len(prog["unwritten"]) == 28)
check("progress lists what is written but unapproved", prog["unapproved"] == ["MIA"])

# ── movement and freeze ──────────────────────────────────────────────────────
print("\nmovement and freeze")
prev = new_article()
prev.update({"id": "art-0", "status": "published", "edition": 1})
prev["final"] = [{"team": t, "rank": i} for i, t in enumerate(ballot_from(["BOS", "OKC", "DEN"]), 1)]

cur = new_article()
cur["id"] = "art-2"
cur["prev_id"] = "art-0"
pr.set_phase(cur, "voting")
pr.set_ballot(cur, "alice", ballot_from(["DEN", "OKC", "BOS"]))
rows = pr.apply_movement(pr.consensus(cur), prev["final"])
by = {r["team"]: r for r in rows}
check("a climb from 3rd to 1st reads +2", by["DEN"]["move"] == 2 and by["DEN"]["prev"] == 3)
check("a fall from 1st to 3rd reads -2", by["BOS"]["move"] == -2)
check("an unmoved team reads 0", by["OKC"]["move"] == 0)

first_edition = new_article()
pr.set_phase(first_edition, "voting")
pr.set_ballot(first_edition, "alice", ballot_from(["BOS"]))
rows = pr.apply_movement(pr.consensus(first_edition), None)
check("with no previous edition, movement is absent not zero",
      all(r["move"] is None and r["prev"] is None for r in rows))

articles = [prev, cur]
pr.set_phase(cur, "blurbs")
pr.freeze(cur, articles)
check("freeze stores the order", cur["final"][0]["team"] == "DEN")
check("freeze numbers the edition after the last published one", cur["edition"] == 2)
check("freeze moves the ranking to its final phase", cur["phase"] == "final")

published_order = [r["team"] for r in cur["final"]]
pr.set_phase(cur, "blurbs")
pr.set_phase(cur, "voting")
pr.set_ballot(cur, "bob", ballot_from(["MIA", "MIL", "NYK"]))
check("a ballot cast after publish does not restate the published order",
      [r["team"] for r in cur["final"]] == published_order)
check("while the live consensus does take the new ballot into account",
      [r["team"] for r in pr.consensus(cur)] != published_order)

empty = new_article()
pr.set_phase(empty, "voting")
pr.set_ballot(empty, "alice", ballot_from(["BOS"]))
refuses("publishing while voting is still open", lambda: pr.freeze(empty, []), "close voting")
pr.set_phase(empty, "blurbs")
empty["ballots"] = {}
refuses("publishing with no ballots at all", lambda: pr.freeze(empty, []), "no submitted ballots")

# ── a baseline stands in for a previous edition ──────────────────────────────
print("\nbaseline")
base = new_article()
pr.set_phase(base, "voting")
pr.set_ballot(base, "alice", ballot_from(["BOS", "OKC"]))
pr.set_phase(base, "blurbs")

sheet = {t: i + 1 for i, t in enumerate(["OKC"] + [t for t in TEAMS if t != "OKC"])}
pr.set_baseline(base, sheet, "the January sheet")
check("a baseline names itself", base["baseline"]["label"] == "the January sheet")
refuses("a baseline with no label", lambda: pr.set_baseline(base, sheet, "  "), "label")
refuses("a baseline missing a team",
        lambda: pr.set_baseline(base, {t: 1 for t in TEAMS[:-1]}, "partial"), "missing 1 team")
refuses("a baseline naming a team that does not exist",
        lambda: pr.set_baseline(base, dict(sheet, XXX=1), "typo"), "unknown team")
refuses("a baseline rank of zero", lambda: pr.set_baseline(base, dict(sheet, BOS=0), "zero"), "1 or more")
pr.set_baseline(base, sheet, "the January sheet")

rows = pr.apply_movement(pr.consensus(base), None, base["baseline"])
by = {r["team"]: r for r in rows}
check("movement is measured from the baseline when there is no previous edition",
      by["BOS"]["prev"] == sheet["BOS"] and by["BOS"]["move"] == sheet["BOS"] - by["BOS"]["rank"])
check("no team reads as new once a baseline is set",
      all(r["prev"] is not None for r in rows))

prev_edition = [{"team": t, "rank": i + 1} for i, t in enumerate(ballot_from(["BOS"]))]
by = {r["team"]: r for r in pr.apply_movement(pr.consensus(base), prev_edition, base["baseline"])}
check("a real previous edition beats the baseline", by["BOS"]["prev"] == 1)

check("the ballot seeds off the baseline order", pr.redact(base, "bob", False)["seed_order"][0] == "OKC")
pr.set_baseline(base, {}, None)
check("an empty map clears the baseline", base["baseline"] is None)
check("and the arrows go back to new",
      pr.apply_movement(pr.consensus(base), None, base["baseline"])[0]["prev"] is None)

# ── redaction shape ──────────────────────────────────────────────────────────
print("\nredaction shape")
plain = {"id": "x", "type": "custom", "title": "hi"}
check("a non-ranking article passes through untouched",
      pr.redact(plain, "alice", False) == plain)
check("is_ranking only matches the power-rankings type",
      pr.is_ranking(cur) and not pr.is_ranking(plain))
view = pr.redact(cur, "carol", False)
check("viewer_is_voter reflects the invite list", view["viewer_is_voter"] is True)
check("a stranger is not flagged as a voter",
      pr.redact(cur, "dave", False)["viewer_is_voter"] is False)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")

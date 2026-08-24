"""Power-rankings article type — ballots, consensus, blurbs.

A power-rankings article is an ordinary news article (same store, same
draft → submitted → published lifecycle) carrying an extra block of fields. It
adds one thing the plain type doesn't have: a *ballot phase* between draft and
publish, in which invited members each rank all 30 teams and write blurbs.

Pure logic only — no FastAPI, no file I/O, no lock. `news.py` owns the routes,
the article store and the single `_news_lock`; every function here takes an
article dict (and sometimes the whole list) and returns data or mutates that
dict in place. Importing nothing from `news.py` is what lets `news.py` import
this without a cycle, and is why the consensus math is directly testable.

The four rules the league settled on, which the code below is shaped around:

  * **Pure average, no override.** The published order is the mean rank across
    submitted ballots, full stop — the author cannot move a team. `final` is
    computed at publish and frozen (see `freeze`), so a ballot edited or a
    voter added afterwards can never silently restate a published ranking.
  * **Blind until close.** While `phase == "voting"` nobody — the author
    included — may read anyone else's ballot. `redact` enforces that; the
    author is usually a voter too, and a live peek would anchor them exactly
    the way seeing other ballots would anchor anyone else.
  * **Any voter claims a team.** Blurbs are first-come: in the `blurbs` phase a
    voter claims a team, writes it, and is credited on the published page. The
    author may edit any blurb, reassign a claim, and marks each `approved` —
    that approval is the "author finalizes" step.
  * **Recurring, with movement.** Editions are chained by `series_id` +
    `prev_id`, so every team carries ▲/▼ against the previous edition.
"""

from datetime import datetime, timezone
from typing import Optional

from .constants import VALID_TEAMS

PR_TYPE = "power_rankings"

# Phases, in order. `setup` and `voting` sit inside news status "draft";
# publishing happens from `blurbs` or `final` via the ordinary publish route.
PHASES = ("setup", "voting", "blurbs", "final")

# Only forward moves, except reopening voting from `blurbs` — which the author
# needs when a straggler turns up after close. Reopening deliberately does NOT
# clear existing ballots: they were submitted blind and stay valid.
PHASE_NEXT = {
    "setup": {"voting"},
    "voting": {"blurbs"},
    "blurbs": {"voting", "final"},
    "final": {"blurbs"},
}

MAX_BLURB_LEN = 1200
MAX_VOTERS = 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_ranking(a: dict) -> bool:
    return (a or {}).get("type") == PR_TYPE


def scaffold(series_id: Optional[str], prev_id: Optional[str]) -> dict:
    """The extra fields a power-rankings article carries, merged in at create."""
    return {
        "type": PR_TYPE,
        "series_id": (series_id or "main").strip() or "main",
        "prev_id": (prev_id or None),
        "edition": None,        # assigned at publish, by position in the series
        "phase": "setup",
        "voters": [],           # member names invited to rank
        "ballots": {},          # name -> {"order": [30 abbrs], "submitted_at": iso}
        "blurbs": {},           # ABBR -> {"claimed_by", "body", "approved", "updated_at"}
        "final": None,          # frozen consensus rows, set once at publish
    }


# ── Voters and ballots ────────────────────────────────────────────────────────

def is_voter(a: dict, name: Optional[str]) -> bool:
    return bool(name) and name in (a.get("voters") or [])


def set_voters(a: dict, names: list[str], known_members: set[str]) -> list[str]:
    """Replace the invite list. Unknown names are rejected rather than silently
    dropped — a typo'd invite is a voter who never shows up and a ranking that
    waits forever for them."""
    clean, seen = [], set()
    for n in names or []:
        n = str(n).strip()
        if not n or n in seen:
            continue
        if n not in known_members:
            raise ValueError(f"unknown member: {n}")
        seen.add(n)
        clean.append(n)
        if len(clean) > MAX_VOTERS:
            raise ValueError(f"at most {MAX_VOTERS} voters")
    # Dropping a voter drops their ballot too; leaving it behind would keep
    # counting someone who is no longer in the ranking.
    a["voters"] = clean
    a["ballots"] = {k: v for k, v in (a.get("ballots") or {}).items() if k in seen}
    return clean


def validate_order(order) -> list[str]:
    """A ballot is a permutation of all 30 teams — no partial ballots, since a
    missing team has no defensible average."""
    if not isinstance(order, list):
        raise ValueError("order must be a list of team abbreviations")
    up = [str(t).strip().upper() for t in order]
    if len(up) != len(VALID_TEAMS) or set(up) != set(VALID_TEAMS):
        missing = sorted(VALID_TEAMS - set(up))
        dupes = sorted({t for t in up if up.count(t) > 1})
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
        if dupes:
            detail.append("duplicated " + ", ".join(dupes[:5]))
        raise ValueError("ballot must rank all 30 teams exactly once"
                         + (" (" + "; ".join(detail) + ")" if detail else ""))
    return up


def set_ballot(a: dict, name: str, order) -> dict:
    if a.get("phase") != "voting":
        raise ValueError("voting is not open on this ranking")
    if not is_voter(a, name):
        raise ValueError("you are not on this ranking's voter list")
    ballot = {"order": validate_order(order), "submitted_at": _now()}
    a.setdefault("ballots", {})[name] = ballot
    return ballot


def submitted_ballots(a: dict) -> dict:
    return {n: b for n, b in (a.get("ballots") or {}).items()
            if b.get("submitted_at") and b.get("order")}


# ── Consensus ─────────────────────────────────────────────────────────────────

def consensus(a: dict) -> list[dict]:
    """Mean rank across submitted ballots, ascending — the published order.

    Ties break on most first-place votes, then alphabetically by abbreviation.
    "Pure average" still needs *some* deterministic rule when two teams draw
    level, and one that depends only on the ballots keeps the author's hand off
    the result; the published page states it.
    """
    ballots = submitted_ballots(a)
    if not ballots:
        return []
    rows = []
    for team in sorted(VALID_TEAMS):
        ranks = [b["order"].index(team) + 1 for b in ballots.values()]
        rows.append({
            "team": team,
            "avg": round(sum(ranks) / len(ranks), 2),
            "firsts": sum(1 for r in ranks if r == 1),
            "hi": min(ranks),
            "lo": max(ranks),
            "votes": len(ranks),
        })
    rows.sort(key=lambda r: (r["avg"], -r["firsts"], r["team"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def apply_movement(rows: list[dict], prev_final: Optional[list[dict]]) -> list[dict]:
    """Stamp each row with its rank in the previous edition and the delta.

    `move` is positive for a climb (rank 5 → 2 is +3). A team with no previous
    rank — first edition of a series — gets `prev: None, move: None`, which the
    page renders as "new" rather than as a zero move.
    """
    prev_rank = {r["team"]: r["rank"] for r in (prev_final or [])}
    for r in rows:
        p = prev_rank.get(r["team"])
        r["prev"] = p
        r["move"] = (p - r["rank"]) if p else None
    return rows


# ── Blurbs ────────────────────────────────────────────────────────────────────

def _blurb(a: dict, team: str) -> dict:
    return a.setdefault("blurbs", {}).setdefault(
        team, {"claimed_by": None, "body": "", "approved": False, "updated_at": None})


def claim_blurb(a: dict, team: str, name: str, is_editor: bool) -> dict:
    team = str(team).strip().upper()
    if team not in VALID_TEAMS:
        raise ValueError(f"unknown team: {team}")
    if a.get("phase") != "blurbs":
        raise ValueError("blurbs are not open on this ranking")
    if not is_editor and not is_voter(a, name):
        raise ValueError("only invited voters can claim a blurb")
    b = _blurb(a, team)
    if b["claimed_by"] and b["claimed_by"] != name and not is_editor:
        raise ValueError(f"{team} is already claimed by {b['claimed_by']}")
    b["claimed_by"] = name
    b["updated_at"] = _now()
    return b


def release_blurb(a: dict, team: str, name: str, is_editor: bool) -> dict:
    team = str(team).strip().upper()
    b = _blurb(a, team)
    if b["claimed_by"] != name and not is_editor:
        raise ValueError("only the claimer or an editor can release this blurb")
    b["claimed_by"] = None
    b["updated_at"] = _now()
    return b


def set_blurb(a: dict, team: str, name: str, body: Optional[str],
              approved: Optional[bool], is_editor: bool) -> dict:
    """Write a blurb, and (editors only) mark it finalized.

    An editor writing an unclaimed team's blurb claims it for themselves — the
    author covering a team nobody took should still be credited for it.
    """
    team = str(team).strip().upper()
    if team not in VALID_TEAMS:
        raise ValueError(f"unknown team: {team}")
    b = _blurb(a, team)
    if body is not None:
        if b["claimed_by"] != name and not is_editor:
            raise ValueError(f"{team} is claimed by {b['claimed_by'] or 'nobody'}")
        if a.get("phase") not in ("blurbs", "final"):
            raise ValueError("blurbs are not open on this ranking")
        text = str(body).strip()
        if len(text) > MAX_BLURB_LEN:
            raise ValueError(f"blurb is limited to {MAX_BLURB_LEN} characters")
        b["body"] = text
        if is_editor and not b["claimed_by"]:
            b["claimed_by"] = name
        # An author's edit is a revision, not an approval; approving is explicit.
        b["updated_at"] = _now()
    if approved is not None:
        if not is_editor:
            raise ValueError("only the author or an editor can approve a blurb")
        b["approved"] = bool(approved)
        b["updated_at"] = _now()
    return b


def blurb_progress(a: dict) -> dict:
    blurbs = a.get("blurbs") or {}
    written = [t for t in VALID_TEAMS if (blurbs.get(t, {}).get("body") or "").strip()]
    approved = [t for t in written if blurbs.get(t, {}).get("approved")]
    return {
        "written": len(written),
        "approved": len(approved),
        "total": len(VALID_TEAMS),
        "unwritten": sorted(set(VALID_TEAMS) - set(written)),
        "unapproved": sorted(set(written) - set(approved)),
    }


# ── Phase and publish ─────────────────────────────────────────────────────────

def set_phase(a: dict, phase: str) -> str:
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    cur = a.get("phase") or "setup"
    if phase == cur:
        return cur
    if phase not in PHASE_NEXT.get(cur, set()):
        raise ValueError(f"cannot move from {cur} to {phase}")
    if phase == "voting" and not (a.get("voters") or []):
        raise ValueError("invite at least one voter before opening the ballot")
    if phase == "blurbs" and cur == "voting" and not submitted_ballots(a):
        raise ValueError("no ballots have been submitted yet")
    a["phase"] = phase
    return phase


def series_editions(articles: list[dict], series_id: str) -> list[dict]:
    """Published editions of one series, oldest first."""
    out = [a for a in articles
           if is_ranking(a) and a.get("series_id") == series_id
           and a.get("status") == "published"]
    out.sort(key=lambda a: (a.get("edition") or 0, a.get("published_at") or ""))
    return out


def freeze(a: dict, articles: list[dict]) -> list[dict]:
    """Compute the consensus once, stamp movement, and store it as `final`.

    Called from the publish route. After this the published order is a stored
    fact rather than a live recomputation, so a late ballot edit cannot rewrite
    an edition that has already gone out.
    """
    rows = consensus(a)
    if not rows:
        raise ValueError("cannot publish a ranking with no submitted ballots")
    prev = next((p for p in articles if p.get("id") == a.get("prev_id")), None)
    apply_movement(rows, (prev or {}).get("final"))
    a["final"] = rows
    a["phase"] = "final"
    if not a.get("edition"):
        a["edition"] = len(series_editions(articles, a.get("series_id") or "main")) + 1
    return rows


# ── Read-side redaction ───────────────────────────────────────────────────────

def redact(a: dict, viewer: Optional[str], is_editor: bool,
           prev_final: Optional[list[dict]] = None) -> dict:
    """Viewer-safe copy of a ranking article.

    While voting is open every ballot but the viewer's own is reduced to "has
    submitted, at this time" — for the author too (see the module docstring).
    Once voting closes the ballots open up to everyone, including the public
    after publish: blind voting is about anchoring, not secrecy.
    """
    out = dict(a)
    if not is_ranking(a):
        return out

    blind = a.get("phase") == "voting"
    ballots = a.get("ballots") or {}
    if blind:
        out["ballots"] = {
            n: ({"order": b.get("order"), "submitted_at": b.get("submitted_at")}
                if n == viewer else {"submitted_at": b.get("submitted_at")})
            for n, b in ballots.items()
        }
        # No live consensus while blind — it would leak the ballots it is made of.
        out["consensus"] = None
    else:
        out["ballots"] = ballots
        # A published edition shows its frozen `final`; an in-progress one shows
        # the live standing, with movement against the previous edition so the
        # author can see the shape of the piece before publishing it.
        out["consensus"] = a.get("final") or apply_movement(consensus(a), prev_final)

    out["ballot_progress"] = {
        "submitted": sorted(submitted_ballots(a).keys()),
        "pending": sorted(set(a.get("voters") or []) - set(submitted_ballots(a).keys())),
        "voters": len(a.get("voters") or []),
    }
    out["blurb_progress"] = blurb_progress(a)
    out["viewer_is_voter"] = is_voter(a, viewer)
    return out

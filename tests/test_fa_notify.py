"""Regression tests for routers.fa_notify — the PDC free-agency Discord feeds
(§ 9, Phase 6). Spec: nbn-today/docs/pdc-free-agency-spec.md.

The load-bearing property is a **disclosure boundary**, not a formatting one.
`pdc-alerts` is private and gets everything; `fa-news` is public and gets two
posts per player containing a name and a deadline. That a team is bidding, and
for how much, is committee information — a single leak into the public channel
is not a cosmetic bug, it is the league learning a rival's offer.

So the suite proves, independently:

  * **No team abbreviation and no `$` ever reaches the public channel**, checked
    against every rendered public payload rather than against the code path
    that produced it.
  * **Two public posts per player per window** — clock started, window closed —
    across a whole offer lifecycle that also contains a submission, a remand, a
    resubmission and a finalize.
  * **The resubmission diff compares against the frozen prior version**, not
    against itself. Freezing at the remand rather than the resubmission is the
    bug § 4.3a was written after; a diff that reads "nothing changed" on a real
    revision would hide exactly what the committee asked for.
  * **Expiry announces once.** Two requests observing the same expired clock
    produce one post, and a clock that expired weeks ago is stamped but never
    announced — the deploy-time replay case.
  * **Each channel is independently inert without its env var**, which is what
    lets Phase 6 ship the module, then `DISCORD_PDC_CHANNEL`, then
    `DISCORD_FA_NEWS_CHANNEL` last.
  * **Nothing raises into the caller.** The offer, remand or finalize being
    announced is already written.

Nothing here touches the network — the transport's enqueue is a list append.

    venv/bin/python -m tests.test_fa_notify
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_transport as tp  # noqa: E402
import routers.fa_notify as fn  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── Harness ───────────────────────────────────────────────────────────────────

SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"
fn.DISCORD_PDC_CHANNEL = "pdc-chan"
fn.DISCORD_FA_NEWS_CHANNEL = "news-chan"

BIOS = {"curry-stephen": {"name": "CURRY, STEPHEN"}}
fn.load_player_bios = lambda: BIOS


def reset():
    SENT.clear()
    tp._recent_sends.clear()
    tp._suppressed.clear()


def pdc() -> list[dict]:
    return [p["embeds"][0] for c, p in SENT if c == "pdc-chan"]


def news() -> list[str]:
    return [p["content"] for c, p in SENT if c == "news-chan"]


def field(embed: dict, name: str) -> str:
    return next((f["value"] for f in embed.get("fields") or [] if f["name"].startswith(name)), "")


def iso(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


CONTRACT = {"type": "player",
            "salaries": {"26-27": "$50,000,000", "27-28": "$54,000,000"},
            "cap_holds": {"27-28": "PLAYER_OPT"}}

VALIDATION = {"legal": True, "checks": [
    {"check": "roster_max", "passed": True, "level": "error", "message": "26 of 20"},
    {"check": "apron_proximity", "passed": False, "level": "warning",
     "message": "Within $2M of the first apron"},
]}


def offer(**kw) -> dict:
    base = {
        "id": "f7c1a9b2", "number": 42, "player": "curry-stephen", "team": "PHX",
        "status": "submitted", "version": 1, "versions": [], "remands": [],
        "created_by": "phxGM", "submitted_by": "phxOwner", "submitted_at": iso(0),
        "offer": {"player": "curry-stephen", "team": "PHX", "contract": CONTRACT,
                  "signing_method": "bird_rights", "bird_rights_type": "QVFA",
                  "eaps_assumption": None},
        "pitch": "Come win now — you are the last piece.",
        "promises": {"mpg": 32, "playoffs": True, "role": "starter"},
        "validation": VALIDATION,
    }
    base.update(kw)
    return base


# ── The disclosure boundary ───────────────────────────────────────────────────
# Asserted against rendered output, so it holds however the message was built.
print("\nfa-news — a name and a deadline, nothing else")

TEAM_ABBRS = ["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
              "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
              "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"]
ABBR_RE = re.compile(r"\b(" + "|".join(TEAM_ABBRS) + r")\b")

FFA = {"deadline": iso(24), "started_by_offer": "f7c1a9b2", "started_by": "phxOwner"}

reset()
fn.notify_ffa_started("curry-stephen", FFA)
fn.notify_ffa_closed("curry-stephen", {"deadline": iso(-1)})
public = news()
check("both clock posts reach the public channel", len(public) == 2, public)
check("no team abbreviation in any public post",
      not any(ABBR_RE.search(t) for t in public), public)
check("no dollar figure in any public post", not any("$" in t for t in public), public)
check("the clock-start post names the player", "Stephen Curry" in public[0], public[0])
check("...and carries a deadline every reader sees in their own timezone",
      "<t:" in public[0], public[0])
check("the closing post says no further offers are accepted",
      "No further offers" in public[1], public[1])
check("...and claims no outcome",
      not any(w in public[1] for w in ("signs", "wins", "awarded")), public[1])

# The private side of the same two events may say more — that's the point of it
# being private — but must still be sent.
check("the committee channel hears about the clock too", len(pdc()) == 2, len(pdc()))


# ── § 4.1 window extension ────────────────────────────────────────────────────
print("\nExtending a window announces on both channels, leaking neither team nor figure")

EXT = {"deadline": iso(6), "started_at": iso(-24), "window_hours": 30.0,
       "started_by_offer": "f7c1a9b2", "started_by": "phxOwner"}

reset()
fn.notify_ffa_extended("curry-stephen", EXT, 6, "MEM never got a window", "facHead", False)
ext_pub, ext_pdc = news(), pdc()
check("an extension posts to both channels", len(ext_pub) == 1 and len(ext_pdc) == 1,
      (ext_pub, ext_pdc))
check("the public line names the player and the new deadline",
      "Stephen Curry" in ext_pub[0] and "<t:" in ext_pub[0], ext_pub[0])
check("...and carries the head's reason verbatim",
      "MEM never got a window" in ext_pub[0], ext_pub[0])
# The reason is author-supplied text, so it is the one string on this path that
# could carry an abbreviation into the public channel. That is the head's call
# and visible to them as they type it; what must never leak is anything the
# *system* adds. Assert the machine-built part specifically.
machine = ext_pub[0].split("Reason:")[0]
check("nothing the system composes leaks a team",
      not ABBR_RE.search(machine), machine)
check("nothing the system composes leaks a figure", "$" not in machine, machine)
check("the private post distinguishes an extension from a reopen",
      "extended" in ext_pdc[0]["title"].lower(), ext_pdc[0]["title"])
check("...and states that existing offers stand",
      "stand" in ext_pdc[0]["description"], ext_pdc[0]["description"])
check("...and reports the window's new total length",
      "30-hour" in field(ext_pdc[0], "Window now"), field(ext_pdc[0], "Window now"))

reset()
fn.notify_ffa_extended("curry-stephen", {**EXT, "deadline": iso(6)}, 6,
                       "clock lapsed over a holiday", "facHead", True)
check("reviving a lapsed window says reopened, not extended",
      "reopened" in pdc()[0]["title"].lower() and "reopened" in news()[0],
      (pdc()[0]["title"], news()[0]))



# The head can change how long a window runs (§ 4.1), so neither post may say
# "24-hour" from a constant — each names the length off the clock it announces.
reset()
long_ffa = {"started_at": iso(0), "deadline": iso(72), "window_hours": 72,
            "started_by_offer": "f7c1a9b2", "started_by": "phxOwner"}
fn.notify_ffa_started("curry-stephen", long_ffa)
fn.notify_ffa_closed("curry-stephen", dict(long_ffa, deadline=iso(-1)))
public = news()
check("a clock post names the window that clock actually got",
      all("72-hour" in t and "24-hour" not in t for t in public), public)

reset()
fn.notify_ffa_window_change(24, 72, "facHead", running=2)
check("changing the window length is committee business — private only",
      len(pdc()) == 1 and len(news()) == 0, (len(pdc()), len(news())))
check("...and says plainly that running clocks are untouched",
      "clocks already running keep the deadline" in pdc()[0]["description"],
      pdc()[0]["description"])


print("\nfa-news — exactly twice per player, across a whole lifecycle")

reset()
o = offer()
fn.notify_offer_submitted(o)
fn.notify_ffa_started("curry-stephen", FFA)
fn.notify_offer_remanded(o, {"at": iso(0), "by": "memberB", "note": "Add a third year",
                             "from_version": 1, "conflict": None})
fn.notify_offer_submitted(offer(version=2, versions=[{"version": 1, "offer": o["offer"],
                                                      "pitch": o["pitch"],
                                                      "promises": o["promises"]}]),
                          {"version": 1, "offer": o["offer"], "pitch": o["pitch"],
                           "promises": o["promises"]})
fn.notify_ffa_closed("curry-stephen", {"deadline": iso(-1)})
# The agent stage sits between the window closing and the ballot (§ 4.7), so a
# real lifecycle now runs through it — and every one of these names a team, an
# agent or a slate of bids, which is why none of them may reach the public feed.
fn.notify_player_claimed("curry-stephen", "agentA", ["WAS"])
fn.notify_player_released("curry-stephen", "agentA", "agentA")
fn.notify_player_claimed("curry-stephen", "agentB", ["ORL"])
fn.notify_player_advanced("curry-stephen", "agentB", [o], "PHX is the only real bid")
fn.notify_returned_to_agent("curry-stephen", "agentB", "facHead", "year 3 went illegal")
fn.notify_player_finalized("curry-stephen", {"totals": {}, "voters": [], "locked_by": "facHead"}, [])
check("eleven committee events produce eleven private posts", len(pdc()) == 11, len(pdc()))
check("...and still exactly two public posts", len(news()) == 2, news())
check("...neither of which leaks a team or a figure",
      not any(ABBR_RE.search(t) or "$" in t for t in news()), news())


# ── The offer announcement (§ 9.1) ────────────────────────────────────────────
print("\npdc-alerts — an offer arrives with everything needed to review it")

reset()
fn.notify_offer_submitted(offer())
e = pdc()[0]
check("title names the team and the player", e["title"] == "PHX — offer to Stephen Curry", e["title"])
check("description carries the deal's shape and total",
      e["description"].startswith("1+1 PO · $104.0M"), e["description"])
check("...and the funding method", "Bird Rights (QVFA)" in e["description"], e["description"])
check("year-by-year figures are shown in full",
      "$50,000,000" in field(e, "Year by year") and "$54,000,000" in field(e, "Year by year"),
      field(e, "Year by year"))
check("...with the option year named", "player option" in field(e, "Year by year"))
check("the legality verdict is stated", "Legal" in field(e, "Legality"), field(e, "Legality"))
check("...and warning checks are surfaced, not just the pass/fail",
      "first apron" in field(e, "Legality"), field(e, "Legality"))
check("promises are spelled out",
      field(e, "Promises") == "32 mpg · starter · playoff contention", field(e, "Promises"))
check("the pitch is included", "last piece" in field(e, "Pitch"), field(e, "Pitch"))
check("it links to the player on the dashboard",
      e["url"] == "https://pdc.nbn.today/#/p/curry-stephen", e["url"])
check("the footer separates who drafted it from who submitted it",
      "phxOwner" in e["footer"]["text"] and "phxGM" in e["footer"]["text"], e["footer"]["text"])

reset()
fn.notify_offer_submitted(offer(validation={"legal": False, "checks": [
    {"check": "hard_cap", "passed": False, "level": "error", "message": "over"}]}))
check("an illegal verdict is stated as one", "❌" in field(pdc()[0], "Legality"),
      field(pdc()[0], "Legality"))


# ── The resubmission diff (§ 4.3a) ────────────────────────────────────────────
# The freeze happens at the remand, so `prev` is the terms the committee
# objected to. Diffing against the resubmission itself would print "nothing
# changed" on every real revision — the bug this design was written after.
print("\npdc-alerts — a resubmission shows what moved")

V1 = {"version": 1,
      "offer": {"contract": {"salaries": {"26-27": "$50,000,000", "27-28": "$54,000,000"},
                             "cap_holds": {"27-28": "PLAYER_OPT"}},
                "signing_method": "bird_rights"},
      "pitch": "Come win now.", "promises": {"mpg": 32, "playoffs": True, "role": "starter"}}

V2 = offer(version=2, versions=[V1],
           remands=[{"at": iso(-2), "by": "memberB", "note": "Third year, and drop the option.",
                     "from_version": 1, "conflict": None}],
           offer={"player": "curry-stephen", "team": "PHX",
                  "contract": {"type": "player",
                               "salaries": {"26-27": "$50,000,000", "27-28": "$54,000,000",
                                            "28-29": "$58,000,000"},
                               "cap_holds": {}},
                  "signing_method": "cap_space", "bird_rights_type": None},
           promises={"mpg": 34, "playoffs": True, "role": "starter"})

reset()
fn.notify_offer_submitted(V2, V1)
e = pdc()[0]
diff = field(e, "Changed since")
check("a revision is titled as one", "revised offer" in e["title"], e["title"])
check("...and coloured differently from a first submission", e["color"] == fn.COLOR_REVISION)
check("an added year shows as added", "28-29" in diff and "$58,000,000" in diff, diff)
check("a dropped option shows as dropped", "PLAYER_OPT" in diff and "→ —" in diff, diff)
check("a changed funding method is called out", "cap_space" in diff, diff)
check("changed promises are shown", "34 mpg" in diff, diff)
check("an unchanged year is not listed as a change", "26-27" not in diff, diff)
check("the note being answered travels with it",
      "Third year" in field(e, "Answering"), field(e, "Answering"))

reset()
same = offer(version=2)
fn.notify_offer_submitted(same, {"version": 1, "offer": same["offer"], "pitch": same["pitch"],
                                 "promises": same["promises"]})
check("a resubmission that changed nothing says so, rather than showing a blank diff",
      "Nothing changed" in field(pdc()[0], "Changed since"), field(pdc()[0], "Changed since"))


# ── Remand (§ 4.3a, § 4.6) ────────────────────────────────────────────────────
print("\npdc-alerts — a remand carries its note and its conflict")

reset()
o = offer(status="returned")
r = {"at": iso(0), "by": "memberB", "note": "Bump the third year.", "from_version": 1,
     "conflict": "BKN"}
o["remands"].append(r)
fn.notify_offer_remanded(o, r)
e = pdc()[0]
check("the title says it was sent back", "sent back" in e["title"], e["title"])
check("the note is quoted and attributed",
      "Bump the third year." in field(e, "Outstanding") and "memberB" in field(e, "Outstanding"),
      field(e, "Outstanding"))
check("a conflicted remand is flagged", any("Conflict" in f["name"] for f in e["fields"]))
check("...naming the member's own team", "BKN" in field(e, "⚠️ Conflict"), field(e, "⚠️ Conflict"))

reset()
fn.notify_offer_remanded(offer(), {"at": iso(0), "by": "memberA", "note": "n", "from_version": 1,
                                   "conflict": None})
check("an unconflicted remand carries no warning",
      not any("Conflict" in f["name"] for f in pdc()[0]["fields"]))


# ── Void / restore (§ 4.3b) ───────────────────────────────────────────────────
# A void names a team and prices its bid, so it is committee information exactly
# like a submission is. Nothing about it being a *removal* makes it public.
print("\npdc-alerts — a void carries the terms it removed, and stays private")

reset()
v = offer(status="voided",
          void={"at": iso(0), "by": "facHead", "reason": "Submitted on the wrong player",
                "from_status": "submitted"})
fn.notify_offer_voided(v)
e = pdc()[0]
check("the void is announced privately, and only privately",
      len(pdc()) == 1 and news() == [], news())
check("the title says it was voided", "voided" in e["title"], e["title"])
check("...coloured apart from a remand, which comes back", e["color"] == fn.COLOR_VOID)
check("the reason is quoted", "wrong player" in field(e, "Reason"), field(e, "Reason"))
check("the terms leaving the board are shown one last time",
      "26-27" in field(e, "Voided terms"), field(e, "Voided terms"))
check("...and it is attributed to the head who did it",
      "facHead" in e["footer"]["text"], e["footer"])
check("the announcement says the team may bid again",
      "bid again" in e["description"], e["description"])

reset()
fn.notify_offer_restored(offer(status="submitted"), v["void"])
e = pdc()[0]
check("the undo is announced too — the channel watched the bid leave",
      len(pdc()) == 1 and news() == [], news())
check("...saying what it came back as", "**submitted**" in e["description"], e["description"])
check("...and recalling why it went", "wrong player" in field(e, "Stated reason"),
      field(e, "Stated reason"))


# ── Finalize (§ 4.4, § 11.1) ──────────────────────────────────────────────────
print("\npdc-alerts — finalize records the allocation and signs nobody")

reset()
FINAL = {"locked_at": iso(0), "locked_by": "facHead",
         "totals": {"f7c1a9b2": 1400, "QO": 700, "NO_SIGNING": 900},
         "voters": ["memberA", "memberB"], "abstained": ["memberC"],
         "outstanding_remands": [{"offer": "f7c1a9b2", "number": 42, "team": "PHX",
                                  "remands": [{"by": "memberB", "note": "x"}]}]}
fn.notify_player_finalized("curry-stephen", FINAL, [offer()])
e = pdc()[0]
alloc = field(e, "Allocation")
check("offers are labelled by team and number", "PHX · offer #42" in alloc, alloc)
check("the synthetic options are spelled out",
      "Qualifying offer" in alloc and "No signing" in alloc, alloc)
check("totals are ordered by balls received", alloc.index("1400") < alloc.index("900"), alloc)
check("voters and abstentions are both recorded",
      "memberA" in field(e, "Voted") and "memberC" in field(e, "Abstained"))
check("locking with unanswered remands is on the record",
      any("unanswered remands" in f["name"] for f in e["fields"]))
check("the post is explicit that nobody has been signed",
      "No signing has been made" in e["description"], e["description"])


# ── The agent stage (§ 4.7, § 9.1) ────────────────────────────────────────────
print("\npdc-alerts — the agent stage, private and never public")

reset()
fn.notify_player_claimed("curry-stephen", "Avatar", ["WAS"])
e = pdc()[0]
check("a claim is announced — it is the moment a team loses the right to bid",
      len(pdc()) == 1 and news() == [], news())
check("...naming the agent", "Avatar" in e["title"], e["title"])
check("...and the team it just barred", "WAS" in field(e, "Now barred"), field(e, "Now barred"))
check("...saying the bar outlives the claim",
      "survives a release" in e["footer"]["text"], e["footer"])

reset()
fn.notify_player_claimed("curry-stephen", "facHead", [])
check("a head's claim bars nobody, and says so rather than rendering blank",
      "head claim" in field(pdc()[0], "Now barred"), field(pdc()[0], "Now barred"))

reset()
fn.notify_player_released("curry-stephen", "Avatar", "Avatar")
e = pdc()[0]
check("a release is announced privately", len(pdc()) == 1 and news() == [], news())
check("...and repeats that the bar stays", "stay barred" in e["description"], e["description"])

reset()
fn.notify_player_released("curry-stephen", "Avatar", "facHead")
check("a head prising a claim loose names both parties",
      "Avatar" in pdc()[0]["title"] and "facHead" in pdc()[0]["title"], pdc()[0]["title"])

reset()
fn.notify_player_advanced("curry-stephen", "Avatar",
                          [offer(), offer(id="b2", number=43, team="BKN")],
                          "PHX is the stronger fit")
e = pdc()[0]
adv = field(e, "Advanced")
check("the advance is private", len(pdc()) == 1 and news() == [], news())
check("...and carries the surviving slate, not just the fact of it",
      "PHX" in adv and "BKN" in adv and "#42" in adv and "#43" in adv, adv)
check("...counted in the field name", "(2)" in
      next(f["name"] for f in e["fields"] if f["name"].startswith("Advanced")))
check("...with the agent's note", "stronger fit" in field(e, "Agent's note"))
check("...and says the ballot waits on the head assigning a sub-committee",
      "assigns a sub-committee" in e["description"], e["description"])

reset()
fn.notify_player_advanced("curry-stephen", "Avatar", [offer()], "")
check("no note renders no note field, rather than an empty one",
      not any(f["name"] == "Agent's note" for f in pdc()[0]["fields"]))

reset()
fn.notify_returned_to_agent("curry-stephen", "Avatar", "facHead", "PHX's year 3 went illegal")
e = pdc()[0]
check("a send-back is private", len(pdc()) == 1 and news() == [], news())
check("...carries the head's reason", "year 3" in field(e, "Reason"), field(e, "Reason"))
check("...and states that cast ballots survive it",
      "Ballots already cast stand" in e["description"], e["description"])

reset()
fn.notify_player_finalized("curry-stephen",
                           {"locked_at": iso(0), "locked_by": "Avatar", "totals": {},
                            "voters": [], "path": "agent"}, [offer()])
e = pdc()[0]
check("an uncontested lock reads as uncontested, not as a ballot nobody filled in",
      "uncontested" in e["title"].lower(), e["title"])
check("...explaining that curation left one bid standing",
      "single bid" in e["description"], e["description"])
check("...and still signing nobody", "No signing has been made" in e["description"])

reset()
fn.notify_player_finalized("curry-stephen",
                           {"locked_at": iso(0), "locked_by": "facHead", "totals": {},
                            "voters": [], "path": "committee"}, [offer()])
check("a committee lock keeps the plain title",
      "uncontested" not in pdc()[0]["title"].lower(), pdc()[0]["title"])


# ── Expiry announces once, and never re-announces (§ 4.1, § 9.2) ──────────────
print("\nexpiry — once, by whoever observes it, and never on a replay")

reset()
fn.notify_ffa_closed("curry-stephen", {"deadline": iso(-(fn.MAX_CLOSE_AGE_HOURS + 5))})
check("a long-expired window is not announced at all", len(SENT) == 0, SENT)

import routers.free_agency as fa  # noqa: E402

STATE = {"seq": 0, "mode": "ffa", "rounds": [], "players": {
    "curry-stephen": {"status": "open", "round_id": "ffa-aaa",
                      "ffa": {"deadline": iso(-1), "started_by_offer": "f7c1a9b2"}},
}}
SAVED = {"n": 0}
fa._load_state = lambda: STATE
fa._save_state = lambda s: SAVED.__setitem__("n", SAVED["n"] + 1)

reset()
fa._sweep_ffa_expiry()
first = len(news())
fa._sweep_ffa_expiry()
fa._sweep_ffa_expiry()
check("an expired clock announces on first observation", first == 1, first)
check("...and never again, however often it is observed", len(news()) == 1, news())
check("the guard is persisted, not just in memory",
      STATE["players"]["curry-stephen"]["ffa"].get("closed_posted") and SAVED["n"] == 1,
      SAVED["n"])

reset()
STATE["players"]["curry-stephen"]["ffa"] = {"deadline": iso(-1)}
STATE["mode"] = "rounds"
fa._sweep_ffa_expiry()
check("outside FFA mode there is no clock to close", len(SENT) == 0, SENT)
STATE["mode"] = "ffa"

reset()
STATE["players"]["curry-stephen"]["ffa"] = {"deadline": iso(6)}
fa._sweep_ffa_expiry()
check("a running clock is left alone", len(SENT) == 0, SENT)


# ── Kill switches, independently (Phase 6 rollout order) ──────────────────────
print("\nkill switches — each channel inert on its own")

reset()
fn.DISCORD_FA_NEWS_CHANNEL = ""
fn.notify_ffa_started("curry-stephen", FFA)
check("with fa-news unset the committee still hears about the clock", len(pdc()) == 1)
check("...and the league hears nothing", len(news()) == 0)
fn.DISCORD_FA_NEWS_CHANNEL = "news-chan"

reset()
fn.DISCORD_PDC_CHANNEL = ""
fn.notify_offer_submitted(offer())
fn.notify_ffa_started("curry-stephen", FFA)
check("with pdc-alerts unset nothing private is posted", len(pdc()) == 0)
check("...while the public clock post is unaffected", len(news()) == 1)
fn.DISCORD_PDC_CHANNEL = "pdc-chan"

reset()
tp.DISCORD_BOT_TOKEN = ""
fn.notify_offer_submitted(offer())
fn.notify_ffa_started("curry-stephen", FFA)
check("no bot token means nothing is sent anywhere", len(SENT) == 0, SENT)
tp.DISCORD_BOT_TOKEN = "test-token"


# ── Burst caps are per channel ────────────────────────────────────────────────
# A runaway on one feed must not silence the other: the committee losing its
# alerts because the league feed is looping (or vice versa) turns one bug into
# two, and the two channels' sizing has nothing to do with each other.
print("\nburst caps — one feed flooding must not silence the other")

reset()
for i in range(fn.PDC_MAX_BURST + 60):
    fn.notify_offer_submitted(offer(id=f"x{i}"))
check(f"a private flood is clipped at PDC_MAX_BURST ({fn.PDC_MAX_BURST})",
      len(pdc()) == fn.PDC_MAX_BURST, len(pdc()))
fn.notify_ffa_started("curry-stephen", FFA)
check("...and the public channel is unaffected", len(news()) == 1, len(news()))

reset()
for i in range(fn.NEWS_MAX_BURST + 20):
    fn.notify_ffa_closed(f"p{i}", {"deadline": iso(-1)})
check(f"a public flood is clipped at NEWS_MAX_BURST ({fn.NEWS_MAX_BURST})",
      len(news()) == fn.NEWS_MAX_BURST, len(news()))
check("the sweep's worst case — a full slate closing at once — is not clipped",
      fn.NEWS_MAX_BURST >= 40, fn.NEWS_MAX_BURST)


# ── Never raises into the caller ──────────────────────────────────────────────
print("\nrobustness — a notification failure is never the caller's problem")

reset()
for name, call in [
    ("a malformed offer", lambda: fn.notify_offer_submitted({})),
    ("a malformed remand", lambda: fn.notify_offer_remanded({}, {})),
    ("a malformed void", lambda: fn.notify_offer_voided({})),
    ("a malformed restore", lambda: fn.notify_offer_restored({}, {})),
    ("a malformed finalize", lambda: fn.notify_player_finalized("x", {}, [])),
    ("an ffa object with no deadline", lambda: fn.notify_ffa_started("x", {})),
    ("bios being unavailable", None),
]:
    if call is None:
        fn.load_player_bios = lambda: (_ for _ in ()).throw(RuntimeError("bios down"))
        call = lambda: fn.notify_offer_submitted(offer())  # noqa: E731
    try:
        call()
        check(f"{name} doesn't raise", True)
    except Exception as exc:
        check(f"{name} doesn't raise", False, exc)
fn.load_player_bios = lambda: BIOS

reset()
fn.notify_offer_submitted(offer())
check("...and a working call still posts after all that", len(pdc()) == 1)


print()
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

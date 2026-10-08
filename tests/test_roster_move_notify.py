"""Regression tests for routers.roster_move_notify — the public Discord line
for a roster move made on the site.

What matters:

  * **Each move goes to the channel the league uses for it.** Renounce,
    team option, two-way conversion, rookie-scale signing and stash go to
    #fa-news; a void goes to #waivers (decided 2026-09-30). A release is not
    announced here — `waiver_notify.notify_waived` owns it.
  * **An owner's move reaches #roster-log exactly once.** Its #fa-news line is
    a bot post, which the relay skips, so the embed must be relayed instead
    (`relay_to_roster_log=True`). A self release keeps its embed unrelayed,
    because its #waivers post is relayed already.
  * **No dollars in the public line.** The contract lives in the embed.

Nothing here touches the network — the transport's enqueue is a list append.

    venv/bin/python -m tests.test_roster_move_notify
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_transport as tp  # noqa: E402
import routers.roster_move_notify as rm  # noqa: E402
import routers.transactions as tx  # noqa: E402
from routers.discord_notify import NO_RELAY_MARKER, build_embed  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"
rm.DISCORD_FA_NEWS_CHANNEL = "fa-news"
rm.DISCORD_WAIVERS_CHANNEL = "waivers"
rm.load_player_bios = lambda: {"ishchenko-vsevolod": {"name": "ISHCHENKO, VSEVOLOD"}}


def txn(kind, **details):
    return {"id": "t1", "type": kind,
            "details": {"player": "ishchenko-vsevolod", "team": "MIL", **details}}


def said(t):
    return rm.announcement(t)


print("\nannouncement — channel and wording per move")

cases = [
    (txn("renounce"), "fa-news", "The Milwaukee Bucks renounce the rights to Vsevolod Ishchenko."),
    (txn("option", decision="accept", year="27-28"), "fa-news",
     "The Milwaukee Bucks exercise Vsevolod Ishchenko's 27-28 team option."),
    (txn("option", decision="decline", year="27-28", cap_hold_type="UFA"), "fa-news",
     "The Milwaukee Bucks decline Vsevolod Ishchenko's 27-28 team option. "
     "Vsevolod Ishchenko becomes an unrestricted free agent."),
    (txn("option", decision="decline", year="27-28", cap_hold_type="RFA"), "fa-news",
     "The Milwaukee Bucks decline Vsevolod Ishchenko's 27-28 team option. "
     "Vsevolod Ishchenko becomes a restricted free agent."),
    (txn("convert_twoway"), "fa-news",
     "The Milwaukee Bucks convert Vsevolod Ishchenko from a two-way to a standard contract."),
    (txn("sign_pick"), "fa-news", "The Milwaukee Bucks sign Vsevolod Ishchenko to a rookie-scale contract."),
    (txn("stash"), "fa-news", "The Milwaukee Bucks stash the draft rights to Vsevolod Ishchenko."),
    (txn("void_player"), "waivers", "The Milwaukee Bucks void Vsevolod Ishchenko's contract."),
]
for t, channel, text in cases:
    got = said(t)
    check(f"{t['type']} {t['details'].get('decision', '')}".strip() + f" → #{channel}",
          got == (channel, text), got)
    check(f"{t['type']}: no dollar figure in the public line", got and "$" not in got[1])

for kind in ("release", "trade", "sign", "extension"):
    check(f"{kind} is not announced here", said(txn(kind)) is None)


print("\nfa_result_announcement — a PDC declare-winner signing")

contract = {"salaries": {"26-27": "$4,000,000", "27-28": "$4,200,000"}, "cap_holds": {"27-28": "TEAM_OPT"}}
got = rm.fa_result_announcement(txn("sign", contract=contract))
check("a declared sign → #fa-news with its terms",
      got == ("fa-news", "The Milwaukee Bucks sign Vsevolod Ishchenko — 1+1 TO · $8.2M."), got)
got = rm.fa_result_announcement({"id": "t2", "type": "offer_sheet", "details": {
    "player": "ishchenko-vsevolod", "teams": ["MIL", "BOS"], "contract": contract}})
check("a declared offer sheet names both teams and the match window",
      got == ("fa-news", "The Milwaukee Bucks sign Vsevolod Ishchenko to an offer sheet — 1+1 TO · $8.2M. "
                         "The Boston Celtics have 48 hours to match."), got)
check("anything else is not a declare-winner result", rm.fa_result_announcement(txn("renounce")) is None)
SENT.clear()
check("announce_fa_result queues it", rm.announce_fa_result(txn("sign", contract=contract)) is True
      and SENT and SENT[0][0] == "fa-news", SENT)


print("\nannounce — sends, and stays inert without a channel")

SENT.clear()
check("a renounce is queued", rm.announce(txn("renounce")) is True)
check("…to #fa-news as plain content",
      SENT == [("fa-news", {"content": "The Milwaukee Bucks renounce the rights to Vsevolod Ishchenko."})],
      SENT)

SENT.clear()
rm.DISCORD_WAIVERS_CHANNEL = ""
check("no #waivers channel → a void sends nothing", rm.announce(txn("void_player")) is False and not SENT)
rm.DISCORD_WAIVERS_CHANNEL = "waivers"

SENT.clear()
check("a release sends nothing from here", rm.announce(txn("release")) is False and not SENT)

rm.announcement, real = (lambda t: 1 / 0), rm.announcement
check("an error inside never raises", rm.announce(txn("renounce")) is False)
rm.announcement = real


print("\nself-serve wiring — each owner move reaches #roster-log once")

calls = []
tx.notify_transaction = lambda t, *a, **k: calls.append(("embed", k.get("relay_to_roster_log", False)))
tx.announce_roster_move = lambda t: calls.append(("line", t["type"]))
tx._notify_self_serve(txn("renounce"))
check("_notify_self_serve relays the embed and posts the line",
      calls == [("embed", True), ("line", "renounce")], calls)

for fn in ("self_renounce", "self_option", "self_convert_twoway", "self_sign_pick", "self_stash"):
    src = inspect.getsource(getattr(tx, fn))
    check(f"{fn} announces through _notify_self_serve", "_notify_self_serve(txn)" in src)
src = inspect.getsource(tx.self_release)
check("self_release keeps its embed unrelayed (its #waivers post is relayed)",
      "_notify_self_serve" not in src and "notify_transaction(txn)" in src and "notify_waived" in src)
src = inspect.getsource(tx.create_transaction)
check("office void_player posts to #waivers", 'body.type == "void_player"' in src
      and "announce_roster_move(txn)" in src)

tx.load_player_bios = rm.load_player_bios
import routers.discord_notify as dn  # noqa: E402
dn.load_player_bios = rm.load_player_bios
relayed = build_embed({**txn("renounce"), "date": "2026-09-30"}, relay_to_roster_log=True)
check("a relayed embed carries no do-not-relay marker",
      NO_RELAY_MARKER not in (relayed.get("footer") or {}).get("text", ""))


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all passed")

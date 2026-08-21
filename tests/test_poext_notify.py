"""Regression tests for routers.poext_notify — the PDC extension pipeline's
Discord feeds. Spec: nbn-today/docs/poext-extension-pipeline.md D9/D12.

Three channels, and the property worth pinning is the disclosure boundary,
same as test_fa_notify.py's for the module this one is modeled on:

  * **pdc-alerts** (private) gets every real event — submitted, remanded,
    voided, restored, and finalize either way.
  * **#roster-log** and **fa-news** (both public) get `agreed` only — never
    a submission, a remand, or a rejection. `#roster-log` carries full
    detail (team, contract shorthand); **fa-news carries neither** — no team
    abbreviation, no `$`, ever, asserted against rendered output the same
    way test_fa_notify.py asserts it for free agency's own public channel.
  * Each channel is independently inert without its own env var.
  * Nothing raises into the caller.

Nothing here touches the network — the transport's enqueue is a list append.

    venv/bin/python -m tests.test_poext_notify
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_transport as tp  # noqa: E402
import routers.poext_notify as pn  # noqa: E402
import routers.roster_log_relay as rlr  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"
pn.DISCORD_PDC_CHANNEL = "pdc-chan"
pn.DISCORD_FA_NEWS_CHANNEL = "fa-news-chan"
rlr.DISCORD_ROSTER_LOG_CHANNEL = "roster-log-chan"
pn.load_player_bios = lambda: {"barlow-dominick": {"name": "BARLOW, DOMINICK"}}

PROPOSAL = {
    "id": "abc1", "number": 7, "player": "barlow-dominick", "team": "SAS",
    "version": 1, "kind": "veteran",
    "contract": {"salaries": {"27-28": "$3,000,000", "28-29": "$3,200,000"}, "cap_holds": {}},
}


def reset():
    SENT.clear()


def by_channel(channel):
    return [p for c, p in SENT if c == channel]


def payload_text(p):
    if "content" in p:
        return p["content"]
    e = p["embeds"][0]
    return " ".join(str(v) for v in [e.get("title", ""), e.get("description", "")]
                    + [f.get("value", "") for f in e.get("fields", [])])


print("each real event posts once to pdc-alerts only")
reset()
pn.notify_proposal_submitted(PROPOSAL)
check("submitted -> 1 post, private only", len(SENT) == 1 and SENT[0][0] == "pdc-chan")
check("names the team and player", "SAS" in payload_text(SENT[0][1]) and "Dominick" in payload_text(SENT[0][1]))

reset()
pn.notify_proposal_remanded(PROPOSAL, {"by": "headMember", "note": "raise Year 1", "from_version": 1})
check("remanded -> 1 post, private only", len(SENT) == 1 and SENT[0][0] == "pdc-chan")
check("names who and the note", "headMember" in payload_text(SENT[0][1]) and "raise Year 1" in payload_text(SENT[0][1]))

reset()
pn.notify_proposal_voided({**PROPOSAL, "void": {"by": "headMember", "reason": "wrong player"}})
check("voided -> 1 post, private only", len(SENT) == 1 and SENT[0][0] == "pdc-chan")
check("names the reason", "wrong player" in payload_text(SENT[0][1]))

reset()
pn.notify_proposal_restored({**PROPOSAL, "status": "submitted"})
check("restored -> 1 post, private only", len(SENT) == 1 and SENT[0][0] == "pdc-chan")

print("\nfinalize: agreed reaches all three channels, rejected reaches only pdc-alerts")
reset()
pn.notify_player_finalized("barlow-dominick", PROPOSAL, {
    "outcome": "agreed", "accept": 3, "reject": 0, "locked_by": "headMember", "rejections_total": 0, "exhausted": False,
})
check("agreed -> exactly 3 posts (pdc-alerts, roster-log, fa-news)", len(SENT) == 3, SENT)
check("...one to pdc-alerts", len(by_channel("pdc-chan")) == 1)
check("...one to roster-log", len(by_channel("roster-log-chan")) == 1)
check("...one to fa-news", len(by_channel("fa-news-chan")) == 1)
pdc_payload = by_channel("pdc-chan")[0]
check("pdc-alerts footer notes the manual hand-off",
      "hand" in pdc_payload["embeds"][0].get("footer", {}).get("text", ""))
roster_log_text = by_channel("roster-log-chan")[0]["embeds"][0]["description"]
check("roster-log names the team", "SAS" in roster_log_text)
check("roster-log carries the contract shorthand", "3.0" in roster_log_text or "$" in roster_log_text)
check("roster-log is worded as pending, not applied", "pending" in roster_log_text.lower())
fa_news_text = by_channel("fa-news-chan")[0]["content"]
check("fa-news names the player", "Dominick" in fa_news_text)

reset()
pn.notify_player_finalized("barlow-dominick", PROPOSAL, {
    "outcome": "rejected", "accept": 1, "reject": 3, "locked_by": "headMember", "rejections_total": 3, "exhausted": True,
})
check("rejected -> exactly 1 post (pdc-alerts only)", len(SENT) == 1 and SENT[0][0] == "pdc-chan", SENT)
check("exhaustion is stated in the private post", "§ 6.3" in payload_text(SENT[0][1]))
check("no public post leaked a rejection", not by_channel("roster-log-chan") and not by_channel("fa-news-chan"))

print("\nthe disclosure boundary — asserted against rendered output")
TEAM_ABBRS = ["ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
              "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
              "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"]
ABBR_RE = re.compile(r"\b(" + "|".join(TEAM_ABBRS) + r")\b")
check("no team abbreviation in the fa-news post", not ABBR_RE.search(fa_news_text), fa_news_text)
check("no dollar figure in the fa-news post", "$" not in fa_news_text, fa_news_text)
check("_news()'s own signature takes only (slug, text) — can't be handed a proposal or a team",
      pn._news.__code__.co_argcount == 2)

print("\neach public channel is independently inert without its own config")
reset()
pn.DISCORD_FA_NEWS_CHANNEL = ""
pn.notify_player_finalized("barlow-dominick", PROPOSAL, {
    "outcome": "agreed", "accept": 3, "reject": 0, "locked_by": "headMember", "rejections_total": 0, "exhausted": False,
})
check("no fa-news channel -> pdc-alerts and roster-log still post, fa-news doesn't",
      len(by_channel("pdc-chan")) == 1 and len(by_channel("roster-log-chan")) == 1 and not by_channel("fa-news-chan"))
pn.DISCORD_FA_NEWS_CHANNEL = "fa-news-chan"

reset()
rlr.DISCORD_ROSTER_LOG_CHANNEL = ""
pn.notify_player_finalized("barlow-dominick", PROPOSAL, {
    "outcome": "agreed", "accept": 3, "reject": 0, "locked_by": "headMember", "rejections_total": 0, "exhausted": False,
})
check("no roster-log channel -> pdc-alerts and fa-news still post, roster-log doesn't",
      len(by_channel("pdc-chan")) == 1 and len(by_channel("fa-news-chan")) == 1 and not by_channel("roster-log-chan"))
rlr.DISCORD_ROSTER_LOG_CHANNEL = "roster-log-chan"

reset()
pn.DISCORD_PDC_CHANNEL = ""
pn.notify_proposal_submitted(PROPOSAL)
check("no pdc-alerts channel -> nothing sent (submit has no public path)", len(SENT) == 0)
pn.DISCORD_PDC_CHANNEL = "pdc-chan"

reset()
tp.DISCORD_BOT_TOKEN = ""
pn.notify_proposal_submitted(PROPOSAL)
check("no bot token -> nothing sent", len(SENT) == 0)
tp.DISCORD_BOT_TOKEN = "test-token"

print("\nnever raises into the caller")
reset()


def boom():
    raise RuntimeError("payload build blew up")


ok = True
try:
    pn._alert(boom)
except Exception:
    ok = False
check("a payload builder that raises is swallowed, not propagated", ok)

print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")

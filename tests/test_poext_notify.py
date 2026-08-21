"""Regression tests for routers.poext_notify — the PDC extension pipeline's
Discord feed. Spec: nbn-today/docs/poext-extension-pipeline.md D9/D12.

Unlike fa_notify, there's only one channel here (the shared `pdc-alerts`,
DISCORD_PDC_CHANNEL — deliberately the same env var free agency already
posts to, not a second one) and no public half yet, so the property worth
pinning is narrower: every real event posts exactly once, the module is
inert without config, and nothing raises into the caller.

Nothing here touches the network — the transport's enqueue is a list append.

    venv/bin/python -m tests.test_poext_notify
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.discord_transport as tp  # noqa: E402
import routers.poext_notify as pn  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


SENT: list[tuple[str, dict]] = []
tp._enqueue = lambda msg: SENT.append((msg["channel"], msg["payload"]))
tp.DISCORD_BOT_TOKEN = "test-token"
pn.DISCORD_PDC_CHANNEL = "pdc-chan"
pn.load_player_bios = lambda: {"barlow-dominick": {"name": "BARLOW, DOMINICK"}}

PROPOSAL = {
    "id": "abc1", "number": 7, "player": "barlow-dominick", "team": "SAS",
    "version": 1, "kind": "veteran",
    "contract": {"salaries": {"27-28": "$3,000,000", "28-29": "$3,200,000"}, "cap_holds": {}},
}


def reset():
    SENT.clear()


def payload_text(p):
    e = p["embeds"][0]
    return " ".join(str(v) for v in [e.get("title", ""), e.get("description", "")]
                    + [f.get("value", "") for f in e.get("fields", [])])


print("each real event posts exactly once")
reset()
pn.notify_proposal_submitted(PROPOSAL)
check("submitted -> 1 post", len(SENT) == 1)
check("names the team and player", "SAS" in payload_text(SENT[0][1]) and "Dominick" in payload_text(SENT[0][1]))

reset()
pn.notify_proposal_remanded(PROPOSAL, {"by": "headMember", "note": "raise Year 1", "from_version": 1})
check("remanded -> 1 post", len(SENT) == 1)
check("names who and the note", "headMember" in payload_text(SENT[0][1]) and "raise Year 1" in payload_text(SENT[0][1]))

reset()
pn.notify_proposal_voided({**PROPOSAL, "void": {"by": "headMember", "reason": "wrong player"}})
check("voided -> 1 post", len(SENT) == 1)
check("names the reason", "wrong player" in payload_text(SENT[0][1]))

reset()
pn.notify_proposal_restored({**PROPOSAL, "status": "submitted"})
check("restored -> 1 post", len(SENT) == 1)

reset()
pn.notify_player_finalized("barlow-dominick", "SAS", {
    "outcome": "agreed", "accept": 3, "reject": 0, "locked_by": "headMember", "rejections_total": 0, "exhausted": False,
})
check("finalize (agreed) -> 1 post", len(SENT) == 1)
check("agreed footer notes the manual hand-off", "hand" in SENT[0][1]["embeds"][0].get("footer", {}).get("text", ""))

reset()
pn.notify_player_finalized("barlow-dominick", "SAS", {
    "outcome": "rejected", "accept": 1, "reject": 3, "locked_by": "headMember", "rejections_total": 3, "exhausted": True,
})
check("finalize (rejected, exhausted) -> 1 post", len(SENT) == 1)
check("exhaustion is stated", "§ 6.3" in payload_text(SENT[0][1]))
check("no manual-entry footer on a rejection", "footer" not in SENT[0][1]["embeds"][0])

print("\ninert without config")
reset()
pn.DISCORD_PDC_CHANNEL = ""
pn.notify_proposal_submitted(PROPOSAL)
check("no channel configured -> nothing sent", len(SENT) == 0)
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

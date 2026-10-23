"""The 2K injury report — routers/injuries.py.

What is pinned:

  * **One pending report per date.** A second screenshot the same day joins
    the first; the upload is gated to stats/rosters (not streamer) and refuses an
    unreadable format.
  * **The diff.** New, changed (status, injury or duration — not OVR) and
    healed. A player on the current list who isn't on the new report is
    healed — the report is the whole league's list. **A long injury that
    vanishes is confirmed, not assumed:** healing a player last reported at
    2+ weeks (or an unreadable duration) is a 409 that writes nothing, until
    the request names that player in `confirm_heals`.
  * **The team is the roster's.** A logo that disagrees is a warning, not the
    answer; a player on no roster falls back to the logo, with a warning.
  * **Refusals name every problem and write nothing**: an unknown slug, a
    player twice, a report older than the current list.
  * **Discord gets only changes**, pings exactly the affected teams, and a
    re-commit of the same report posts nothing. A long report splits by team.
  * **Paths**: nothing from a URL reaches the filesystem without matching a
    strict pattern.

Writes go to a temp directory; nothing here touches live data.

    venv/bin/python -m tests.test_injuries
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import routers.audit as audit  # noqa: E402
import routers.auth as auth  # noqa: E402
import routers.discord_transport as tp  # noqa: E402
import routers.injuries as inj  # noqa: E402
from routers.discord_notify import TEAM_ROLE_IDS  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


# ── in-memory world ───────────────────────────────────────────────────────────

STREAM_TOKEN = "s" * 64
STATS_TOKEN = "k" * 64
PLAIN_TOKEN = "n" * 64
MEMBERS = {
    "Streamy": {"token": STREAM_TOKEN, "roles": ["streamer"], "tenures": []},
    "Statsy": {"token": STATS_TOKEN, "roles": ["stats"], "tenures": []},
    "Nobody": {"token": PLAIN_TOKEN, "roles": [], "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-injuries-test-"))
inj.PENDING_DIR = TMP / "pending-injury-reports"
inj.KEPT_DIR = TMP / "injury-report-screenshots"
inj.INJURIES_FILE = TMP / "injuries.json"
inj.HISTORY_FILE = TMP / "injury-history.jsonl"
inj.ROSTERS_DIR = TMP
audit.EDITS_FILE = TMP / "edits.jsonl"

BIOS = {
    "reed-paul": {"name": "REED, PAUL"},
    "ingram-brandon": {"name": "INGRAM, BRANDON"},
    "jokic-nikola": {"name": "JOKIC, NIKOLA"},
    "okongwu-onyeka": {"name": "OKONGWU, ONYEKA"},
    "free-agent": {"name": "AGENT, FREE"},
}
inj.load_player_bios = lambda: BIOS
for team, slugs in {"LAL": ["reed-paul"], "CLE": ["ingram-brandon"],
                    "DEN": ["jokic-nikola"], "ATL": ["okongwu-onyeka"]}.items():
    (TMP / f"{team.lower()}-roster.csv").write_text("SLUG\n" + "\n".join(slugs) + "\n")

SENT = []
inj.DISCORD_INJURY_CHANNEL = "123"
tp._enqueue = lambda msg: SENT.append(msg["payload"])
tp.DISCORD_BOT_TOKEN = "test-token"

app = FastAPI()
app.include_router(inj.router)
c = TestClient(app)

STREAM = {"Authorization": "Bearer " + STREAM_TOKEN}
STATS = {"Authorization": "Bearer " + STATS_TOKEN}
PLAIN = {"Authorization": "Bearer " + PLAIN_TOKEN}


def png(color=(20, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 32), color).save(buf, "PNG")
    return buf.getvalue()


def upload(headers=STATS, data=None, date="2026-10-22"):
    return c.post("/api/injuries/upload", data={"date": date},
                  files={"image": ("shot.png", data or png(), "image/png")}, headers=headers)


def row(slug, status="Hurt", injury="Strained Left Hip", duration="4-6 weeks", ovr=80, delta=-2, shown=None):
    return {"slug": slug, "status": status, "injury": injury, "duration": duration,
            "ovr": ovr, "ovr_delta": delta, "team_shown": shown}


def commit(rows, date="2026-10-22", report_id=None, headers=STATS, confirm=()):
    return c.post("/api/injuries/commit", headers=headers,
                  json={"date": date, "rows": rows, "report_id": report_id, "confirm_heals": list(confirm)})


def pinged(payload):
    return set(payload.get("allowed_mentions", {}).get("roles", []))


# ── upload ────────────────────────────────────────────────────────────────────

print("upload")
check("no token cannot upload", upload(headers={}).status_code in (401, 403))
check("a member without the role cannot upload", upload(headers=PLAIN).status_code == 403)
check("a streamer cannot upload", upload(headers=STREAM).status_code == 403)
check("an unreadable format is refused", upload(data=b"not an image").status_code == 415)
check("a bad date is refused", upload(date="10/22").status_code == 422)
r = upload()
check("the stats role uploads", r.status_code == 200, r.text)
item = r.json()["item"]
r2 = upload(headers=STATS)
check("a second screenshot joins the same date's report",
      r2.json()["item"]["id"] == item["id"] and len(r2.json()["item"]["images"]) == 2)
check("a different date is a different report", upload(date="2026-10-21").json()["item"]["id"] != item["id"])
pending = c.get("/api/injuries/pending", headers=STATS).json()
check("pending lists both dates, oldest first", [p["date"] for p in pending] == ["2026-10-21", "2026-10-22"])
check("pending needs a role", c.get("/api/injuries/pending", headers=PLAIN).status_code == 403)
older = pending[0]
check("a streamer cannot remove an image",
      c.delete(f"/api/injuries/pending/{older['id']}/images/shot-1.png", headers=STREAM).status_code == 403)
r = c.delete(f"/api/injuries/pending/{older['id']}/images/shot-1.png", headers=STATS)
check("removing a report's last image removes the report", r.status_code == 200 and r.json()["item"] is None)
check("a non-id is a 404", c.delete("/api/injuries/pending/..%2F..", headers=STATS).status_code == 404)
check("a non-id image is a 404", c.delete("/api/injuries/pending/xx/images/shot-1.png", headers=STATS).status_code == 404)
check("a bad file name is a 404", c.get(f"/api/injuries/screenshots/{item['id']}/../meta.json").status_code == 404)
check("the image is readable publicly",
      c.get(f"/api/injuries/screenshots/{item['id']}/shot-1.png").status_code == 200)

# ── commit refusals ───────────────────────────────────────────────────────────

print("commit refusals")
check("a streamer cannot commit", commit([row("reed-paul")], headers=STREAM).status_code == 403)
r = commit([row("nobody-at-all"), row("reed-paul"), row("reed-paul")])
check("unknown slug and duplicate are both named", r.status_code == 422
      and "not a player bio" in r.text and "listed twice" in r.text, r.text)
check("missing fields are refused", commit([row("reed-paul", injury=" ")]).status_code == 422)
check("nothing was written", not inj.INJURIES_FILE.exists() and SENT == [])

# ── first report ──────────────────────────────────────────────────────────────

print("first report")
r = commit([row("reed-paul", status="hurt", shown="LAL"),
            row("ingram-brandon", injury="Strained Right Hip", shown="ATL"),
            row("free-agent", shown="BOS")], report_id=item["id"])
check("commits", r.status_code == 200, r.text)
body = r.json()
check("three new", body["new"] == 3 and body["changed"] == 0 and body["healed"] == 0, body)
check("a logo that disagrees with the roster is a warning",
      any("Brandon Ingram showed as ATL" in w and "filed under CLE" in w for w in body["warnings"]), body["warnings"])
check("a player on no roster falls back to the logo",
      any("Free Agent is on no roster; filed under BOS" in w for w in body["warnings"]), body["warnings"])
cur = c.get("/api/injuries").json()
by = {p["slug"]: p for p in cur["players"]}
check("status is title-cased", by["reed-paul"]["status"] == "Hurt")
check("team comes from the roster", by["ingram-brandon"]["team"] == "CLE")
check("names are joined", by["reed-paul"]["name"] == "Paul Reed")
check("since is the first report", by["reed-paul"]["since"] == "2026-10-22")
check("last report recorded", cur["last_report"]["id"] == item["id"] and cur["last_report"]["date"] == "2026-10-22")
check("one Discord message", len(SENT) == 1, SENT)
msg = SENT[0]
check("it pings exactly the affected teams",
      pinged(msg) == {TEAM_ROLE_IDS["LAL"], TEAM_ROLE_IDS["CLE"], TEAM_ROLE_IDS["BOS"]}, pinged(msg))
check("it reads as a new injury",
      "🚑 **New**" in msg["content"] and f"<@&{TEAM_ROLE_IDS['LAL']}> Paul Reed - Hurt - Strained Left Hip - 4-6 weeks - OVR 80 (-2)" in msg["content"],
      msg["content"])
check("no @everyone can slip through", msg["allowed_mentions"]["parse"] == [])
lines = msg["content"].splitlines()
check("each player line starts with its team's tag",
      all(ln.startswith("<@&") for ln in lines if " - " in ln), msg["content"])
check("there is no trailing line of tags", all(" - " in ln for ln in lines if "<@&" in ln), msg["content"])
check("the screenshots moved to the kept folder as WebP",
      not (inj.PENDING_DIR / item["id"]).exists() and (inj.KEPT_DIR / item["id"] / "shot-1.webp").exists())
check("committing a report that's gone is a 404",
      commit([row("reed-paul")], report_id=item["id"]).status_code == 404)

# ── same report again ─────────────────────────────────────────────────────────

print("re-commit")
SENT.clear()
r = commit([row("reed-paul", shown="LAL"), row("ingram-brandon", injury="Strained Right Hip"),
            row("free-agent", shown="BOS")])
check("nothing changed", r.json()["new"] == r.json()["changed"] == r.json()["healed"] == 0, r.json())
check("and nothing is posted", SENT == [])

# ── next day: a change, a new one, and one off the report ─────────────────────

print("next report")
DAY2 = [row("reed-paul", status="Out", ovr=79, delta=-3), row("ingram-brandon", injury="Strained Right Hip"),
        row("jokic-nikola", injury="Bruised Left Hip", duration="1-2 weeks")]
SENT.clear()
r = commit(DAY2, date="2026-10-23")
check("healing a 4-6 week injury needs confirming", r.status_code == 409
      and r.json()["detail"]["error"] == "confirm_heals", r.text)
check("the refusal names who and what", [(p["slug"], p["duration"], p["since"]) for p in r.json()["detail"]["players"]]
      == [("free-agent", "4-6 weeks", "2026-10-22")], r.json())
check("and nothing was written or posted", SENT == [] and len(c.get("/api/injuries").json()["players"]) == 3
      and c.get("/api/injuries").json()["last_report"]["date"] == "2026-10-22")
r = commit(DAY2, date="2026-10-23", confirm=["somebody-else"])
check("confirming someone else doesn't count", r.status_code == 409)
r = commit(DAY2, date="2026-10-23", confirm=["free-agent"])
body = r.json()
check("one changed, one new, one healed", (body["new"], body["changed"], body["healed"]) == (1, 1, 1), body)
check("the player off the report is no longer injured", body["injured"] == 3
      and "free-agent" not in {p["slug"] for p in c.get("/api/injuries").json()["players"]})
check("the change shows the status move", "Hurt → Out" in SENT[-1]["content"], SENT[-1]["content"])
check("the healed player is announced", "Free Agent - back from Strained Left Hip" in SENT[-1]["content"], SENT[-1]["content"])
check("only the teams with a change are pinged",
      pinged(SENT[-1]) == {TEAM_ROLE_IDS["LAL"], TEAM_ROLE_IDS["DEN"], TEAM_ROLE_IDS["BOS"]}, pinged(SENT[-1]))
cur = {p["slug"]: p for p in c.get("/api/injuries").json()["players"]}
check("since survives a change", cur["reed-paul"]["since"] == "2026-10-22" and cur["reed-paul"]["updated"] == "2026-10-23")

SENT.clear()
r = commit([row("reed-paul", status="Out", ovr=81, delta=-1)] + DAY2[1:], date="2026-10-23")
check("an OVR move alone is not news", r.json()["changed"] == 0 and SENT == [], r.json())

# ── everyone off the report is healed ─────────────────────────────────────────

print("healing")
SENT.clear()
r = commit([row("reed-paul", status="Out", ovr=81, delta=-1)], date="2026-10-24")
check("a short injury heals without asking; only the long one is flagged",
      r.status_code == 409 and [p["slug"] for p in r.json()["detail"]["players"]] == ["ingram-brandon"], r.text)
r = commit([row("reed-paul", status="Out", ovr=81, delta=-1)], date="2026-10-24", confirm=["ingram-brandon"])
body = r.json()
check("everyone not on the report is healed", body["healed"] == 2 and body["injured"] == 1, body)
check("cleared players are announced", "✅ **Cleared**" in SENT[-1]["content"]
      and "Nikola Jokic - back from Bruised Left Hip" in SENT[-1]["content"], SENT[-1]["content"])
check("an older report is refused", commit([row("reed-paul")], date="2026-10-23").status_code == 409)
SENT.clear()
r = commit([], date="2026-10-25", confirm=["reed-paul"])
check("an empty report clears everyone", r.json()["healed"] == 1 and r.json()["injured"] == 0, r.json())
check("and says so", "Paul Reed - back from Strained Left Hip" in SENT[-1]["content"], SENT[-1]["content"])

hist = c.get("/api/injuries/history").json()
check("history is newest first", hist[0]["date"] == "2026-10-25" and hist[-1]["date"] == "2026-10-22")
check("history has every change", len(hist) == 3 + 3 + 2 + 1, len(hist))
check("history filters by team", {e["team"] for e in c.get("/api/injuries/history?team=den").json()} == {"DEN"})
check("history filters by player", all(e["slug"] == "reed-paul"
                                       for e in c.get("/api/injuries/history?slug=reed-paul").json()))
check("history carries names", hist[0]["name"])
check("the write is in the edit log", "injuries.json" in audit.EDITS_FILE.read_text())

# ── a long report splits ──────────────────────────────────────────────────────

print("durations")
for d, days, ask in [("4-6 weeks", 28, True), ("2-4 Weeks", 14, True), ("1-2 weeks", 7, False),
                     ("3 days", 3, False), ("Day-To-Day", 0, False), ("1 month", 30, True),
                     ("Career", float("inf"), True), ("Out For Season", float("inf"), True)]:
    check(f"{d!r} is {days} days at the low end, confirm={ask}",
          inj.min_duration_days(d) == days and inj.needs_confirming({"duration": d}) == ask)
check("an unreadable duration is confirmed, not assumed",
      inj.min_duration_days("soon") is None and inj.needs_confirming({"duration": "soon"}))

print("splitting")
names = {f"p{i}": f"Player Number {i} With A Long Name" for i in range(120)}
teams = sorted(TEAM_ROLE_IDS)
events = [{"kind": "new", "slug": f"p{i}", "team": teams[i % 30],
           "after": {"status": "Out", "injury": "Pinched Nerve In The Back", "duration": "2-4 weeks",
                     "ovr": 80, "ovr_delta": -4}} for i in range(120)]
msgs = inj.build_messages("2026-10-25", events, names)
check("a long report is several messages", len(msgs) > 1, len(msgs))
check("each fits Discord's limit", all(len(m["content"]) <= 2000 for m in msgs), [len(m["content"]) for m in msgs])
check("every team is pinged exactly once overall",
      sorted(r for m in msgs for r in pinged(m)) == sorted(TEAM_ROLE_IDS.values()))
check("each message pings only the teams it names",
      all(all(f"<@&{rid}>" in m["content"] for rid in pinged(m)) for m in msgs))
check("a team with no role is written out in bold", inj._team_tag("XYZ") == "**XYZ**" and inj._team_tag(None) == "**—**")
check("no changes, no messages", inj.build_messages("2026-10-25", [], {}) == [])

print("format")
avatar = inj.build_messages("2026-10-22", [{"kind": "new", "slug": "avatar", "team": "WAS",
    "after": {"status": "Hurt", "injury": "Weak Knees", "duration": "Career", "ovr": 69, "ovr_delta": -33}}],
    {"avatar": "Avatar"})[0]
check("a line reads: @Team Player - Status - Injury - Duration - OVR n (-d)",
      avatar["content"].splitlines()[-1] == f"<@&{TEAM_ROLE_IDS['WAS']}> Avatar - Hurt - Weak Knees - Career - OVR 69 (-33)",
      avatar["content"])

print()
if FAILS:
    print(f"test_injuries: {len(FAILS)} FAILED")
    sys.exit(1)
print("test_injuries: all pass")

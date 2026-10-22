"""The 2K injury report — routers/injuries.py.

What is pinned:

  * **One pending report per date.** A second screenshot the same day joins
    the first; the upload is gated to streamer/stats/rosters and refuses an
    unreadable format.
  * **The diff.** New, changed (status, injury or duration — not OVR) and
    healed. A report heals only when it is `complete`: a partial one must never
    clear a player it simply didn't show.
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


def upload(headers=STREAM, data=None, date="2026-10-22"):
    return c.post("/api/injuries/upload", data={"date": date},
                  files={"image": ("shot.png", data or png(), "image/png")}, headers=headers)


def row(slug, status="Hurt", injury="Strained Left Hip", duration="4-6 weeks", ovr=80, delta=-2, shown=None):
    return {"slug": slug, "status": status, "injury": injury, "duration": duration,
            "ovr": ovr, "ovr_delta": delta, "team_shown": shown}


def commit(rows, date="2026-10-22", complete=True, report_id=None, headers=STATS):
    return c.post("/api/injuries/commit", headers=headers,
                  json={"date": date, "complete": complete, "rows": rows, "report_id": report_id})


def pinged(payload):
    return set(payload.get("allowed_mentions", {}).get("roles", []))


# ── upload ────────────────────────────────────────────────────────────────────

print("upload")
check("no token cannot upload", upload(headers={}).status_code in (401, 403))
check("a member without the role cannot upload", upload(headers=PLAIN).status_code == 403)
check("an unreadable format is refused", upload(data=b"not an image").status_code == 415)
check("a bad date is refused", upload(date="10/22").status_code == 422)
r = upload()
check("a streamer uploads", r.status_code == 200, r.text)
item = r.json()["item"]
r2 = upload(headers=STATS)
check("a second screenshot joins the same date's report",
      r2.json()["item"]["id"] == item["id"] and len(r2.json()["item"]["images"]) == 2)
check("a different date is a different report", upload(date="2026-10-21").json()["item"]["id"] != item["id"])
pending = c.get("/api/injuries/pending", headers=STATS).json()
check("pending lists both dates, oldest first", [p["date"] for p in pending] == ["2026-10-21", "2026-10-22"])
check("pending needs a role", c.get("/api/injuries/pending", headers=PLAIN).status_code == 403)
older = pending[0]
r = c.delete(f"/api/injuries/pending/{older['id']}/images/shot-1.png", headers=STREAM)
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
            row("free-agent", shown="BOS")], report_id=item["id"], complete=False)
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
check("last report recorded", cur["last_report"]["id"] == item["id"] and cur["last_report"]["complete"] is False)
check("one Discord message", len(SENT) == 1, SENT)
msg = SENT[0]
check("it pings exactly the affected teams",
      pinged(msg) == {TEAM_ROLE_IDS["LAL"], TEAM_ROLE_IDS["CLE"], TEAM_ROLE_IDS["BOS"]}, pinged(msg))
check("it reads as a new injury",
      "🚑 **New**" in msg["content"] and "**LAL** · Paul Reed — Hurt · Strained Left Hip · 4-6 weeks · OVR 80 (−2)" in msg["content"],
      msg["content"])
check("no @everyone can slip through", msg["allowed_mentions"]["parse"] == [])
check("the screenshots moved to the kept folder as WebP",
      not (inj.PENDING_DIR / item["id"]).exists() and (inj.KEPT_DIR / item["id"] / "shot-1.webp").exists())
check("committing a report that's gone is a 404",
      commit([row("reed-paul")], report_id=item["id"]).status_code == 404)

# ── same report again ─────────────────────────────────────────────────────────

print("re-commit")
SENT.clear()
r = commit([row("reed-paul", shown="LAL"), row("ingram-brandon", injury="Strained Right Hip"),
            row("free-agent", shown="BOS")], complete=False)
check("nothing changed", r.json()["new"] == r.json()["changed"] == r.json()["healed"] == 0, r.json())
check("and nothing is posted", SENT == [])

# ── next day: a change, a new one, and a partial report ───────────────────────

print("partial report")
r = commit([row("reed-paul", status="Out", ovr=79, delta=-3), row("jokic-nikola", injury="Bruised Left Hip", duration="1-2 weeks")],
           date="2026-10-23", complete=False)
body = r.json()
check("one changed, one new, none healed", (body["new"], body["changed"], body["healed"]) == (1, 1, 0), body)
check("a partial report keeps players it didn't show", body["injured"] == 4)
check("the change shows the status move", "Hurt → Out" in SENT[-1]["content"], SENT[-1]["content"])
check("only the changed teams are pinged",
      pinged(SENT[-1]) == {TEAM_ROLE_IDS["LAL"], TEAM_ROLE_IDS["DEN"]}, pinged(SENT[-1]))
cur = {p["slug"]: p for p in c.get("/api/injuries").json()["players"]}
check("since survives a change", cur["reed-paul"]["since"] == "2026-10-22" and cur["reed-paul"]["updated"] == "2026-10-23")

SENT.clear()
r = commit([row("reed-paul", status="Out", ovr=81, delta=-1), row("jokic-nikola", injury="Bruised Left Hip", duration="1-2 weeks")],
           date="2026-10-23", complete=False)
check("an OVR move alone is not news", r.json()["changed"] == 0 and SENT == [], r.json())

# ── complete report heals ─────────────────────────────────────────────────────

print("complete report")
SENT.clear()
r = commit([row("reed-paul", status="Out", ovr=81, delta=-1)], date="2026-10-24", complete=True)
body = r.json()
check("a complete report heals everyone it didn't show", body["healed"] == 3 and body["injured"] == 1, body)
check("cleared players are announced", "✅ **Cleared**" in SENT[-1]["content"]
      and "Nikola Jokic — back from Bruised Left Hip" in SENT[-1]["content"], SENT[-1]["content"])
check("an older report is refused", commit([row("reed-paul")], date="2026-10-23").status_code == 409)

hist = c.get("/api/injuries/history").json()
check("history is newest first", hist[0]["date"] == "2026-10-24" and hist[-1]["date"] == "2026-10-22")
check("history has every change", len(hist) == 3 + 2 + 3, len(hist))
check("history filters by team", {e["team"] for e in c.get("/api/injuries/history?team=den").json()} == {"DEN"})
check("history filters by player", all(e["slug"] == "reed-paul"
                                       for e in c.get("/api/injuries/history?slug=reed-paul").json()))
check("history carries names", hist[0]["name"])
check("the write is in the edit log", "injuries.json" in audit.EDITS_FILE.read_text())

# ── a long report splits ──────────────────────────────────────────────────────

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
      all(all(f"**{t}**" in m["content"] for t, rid in TEAM_ROLE_IDS.items() if rid in pinged(m)) for m in msgs))
check("no changes, no messages", inj.build_messages("2026-10-25", [], {}) == [])

print()
if FAILS:
    print(f"test_injuries: {len(FAILS)} FAILED")
    sys.exit(1)
print("test_injuries: all pass")

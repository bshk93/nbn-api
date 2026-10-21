"""NB¥ after the 2026-09 reset — `routers/wallet.py` and what goes through it.

Pins:

- **The wallet.** A post is all-or-nothing, never overdraws (except a donation
  correction, which may), refuses a paused kind with a 423, and the balances
  file always equals the ledger summed.
- **Betting.** Fixed odds only; the per-bet cap; a payout can exceed the stakes
  and `GET /api/bets/house` shows by how much; deleting a bet refunds.
- **Buying a stream.** A member requests it, free; a streamer accepts (and the
  1,000 NB¥ is spent then) or declines. Upcoming games only; no daily limit;
  only a streamer refunds, in full; a bought game can't be unflagged or deleted.
- **Donations** credit 100 NB¥ a dollar, and an edit moves exactly the difference.
- **A new member** gets a real `start` line.
- **Twitch**: rates by tier, a gift pays the gifter, and an unmatched login pays nobody.
- **Paused rewards** pay nothing and don't break the action they ride on.

Writes go to a temp directory.

    venv/bin/python -m tests.test_nbyen
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.wallet as wallet  # noqa: E402
import routers.bets as bets  # noqa: E402
import routers.schedule as sched  # noqa: E402
import routers.donations as donations  # noqa: E402
import routers.misc as misc  # noqa: E402
import routers.nbyen as nbyen_r  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-nbyen-test-"))
wallet.LEDGER_FILE = TMP / "nbyen-ledger.jsonl"
wallet.BALANCES_FILE = TMP / "member-balances.json"
bets.BETS_FILE = TMP / "bets.json"
bets.TIPS_FILE = TMP / "tips.json"
donations.DONATIONS_FILE = TMP / "donations.json"
sched.DATA_DIR = TMP

T = {n: c * 64 for n, c in [("Ann", "a"), ("Ben", "b"), ("Bookie", "k"), ("Streamer", "s"),
                            ("Admin", "z"), ("Board", "d")]}
MEMBERS = {
    "Ann":      {"token": T["Ann"],      "roles": [],           "tenures": []},
    "Ben":      {"token": T["Ben"],      "roles": [],           "tenures": []},
    "Bookie":   {"token": T["Bookie"],   "roles": ["bookie"],   "tenures": []},
    "Streamer": {"token": T["Streamer"], "roles": ["streamer"], "tenures": []},
    "Admin":    {"token": T["Admin"],    "roles": ["admin"],    "tenures": []},
    "Board":    {"token": T["Board"],    "roles": ["bod"],      "tenures": []},
}
auth.load_members = lambda: MEMBERS
auth.save_members = lambda m: None
bets.load_members = auth.load_members
donations.load_members = auth.load_members
nbyen_r.load_members = auth.load_members
for mod in (bets, sched, donations, misc, auth):
    mod.log_write = lambda info, msg: None
bets._discord_post_bet = lambda embed: None

app = FastAPI()
for mod in (bets, sched, donations, misc, auth, nbyen_r):
    app.include_router(mod.router)
c = TestClient(app)


def H(name):
    return {"Authorization": "Bearer " + T[name]}


def B(name):
    return wallet.balance(name)


def consistent():
    return wallet.balances() == wallet.rebuild_balances()


def give(name, amount):
    wallet.post([{"member": name, "delta": amount, "kind": "admin", "reason": "test"}])


# ── the wallet ───────────────────────────────────────────────────────────────

print("\n-- the wallet --")

check("an unknown member has nothing — no silent starting balance", B("Ann") == 0.0)
give("Ann", 1000)
check("a credit lands", B("Ann") == 1000)
try:
    wallet.post([{"member": "Ann", "delta": 500, "kind": "admin", "reason": "x"},
                 {"member": "Ben", "delta": -1, "kind": "admin", "reason": "x"}])
    ok = False
except HTTPException as e:
    ok = e.status_code == 402
check("an overdraw anywhere in a post is a 402", ok)
check("...and nothing in that post landed", B("Ann") == 1000 and len(wallet.read_ledger()) == 1)
try:
    wallet.credit("Ann", 5, "perry", "x")
    ok = False
except HTTPException as e:
    ok = e.status_code == 423
check("a paused kind is a 423", ok)
check("try_credit on a paused kind pays nothing and doesn't raise",
      wallet.try_credit("Ann", 5, "perry", "x") == (0.0, 1000))
check("every line carries a kind", all("kind" in r for r in wallet.read_ledger()))
check("balances equal the ledger summed", consistent())

# ── betting ──────────────────────────────────────────────────────────────────

print("\n-- betting --")

r = c.post("/api/bets", json={"title": "pool", "options": [{"label": "a"}, {"label": "b"}],
                              "bet_type": "pool"}, headers=H("Bookie"))
check("a pool bet can't be created", r.status_code == 422)
r = c.post("/api/bets", json={"title": "Game 7", "options": [
    {"label": "Home", "probability": 0.25}, {"label": "Away", "probability": 0.75}]}, headers=H("Bookie"))
check("a fixed-odds bet is the default", r.status_code == 200 and r.json()["bet_type"] == "fixed_odds")
bet = r.json()
home, away = bet["options"][0]["id"], bet["options"][1]["id"]

give("Ben", 1000)
r = c.post(f"/api/bets/{bet['id']}/wager", json={"option_id": home, "amount": 150}, headers=H("Ann"))
check("a wager over the per-bet cap is refused", r.status_code == 422 and B("Ann") == 1000)
check("the cap is 100", bets.NBY_MAX_WAGER == 100)
c.post(f"/api/bets/{bet['id']}/wager", json={"option_id": home, "amount": 100}, headers=H("Ann"))
c.post(f"/api/bets/{bet['id']}/wager", json={"option_id": away, "amount": 60}, headers=H("Ben"))
check("wagers are debited", B("Ann") == 900 and B("Ben") == 940)
house = c.get("/api/bets/house").json()
check("open stakes aren't counted as the house's yet", house["net"] == 0 and house["open_stakes"] == 160)

r = c.post(f"/api/bets/{bet['id']}/close", json={"winning_option_id": home}, headers=H("Bookie"))
check("closing pays the winner at their odds", r.status_code == 200 and B("Ann") == 1300)
house = c.get("/api/bets/house").json()
check("the house shows the NB¥ the odds created (160 in, 400 out)", house["net"] == -240)

r = c.post("/api/bets", json={"title": "Refund me", "options": [
    {"label": "x", "probability": 0.5}, {"label": "y", "probability": 0.5}]}, headers=H("Bookie"))
b2 = r.json()
c.post(f"/api/bets/{b2['id']}/wager", json={"option_id": b2["options"][0]["id"], "amount": 50}, headers=H("Ben"))
r = c.delete(f"/api/bets/{b2['id']}", headers=H("Admin"))
check("deleting a bet with wagers refunds them", r.status_code == 200 and B("Ben") == 940)
check("...and the house is unchanged by it", c.get("/api/bets/house").json()["net"] == -240)
check("balances equal the ledger summed", consistent())

# ── admin adjust ─────────────────────────────────────────────────────────────

print("\n-- admin adjust --")

r = c.post("/api/bets/admin/adjust", json={"member": "Ben", "delta": 250, "reason": "Achievement: X",
                                           "kind": "achievement"}, headers=H("Admin"))
check("an achievement award is refused while achievements are paused", r.status_code == 423 and B("Ben") == 940)
r = c.post("/api/bets/admin/adjust", json={"member": "Ben", "delta": 10, "reason": ""}, headers=H("Admin"))
check("a manual adjustment needs a reason", r.status_code == 422)
r = c.post("/api/bets/admin/adjust", json={"member": "Ben", "delta": 10, "reason": "fix"}, headers=H("Admin"))
check("a manual adjustment with a reason lands as kind 'admin'",
      r.status_code == 200 and wallet.read_ledger()[-1]["kind"] == "admin")

# ── buying a stream ──────────────────────────────────────────────────────────

print("\n-- buying a stream --")

SENT = []   # (who, text): inbox notices, captured instead of written
sched.inbox.notify_member = lambda name, text, link=None: SENT.append((name, text))
sched.inbox.notify_roles = lambda roles, text, link=None: SENT.append(("+".join(roles), text))

sched.league_today_str = lambda: "2026-10-20"
(TMP / "schedule-26-27.json").write_text(json.dumps({"season": "26-27", "source": "", "games": [
    {"id": "g-past",  "date": "2026-10-19", "away_team": "BOS", "home_team": "NYK"},
    {"id": "g1",      "date": "2026-10-21", "away_team": "LAL", "home_team": "GSW"},
    {"id": "g2",      "date": "2026-10-21", "away_team": "MIA", "home_team": "ORL"},
    {"id": "g3",      "date": "2026-10-22", "away_team": "DEN", "home_team": "PHX"},
    {"id": "g4",      "date": "2026-10-22", "away_team": "DAL", "home_team": "HOU"},
    {"id": "g5",      "date": "2026-10-23", "away_team": "ATL", "home_team": "CHI"},
    {"id": "g6",      "date": "2026-10-22", "away_team": "SAC", "home_team": "POR"},
]}))
REQ = "/api/schedule/{}/stream-request?season=26-27"
ACCEPT = "/api/schedule/{}/stream-request/accept?season=26-27"
DECLINE = "/api/schedule/{}/stream-request/decline?season=26-27"
REFUND = "/api/schedule/{}/purchase?season=26-27"


def game(gid):
    return next(g for g in c.get("/api/schedule?season=26-27").json()["games"] if g["id"] == gid)


check("the price is 1,000", wallet.STREAM_PRICE == 1000)
check("a played game can't be requested", c.post(REQ.format("g-past"), headers=H("Ann")).status_code == 409)
give("Ben", 50)   # Ann 1300, Ben 1000
before = B("Ann")
r = c.post(REQ.format("g1"), headers=H("Ann"))
check("requesting a stream → 200, nothing spent", r.status_code == 200 and B("Ann") == before)
g = game("g1")
check("the game names the request, and isn't a stream yet",
      g["stream_request"] == "Ann" and not g["stream"] and g["stream_buyer"] is None)
check("the streamers are told", SENT[-1][0] == "streamer" and "Ann" in SENT[-1][1])
check("a second request on the same game → 409", c.post(REQ.format("g1"), headers=H("Ben")).status_code == 409)
r = c.post(REQ.format("g2"), headers=H("Ben"))
check("another game the same day can be requested too", r.status_code == 200)
check("a member can't accept a request", c.post(ACCEPT.format("g1"), headers=H("Ann")).status_code == 403)
check("only the requester withdraws", c.delete(REQ.format("g1"), headers=H("Ben")).status_code == 403)

r = c.post(ACCEPT.format("g1"), headers=H("Streamer"))
g = game("g1")
check("accepting spends the 1,000 then", r.status_code == 200 and B("Ann") == before - 1000)
check("...flags it, names the buyer, clears the request",
      g["stream"] and g["stream_buyer"] == "Ann" and g["stream_request"] is None)
check("...as a stream_purchase line", wallet.read_ledger()[-1]["kind"] == "stream_purchase")
check("the buyer is told", SENT[-1][0] == "Ann" and "accepted" in SENT[-1][1])
check("...and the day's other request still waits", game("g2")["stream_request"] == "Ben")
r = c.post(ACCEPT.format("g2"), headers=H("Streamer"))
check("a second bought stream the same day is fine", r.status_code == 200
      and game("g2")["stream_buyer"] == "Ben" and B("Ben") == 0)
c.delete(REFUND.format("g2"), headers=H("Streamer"))

check("not enough NB¥ to request → 402", c.post(REQ.format("g3"), headers=H("Ann")).status_code == 402)
c.post(REQ.format("g5"), headers=H("Ben"))
c.delete(REQ.format("g5"), headers=H("Ben"))
check("the requester can withdraw, free", game("g5")["stream_request"] is None and B("Ben") == 1000)
c.post(REQ.format("g5"), headers=H("Ben"))
r = c.post(DECLINE.format("g5"), headers=H("Streamer"))
check("a streamer declines, free, and the member is told", r.status_code == 200
      and game("g5")["stream_request"] is None and B("Ben") == 1000
      and SENT[-1][0] == "Ben" and "declined" in SENT[-1][1])

c.post(REQ.format("g5"), headers=H("Ben"))
wallet.debit("Ben", 500, "admin", "test")
r = c.post(ACCEPT.format("g5"), headers=H("Streamer"))
check("accepting when the member can no longer pay → 402, nothing changes",
      r.status_code == 402 and game("g5")["stream_request"] == "Ben" and not game("g5")["stream"])
give("Ben", 500)
check("...and goes through once they can", c.post(ACCEPT.format("g5"), headers=H("Streamer")).status_code == 200
      and B("Ben") == 0)

c.post("/api/schedule/g3/stream?season=26-27", headers=H("Streamer"))
c.post("/api/schedule/g4/stream?season=26-27", headers=H("Streamer"))
check("two free streams on one day are still fine", game("g3")["stream"] and game("g4")["stream"])
r = c.patch("/api/schedule/g5", json={"date": "2026-10-21", "season": "26-27"}, headers=H("Board"))
check("a bought game can move onto another bought day", r.status_code == 200)
r = c.delete("/api/schedule/g1/stream?season=26-27", headers=H("Streamer"))
check("a bought stream can't just be unflagged", r.status_code == 409 and game("g1")["stream"])
r = c.delete("/api/schedule/g1?season=26-27", headers=H("Board"))
check("a bought game can't be deleted", r.status_code == 409)

r = c.delete(REFUND.format("g1"), headers=H("Ann"))
check("the buyer can't refund it themselves", r.status_code == 403)
r = c.delete(REFUND.format("g1"), headers=H("Streamer"))
check("a streamer refunds it in full", r.status_code == 200 and B("Ann") == before)
g = game("g1")
check("...and the game is clear again", not g["stream"] and g["stream_buyer"] is None)
check("...and can be requested again", c.post(REQ.format("g1"), headers=H("Ann")).status_code == 200)
give("Ben", 1000)
c.post(REQ.format("g6"), headers=H("Ben"))
r = c.post(ACCEPT.format("g6"), headers=H("Streamer"))
check("a game can be bought on a day a streamer already flagged", r.status_code == 200 and game("g4")["stream"])
check("balances equal the ledger summed", consistent())

# ── donations ────────────────────────────────────────────────────────────────

print("\n-- donations --")

before = B("Ben")
r = c.post("/api/donations", json={"team": "ATL", "member": "Ben", "amount": 20, "date": "2026-10-01"},
           headers=H("Board"))
did = r.json()["id"]
check("a $20 donation credits 2,000", r.status_code == 201 and B("Ben") == before + 2000)
check("...as a donation line carrying the donation's id",
      wallet.read_ledger()[-1]["kind"] == "donation" and wallet.read_ledger()[-1]["ref"] == f"donation:{did}")
r = c.put(f"/api/donations/{did}", json={"team": "ATL", "member": "Ben", "amount": 25, "date": "2026-10-01"},
          headers=H("Board"))
check("raising it to $25 credits the 500 difference", B("Ben") == before + 2500)
ben_mid, ann_before = B("Ben"), B("Ann")
c.put(f"/api/donations/{did}", json={"team": "ATL", "member": "Ann", "amount": 25, "date": "2026-10-01"},
      headers=H("Board"))
check("moving it to another member moves the NB¥", B("Ben") == ben_mid - 2500 and B("Ann") == ann_before + 2500)
r = c.post("/api/donations", json={"team": "ATL", "member": "Some Stranger", "amount": 5, "date": "2026-10-01"},
           headers=H("Board"))
check("a non-member's donation is still recorded, and credits nobody",
      r.status_code == 201 and "Some Stranger" not in wallet.balances())

# ── a new member ─────────────────────────────────────────────────────────────

print("\n-- a new member --")

r = c.post("/api/members", json={"name": "Newbie", "roles": [], "tenures": []}, headers=H("Admin"))
check("a new member starts with 1,000 as a real start line",
      r.status_code in (200, 201) and B("Newbie") == 1000
      and wallet.read_ledger()[-1]["kind"] == "start")

# ── paused rewards ───────────────────────────────────────────────────────────

print("\n-- paused rewards --")

before = B("Ann")
check("a bio reward pays nothing while paused", bets._award_bio_reward("Ann", 10) == (0.0, before))
check("a box score reward pays 50", bets._award_submission_reward("Ann") == (50.0, before + 50))
before = B("Ann")
r = c.post("/api/trivia/answer", json={"streak": 10}, headers=H("Ann"))
check("trivia pays nothing while paused", r.status_code == 200 and r.json()["reward"] == 0.0 and B("Ann") == before)

# ── Twitch ───────────────────────────────────────────────────────────────────

print("\n-- Twitch --")

import credit_twitch_subs as tw  # noqa: E402

subs = [
    {"user_id": "1", "user_login": "AnnTV",  "tier": "1000", "is_gift": False},
    {"user_id": "2", "user_login": "bentv",  "tier": "3000", "is_gift": False},
    {"user_id": "3", "user_login": "friend", "tier": "2000", "is_gift": True, "gifter_login": "anntv"},
    {"user_id": "4", "user_login": "lurker", "tier": "1000", "is_gift": False},
]
people = {"Ann": {"twitch": "anntv"}, "Ben": {"twitch": "BenTV"}}
lines, unmatched = tw.plan(subs, people, "2026-10")
pay = {(l["member"], l["delta"]) for l in lines}
check("tier 1 pays 300, tier 3 pays 1,700", ("Ann", 300) in pay and ("Ben", 1700) in pay)
check("a gift pays the gifter at the gift's tier", ("Ann", 700) in pay)
check("logins match case-insensitively, and nobody else is paid", len(lines) == 3)
check("an unmatched subscriber is reported, not paid", len(unmatched) == 1 and "lurker" in unmatched[0])
check("refs are per month and per subscriber", {l["ref"] for l in lines} ==
      {"twitch:2026-10:1", "twitch:2026-10:2", "twitch:2026-10:3"})

# ── the public read ──────────────────────────────────────────────────────────

print("\n-- the public read --")

summ = c.get("/api/nbyen/summary").json()
check("the summary total equals the balances", summ["total"] == round(sum(wallet.balances().values()), 2))
check("by_kind sums to the total", round(sum(summ["by_kind"].values()), 2) == summ["total"])
check("paused kinds are listed", "tip" in summ["paused"] and "wager" not in summ["paused"])
led = c.get("/api/nbyen/ledger?member=Ann").json()
check("a member's history is theirs only, newest first",
      all(e["member"] == "Ann" for e in led["entries"]) and led["entries"][0]["ts"] >= led["entries"][-1]["ts"])
check("filter by kind", all(e["kind"] == "donation" for e in c.get("/api/nbyen/ledger?kind=donation").json()["entries"]))
check("an unknown kind is a 422", c.get("/api/nbyen/ledger?kind=nope").status_code == 422)

print("\n" + ("=" * 40))
if FAILS:
    print(f"FAILED: {FAILS}")
    sys.exit(1)
print("test_nbyen: ALL PASS")

"""Futures markets — `routers/markets.py`.

Pins:

- **Prices.** They open where the bookie set them (uniform when unset, never
  under the 1% floor), always sum to 100, and a buy moves its outcome up and
  every other one down.
- **Round trips are free except the fee.** Buying and selling the same shares
  back returns the cost, so churn can't mint.
- **The money supply moves by exactly the formula.** At settlement the NB¥
  created equals b × ln(winner's closing price ÷ opening price), less the fees
  burned — checked against the wallet, not the market's own bookkeeping.
- **Yes and No.** A No pushes its outcome's price down, pays when anything
  else wins, and round-trips like a Yes.
- **Nobody buys No on their own team.** Bets on any other team are
  unlimited, someone who joins a team can still sell out of a bet against it,
  and admin isn't every team.
- **Every market has a close time.**
- **Shares round down**, so rounding never pays the buyer.
- **Limits.** No selling what you don't hold; no stake cap unless a market sets
  one, and then it's net of sales and includes the fee; a moved price refuses a trade with `min_shares`;
  a locked, closed or settled market doesn't trade; an overdraw leaves the
  market untouched.
- **Endings.** Settle pays 100 a winning share; void refunds net spend.

    venv/bin/python -m tests.test_markets
"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.wallet as wallet  # noqa: E402
import routers.markets as mk  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="nbn-markets-test-"))
wallet.LEDGER_FILE = TMP / "nbyen-ledger.jsonl"
wallet.BALANCES_FILE = TMP / "member-balances.json"
mk.MARKETS_FILE = TMP / "markets.json"

T = {n: c * 64 for n, c in [("Ann", "a"), ("Ben", "b"), ("Cat", "c"), ("Bookie", "k"),
                            ("Gm", "g"), ("Coach", "h"), ("Admin", "z")]}
ROLES = {"Bookie": ["bookie"], "Gm": ["bos"], "Admin": ["admin"]}
MEMBERS = {n: {"token": T[n], "roles": ROLES.get(n, []), "tenures": []} for n in T}
MEMBERS["Coach"]["tenures"] = [{"team": "CHI", "position": "coach", "start": "2020-01-01", "end": None}]
auth.load_members = lambda: MEMBERS
auth.save_members = lambda m: None
mk.load_members = auth.load_members
mk.log_write = lambda info, msg: None
mk._discord_post_bet = lambda embed: None

app = FastAPI()
app.include_router(mk.router)
c = TestClient(app)


def H(name):
    return {"Authorization": "Bearer " + T[name]}


def B(name):
    return wallet.balance(name)


def circulation():
    return round(sum(wallet.balances().values()), 2)


def give(name, amount):
    wallet.post([{"member": name, "delta": amount, "kind": "admin", "reason": "test"}])


def near(a, b, tol=0.02):
    return abs(a - b) <= tol


def prices(m):
    return [o["price"] for o in m["outcomes"]]


def new_market(**kw):
    body = {"title": kw.pop("title", "Who wins?"), "closes_at": kw.pop("closes_at", "2099-01-01T00:00:00"),
            "outcomes": kw.pop("outcomes", [{"label": x} for x in ("ATL", "BOS", "CHI")]), **kw}
    r = c.post("/api/markets", json=body, headers=H("Bookie"))
    assert r.status_code == 200, r.text
    return r.json()


for n in ("Ann", "Ben", "Cat"):
    give(n, 2000)

# ── prices ───────────────────────────────────────────────────────────────────

print("\n-- prices --")

r = c.post("/api/markets", json={"title": "x", "outcomes": [{"label": "a"}, {"label": "b"}],
                                 "closes_at": "2099-01-01T00:00:00"}, headers=H("Ann"))
check("only a bookie opens a market", r.status_code == 403)
r = c.post("/api/markets", json={"title": "x", "outcomes": [{"label": "a"}, {"label": "b"}]}, headers=H("Bookie"))
check("a market needs a close time", r.status_code == 422)

m = new_market()
check("no opening prices means uniform", all(near(p, 100 / 3) for p in prices(m)))

p = mk._normalize_open([90, 9.9, 0.05, 0.05])
check("the floor lifts a long shot to 1%", near(min(p), 0.01, 1e-9))
check("...and prices still sum to 1", near(sum(p), 1, 1e-9))
check("...and the favourite keeps its lead", p[0] > p[1] > p[2])

m2 = new_market(title="seeded", outcomes=[{"label": "fav", "open_price": 60},
                                          {"label": "mid", "open_price": 30},
                                          {"label": "dog", "open_price": 10}])
check("seeded prices open where they were set", near(prices(m2)[0], 60) and near(prices(m2)[2], 10))
pv = c.post("/api/markets/preview", json={"open_prices": [60, 30, 10]}).json()
check("the form's preview gives the prices create will open at",
      [round(x) for x in pv["prices"]] == [round(x) for x in prices(m2)] and near(pv["max_mint"], m2["max_mint"], 0.05))
check("the preview applies the floor too",
      near(min(c.post("/api/markets/preview", json={"open_prices": [99, 0.5, 0.5]}).json()["prices"]), 1.0))
# Power-rankings seed: the latest published edition wins, weights follow avg rank.
def _ed(eid, when, avgs, status="published"):
    return {"id": eid, "type": "power_rankings", "status": status, "published_at": when, "title": eid,
            "final": [{"team": t, "avg": a, "rank": i + 1} for i, (t, a) in enumerate(avgs)]}
mk.load_articles = lambda: [
    _ed("old", "2026-02-10", [("UTA", 1.0), ("ORL", 5.0)]),
    _ed("new", "2026-09-16", [("ORL", 1.67), ("PHX", 2.5), ("MIL", 7.83), ("UTA", 29.83)]),
    _ed("draft", "2026-09-20", [("UTA", 1.0)], status="draft"),
]
sd = c.get("/api/markets/seeds/power-rankings").json()
check("the seed uses the latest published edition, not a draft", sd["source"]["id"] == "new")
w = {t: v["weight"] for t, v in sd["teams"].items()}
check("...a better average rank gets a bigger weight", w["ORL"] > w["PHX"] > w["MIL"] > w["UTA"])
check("...and the gap follows the average, not the rank",
      near(w["ORL"] / w["PHX"], math.exp((2.5 - 1.67) / mk.SEED_SPREAD), 0.05))
check("...and no weight is zero, so the market can open from it", min(w.values()) >= 0.1)
mk.load_articles = lambda: []
check("no published rankings is a 404", c.get("/api/markets/seeds/power-rankings").status_code == 404)
check("max_mint is b × ln(1 ÷ lowest opening price)", near(m2["max_mint"], 1500 * math.log(10), 0.05))

a_id = m["outcomes"][0]["id"]
r = c.post(f"/api/markets/{m['id']}/quote", json={"outcome_id": a_id, "side": "buy", "spend": 100})
q = r.json()
check("a quote doesn't trade", c.get(f"/api/markets/{m['id']}").json()["trade_count"] == 0)
before_ann = B("Ann")
m_before_q = next(x for x in mk._load() if x["id"] == m["id"])["q"]
r = c.post(f"/api/markets/{m['id']}/buy", json={"outcome_id": a_id, "spend": 100}, headers=H("Ann"))
check("a buy succeeds", r.status_code == 200)
m = r.json()["market"]
check("the buy got the quoted shares", near(r.json()["trade"]["shares"], q["shares"], 1e-4))
exact = mk._buy_shares_for(m_before_q, 1500, 0, 100)
check("shares round down, never up", r.json()["trade"]["shares"] <= exact)
check("the member paid spend + fee", near(before_ann - B("Ann"), 102))
check("the outcome's price went up", prices(m)[0] > 100 / 3)
check("...every other one went down", all(p < 100 / 3 for p in prices(m)[1:]))
check("...and they still sum to 100", near(sum(prices(m)), 100))
check("Ann holds the shares", near(m["positions"]["Ann"][a_id]["yes"], q["shares"], 1e-4))
check("no per-member cap unless the bookie sets one", m["max_stake"] is None)

# ── round trips ──────────────────────────────────────────────────────────────

print("\n-- round trips --")

free = new_market(title="no fee", fee=0)
fid = free["outcomes"][1]["id"]
b0 = B("Ben")
t = c.post(f"/api/markets/{free['id']}/buy", json={"outcome_id": fid, "spend": 250}, headers=H("Ben")).json()
c.post(f"/api/markets/{free['id']}/sell", json={"outcome_id": fid, "shares": t["trade"]["shares"]},
       headers=H("Ben"))
check("with no fee, buying and selling back costs at most a cent or two", near(B("Ben"), b0, 0.02))
free = c.get(f"/api/markets/{free['id']}").json()
check("...and the prices return to where they opened", all(near(p, 100 / 3) for p in prices(free)))

# ── yes and no ───────────────────────────────────────────────────────────────

print("\n-- yes and no --")

yn = new_market(title="yes/no", fee=0,
                outcomes=[{"label": "Boston", "team": "BOS"}, {"label": "Chicago", "team": "CHI"},
                          {"label": "Utah", "team": "UTA"}])
ynid = yn["id"]
bos, chi, uta = [o["id"] for o in yn["outcomes"]]
q = c.post(f"/api/markets/{ynid}/quote", json={"outcome_id": bos, "side": "buy", "contract": "no", "spend": 100}).json()
check("a No quote reports Boston's own price going down", q["price_after"] < q["price_before"])
b0 = B("Ben")
r = c.post(f"/api/markets/{ynid}/buy", json={"outcome_id": bos, "contract": "no", "spend": 100}, headers=H("Ben"))
yn = r.json()["market"]
check("buying No on Boston lowers Boston's price", prices(yn)[0] < 100 / 3)
check("...and raises everyone else's", all(p > 100 / 3 for p in prices(yn)[1:]))
check("...and Ben holds No shares", near(yn["positions"]["Ben"][bos]["no"], q["shares"], 1e-4))
c.post(f"/api/markets/{ynid}/sell", json={"outcome_id": bos, "contract": "no", "shares": q["shares"]}, headers=H("Ben"))
check("a No round-trips like a Yes", near(B("Ben"), b0, 0.02))

print("\n-- your own team --")

r = c.post(f"/api/markets/{ynid}/buy", json={"outcome_id": bos, "contract": "no", "spend": 20}, headers=H("Gm"))
check("a BOS member can't buy No on Boston", r.status_code == 422)
give("Gm", 5000); give("Coach", 2000); give("Admin", 2000)
r = c.post(f"/api/markets/{ynid}/buy", json={"outcome_id": bos, "contract": "no", "spend": 5}, headers=H("Gm"))
check("...not even a small one", r.status_code == 422 and "own team" in r.json()["detail"])
r = c.post(f"/api/markets/{ynid}/buy", json={"outcome_id": chi, "contract": "no", "spend": 20}, headers=H("Coach"))
check("a current tenure counts as your team too (CHI coach, No on Chicago)", r.status_code == 422)
r = c.post(f"/api/markets/{ynid}/buy", json={"outcome_id": bos, "contract": "no", "spend": 20}, headers=H("Admin"))
check("admin isn't every team", r.status_code == 200)

# Any other team, any amount — even every other team.
teams30 = ["BOS"] + sorted(t for t in mk.VALID_TEAMS if t != "BOS")
big = new_market(title="thirty", outcomes=[{"label": t, "team": t, "open_price": 25 if t == "BOS" else 2.5}
                                           for t in teams30])
bid = big["id"]
oid = {o["team"]: o["id"] for o in big["outcomes"]}
r = c.post(f"/api/markets/{bid}/buy", json={"outcome_id": oid["CHI"], "spend": 1500}, headers=H("Gm"))
check("a GM can put a big bet on another team", r.status_code == 200)
ok = all(c.post(f"/api/markets/{bid}/buy", json={"outcome_id": oid[t], "spend": 20},
                headers=H("Gm")).status_code == 200 for t in teams30[2:])
check("...or on every other team (public in the trade log; a conduct question, not a code one)", ok)

# Joining a team while holding a bet against it: you can still sell out.
stuck = new_market(title="stuck", outcomes=[{"label": t, "team": t} for t in ("BOS", "CHI", "UTA")])
sid = stuck["id"]
sbos = stuck["outcomes"][0]["id"]
t = c.post(f"/api/markets/{sid}/buy", json={"outcome_id": sbos, "contract": "no", "spend": 300}, headers=H("Cat")).json()
MEMBERS["Cat"]["roles"] = ["bos"]
r = c.post(f"/api/markets/{sid}/sell", json={"outcome_id": sbos, "contract": "no", "shares": t["trade"]["shares"] / 2},
           headers=H("Cat"))
check("someone who joins a team can sell part of a bet against it", r.status_code == 200)
r = c.post(f"/api/markets/{sid}/buy", json={"outcome_id": sbos, "contract": "no", "spend": 20}, headers=H("Cat"))
check("...but can't add to it", r.status_code == 422)
MEMBERS["Cat"]["roles"] = []

# ── limits ───────────────────────────────────────────────────────────────────

print("\n-- limits --")

r = c.post(f"/api/markets/{m['id']}/sell", json={"outcome_id": m["outcomes"][1]["id"], "shares": 1},
           headers=H("Ann"))
check("no selling an outcome you don't hold (no shorting)", r.status_code == 422)
capped = new_market(title="capped", max_stake=150)
cid = capped["outcomes"][0]["id"]
c.post(f"/api/markets/{capped['id']}/buy", json={"outcome_id": cid, "spend": 100}, headers=H("Ann"))
r = c.post(f"/api/markets/{capped['id']}/buy", json={"outcome_id": cid, "spend": 49}, headers=H("Ann"))
check("a market's stake cap counts what's already in, fee included", r.status_code == 422)
r = c.post(f"/api/markets/{m['id']}/buy", json={"outcome_id": a_id, "spend": 50, "min_shares": 10_000},
           headers=H("Ann"))
check("a price that moved past min_shares refuses the buy", r.status_code == 409)

# Buying by share count — what the page sends.
mq = next(x for x in mk._load() if x["id"] == m["id"])["q"]
q = c.post(f"/api/markets/{m['id']}/quote", json={"outcome_id": a_id, "side": "buy", "shares": 10}).json()
check("a share-count quote gives exactly those shares", near(q["shares"], 10, 1e-9))
exact = mk._buy_cost(mq, 1500, 0, 10)
check("...priced at the LMSR cost, rounded up to the cent", q["cost"] >= exact and q["cost"] - exact < 0.01)
check("...which buying that NB¥ back gets the same shares", near(mk._buy_shares_for(mq, 1500, 0, exact), 10, 1e-6))
b0 = B("Ann")
r = c.post(f"/api/markets/{m['id']}/buy", json={"outcome_id": a_id, "shares": 10, "max_total": q["total"]},
           headers=H("Ann"))
check("a share-count buy succeeds", r.status_code == 200 and near(r.json()["trade"]["shares"], 10, 1e-9))
check("...and costs what it was quoted", near(b0 - B("Ann"), q["total"]))
m = r.json()["market"]
r = c.post(f"/api/markets/{m['id']}/buy", json={"outcome_id": a_id, "shares": 10, "max_total": 1},
           headers=H("Ann"))
check("a price that moved past max_total refuses the buy", r.status_code == 409)
r = c.post(f"/api/markets/{m['id']}/quote", json={"outcome_id": a_id, "side": "buy", "shares": 1, "spend": 10})
check("a buy with both spend and shares is refused", r.status_code == 422)
r = c.post(f"/api/markets/{m['id']}/quote", json={"outcome_id": a_id, "side": "buy"})
check("...and with neither", r.status_code == 422)
r = c.post(f"/api/markets/{m['id']}/quote", json={"outcome_id": a_id, "side": "buy", "shares": 0.001})
check("a share-count buy under the NB¥1 minimum is refused", r.status_code == 422)

poor = "Cat"
wallet.post([{"member": poor, "delta": -B(poor) + 5, "kind": "admin", "reason": "test"}])
n_trades = c.get(f"/api/markets/{m['id']}").json()["trade_count"]
r = c.post(f"/api/markets/{m['id']}/buy", json={"outcome_id": a_id, "spend": 100}, headers=H(poor))
check("an overdraw is a 402", r.status_code == 402)
check("...and the market didn't move", c.get(f"/api/markets/{m['id']}").json()["trade_count"] == n_trades)
give(poor, 2000)

c.post(f"/api/markets/{m['id']}/lock", headers=H("Bookie"))
r = c.post(f"/api/markets/{m['id']}/buy", json={"outcome_id": a_id, "spend": 10}, headers=H("Ben"))
check("a locked market doesn't trade", r.status_code == 409)
c.post(f"/api/markets/{m['id']}/unlock", headers=H("Bookie"))

past = new_market(title="closes soon", closes_at="2099-01-01T00:00:00")
check("closes_at is stored in UTC", past["closes_at"].endswith("+00:00"))
mk_all = mk._load()
next(x for x in mk_all if x["id"] == past["id"])["closes_at"] = "2000-01-01T00:00:00+00:00"
mk._save(mk_all)
r = c.post(f"/api/markets/{past['id']}/buy", json={"outcome_id": past["outcomes"][0]["id"], "spend": 10},
           headers=H("Ben"))
check("a market past closes_at doesn't trade", r.status_code == 409)

# ── the money supply ─────────────────────────────────────────────────────────

print("\n-- the money supply --")

g = new_market(title="supply", outcomes=[{"label": x} for x in "ABCDE"])
gid = g["id"]
ids = [o["id"] for o in g["outcomes"]]
open_price = g["outcomes"][0]["price"] / 100
start = circulation()
c.post(f"/api/markets/{gid}/buy", json={"outcome_id": ids[0], "spend": 300}, headers=H("Ann"))
c.post(f"/api/markets/{gid}/buy", json={"outcome_id": ids[1], "spend": 200}, headers=H("Ben"))
t = c.post(f"/api/markets/{gid}/buy", json={"outcome_id": ids[0], "spend": 150}, headers=H("Cat")).json()
c.post(f"/api/markets/{gid}/buy", json={"outcome_id": ids[2], "contract": "no", "spend": 120}, headers=H("Ben"))
c.post(f"/api/markets/{gid}/buy", json={"outcome_id": ids[0], "contract": "no", "spend": 60}, headers=H("Ann"))
c.post(f"/api/markets/{gid}/sell", json={"outcome_id": ids[0], "shares": t["trade"]["shares"] / 2},
       headers=H("Cat"))
g = c.get(f"/api/markets/{gid}").json()
close_price = g["outcomes"][0]["price"] / 100
fees = g["house"]["fees_burned"]
ann_shares = g["positions"]["Ann"][ids[0]]["yes"]
ben_no = g["positions"]["Ben"][ids[2]]["no"]
ann_before, ben_before = B("Ann"), B("Ben")
r = c.post(f"/api/markets/{gid}/settle", json={"winner": ids[0]}, headers=H("Bookie"))
check("settle succeeds", r.status_code == 200)
check("a winning Yes pays 100, a No on the winner pays nothing",
      near(B("Ann") - ann_before, ann_shares * 100, 0.02))
check("a No on a loser pays 100", near(B("Ben") - ben_before, ben_no * 100, 0.02))
created = circulation() - start
expected = 1500 * math.log(close_price / open_price) - fees
check(f"NB¥ created ({created:.2f}) = b·ln(close/open) − fees ({expected:.2f})", near(created, expected, 1.0))
check("...and the market's own house figure agrees", near(-r.json()["house"]["net"], created, 0.05))
r = c.post(f"/api/markets/{gid}/buy", json={"outcome_id": ids[0], "spend": 10}, headers=H("Ben"))
check("a settled market doesn't trade", r.status_code == 409)
check("house totals sum every market", "net" in c.get("/api/markets/house").json())

# ── void ─────────────────────────────────────────────────────────────────────

print("\n-- void --")

v = new_market(title="void me")
vid = v["id"]
b0 = B("Ben")
c.post(f"/api/markets/{vid}/buy", json={"outcome_id": v["outcomes"][2]["id"], "spend": 80}, headers=H("Ben"))
r = c.post(f"/api/markets/{vid}/void", headers=H("Bookie"))
check("void refunds net spend, fee included", r.status_code == 200 and near(B("Ben"), b0))

check("balances equal the ledger summed", wallet.balances() == wallet.rebuild_balances())
check("every market line has a market_* kind",
      all(x["kind"].startswith("market_") for x in wallet.read_ledger() if x.get("ref", "").startswith("market:")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all passed")

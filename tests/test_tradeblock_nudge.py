"""The weekly "still on the block?" nudge and its Remove / Still available buttons.

- **`added_at` belongs to the listing.** The server stamps it when a player
  goes on the block and keeps it through every later save, whole-block or
  one-player; the caller can't set it. Taken off and put back is a new listing.
- **There is no permanent keep.** "Still available" snoozes a player two weeks,
  then he's back in the nudge. The snooze is server-only and survives saves;
  Remove takes him off the block. Either is team-wide: one member's click
  reads as done on every teammate's copy, and closes the other button.
- **The sweep nudges once a week per team**, only about players listed 7+ days
  and not snoozed, and stamps a date on entries from before the field existed
  instead of nudging about them at once.
- **A button checks the clicker's permission itself** — receiving the message
  grants nothing — and refuses an unknown kind.

Every read and write goes through patched loaders, so nothing touches the live
data directory.

    venv/bin/python -m tests.test_tradeblock_nudge
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
import routers.inbox as ib  # noqa: E402
import routers.players as pl  # noqa: E402
import routers.roster_picks as rp  # noqa: E402
import routers.tradeblock_nudge as N  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def raises(name, status, fn):
    try:
        fn()
    except HTTPException as e:
        check(f"{name} → {status}", e.status_code == status, e.detail)
        return
    check(f"{name} → {status}", False)


BIOS = {"curry-stephen": {"name": "CURRY, STEPHEN"}, "green-draymond": {"name": "GREEN, DRAYMOND"},
        "kuminga-jonathan": {"name": "KUMINGA, JONATHAN"}}
BLOCK: dict = {}
INBOX: dict = {}
MEMBERS = {"ann": {"roles": ["gsw"]}, "bo": {"roles": ["gsw"]}, "cy": {"roles": ["bos"]}}
TODAY = ["2026-10-01"]

rp.load_trading_block = lambda: {t: rp._normalize_team_block(v) for t, v in BLOCK.items()}
rp.save_trading_block = lambda data: BLOCK.update(data)
rp.log_write = lambda *a, **k: None
rp.read_csv = lambda path: (["SLUG"], [{"SLUG": s} for s in BIOS])
rp.league_today_str = lambda: TODAY[0]
rp.league_today = lambda: date.fromisoformat(TODAY[0])
pl.load_player_bios = lambda: BIOS
ib._load_store = lambda: INBOX
ib._save_store = lambda store: None
ib.log_write = lambda *a, **k: None
ib.load_members = lambda: MEMBERS
N.TRADEBLOCK_NUDGES_FILE = Path(tempfile.mkdtemp()) / "nudges.json"
# _block_player_name checks the roster file exists before reading it.
rp.DATA_DIR = Path(tempfile.mkdtemp())
(rp.DATA_DIR / "gsw-roster.csv").write_text("SLUG\n")

ANN = {"name": "ann", "roles": ["gsw"]}
BO = {"name": "bo", "roles": ["gsw"]}
CY = {"name": "cy", "roles": ["bos"]}


def gsw():
    return {e["player"]: e for e in BLOCK["GSW"]["players"]}


def put_block(*names):
    rp.put_trading_block("GSW", rp.TeamTradeBlock(players=[rp.TradingBlockEntry(player=n, notes="") for n in names]), ANN)


def sweep(day):
    return N.sweep_tradeblock_nudges(today=date.fromisoformat(day))


print("\nadded_at is stamped once and survives later saves")
BLOCK.clear()
TODAY[0] = "2026-10-01"
put_block("Stephen Curry")
check("new player stamped today", gsw()["Stephen Curry"]["added_at"] == "2026-10-01")
check("not snoozed", "snoozed_until" not in gsw()["Stephen Curry"])
TODAY[0] = "2026-10-05"
put_block("Stephen Curry", "Draymond Green")
check("whole-block save keeps the old date", gsw()["Stephen Curry"]["added_at"] == "2026-10-01")
check("player added later gets his own date", gsw()["Draymond Green"]["added_at"] == "2026-10-05")
rp.put_trading_block_player("GSW", "curry-stephen", rp.TradingBlockPlayerIn(notes="re-noted"), ANN)
check("one-player re-note keeps the date", gsw()["Stephen Curry"]["added_at"] == "2026-10-01")
rp.put_trading_block_player("GSW", "kuminga-jonathan", rp.TradingBlockPlayerIn(notes=""), ANN)
check("one-player add stamps today", gsw()["Jonathan Kuminga"]["added_at"] == "2026-10-05")
put_block("Draymond Green", "Jonathan Kuminga")
TODAY[0] = "2026-10-06"
put_block("Draymond Green", "Jonathan Kuminga", "Stephen Curry")
check("taken off and put back is a new listing", gsw()["Stephen Curry"]["added_at"] == "2026-10-06")

print("\nthe caller can't set server fields")
rp.put_trading_block("GSW", rp.TeamTradeBlock.model_validate({"players": [
    {"player": "Draymond Green", "notes": "", "added_at": "2020-01-01", "snoozed_until": "2099-01-01"}]}), ANN)
check("added_at from the caller ignored", gsw()["Draymond Green"]["added_at"] == "2026-10-05")
check("snoozed_until from the caller ignored", "snoozed_until" not in gsw()["Draymond Green"])

print("\nthe sweep")
BLOCK.clear()
INBOX.clear()
BLOCK["GSW"] = {"players": [
    {"player": "Stephen Curry", "notes": "", "added_at": "2026-09-01"},
    {"player": "Draymond Green", "notes": "", "added_at": "2026-09-08"},
    {"player": "Jonathan Kuminga", "notes": "", "added_at": "2026-10-10"},
], "picks": []}
BLOCK["BOS"] = {"players": [{"player": "Old Listing", "notes": ""}], "picks": []}
done = sweep("2026-10-12")
check("only GSW nudged", [d["team"] for d in done["nudged"]] == ["GSW"], done)
check("only stale players named, longest first",
      done["nudged"][0]["players"] == ["Stephen Curry", "Draymond Green"], done)
check("legacy entry stamped, not nudged", BLOCK["BOS"]["players"][0]["added_at"] == "2026-10-12")
check("each GSW member got it, BOS didn't", sorted(INBOX) == ["ann", "bo"], sorted(INBOX))
msg = INBOX["ann"][0]
check("message names the ages", "Stephen Curry (5 weeks), Draymond Green (4 weeks)" in msg["text"], msg["text"])
check("links to the block", msg["link"] == "/fo/tradeblock/")
check("remove + still available per player, grouped by player",
      [(a["kind"], a["label"], a["group"]) for a in msg["actions"]] == [
          ("tradeblock_remove", "Remove Stephen Curry", "Stephen Curry"),
          ("tradeblock_snooze", "Stephen Curry still available", "Stephen Curry"),
          ("tradeblock_remove", "Remove Draymond Green", "Draymond Green"),
          ("tradeblock_snooze", "Draymond Green still available", "Draymond Green")], msg["actions"])
check("each button has a short label for the grouped row",
      [a["short"] for a in msg["actions"]] == ["Remove", "Still available"] * 2, msg["actions"])
check("no permanent keep offered", not any("until traded" in a["label"] for a in msg["actions"]))
check("copies share a batch", msg["batch"] and msg["batch"] == INBOX["bo"][0]["batch"])
check("a second run inside the week sends nothing", sweep("2026-10-18")["nudged"] == [])
check("dry run writes nothing",
      N.sweep_tradeblock_nudges(today=date(2026, 11, 1), dry_run=True)["nudged"] and len(INBOX["ann"]) == 1)

print("\nthe buttons")
TODAY[0] = "2026-10-13"
item = INBOX["ann"][0]
INBOX["cy"] = [dict(item, actions=[dict(a) for a in item["actions"]])]
raises("a BOS member can't press GSW's button", 403, lambda: ib.run_action(item["id"], 0, CY))
check("…and nothing changed", "Stephen Curry" in gsw())
raises("no such action index", 404, lambda: ib.run_action(item["id"], 9, ANN))

res = ib.run_action(item["id"], 1, ANN)
check("still available snoozes two weeks", gsw()["Stephen Curry"]["snoozed_until"] == "2026-10-27")
check("done label says when", res["action"]["done_label"] == "Stephen Curry still available — next check Oct 27",
      res["action"].get("done_label"))
check("the clicker's copy is read", INBOX["ann"][0]["read"])
check("teammate's copy reads done, stays unread",
      INBOX["bo"][0]["actions"][1].get("done_by") == "ann" and INBOX["bo"][0]["read"] is False)
raises("the other button for him is closed", 409, lambda: ib.run_action(INBOX["bo"][0]["id"], 0, BO))
check("…so he's still listed", "Stephen Curry" in gsw())
put_block("Stephen Curry", "Draymond Green", "Jonathan Kuminga")
check("snooze survives an editor save", gsw()["Stephen Curry"]["snoozed_until"] == "2026-10-27")

res = ib.run_action(INBOX["bo"][0]["id"], 2, BO)
check("remove takes him off the block", "Draymond Green" not in gsw(), res)
check("a second press is a no-op", ib.run_action(item["id"], 2, ANN)["action"]["done_by"] == "bo")

done = sweep("2026-10-19")
gsw_named = [p for d in done["nudged"] if d["team"] == "GSW" for p in d["players"]]
check("next week: snoozed and removed players aren't named, the new stale one is",
      gsw_named == ["Jonathan Kuminga"], done)
done = sweep("2026-10-27")
gsw_named = [p for d in done["nudged"] if d["team"] == "GSW" for p in d["players"]]
check("snooze over: he's back in the nudge", "Stephen Curry" in gsw_named, done)

print("\nrefusals and edges")
INBOX["ann"].append({"id": "x1", "ts": "z", "text": "", "link": None, "read": False,
                     "actions": [{"kind": "rm -rf", "label": "?"}]})
raises("unknown kind", 400, lambda: ib.run_action("x1", 0, ANN))
INBOX["ann"].append({"id": "x2", "ts": "z", "text": "", "link": None, "read": False, "actions": [
    {"kind": "tradeblock_snooze", "label": "?", "team": "GSW", "player": "Traded Away"},
    {"kind": "tradeblock_remove", "label": "?", "team": "GSW", "player": "Traded Away"}]})
raises("snoozing a player no longer listed", 409, lambda: ib.run_action("x2", 0, ANN))
check("…stays undone", not INBOX["ann"][-1]["actions"][0].get("done_at"))
check("removing one already gone just reads done",
      ib.run_action("x2", 1, ANN)["action"]["done_label"] == "Traded Away is already off the block")
pl._scrub_trading_block({"GSW": {"curry-stephen"}}, BIOS)
check("a trade still scrubs a snoozed player", "Stephen Curry" not in gsw())

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all passed")

"""NBN Twitter — routers/tweets.py, the homepage card.

What's worth pinning:

  * **The shape is checked at the door.** A tweet over 280 characters, a
    handle Twitter wouldn't allow, or a team that doesn't exist is refused,
    because the card trusts what it reads.
  * **`teams` is a game or nothing.** One team alone can't be linked to a
    box score, so it is refused rather than half-linked.
  * **A tweet number is permanent**, and the list is newest first.
  * **?team= finds a team's tweets** whether it played in the game or owns
    the fan account.
  * **Members post as themselves or a made-up identity**, and who really
    posted is kept but never shown publicly, except to admin.
  * **A handle belongs to whoever used it first**: nobody else can post as
    the cast, as another member, or as someone's saved scoop account.
  * **Each stored tweet goes to Discord once**, as an embed, and the batch
    is saved even when Discord fails.

The store is patched into memory, and endpoints are called directly, so
nothing touches tweets.json in NBS_DATA_DIR.

    venv/bin/python -m tests.test_tweets
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
import routers.tweets as tw  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def invalid(name, **tweet):
    base = {"handle": "NBNStatMuse", "name": "StatMuse", "text": "hi"}
    try:
        tw.TweetBatch(date="2026-10-23", tweets=[base | tweet])
    except ValidationError:
        check(f"{name} is refused", True)
        return
    check(f"{name} is refused", False)


STORE = {"seq": 0, "items": []}
tw._load_store = lambda: STORE
tw._save_store = lambda store: None
tw.log_write = lambda info, msg: None
STATS = {"name": "bryn", "roles": ["stats"]}
SENT = []
tw.DISCORD_TWEETS_CHANNEL = "123"
tw.transport.configured = lambda ch: bool(ch)
tw.transport.send = lambda ch, payload, **kw: SENT.append(payload() if callable(payload) else payload)


def batch(date, *tweets):
    return tw.TweetBatch(date=date, tweets=list(tweets))


print("shape")
invalid("281 characters", text="x" * 281)
invalid("an empty tweet", text="")
invalid("a 16-character handle", handle="a" * 16)
invalid("a handle with a dot", handle="nbn.stats")
invalid("an unknown team in teams", teams=["DAL", "XXX"])
invalid("one team in teams", teams=["DAL"])
invalid("an unknown fan-account team", team="SEA")
invalid("negative likes", likes=-1)
try:
    tw.TweetBatch(date="10/23/2026", tweets=[{"handle": "a", "name": "a", "text": "a"}])
    check("a non-ISO date is refused", False)
except ValidationError:
    check("a non-ISO date is refused", True)

ok = tw.TweetIn(handle="@NBNStatMuse", name="StatMuse", text="hi", teams=["dal", "ind"], team="dal")
check("a leading @ is dropped", ok.handle == "NBNStatMuse")
check("teams are upper-cased", ok.teams == ["DAL", "IND"] and ok.team == "DAL")

print("post and list")
first = tw.post_tweets(batch("2026-10-22",
                             {"handle": "a", "name": "A", "text": "one", "teams": ["DAL", "IND"]},
                             {"handle": "b", "name": "B", "text": "two"}), STATS)
check("a batch gets consecutive numbers", [t["id"] for t in first] == [1, 2])
check("each tweet carries the batch's date", all(t["date"] == "2026-10-22" for t in first))
second = tw.post_tweets(batch("2026-10-23",
                              {"handle": "c", "name": "C", "text": "three", "team": "MIL"}), STATS)
check("numbering carries on", second[0]["id"] == 3)

listed = tw.list_tweets(limit=20, before=None, team=None, authorization=None)
check("newest first", [t["id"] for t in listed] == [3, 2, 1])
check("?limit= caps the list", [t["id"] for t in tw.list_tweets(limit=1, before=None, team=None, authorization=None)] == [3])
check("?before= pages back", [t["id"] for t in tw.list_tweets(limit=20, before=3, team=None, authorization=None)] == [2, 1])
check("?team= finds a team in the game", [t["id"] for t in tw.list_tweets(limit=20, before=None, team="ind", authorization=None)] == [1])
check("?team= finds a fan account's team", [t["id"] for t in tw.list_tweets(limit=20, before=None, team="MIL", authorization=None)] == [3])

print("delete")
tw.delete_tweet(3, STATS)
again = tw.post_tweets(batch("2026-10-23", {"handle": "d", "name": "D", "text": "four"}), STATS)
check("a deleted number is not reused", again[0]["id"] == 4)
try:
    tw.delete_tweet(99, STATS)
    check("deleting a missing tweet → 404", False)
except HTTPException as e:
    check("deleting a missing tweet → 404", e.status_code == 404)

print("discord")
check("every posted tweet went to Discord once", len(SENT) == 4)
e = SENT[0]["embeds"][0]
check("the embed carries the text", e["description"] == "one" and e["author"]["name"] == "A (@a)")
check("a tweet can't ping anyone", SENT[0]["allowed_mentions"] == {"parse": []})
from routers.discord import TEAM_COLORS  # noqa: E402
check("a fan account takes its team's colour", SENT[2]["embeds"][0]["color"] == TEAM_COLORS["MIL"])
player = tw._embed({"name": "Giannis", "handle": "Giannis_An34", "text": "w", "slug": "antetokounmpo-giannis",
                    "teams": ["IND", "MIL"], "team": None, "likes": 1200, "retweets": 3,
                    "created_at": "2026-10-23T00:00:00+00:00"})
check("a player's account shows his face", player["author"]["icon_url"].endswith("/api/players/antetokounmpo-giannis/headshot"))
check("the footer names the game", "IND @ MIL" in player["footer"]["text"] and "1,200" in player["footer"]["text"])
tw.transport.send = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down"))
n = len(STORE["items"])
tw.post_tweets(batch("2026-10-24", {"handle": "e", "name": "E", "text": "five"}), STATS)
check("Discord failing doesn't lose the tweet", len(STORE["items"]) == n + 1)
tw.DISCORD_TWEETS_CHANNEL = ""
tw.transport.send = lambda *a, **kw: SENT.append("x")
before = len(SENT)
tw.post_tweets(batch("2026-10-24", {"handle": "f", "name": "F", "text": "six"}), STATS)
check("no channel, no post", len(SENT) == before)

print("member tweets")
tw.DISCORD_TWEETS_CHANNEL = "123"
tw.transport.send = lambda ch, payload, **kw: SENT.append(payload() if callable(payload) else payload)
PROFILES = {"seq": 0, "items": []}
tw._load_profiles = lambda: PROFILES
tw._save_profiles = lambda store: None
tw.load_members = lambda: {"bryn": {}, "Mo Money": {}}
tw._member_team = lambda name: "MIL" if name == "bryn" else None
tw.league_today_str = lambda: "2026-10-28"
BRYN = {"name": "bryn", "roles": []}
MO = {"name": "Mo Money", "roles": []}
ADMIN = {"name": "boss", "roles": ["admin"]}


def refused(name, status, fn, *a):
    try:
        fn(*a)
    except HTTPException as e:
        check(name, e.status_code == status)
        return
    check(name, False)


mine = tw.post_member_tweet(tw.MemberTweet(text="hello"), BRYN)
check("as yourself: your name, handle and team", (mine["name"], mine["handle"], mine["team"]) == ("bryn", "bryn", "MIL"))
check("the poster never comes back", "poster" not in mine and "author" not in mine)
check("it went to Discord", SENT[-1]["embeds"][0]["description"] == "hello")
check("a member's handle drops what Twitter won't allow", tw._self_handle("Mo Money") == "MoMoney")

scoop = tw.post_member_tweet(tw.MemberTweet(text="sources: big trade", identity="anon",
                                            handle="ShamsOfNBN", name="Shams", save=True), MO)
check("a made-up identity posts under its own name", scoop["handle"] == "ShamsOfNBN" and scoop["team"] is None)
check("save keeps it as a profile", [p["handle"] for p in tw.my_profiles(MO)["profiles"]] == ["ShamsOfNBN"])
pid = tw.my_profiles(MO)["profiles"][0]["id"]
again = tw.post_member_tweet(tw.MemberTweet(text="more", identity="profile", profile_id=pid), MO)
check("a saved profile loads", again["handle"] == "ShamsOfNBN")
refused("someone else's profile is refused", 404, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="profile", profile_id=pid), BRYN)
refused("someone else's scoop handle is taken", 409, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="anon", handle="shamsofnbn", name="Fake"), BRYN)
refused("the cast's handles are taken", 409, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="anon", handle="a", name="Fake"), BRYN)
refused("another member's own handle is taken", 409, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="anon", handle="MoMoney", name="Mo"), BRYN)
refused("a made-up identity needs a handle", 422, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="anon", name="Nobody"), BRYN)
refused("a bad handle is refused", 422, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="anon", handle="has space", name="N"), BRYN)

listed = tw.list_tweets(limit=100, before=None, team=None, authorization=None)
check("the public list carries no poster", not any("poster" in t or "author" in t for t in listed))
check("admin can see who posted", tw.tweet_poster(scoop["id"], ADMIN)["poster"] == "Mo Money")
check("an official tweet names its account", tw.tweet_poster(1, ADMIN)["identity"] == "official")
tw._resolve_token = lambda h: MO if h == "Bearer mo" else None
marked = [t["id"] for t in tw.list_tweets(limit=100, before=None, team=None, authorization="Bearer mo") if t.get("mine")]
check("signed in, your own tweets are marked mine", marked == sorted([scoop["id"], again["id"]], reverse=True))

refused("deleting someone else's tweet is refused", 403, tw.delete_tweet, scoop["id"], BRYN)
tw.delete_tweet(again["id"], MO)
check("you can delete your own", all(t["id"] != again["id"] for t in STORE["items"]))
tw.delete_profile(pid, MO)
check("deleting a profile forgets it", tw.my_profiles(MO)["profiles"] == [])
refused("its handle stays taken by its tweets", 409, tw.post_member_tweet,
        tw.MemberTweet(text="x", identity="anon", handle="ShamsOfNBN", name="Fake"), BRYN)

for i in range(tw.MEMBER_DAILY_LIMIT - 1):
    tw.post_member_tweet(tw.MemberTweet(text=f"spam {i}"), BRYN)
refused(f"{tw.MEMBER_DAILY_LIMIT} a day, then 429", 429, tw.post_member_tweet, tw.MemberTweet(text="one more"), BRYN)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")

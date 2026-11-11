"""Regression tests for routers/tonight.py and routers/tonight_card.py — the
homepage's "Tonight" graphic and the 11am #leaguetalk card.

What must keep being true:

  * the featured game is a stream game when there is one (the earliest), else
    the best win% + playoff stakes, ties to the earlier tip;
  * a star is the best mean Game Score over his last 5 games *for this team*,
    needs 3 of them, and is someone on the roster today;
  * storylines say only what the box scores support (series, streaks of 3+,
    odds between 25% and 75%);
  * last night is the newest earlier date with box scores, ranked by Game Score;
  * the card renders at 1200x675 with no headshots and no logos at all;
  * a date is posted at most once, and a day with no games posts nothing.

    venv/bin/python -m tests.test_tonight
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image  # noqa: E402

from routers import game_highs as gh  # noqa: E402
from routers import tonight as T  # noqa: E402
from routers import tonight_card as C  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def g(player, team, opp, date, P=10, R=5, A=5, home=True, tp=110, op=100, season="26-27"):
    return {"PLAYER": player, "TEAM": team, "OPP": opp if home else "@" + opp, "DATE": date,
            "SEASON": season, "gametype": "REG",
            "M": 36, "P": P, "R": R, "A": A, "S": 1, "B": 0, "TO": 2, "PF": 2,
            "FGM": P // 2, "FGA": P, "3PM": 0, "3PA": 0, "FTM": 0, "FTA": 0, "OR": 1, "DR": R - 1,
            "WL": "W" if tp > op else "L", "TEAM_PTS": tp, "OPP_TEAM_PTS": op}


# AAA beats BBB three times running; HOT is AAA's star; TRADED had big games
# for CCC before joining AAA, and only two for AAA.
GAMES = []
for i, d in enumerate(["2026-11-01", "2026-11-03", "2026-11-05", "2026-11-07"]):
    GAMES += [
        g("HOT, ONE", "AAA", "BBB", d, P=30 + i),
        g("ROLE, TWO", "AAA", "BBB", d, P=12),
        g("OTHER, SIDE", "BBB", "AAA", d, P=15, home=False, tp=100, op=110),
    ]
GAMES[-3:] = [
    g("HOT, ONE", "AAA", "BBB", "2026-11-07", P=33, tp=99, op=104),
    g("ROLE, TWO", "AAA", "BBB", "2026-11-07", P=12, tp=99, op=104),
    g("OTHER, SIDE", "BBB", "AAA", "2026-11-07", P=15, home=False, tp=104, op=99),
]
for d in ["2026-10-25", "2026-10-27", "2026-10-29"]:
    GAMES.append(g("TRADED, GUY", "CCC", "DDD", d, P=50))
for d in ["2026-11-03", "2026-11-05"]:
    GAMES.append(g("TRADED, GUY", "AAA", "BBB", d, P=40))
GAMES.append(g("LATE, GAME", "AAA", "BBB", "2026-11-11", P=60))   # the day itself
index = gh.GameIndex(iter(GAMES))

print("results, records, streaks, series")
res = T.season_results(index, "26-27", before="2026-11-11")
recs = T.records(res)
check("the day itself is not a result", all(r["date"] < "2026-11-11" for r in res))
check("records count both sides", recs["AAA"] == [3, 1] and recs["BBB"] == [1, 3])
check("a streak is the current run", T.streak(res, "BBB") == ("W", 1) and T.streak(res, "AAA") == ("L", 1))
check("season series", T.series(res, "AAA", "BBB") == (3, 1))

print("\nhot hand")
roster = {"hot-one", "role-two", "traded-guy"}
star = T.hot_hand(index, "26-27", "AAA", roster, "2026-11-11")
check("best mean Game Score wins", star and star["slug"] == "hot-one")
check("last-5 line is per game", star["last5"]["gp"] == 4 and star["last5"]["pts"] == round((30 + 31 + 32 + 33) / 4, 1))
check("games for another team don't count (2 for AAA < 3)",
      T.hot_hand(index, "26-27", "AAA", {"traded-guy"}, "2026-11-11") is None)
check("only today's roster is considered", T.hot_hand(index, "26-27", "AAA", {"role-two"}, "2026-11-11")["slug"] == "role-two")
check("the day itself doesn't count", T.hot_hand(index, "26-27", "AAA", {"late-game"}, "2026-11-11") is None)
check("only the last 5 games", T.hot_hand(index, "26-27", "AAA", roster, "2026-11-11", n=2)["last5"]["pts"] == 32.5)
ovr = T.ovr_star({"players": [{"slug": "a", "name": "A, A", "ovr": 80}, None, {"slug": "b", "name": "B, B", "ovr": 88}]})
check("OVR fallback picks the top rating", ovr["slug"] == "b" and ovr["basis"] == "ovr" and ovr["last5"] is None)
check("no five, no star", T.ovr_star({}) is None)

print("\nfeatured game")
SLATE = [
    {"id": "1", "away_team": "CCC", "home_team": "DDD", "time_et": "7:00p"},
    {"id": "2", "away_team": "AAA", "home_team": "BBB", "time_et": "8:00p"},
    {"id": "3", "away_team": "EEE", "home_team": "FFF", "time_et": "9:00p", "stream": True},
    {"id": "4", "away_team": "GGG", "home_team": "HHH", "time_et": "7:30p", "stream": True},
]
check("a stream game wins, the earliest tip", T.pick_featured(SLATE, recs, {})["id"] == "4")
plain = [dict(x, stream=False) for x in SLATE]
check("otherwise the best records", T.pick_featured(plain, {"AAA": [9, 1], "BBB": [8, 2]}, {})["id"] == "2")
odds = {"CCC": 0.5, "DDD": 0.5}
check("bubble stakes count", T.pick_featured(plain, {}, odds)["id"] == "1")
check("ties go to the earlier tip", T.pick_featured(plain, {}, {})["id"] == "1")
check("no games, no featured", T.pick_featured([], {}, {}) is None)

print("\nstorylines")
st = {"AAA": {"name": "One Hot", "last5": {"gp": 4, "pts": 31.5}}, "BBB": None}
lines = T.storylines({"away_team": "AAA", "home_team": "BBB", "stream": True, "streamer": "Kamal"},
                     res, {"AAA": 0.9, "BBB": 0.4}, st)
check("the stream comes first", lines[0] == "Live on Twitch, streamed by Kamal")
check("season series", "AAA lead the season series 3–1" in lines)
check("no streak line under 3", not any("straight" in s for s in lines))
check("the star's last 5", "Hot: 31.5 PPG over his last 4" in lines)
check("odds only when uncertain", "BBB playoff odds: 40%" in lines and not any(s.startswith("AAA playoff") for s in lines))
check("names read like the site", T.display_name("BARRETT, RJ") == "RJ Barrett"
      and T.last_name("Shai Gilgeous-Alexander") == "Gilgeous-Alexander"
      and T.last_name("Jaren Jackson Jr.") == "Jackson")

print("\nlast night")
ln = T.last_night(index, "2026-11-11", lambda raw, slug: slug)
check("the newest earlier date with box scores", ln["date"] == "2026-11-07")
check("ranked by Game Score", [x["slug"] for x in ln["top"]] == ["hot-one", "other-side", "role-two"])
check("nothing in the lookback is None", T.last_night(index, "2027-03-01", lambda r, s: s) is None)

print("\nthe card")
DATA = {
    "date": "2026-11-11", "season": "26-27", "count": 3, "featured": "4",
    "theme": {"away": {"primary": "#000000", "accent": "#FFFFFF"}, "home": {"primary": "#CE1141"}},
    "games": [
        {"id": "4", "away": "BKN", "home": "CHI", "time_et": "7:30p", "stream": True, "featured": True,
         "records": {"away": "1–0", "home": "0–1"},
         "stars": {"away": {"slug": "x", "name": "Zed Ex", "last5": {"gp": 5, "pts": 30, "reb": 5, "ast": 5}},
                   "home": None},
         "storylines": ["Live on Twitch", "BKN lead the season series 1–0"]},
        {"id": "1", "away": "UTA", "home": "SAS", "time_et": "9:00p", "featured": False,
         "records": {}, "stars": {}, "storylines": []},
        {"id": "2", "away": "ATL", "home": "BOS", "time_et": "", "featured": False,
         "records": {}, "stars": {}, "storylines": []},
    ],
}
png = C.render(DATA, headshot=lambda s: None, logo=lambda t: None)
img = Image.open(io.BytesIO(png))
check("renders 1200x675 with no headshots or logos", img.size == (1200, 675))
red = Image.new("RGB", (160, 160), (200, 0, 0))
logo = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
logo.paste((10, 10, 10, 255), (60, 40, 240, 160))
png2 = C.render(DATA, headshot=lambda s: red, logo=lambda t: logo)
check("renders with images", Image.open(io.BytesIO(png2)).size == (1200, 675))
check("a near-black logo is dark", C._is_dark(logo.crop(logo.getchannel("A").getbbox())))
check("the hash names the answer", C.card_hash(DATA) == C.card_hash(dict(DATA))
      and C.card_hash(DATA) != C.card_hash({**DATA, "count": 4}))

print("\nthe post")
post = C.build_post(DATA)
check("one line, no pings", post["content"].startswith("\U0001F3C0 **3 games tonight**")
      and post["allowed_mentions"] == {"parse": []})
check("image is the card for that date and answer",
      post["embeds"][0]["image"]["url"].endswith(f"card.png?date=2026-11-11&v={C.card_hash(DATA)}"))
check("featured game and storylines in the embed",
      "BKN @ CHI" in post["embeds"][0]["description"] and "season series" in post["embeds"][0]["description"]
      and "Live on Twitch" not in post["embeds"][0]["description"])
check("embed colour is the home team's", post["embeds"][0]["color"] == 0xCE1141)

sent = []


class FakeTransport:
    @staticmethod
    def configured(ch): return True
    @staticmethod
    def send(ch, payload, **kw): sent.append(payload); return True
    @staticmethod
    def flush(t=30): return True


with tempfile.TemporaryDirectory() as tmp:
    import routers.discord_transport as real_transport
    orig = (C.POSTED_FILE, C.CARD_DIR, C.tonight, C._leaguetalk_channel, C.render,
            real_transport.configured, real_transport.send, real_transport.flush)
    C.POSTED_FILE = Path(tmp) / "posted.json"
    C.CARD_DIR = Path(tmp) / "cards"
    C._leaguetalk_channel = lambda: "123"
    C.render = lambda data: b"png"
    real_transport.configured, real_transport.send, real_transport.flush = (
        FakeTransport.configured, FakeTransport.send, FakeTransport.flush)
    try:
        C.tonight = lambda day=None: DATA
        first = C.post_tonight()
        second = C.post_tonight()
        check("posts once", first["posted"] and len(sent) == 1)
        check("a second run the same date posts nothing", not second["posted"]
              and second["reason"] == "already posted" and len(sent) == 1)
        check("the card is drawn before posting", any(Path(tmp, "cards").glob("2026-11-11-*.png")))
        dry = C.post_tonight(dry_run=True)
        check("a dry run returns the payload and sends nothing", dry["payload"] and len(sent) == 1)
        C.tonight = lambda day=None: {**DATA, "date": "2026-11-12", "games": [], "count": 0}
        check("no games, no post", C.post_tonight()["reason"] == "no games" and len(sent) == 1)
    finally:
        (C.POSTED_FILE, C.CARD_DIR, C.tonight, C._leaguetalk_channel, C.render,
         real_transport.configured, real_transport.send, real_transport.flush) = orig

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all checks passed")

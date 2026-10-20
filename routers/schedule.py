"""The league's game schedule — one file per season, seeded and then edited.

Seeded from the real NBA schedule (nbn-today/build/load_nba_schedule.py), which
NBN follows exactly, and mutable from there on. Mutable is the point rather than
a concession: the NBA leaves two games per team unassigned until its in-season
cup's group play resolves, and NBN's cup does not follow the NBA's — the 60
group games are ours to reassign and the NBA's knockout round is not reproduced
at all. So the seed is a *seed*, run once, and every change after it comes
through these endpoints. The loader refuses to overwrite an existing file for
exactly that reason: a re-run would silently discard whatever the league had
decided since.

Deliberately its own file rather than more of `calendar-games.json`. That one is
a hand-curated handful of notable upcoming games a BOD member types in one at a
time; this is 1,200 rows with per-season identity. Mixing them would leave no
way to tell a league fixture from someone's note, and no way to replace a season
without taking the notes with it.

The one structural rule enforced here is that a team cannot play twice on the
same date. That is what makes the file trustworthy enough to check a submitted
box score against later. It is a 409 rather than a wall — pass
`allow_conflict: true` if the league ever genuinely wants a doubleheader.
"""
import re
import secrets
import threading

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .constants import DATA_DIR, SCHEDULE_FILE_FMT, VALID_TEAMS
from .storage import _load_json, _save_json, log_write, _current_league_year
from .auth import require_role, require_admin, get_token_info, has_role
from .league_time import league_today_str
from . import auth as auth_module
from . import game_day_notify
from . import wallet
from . import inbox

router = APIRouter()

_schedule_lock = threading.Lock()

DATE_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEASON_RE = re.compile(r"^\d{2}-\d{2}$")
# "7:00p" / "10:30a" — the NBA's own rendering, which the seed carries verbatim.
TIME_RE   = re.compile(r"^(\d{1,2}):(\d{2})([ap])$")
# "19:00" / "9:30" — accepted on input and normalized to the form above, so the
# file never ends up holding two spellings of the same tip-off.
TIME24_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _schedule_path(season: str):
    return DATA_DIR / SCHEDULE_FILE_FMT.format(season=season)


def _load(season: str) -> dict:
    return _load_json(_schedule_path(season), {"season": season, "source": "", "games": []})


def _save(season: str, data: dict):
    data["games"].sort(key=lambda g: (g["date"], _time_key(g.get("time_et", "")), g["home_team"]))
    _save_json(_schedule_path(season), data)


def _time_key(t: str) -> tuple:
    """Sort key for a "7:00p" tip-off. Unset times sort last within a date."""
    m = TIME_RE.match(t or "")
    if not m:
        return (99, 99)
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if hh == 12:
        hh = 0
    if ap == "p":
        hh += 12
    return (hh, mm)


def _normalize_time(t: str) -> str:
    t = (t or "").strip().lower().replace(" ", "")
    if not t:
        return ""
    t = t.removesuffix("m")          # "7:00pm" -> "7:00p"
    if TIME_RE.match(t):
        return t
    m = TIME24_RE.match(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise HTTPException(status_code=422, detail=f"Invalid time: {t}")
        ap = "a" if hh < 12 else "p"
        h12 = hh % 12 or 12
        return f"{h12}:{mm:02d}{ap}"
    raise HTTPException(status_code=422, detail=f"Invalid time (expected '7:00p' or '19:00'): {t}")


def _resolve_season(season: str | None) -> str:
    season = (season or _current_league_year()).strip()
    if not SEASON_RE.match(season):
        raise HTTPException(status_code=422, detail=f"season must look like '26-27', got {season!r}")
    return season


def _public(game: dict) -> dict:
    """A game as the API hands it out. `streamer` (who's claimed to broadcast
    it) and `stream` (whether the game itself is flagged as a stream) are each
    stored only once set — 1,200 nulls/falses a season is noise in a file
    whose diffs are read by hand — but both are always present on the way
    out, so no caller has to know that. The two are independent: a game can
    carry either, neither, or both."""
    return {**game, "streamer": game.get("streamer") or None,
            "stream": bool(game.get("stream")),
            "stream_buyer": game.get("stream_buyer") or None,
            "stream_request": game.get("stream_request") or None}


def _check_team(label: str, team: str) -> str:
    team = (team or "").strip().upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown {label}: {team}")
    return team


def _conflict(games: list[dict], date: str, home: str, away: str, exclude_id: str | None):
    """The first already-scheduled game on `date` involving either team."""
    for g in games:
        if g["date"] != date or g["id"] == exclude_id:
            continue
        if g["home_team"] in (home, away) or g["away_team"] in (home, away):
            return g
    return None


def _find(game_id: str, preferred: str) -> tuple[str, dict, dict]:
    """(season, file data, game) for `game_id` — the preferred season first, then
    any other season with a schedule file, so a caller can address a game by id
    alone without having to know which season it belongs to."""
    seasons = [preferred] + [s for s in _known_seasons() if s != preferred]
    for season in seasons:
        data = _load(season)
        for g in data["games"]:
            if g["id"] == game_id:
                return season, data, g
    raise HTTPException(status_code=404, detail=f"Scheduled game '{game_id}' not found")


def _known_seasons() -> list[str]:
    prefix, suffix = SCHEDULE_FILE_FMT.split("{season}")
    out = []
    for p in DATA_DIR.glob(SCHEDULE_FILE_FMT.format(season="*")):
        season = p.name[len(prefix):len(p.name) - len(suffix)]
        if SEASON_RE.match(season):
            out.append(season)
    return sorted(out)


class ScheduleGameIn(BaseModel):
    date: str
    away_team: str
    home_team: str
    time_et: str = ""
    arena: str = ""
    note: str = ""
    season: str | None = None
    allow_conflict: bool = False


class ScheduleGamePatch(BaseModel):
    date: str | None = None
    away_team: str | None = None
    home_team: str | None = None
    time_et: str | None = None
    arena: str | None = None
    note: str | None = None
    season: str | None = None
    allow_conflict: bool = False


@router.get("/api/schedule/seasons")
def list_schedule_seasons():
    """Which seasons have a schedule on file, and how many games each holds."""
    return [{"season": s, "games": len(_load(s)["games"])} for s in _known_seasons()]


@router.get("/api/schedule")
def get_schedule(
    season: str | None = None,
    team: str | None = Query(None, description="Only games this team plays, home or away"),
    from_: str | None = Query(None, alias="from", description="Inclusive YYYY-MM-DD"),
    to: str | None = Query(None, description="Inclusive YYYY-MM-DD"),
):
    season = _resolve_season(season)
    data = _load(season)
    games = data["games"]
    if team:
        t = _check_team("team", team)
        games = [g for g in games if t in (g["home_team"], g["away_team"])]
    if from_:
        games = [g for g in games if g["date"] >= from_]
    if to:
        games = [g for g in games if g["date"] <= to]
    return {"season": season, "source": data.get("source", ""), "count": len(games),
            "games": [_public(g) for g in games]}


@router.post("/api/schedule")
def create_schedule_game(body: ScheduleGameIn, info: dict = Depends(require_role("bod"))):
    season = _resolve_season(body.season)
    if not DATE_RE.match(body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    home = _check_team("home_team", body.home_team)
    away = _check_team("away_team", body.away_team)
    if home == away:
        raise HTTPException(status_code=422, detail="home_team and away_team must differ")
    time_et = _normalize_time(body.time_et)

    with _schedule_lock:
        data = _load(season)
        clash = _conflict(data["games"], body.date, home, away, None)
        if clash and not body.allow_conflict:
            raise HTTPException(
                status_code=409,
                detail=f"{clash['away_team']} @ {clash['home_team']} is already scheduled on "
                       f"{body.date}. Pass allow_conflict to schedule it anyway.")
        game = {
            "id": secrets.token_hex(8),
            "date": body.date,
            "time_et": time_et,
            "away_team": away,
            "home_team": home,
            "arena": body.arena.strip(),
            "note": body.note.strip(),
            "source_id": "",
        }
        data["games"].append(game)
        _save(season, data)
    log_write(info, f"POST schedule — {season} {body.date} {away}@{home}")
    return _public(game)


@router.patch("/api/schedule/{game_id}")
def update_schedule_game(game_id: str, body: ScheduleGamePatch, info: dict = Depends(require_role("bod"))):
    preferred = _resolve_season(body.season)
    with _schedule_lock:
        season, data, game = _find(game_id, preferred)
        updated = dict(game)
        if body.date is not None:
            if not DATE_RE.match(body.date):
                raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
            updated["date"] = body.date
        if body.home_team is not None:
            updated["home_team"] = _check_team("home_team", body.home_team)
        if body.away_team is not None:
            updated["away_team"] = _check_team("away_team", body.away_team)
        if updated["home_team"] == updated["away_team"]:
            raise HTTPException(status_code=422, detail="home_team and away_team must differ")
        if body.time_et is not None:
            updated["time_et"] = _normalize_time(body.time_et)
        if body.arena is not None:
            updated["arena"] = body.arena.strip()
        if body.note is not None:
            updated["note"] = body.note.strip()

        clash = _conflict(data["games"], updated["date"], updated["home_team"], updated["away_team"], game_id)
        if clash and not body.allow_conflict:
            raise HTTPException(
                status_code=409,
                detail=f"{clash['away_team']} @ {clash['home_team']} is already scheduled on "
                       f"{updated['date']}. Pass allow_conflict to move it there anyway.")

        game.update(updated)
        _save(season, data)
    log_write(info, f"PATCH schedule/{game_id} — {season} {game['date']} "
                    f"{game['away_team']}@{game['home_team']}")
    return _public(game)


# ── Streamers ────────────────────────────────────────────────────────────────
#
# A `streamer` puts their own name on a game they intend to stream, and the
# claim is public the moment it lands — the schedule is already a public read,
# so nothing extra is needed to publish it.
#
# One streamer per game (league decision, 2026-09-01), so this is a single name
# and not a list: a list capped at one would be a shape that disagrees with the
# rule, and the first thing to drift from it. A second claimant gets a 409
# naming who holds it, rather than silently joining.
#
# Claiming takes no member argument at all, which is what makes `streamer` a
# role safe to hand out without board standing: its holder can write exactly
# one name, their own. Nor does releasing take one — the claim identifies its
# own holder, so "clear this game" is the whole operation, and who may do it
# follows from who holds it.


@router.post("/api/schedule/{game_id}/streamer")
def claim_schedule_game(game_id: str, season: str | None = None,
                        info: dict = Depends(require_role("streamer"))):
    """Put the caller's own name on a game. Claiming one you already hold is not
    an error — the button that calls this is the same button either way — but
    claiming one someone else holds is a 409."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        held = game.get("streamer")
        if held and held != info["name"]:
            raise HTTPException(
                status_code=409,
                detail=f"{held} is already streaming this game. They have to drop it first.")
        if not held:
            game["streamer"] = info["name"]
            _save(found_season, data)
    log_write(info, f"POST schedule/{game_id}/streamer — {found_season} {game['date']} "
                    f"{game['away_team']}@{game['home_team']}")
    return _public(game)


@router.delete("/api/schedule/{game_id}/streamer")
def unclaim_schedule_game(game_id: str, season: str | None = None,
                          info: dict = Depends(get_token_info)):
    """Clear a game's claim. Yours to drop whenever — including if your
    `streamer` role has since been revoked, since you would otherwise be stuck
    on a game you can no longer reach. Dropping someone *else's* is the board's
    call, not another streamer's."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        held = game.get("streamer")
        if not held:
            raise HTTPException(status_code=404, detail="Nobody is streaming this game")
        if held != info["name"] and not (has_role(info, "admin") or has_role(info, "bod")):
            raise HTTPException(status_code=403,
                                detail=f"{held} holds this game — only 'bod' can drop it for them")
        # Drop the key rather than leaving a null, so a game nobody is streaming
        # is byte-identical to one that was never claimed.
        game.pop("streamer", None)
        _save(found_season, data)
    log_write(info, f"DELETE schedule/{game_id}/streamer — {found_season} {held}")
    return _public(game)


# ── Assigning streamers (admin) ──────────────────────────────────────────────
#
# The one path that writes a name other than the caller's, so it is `admin`
# alone. It overwrites an existing claim instead of 409ing, since moving a game
# from one streamer to another is the reason to have it. The name must be a
# member who could have claimed the game themselves (`streamer`, or `admin`,
# which satisfies it). Unassigning needs nothing new: DELETE above already
# lets an admin drop anyone's claim.

class StreamerAssign(BaseModel):
    name: str


def _check_assignee(name: str) -> str:
    name = (name or "").strip()
    member = auth_module.load_members().get(name)
    if member is None:
        raise HTTPException(status_code=422, detail=f"Unknown member: {name!r}")
    if not has_role({"roles": member.get("roles", [])}, "streamer") \
            and not has_role({"roles": member.get("roles", [])}, "admin"):
        raise HTTPException(status_code=422, detail=f"{name} doesn't hold the streamer role")
    return name


@router.put("/api/schedule/{game_id}/streamer")
def assign_schedule_game(game_id: str, body: StreamerAssign, season: str | None = None,
                         info: dict = Depends(require_admin)):
    """Put a named streamer on one game, replacing whoever held it."""
    name = _check_assignee(body.name)
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        displaced = game.get("streamer")
        if displaced != name:
            game["streamer"] = name
            _save(found_season, data)
    log_write(info, f"PUT schedule/{game_id}/streamer — {found_season} {game['date']} "
                    f"{game['away_team']}@{game['home_team']} → {name}"
                    + (f" (was {displaced})" if displaced and displaced != name else ""))
    if displaced != name:
        game_day_notify.streamer_assigned(name, game["date"], [game], info["name"],
                                          [displaced] if displaced else [])
    return _public(game)


@router.put("/api/schedule/days/{date}/streamer")
def assign_schedule_day(date: str, body: StreamerAssign, season: str | None = None,
                        info: dict = Depends(require_admin)):
    """Put a named streamer on every game that date, replacing any claims.
    Returns the date's games."""
    if not DATE_RE.match(date):
        raise HTTPException(status_code=422, detail=f"date must look like 2026-10-18, got {date!r}")
    name = _check_assignee(body.name)
    season = _resolve_season(season)
    with _schedule_lock:
        data = _load(season)
        day = [g for g in data["games"] if g["date"] == date]
        if not day:
            raise HTTPException(status_code=404, detail=f"No games on {date} in {season}")
        changed = [g for g in day if g.get("streamer") != name]
        displaced = sorted({g["streamer"] for g in changed if g.get("streamer")})
        for g in changed:
            g["streamer"] = name
        if changed:
            _save(season, data)
    log_write(info, f"PUT schedule/days/{date}/streamer — {season} {len(changed)} game(s) → {name}"
                    + (f" (was {', '.join(displaced)})" if displaced else ""))
    if changed:
        game_day_notify.streamer_assigned(name, date, changed, info["name"], displaced)
    return [_public(g) for g in day]


# ── Stream flag ────────────────────────────────────────────────────────────
#
# Independent of `streamer` above: `stream` is a plain yes/no marking that the
# game itself is a stream. It carries no name and no "holder" concept, so
# unlike a claim it isn't gated by who set it — anyone holding the `streamer`
# role can mark or unmark it, the same as they could mark a different game.


@router.post("/api/schedule/{game_id}/stream")
def mark_schedule_game_stream(game_id: str, season: str | None = None,
                              info: dict = Depends(require_role("streamer"))):
    """Flag a game as a stream. Marking one already flagged is not an error."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        game["stream"] = True
        _save(found_season, data)
    log_write(info, f"POST schedule/{game_id}/stream — {found_season} {game['date']} "
                    f"{game['away_team']}@{game['home_team']}")
    return _public(game)


@router.delete("/api/schedule/{game_id}/stream")
def unmark_schedule_game_stream(game_id: str, season: str | None = None,
                                info: dict = Depends(require_role("streamer"))):
    """Clear a game's stream flag."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        if not game.get("stream"):
            raise HTTPException(status_code=404, detail="This game isn't flagged as a stream")
        if game.get("stream_buyer"):
            raise HTTPException(
                status_code=409,
                detail=f"{game['stream_buyer']} bought this stream — refund it instead, so they get their NB¥ back.")
        # Drop the key rather than leaving False, matching `streamer`'s shape.
        game.pop("stream", None)
        _save(found_season, data)
    log_write(info, f"DELETE schedule/{game_id}/stream — {found_season} {game['date']} "
                    f"{game['away_team']}@{game['home_team']}")
    return _public(game)


# ── Buying a stream ────────────────────────────────────────────────────────
#
# Any member can spend NB¥ to have a game streamed — the one thing NB¥ buys at
# the 2026-09 reset. It goes in two steps:
#
# 1. The member requests it. That stores `stream_request` on the game and costs
#    nothing yet; the balance is only checked, so a request nobody could pay
#    for doesn't reach the streamers. The member can withdraw it.
# 2. A streamer accepts or declines. Accepting debits the member then, sets the
#    same `stream` flag a streamer sets, plus `stream_buyer`. Declining clears
#    it and costs nothing.
#
# There is no daily limit: a streamer approves each one, so a day can carry as
# many bought streams as they accept. Once bought, the buyer can't cancel; a
# streamer refunds it, in full, if the stream can't happen.

STREAMS_LINK = "/committees/stream/"


def _label(game: dict) -> str:
    return f"{game['away_team']} @ {game['home_team']} on {game['date']}"


def _requestable(game: dict) -> None:
    """Refuse a request, or its acceptance, that can't become a bought stream."""
    if game["date"] < league_today_str():
        raise HTTPException(status_code=409, detail="That game has already been played")
    if game.get("stream_buyer"):
        raise HTTPException(status_code=409, detail=f"{game['stream_buyer']} already bought this stream")
    if game.get("stream"):
        raise HTTPException(status_code=409, detail="This game is already being streamed")


@router.post("/api/schedule/{game_id}/stream-request")
def request_stream(game_id: str, season: str | None = None, info: dict = Depends(get_token_info)):
    member = info.get("name")
    if not member:
        raise HTTPException(status_code=401, detail="Sign in to request a stream")
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        _requestable(game)
        if game.get("stream_request"):
            raise HTTPException(status_code=409,
                                detail=f"{game['stream_request']} already requested this stream")
        if wallet.balance(member) < wallet.STREAM_PRICE:
            raise HTTPException(status_code=402,
                                detail=f"A stream costs NB¥{wallet.STREAM_PRICE:,}, and you have "
                                       f"NB¥{wallet.balance(member):,.0f}")
        game["stream_request"] = member
        _save(found_season, data)
    log_write(info, f"POST schedule/{game_id}/stream-request — {found_season} {_label(game)}")
    inbox.notify_roles(["streamer"], f"{member} wants to buy a stream of {_label(game)}. "
                                     f"Accept or decline it.", STREAMS_LINK)
    return _public(game)


@router.delete("/api/schedule/{game_id}/stream-request")
def withdraw_stream_request(game_id: str, season: str | None = None,
                            info: dict = Depends(get_token_info)):
    """The requester takes their own request back. Nothing was spent."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        if not game.get("stream_request"):
            raise HTTPException(status_code=404, detail="Nobody has requested a stream of this game")
        if game["stream_request"] != info.get("name"):
            raise HTTPException(status_code=403, detail="Only the member who asked can withdraw it")
        game.pop("stream_request")
        _save(found_season, data)
    log_write(info, f"DELETE schedule/{game_id}/stream-request — {found_season} {_label(game)}")
    return _public(game)


@router.post("/api/schedule/{game_id}/stream-request/accept")
def accept_stream_request(game_id: str, season: str | None = None,
                          info: dict = Depends(require_role("streamer"))):
    """Buy the stream for the member who asked: debit them now and flag the game."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        buyer = game.get("stream_request")
        if not buyer:
            raise HTTPException(status_code=404, detail="Nobody has requested a stream of this game")
        _requestable(game)
        new_balance = wallet.debit(buyer, wallet.STREAM_PRICE, "stream_purchase",
                                   f"Stream: {_label(game)}", ref=f"stream:{found_season}:{game_id}")
        game.pop("stream_request")
        game["stream"] = True
        game["stream_buyer"] = buyer
        try:
            _save(found_season, data)
        except Exception:
            wallet.credit(buyer, wallet.STREAM_PRICE, "stream_refund",
                          f"Refund — stream purchase failed: {_label(game)}",
                          ref=f"stream:{found_season}:{game_id}")
            raise
    log_write(info, f"POST schedule/{game_id}/stream-request/accept — {found_season} {_label(game)}, "
                    f"{buyer} charged")
    inbox.notify_member(buyer, f"{info['name']} accepted your stream of {_label(game)}. "
                               f"NB¥{wallet.STREAM_PRICE:,} spent.", "/calendar/")
    return {**_public(game), "new_balance": new_balance}


@router.post("/api/schedule/{game_id}/stream-request/decline")
def decline_stream_request(game_id: str, season: str | None = None,
                           info: dict = Depends(require_role("streamer"))):
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        member = game.get("stream_request")
        if not member:
            raise HTTPException(status_code=404, detail="Nobody has requested a stream of this game")
        game.pop("stream_request")
        _save(found_season, data)
    log_write(info, f"POST schedule/{game_id}/stream-request/decline — {found_season} {_label(game)}, "
                    f"{member}")
    inbox.notify_member(member, f"{info['name']} declined your stream request for {_label(game)}. "
                                f"Nothing was spent.", "/calendar/")
    return _public(game)


@router.delete("/api/schedule/{game_id}/purchase")
def refund_stream(game_id: str, season: str | None = None,
                  info: dict = Depends(require_role("streamer"))):
    """Refund a bought stream in full and clear it, freeing the day."""
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        buyer = game.get("stream_buyer")
        if not buyer:
            raise HTTPException(status_code=404, detail="Nobody bought a stream of this game")
        label = _label(game)
        wallet.credit(buyer, wallet.STREAM_PRICE, "stream_refund",
                      f"Refund: {label} (by {info['name']})", ref=f"stream:{found_season}:{game_id}")
        game.pop("stream_buyer", None)
        game.pop("stream", None)
        _save(found_season, data)
    log_write(info, f"DELETE schedule/{game_id}/purchase — {found_season} {label}, refunded {buyer}")
    return _public(game)


@router.delete("/api/schedule/{game_id}")
def delete_schedule_game(game_id: str, season: str | None = None, info: dict = Depends(require_role("bod"))):
    preferred = _resolve_season(season)
    with _schedule_lock:
        found_season, data, game = _find(game_id, preferred)
        if game.get("stream_buyer"):
            raise HTTPException(
                status_code=409,
                detail=f"{game['stream_buyer']} bought a stream of this game — refund it first.")
        data["games"] = [g for g in data["games"] if g["id"] != game_id]
        _save(found_season, data)
    log_write(info, f"DELETE schedule/{game_id} — {found_season} {game['date']} "
                    f"{game['away_team']}@{game['home_team']}")
    return {"ok": True}

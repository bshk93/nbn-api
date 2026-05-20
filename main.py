import csv
import io
import json
import logging
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("nbn-api")


def log_write(info: dict, action: str):
    name = info.get("name", "unknown")
    logger.info("[%s] %s", name, action)

DATA_DIR = Path("/var/lib/nothing-but-stats")
TOKENS_FILE        = DATA_DIR / "tokens.json"
TRADING_BLOCK_FILE = DATA_DIR / "trading-block.json"
PLAYER_BIOS_FILE   = DATA_DIR / "player-bios.json"
OVR_FILE           = DATA_DIR / "ovr-history.json"
CAP_LEVELS_FILE    = DATA_DIR / "cap-levels.json"
PICKS_FILE         = DATA_DIR / "draft-picks.csv"
TRANSACTIONS_FILE  = DATA_DIR / "transactions.json"

PICKS_HEADERS = ["YEAR", "ROUND", "ORIG", "OWNER", "PICK", "PLAYER", "PROTECTED", "SWAP_OWNER", "NOTES"]
_picks_lock = threading.Lock()
_txn_lock   = threading.Lock()
_ovr_lock   = threading.Lock()

VALID_TEAMS = {
    "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

VALID_ROLES = {"admin", "rosters"} | {t.lower() for t in VALID_TEAMS}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nbn.today"],
    allow_methods=["GET", "PUT", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


def load_tokens() -> dict:
    if not TOKENS_FILE.exists():
        return {}
    return json.loads(TOKENS_FILE.read_text())


def save_tokens(tokens: dict):
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def get_token_info(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization[7:]
    tokens = load_tokens()
    if token not in tokens:
        raise HTTPException(status_code=403, detail="Invalid token")
    return tokens[token]


def has_role(info: dict, role: str) -> bool:
    return role in info.get("roles", [])


def require_role(role: str):
    def check(info: dict = Depends(get_token_info)) -> dict:
        if not has_role(info, role) and not has_role(info, "admin"):
            raise HTTPException(status_code=403, detail=f"'{role}' role required")
        return info
    return check


def require_admin(info: dict = Depends(get_token_info)) -> dict:
    if not has_role(info, "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")
    return info


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    text = path.read_text()
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)
    return headers, rows


def write_csv(path: Path, headers: list[str], rows: list[dict]):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(out.getvalue())


def team_path(team: str, kind: str) -> Path:
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = DATA_DIR / f"{team.lower()}-{kind}.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{kind} file not found")
    return path


# ── Roster ──────────────────────────────────────────────────────────────────

@app.get("/api/roster/{team}")
def get_roster(team: str):
    path = team_path(team, "roster")
    headers, rows = read_csv(path)
    return {"headers": headers, "rows": rows}


@app.put("/api/roster/{team}")
def put_roster(team: str, body: dict, info: dict = Depends(require_role("rosters"))):
    path = team_path(team, "roster")
    existing_headers, _ = read_csv(path)
    headers = body.get("headers") or existing_headers
    rows = body.get("rows", [])
    write_csv(path, headers, rows)
    log_write(info, f"PUT roster/{team.upper()} — {len(rows)} rows")
    return {"ok": True}


# ── Picks ────────────────────────────────────────────────────────────────────

def load_picks() -> list[dict]:
    if not PICKS_FILE.exists():
        return []
    _, rows = read_csv(PICKS_FILE)
    return rows


def save_picks(picks: list[dict]):
    write_csv(PICKS_FILE, PICKS_HEADERS, picks)


def pick_to_response(p: dict) -> dict:
    pick      = int(p["PICK"])      if p.get("PICK")      else None
    protected = int(p["PROTECTED"]) if p.get("PROTECTED") else None
    conveys = (pick > protected) if (pick is not None and protected is not None) else None
    swap_owner = p.get("SWAP_OWNER", "").strip() or None
    return {
        "year":          int(p["YEAR"]),
        "round":         int(p["ROUND"]),
        "orig":          p["ORIG"],
        "owner":         p["OWNER"],
        "pick":          pick,
        "player":        p.get("PLAYER", "").strip() or None,
        "protected":     protected,
        "conveys":       conveys,
        "swap_owner":    swap_owner,
        "swap_conveys":  None,   # filled in by enrich_swap_conveys
        "notes":         p.get("NOTES", ""),
    }


def enrich_swap_conveys(picks: list[dict]) -> None:
    # Build (year, round, owner) -> pick number for cross-referencing
    owner_pick = {}
    for p in picks:
        if p["pick"] is not None:
            owner_pick[(p["year"], p["round"], p["owner"])] = p["pick"]
    for p in picks:
        if p["swap_owner"] and p["pick"] is not None:
            swap_num = owner_pick.get((p["year"], p["round"], p["swap_owner"]))
            if swap_num is not None:
                # swap conveys when this pick is better (lower #) than swap_owner's own pick
                p["swap_conveys"] = p["pick"] < swap_num
        # else stays None


@app.get("/api/picks")
def get_all_picks():
    picks = [pick_to_response(p) for p in load_picks()]
    enrich_swap_conveys(picks)
    return picks


@app.get("/api/picks/{team}")
def get_team_picks(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    all_picks = [pick_to_response(p) for p in load_picks()]
    enrich_swap_conveys(all_picks)
    return [p for p in all_picks if p["owner"] == team]


class PickUpsert(BaseModel):
    owner: str
    pick: Optional[int] = None
    player: Optional[str] = None
    protected: Optional[int] = None
    swap_owner: Optional[str] = None
    notes: str = ""


@app.put("/api/picks/{year}/{rnd}/{orig}")
def upsert_pick(
    year: int, rnd: int, orig: str,
    body: PickUpsert,
    info: dict = Depends(require_role("rosters")),
):
    orig = orig.upper()
    if orig not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if rnd not in (1, 2):
        raise HTTPException(status_code=422, detail="Round must be 1 or 2")
    owner = body.owner.upper()
    if owner not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown owner team: {owner}")

    swap_owner = body.swap_owner.upper() if body.swap_owner else ""
    if swap_owner and swap_owner not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown swap_owner team: {swap_owner}")
    updated = {"YEAR": str(year), "ROUND": str(rnd), "ORIG": orig,
               "OWNER":      owner,
               "PICK":       str(body.pick)      if body.pick      is not None else "",
               "PLAYER":     body.player.strip() if body.player    else "",
               "PROTECTED":  str(body.protected) if body.protected is not None else "",
               "SWAP_OWNER": swap_owner,
               "NOTES":      body.notes}

    with _picks_lock:
        picks = load_picks()
        for i, p in enumerate(picks):
            if p.get("YEAR") == str(year) and p.get("ROUND") == str(rnd) and p.get("ORIG", "").upper() == orig:
                old_owner = p.get("OWNER", "")
                picks[i] = updated
                save_picks(picks)
                action = f"traded to {owner}" if old_owner.upper() != owner else "updated"
                log_write(info, f"PUT picks {year} R{rnd} {orig} — {action} pick={body.pick} protected={body.protected} swap_owner={swap_owner or None} notes={body.notes!r}")
                responses = [pick_to_response(p) for p in picks]
                enrich_swap_conveys(responses)
                return next(r for r in responses if r["orig"] == orig and r["year"] == year and r["round"] == rnd)

        # New pick
        picks.append(updated)
        picks.sort(key=lambda p: (p["YEAR"], p["ROUND"], p["ORIG"]))
        save_picks(picks)
        log_write(info, f"PUT picks {year} R{rnd} {orig} (new) — owner={owner}")
        responses = [pick_to_response(p) for p in picks]
        enrich_swap_conveys(responses)
        return next(r for r in responses if r["orig"] == orig and r["year"] == year and r["round"] == rnd)


@app.delete("/api/picks/{year}/{rnd}/{orig}")
def delete_pick(year: int, rnd: int, orig: str, info: dict = Depends(require_role("rosters"))):
    orig = orig.upper()
    with _picks_lock:
        picks = load_picks()
        new_picks = [p for p in picks
                     if not (p.get("YEAR") == str(year) and p.get("ROUND") == str(rnd)
                             and p.get("ORIG", "").upper() == orig)]
        if len(new_picks) == len(picks):
            raise HTTPException(status_code=404, detail="Pick not found")
        save_picks(new_picks)
    log_write(info, f"DELETE picks {year} R{rnd} {orig}")
    return {"ok": True}


# ── Trading Block ────────────────────────────────────────────────────────────

def load_trading_block() -> dict:
    if not TRADING_BLOCK_FILE.exists():
        return {t: [] for t in sorted(VALID_TEAMS)}
    return json.loads(TRADING_BLOCK_FILE.read_text())


def save_trading_block(data: dict):
    TRADING_BLOCK_FILE.write_text(json.dumps(data, indent=2))


class TradingBlockEntry(BaseModel):
    player: str
    notes: str = ""


@app.get("/api/trading-block")
def get_trading_block():
    return load_trading_block()


@app.put("/api/trading-block/{team}")
def put_trading_block(
    team: str,
    body: list[TradingBlockEntry],
    info: dict = Depends(get_token_info),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if not has_role(info, team.lower()) and not has_role(info, "admin"):
        raise HTTPException(status_code=403, detail=f"'{team.lower()}' role required")
    data = load_trading_block()
    data[team] = [e.model_dump() for e in body]
    save_trading_block(data)
    log_write(info, f"PUT trading-block/{team} — {len(body)} players")
    return {"ok": True}


# ── Players ──────────────────────────────────────────────────────────────────

def load_player_bios() -> dict:
    if not PLAYER_BIOS_FILE.exists():
        return {}
    return json.loads(PLAYER_BIOS_FILE.read_text())


def save_player_bios(data: dict):
    PLAYER_BIOS_FILE.write_text(json.dumps(data, indent=2))


VALID_POSITIONS = {"PG", "SG", "SF", "PF", "C"}


class PlayerBio(BaseModel):
    name: str
    pos: list[str] = []
    dob: str = ""
    college: str = ""
    country: str = ""
    draft_year: Optional[int] = None
    draft_round: Optional[int] = None
    draft_pick: Optional[int] = None
    photo_url: str = ""
    height: str = ""
    weight: Optional[int] = None
    wingspan: str = ""
    type: str = ""
    cap_holds: str = ""
    salaries: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}  # season → "YYYY-MM-DD" after which salary is fully guaranteed
    jersey_number: Optional[str] = None


class PlayerCreate(PlayerBio):
    slug: str


@app.get("/api/players")
def get_players():
    return load_player_bios()


@app.post("/api/players")
def create_player(body: PlayerCreate, info: dict = Depends(require_role("rosters"))):
    slug = body.slug.strip().lower()
    if not slug:
        raise HTTPException(status_code=422, detail="slug is required")
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    bios = load_player_bios()
    if slug in bios:
        raise HTTPException(status_code=409, detail="Player already exists")
    data = body.model_dump()
    data.pop("slug")
    bios[slug] = data
    save_player_bios(bios)
    log_write(info, f"POST players — created {slug!r} ({body.name})")
    return {"ok": True, "slug": slug}


@app.put("/api/players/{slug}")
def update_player(slug: str, body: PlayerBio, info: dict = Depends(require_role("rosters"))):
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    bios = load_player_bios()
    bios[slug] = body.model_dump()
    save_player_bios(bios)
    log_write(info, f"PUT players/{slug} ({body.name})")
    return {"ok": True}


class JerseyUpdate(BaseModel):
    jersey_number: Optional[str] = None


@app.put("/api/players/{slug}/jersey")
def update_jersey(slug: str, body: JerseyUpdate, info: dict = Depends(get_token_info)):
    bios = load_player_bios()
    if slug not in bios:
        raise HTTPException(status_code=404, detail="Player not found")

    is_admin   = has_role(info, "admin")
    is_rosters = has_role(info, "rosters")
    team_roles = {r.upper() for r in info.get("roles", []) if r.upper() in VALID_TEAMS}

    if not is_admin and not is_rosters:
        if not team_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        team_map = _build_team_map()
        player_team = team_map.get(slug)
        if not player_team or player_team not in team_roles:
            raise HTTPException(status_code=403, detail="Player is not on your roster")

    if body.jersey_number is not None and not re.fullmatch(r'\d{1,2}', body.jersey_number):
        raise HTTPException(status_code=422, detail="jersey_number must be 1–2 digits")
    bios[slug]["jersey_number"] = body.jersey_number
    save_player_bios(bios)
    log_write(info, f"PUT players/{slug}/jersey — {body.jersey_number}")
    return {"ok": True}


# ── OVR history ──────────────────────────────────────────────────────────────

def load_ovr() -> dict:
    if not OVR_FILE.exists():
        return {}
    return json.loads(OVR_FILE.read_text())


def save_ovr(data: dict):
    OVR_FILE.write_text(json.dumps(data, indent=2))


class OvrEntry(BaseModel):
    date: str
    ovr: int


class OvrBatchEntry(BaseModel):
    slug: str
    date: str
    ovr: int


@app.get("/api/ovr")
def get_ovr():
    return load_ovr()


@app.get("/api/ovr/current")
def get_ovr_current():
    history = load_ovr()
    return {slug: entries[-1]["ovr"] for slug, entries in history.items() if entries}


@app.put("/api/ovr/{slug}")
def put_ovr(slug: str, body: OvrEntry, info: dict = Depends(require_role("rosters"))):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', body.date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if not (50 <= body.ovr <= 99):
        raise HTTPException(status_code=422, detail="ovr must be 50–99")
    with _ovr_lock:
        history = load_ovr()
        entries = history.get(slug, [])
        entries.append({"date": body.date, "ovr": body.ovr})
        entries.sort(key=lambda e: e["date"])
        history[slug] = entries
        save_ovr(history)
    log_write(info, f"PUT ovr/{slug} — {body.date} {body.ovr}")
    return {"ok": True}


@app.put("/api/ovr/{slug}/history")
def put_ovr_history(slug: str, body: list[OvrEntry], info: dict = Depends(require_role("rosters"))):
    bad_dates = [e.date for e in body if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', e.date)]
    if bad_dates:
        raise HTTPException(status_code=422, detail=f"bad date format: {bad_dates}")
    bad_ovr = [e.ovr for e in body if not (50 <= e.ovr <= 99)]
    if bad_ovr:
        raise HTTPException(status_code=422, detail="ovr must be 50–99")
    entries = sorted([{"date": e.date, "ovr": e.ovr} for e in body], key=lambda e: e["date"])
    with _ovr_lock:
        history = load_ovr()
        history[slug] = entries
        save_ovr(history)
    log_write(info, f"PUT ovr/{slug}/history — {len(entries)} entries")
    return {"ok": True}


@app.post("/api/ovr/batch")
def post_ovr_batch(body: list[OvrBatchEntry], info: dict = Depends(require_admin)):
    bad_dates = [e.slug for e in body if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', e.date)]
    if bad_dates:
        raise HTTPException(status_code=422, detail=f"bad date format for: {bad_dates}")
    with _ovr_lock:
        history = load_ovr()
        for entry in body:
            entries = history.get(entry.slug, [])
            entries.append({"date": entry.date, "ovr": entry.ovr})
            entries.sort(key=lambda e: e["date"])
            history[entry.slug] = entries
        save_ovr(history)
    log_write(info, f"POST ovr/batch — {len(body)} entries")
    return {"ok": True, "count": len(body)}


# ── Team map ─────────────────────────────────────────────────────────────────

@app.get("/api/team-map")
def get_team_map():
    result = {}
    for team in VALID_TEAMS:
        path = DATA_DIR / f"{team.lower()}-roster.csv"
        if not path.exists():
            continue
        _, rows = read_csv(path)
        for row in rows:
            slug = row.get("SLUG", "").strip()
            if slug and row.get("TYPE", "").strip() != "dead":
                result[slug] = team
    return result


# ── Cap levels ───────────────────────────────────────────────────────────────

class CapLevel(BaseModel):
    cap: int
    apron1: int
    apron2: int

@app.get("/api/cap-levels")
def get_cap_levels():
    if not CAP_LEVELS_FILE.exists():
        return {}
    return json.loads(CAP_LEVELS_FILE.read_text())

@app.put("/api/cap-levels/{season}")
def put_cap_level(season: str, body: CapLevel, info: dict = Depends(require_role("rosters"))):
    levels = json.loads(CAP_LEVELS_FILE.read_text()) if CAP_LEVELS_FILE.exists() else {}
    levels[season] = body.model_dump()
    CAP_LEVELS_FILE.write_text(json.dumps(levels, indent=2))
    log_write(info, f"PUT cap-levels/{season} — cap={body.cap} apron1={body.apron1} apron2={body.apron2}")
    return levels[season]


# ── Transactions ─────────────────────────────────────────────────────────────

def _load_transactions() -> list[dict]:
    if not TRANSACTIONS_FILE.exists():
        return []
    return json.loads(TRANSACTIONS_FILE.read_text())


def _append_transaction(txn: dict):
    txns = _load_transactions()
    txns.append(txn)
    TRANSACTIONS_FILE.write_text(json.dumps(txns, indent=2))


def _build_team_map() -> dict[str, str]:
    result = {}
    for team in VALID_TEAMS:
        path = DATA_DIR / f"{team.lower()}-roster.csv"
        if not path.exists():
            continue
        _, rows = read_csv(path)
        for row in rows:
            slug = row.get("SLUG", "").strip()
            if slug and row.get("TYPE", "").strip() != "dead":
                result[slug] = team
    return result


class ContractIn(BaseModel):
    type: str = "player"  # "player" or "two-way"
    salaries: dict[str, str] = {}
    cap_holds: str = ""


class SignDetails(BaseModel):
    player: str
    team: str
    contract: ContractIn


class PickIn(BaseModel):
    year: int
    round: int
    orig: str
    pick_number: Optional[int] = None


class PickDetails(BaseModel):
    player: str
    team: str
    pick: PickIn
    contract: ContractIn


class TransactionIn(BaseModel):
    type: str
    date: str
    description: str = ""
    details: dict


class OptionDetails(BaseModel):
    player: str
    decision: str       # "accept" or "decline"
    option_type: str    # "PLAYER_OPT" or "TEAM_OPT"
    year: str           # e.g. "26-27"
    cap_hold_type: str = "UFA"
    cap_hold_amount: Optional[str] = None


class ReleaseDetails(BaseModel):
    player: str


class ConvertTwoWayDetails(BaseModel):
    player: str
    contract: ContractIn


class TradeAsset(BaseModel):
    type: str                        # "player" or "pick"
    slug: Optional[str] = None
    year: Optional[int] = None
    round: Optional[int] = None
    orig: Optional[str] = None
    protection: Optional[int] = None  # if set, replaces PROTECTED on the pick
    swap_with: Optional[str] = None   # if set, replaces SWAP_OWNER on the pick


class TradeTransfer(BaseModel):
    from_team: str
    to_team: str
    assets: list[TradeAsset]


class TradeIn(BaseModel):
    transfers: list[TradeTransfer]
    legality: str = "tbd"


def _parse_cap_holds(s: str) -> list[tuple[str, str]]:
    if not s or not s.strip():
        return []
    result = []
    for part in s.split(','):
        part = part.strip()
        if ':' in part:
            year, typ = part.split(':', 1)
            result.append((year.strip(), typ.strip()))
    return result


def _serialize_cap_holds(holds: list[tuple[str, str]]) -> str:
    return ','.join(f"{year}:{typ}" for year, typ in holds)


def _season_start(s: str) -> int:
    try:
        return int(s.split('-')[0])
    except Exception:
        return 0


def _apply_option(details: OptionDetails, info: dict) -> Optional[str]:
    """Applies an option decision. Returns the player's current team for storage."""
    if details.option_type not in ("PLAYER_OPT", "TEAM_OPT"):
        raise HTTPException(status_code=422, detail="option_type must be PLAYER_OPT or TEAM_OPT")
    if details.decision not in ("accept", "decline"):
        raise HTTPException(status_code=422, detail="decision must be 'accept' or 'decline'")
    if details.cap_hold_type not in ("UFA", "RFA"):
        raise HTTPException(status_code=422, detail="cap_hold_type must be UFA or RFA")

    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    bio = bios[details.player]
    holds = _parse_cap_holds(bio.get("cap_holds", ""))

    if not any(yr == details.year and typ == details.option_type for yr, typ in holds):
        raise HTTPException(
            status_code=422,
            detail=f"Player {details.player!r} has no {details.option_type} for year {details.year!r}",
        )

    if details.decision == "accept":
        # Remove the option entry; salary for that year is now fully guaranteed
        holds = [(yr, typ) for yr, typ in holds
                 if not (yr == details.year and typ == details.option_type)]
        bio["cap_holds"] = _serialize_cap_holds(holds)
    else:
        # Decline: wipe option year + all future years from salaries
        key = _season_start(details.year)
        bio["salaries"] = {yr: amt for yr, amt in bio.get("salaries", {}).items()
                           if _season_start(yr) < key}
        # Write cap hold salary for the option year if provided
        if details.cap_hold_amount:
            bio["salaries"][details.year] = details.cap_hold_amount
        # Remove option entry and any cap_holds at or after the option year
        holds = [(yr, typ) for yr, typ in holds
                 if not (yr == details.year and typ == details.option_type)
                 and _season_start(yr) < key]
        holds.append((details.year, details.cap_hold_type))
        bio["cap_holds"] = _serialize_cap_holds(holds)

    save_player_bios(bios)
    log_write(info, f"TXN option — {details.player} {details.decision} {details.option_type} {details.year}")
    return team


def _apply_release(details: ReleaseDetails, txn_date: str, info: dict) -> tuple[str, dict]:
    """Removes player from roster, converts guaranteed salary to dead cap. Returns (team, dead_cap)."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    # Determine current season start year (NBA season starts in October)
    today = datetime.now(timezone.utc)
    current_season_start = today.year - 1 if today.month < 10 else today.year

    bio = bios[details.player]
    holds = _parse_cap_holds(bio.get("cap_holds", ""))
    holds_map = {yr: typ for yr, typ in holds}
    guaranteed = bio.get("guaranteed", {})
    guarantee_dates = bio.get("guarantee_dates", {})

    dead_cap: dict[str, str] = {}
    for season, salary in bio.get("salaries", {}).items():
        if _season_start(season) < current_season_start:
            continue
        hold_type = holds_map.get(season)
        if hold_type in ("TEAM_OPT", "UFA", "RFA"):
            continue
        if hold_type == "NON_GTD":
            gtd_date = guarantee_dates.get(season)
            if gtd_date and txn_date >= gtd_date:
                # Released on or after guarantee date → full salary is now dead cap
                dead_cap[season] = salary
            elif guaranteed.get(season):
                dead_cap[season] = guaranteed[season]
            # else: $0 dead cap, skip
            continue
        # Fully or partially guaranteed (including PLAYER_OPT years)
        dead_cap[season] = guaranteed.get(season, salary)

    existing_dead = bio.get("dead_cap") or {}
    existing_dead.update(dead_cap)
    bio["dead_cap"] = existing_dead
    bio["salaries"] = {}
    bio["cap_holds"] = ""
    bio["guaranteed"] = {}
    bio["guarantee_dates"] = {}
    bio["type"] = "dead"
    save_player_bios(bios)

    # Remove from roster CSV
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    write_csv(path, headers, rows)

    log_write(info, f"TXN release — {details.player} from {team}")
    return team, dead_cap


def _apply_convert_twoway(details: ConvertTwoWayDetails, info: dict) -> str:
    """Converts a two-way contract to a standard player contract. Returns the player's team."""
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    bio = bios[details.player]
    if bio.get("type") != "two-way":
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on a two-way contract")

    team_map = _build_team_map()
    team = team_map.get(details.player)
    if not team:
        raise HTTPException(status_code=422, detail=f"Player {details.player!r} is not on any roster")

    # Update bio: promote to player, replace contract
    bio["type"] = "player"
    bio["salaries"] = details.contract.salaries
    bio["cap_holds"] = details.contract.cap_holds
    bio.pop("guaranteed", None)
    bio.pop("guarantee_dates", None)
    save_player_bios(bios)

    # Update roster CSV: clear TYPE field (two-way → standard player)
    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if roster_path.exists():
        headers, rows = read_csv(roster_path)
        for row in rows:
            if row.get("SLUG") == details.player:
                row["TYPE"] = ""
                break
        write_csv(roster_path, headers, rows)

    log_write(info, f"TXN convert_twoway — {details.player} ({team})")
    return team


def _apply_sign(details: SignDetails, info: dict):
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team = details.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {team!r}")

    team_map = _build_team_map()
    if details.player in team_map:
        existing = team_map[details.player]
        raise HTTPException(status_code=409, detail=f"Player {details.player!r} is already on {existing}")

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Roster file not found for {team}")
    headers, rows = read_csv(path)
    if "SLUG" not in headers:
        raise HTTPException(status_code=422, detail=f"Roster for {team} uses legacy format; migrate before using transactions")

    row_type = "two-way" if details.contract.type == "two-way" else ""
    rows.append({"SLUG": details.player, "TYPE": row_type})
    write_csv(path, headers, rows)

    bio = bios[details.player]
    # Migrate old dead players whose dead cap was stored in salaries
    if bio.get("type") == "dead" and bio.get("salaries") and not bio.get("dead_cap"):
        bio["dead_cap"] = bio["salaries"]
    bio["salaries"] = details.contract.salaries
    bio["cap_holds"] = details.contract.cap_holds
    bio["type"] = details.contract.type
    save_player_bios(bios)

    log_write(info, f"TXN sign — {details.player} → {team}")


def _apply_pick(details: PickDetails, info: dict):
    bios = load_player_bios()
    if details.player not in bios:
        raise HTTPException(status_code=422, detail=f"Unknown player slug: {details.player!r}")

    team = details.team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown team: {team!r}")

    team_map = _build_team_map()
    if details.player in team_map:
        existing = team_map[details.player]
        raise HTTPException(status_code=409, detail=f"Player {details.player!r} is already on {existing}")

    orig = details.pick.orig.upper()
    if orig not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail=f"Unknown orig team: {orig!r}")
    if details.pick.round not in (1, 2):
        raise HTTPException(status_code=422, detail="Pick round must be 1 or 2")

    path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Roster file not found for {team}")
    headers, rows = read_csv(path)
    if "SLUG" not in headers:
        raise HTTPException(status_code=422, detail=f"Roster for {team} uses legacy format; migrate before using transactions")

    row_type = "two-way" if details.contract.type == "two-way" else ""
    rows.append({"SLUG": details.player, "TYPE": row_type})
    write_csv(path, headers, rows)

    bio = bios[details.player]
    if bio.get("type") == "dead" and bio.get("salaries") and not bio.get("dead_cap"):
        bio["dead_cap"] = bio["salaries"]
    if details.contract.salaries:
        bio["salaries"] = details.contract.salaries
        bio["cap_holds"] = details.contract.cap_holds
    bio["type"] = details.contract.type
    save_player_bios(bios)

    pick = details.pick
    updated_row = {
        "YEAR": str(pick.year), "ROUND": str(pick.round), "ORIG": orig,
        "OWNER": team,
        "PICK": str(pick.pick_number) if pick.pick_number is not None else "",
        "PLAYER": details.player,
        "PROTECTED": "", "SWAP_OWNER": "", "NOTES": "",
    }
    with _picks_lock:
        picks = load_picks()
        for i, p in enumerate(picks):
            if (p.get("YEAR") == str(pick.year) and p.get("ROUND") == str(pick.round)
                    and p.get("ORIG", "").upper() == orig):
                picks[i] = updated_row
                save_picks(picks)
                log_write(info, f"TXN pick — {details.player} → {team} ({pick.year} R{pick.round} {orig})")
                return
        picks.append(updated_row)
        picks.sort(key=lambda p: (p["YEAR"], p["ROUND"], p["ORIG"]))
        save_picks(picks)

    log_write(info, f"TXN pick — {details.player} → {team} ({pick.year} R{pick.round} {orig} — new row)")


def _apply_trade(details: TradeIn, info: dict) -> list[str]:
    if len(details.transfers) < 2:
        raise HTTPException(status_code=422, detail="A trade requires at least 2 transfers")

    # Normalise and validate teams
    for xfer in details.transfers:
        xfer.from_team = xfer.from_team.upper()
        xfer.to_team   = xfer.to_team.upper()
        if xfer.from_team not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"Unknown team: {xfer.from_team!r}")
        if xfer.to_team not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"Unknown team: {xfer.to_team!r}")
        for asset in xfer.assets:
            if asset.type not in ("player", "pick"):
                raise HTTPException(status_code=422, detail=f"Unknown asset type: {asset.type!r}")
            if asset.swap_with:
                asset.swap_with = asset.swap_with.upper()
                if asset.swap_with not in VALID_TEAMS:
                    raise HTTPException(status_code=422, detail=f"Unknown swap_with team: {asset.swap_with!r}")

    # Dedup check — each asset may appear in at most one transfer
    seen_players: set[str] = set()
    seen_picks: set[tuple] = set()
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                if not asset.slug:
                    raise HTTPException(status_code=422, detail="Player asset missing slug")
                if asset.slug in seen_players:
                    raise HTTPException(status_code=422, detail=f"Player {asset.slug!r} appears in multiple transfers")
                seen_players.add(asset.slug)
            else:
                if not all([asset.year, asset.round, asset.orig]):
                    raise HTTPException(status_code=422, detail="Pick asset missing year/round/orig")
                asset.orig = asset.orig.upper()
                key = (asset.year, asset.round, asset.orig)
                if key in seen_picks:
                    raise HTTPException(status_code=422, detail=f"Pick {asset.year} R{asset.round} {asset.orig} appears in multiple transfers")
                seen_picks.add(key)

    # Load data once for validation
    bios     = load_player_bios()
    team_map = _build_team_map()
    picks    = load_picks()
    pick_index = {(int(p["YEAR"]), int(p["ROUND"]), p["ORIG"].upper()): p for p in picks}

    # Validate all assets before touching any file
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                if asset.slug not in bios:
                    raise HTTPException(status_code=422, detail=f"Unknown player: {asset.slug!r}")
                actual_team = team_map.get(asset.slug)
                if actual_team != xfer.from_team:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Player {asset.slug!r} is on {actual_team or 'no team'}, not {xfer.from_team}",
                    )
            else:
                key = (asset.year, asset.round, asset.orig)
                pick_row = pick_index.get(key)
                if not pick_row:
                    raise HTTPException(status_code=422, detail=f"Pick {asset.year} R{asset.round} {asset.orig} not found")
                if pick_row["OWNER"].upper() != xfer.from_team:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Pick {asset.year} R{asset.round} {asset.orig} is owned by {pick_row['OWNER']}, not {xfer.from_team}",
                    )

    # ── Mutations ────────────────────────────────────────────────────────────
    # Roster changes: collect all moves first (list of (from, to, row))
    roster_moves: list[tuple[str, str, dict]] = []
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                path = DATA_DIR / f"{xfer.from_team.lower()}-roster.csv"
                headers, rows = read_csv(path)
                matching = [r for r in rows if r.get("SLUG", "").strip() == asset.slug]
                if matching:
                    roster_moves.append((xfer.from_team, xfer.to_team, headers, matching[0]))

    # Apply roster moves: remove from source, add to dest
    roster_cache: dict[str, tuple[list, list]] = {}
    for from_team, to_team, headers, row in roster_moves:
        if from_team not in roster_cache:
            path = DATA_DIR / f"{from_team.lower()}-roster.csv"
            roster_cache[from_team] = list(read_csv(path))
        if to_team not in roster_cache:
            path = DATA_DIR / f"{to_team.lower()}-roster.csv"
            roster_cache[to_team] = list(read_csv(path))

        src_hdrs, src_rows = roster_cache[from_team]
        dst_hdrs, dst_rows = roster_cache[to_team]
        roster_cache[from_team] = (src_hdrs, [r for r in src_rows if r.get("SLUG", "").strip() != row.get("SLUG")])
        dst_rows.append(row)
        roster_cache[to_team] = (dst_hdrs, dst_rows)

    for team, (hdrs, rows) in roster_cache.items():
        write_csv(DATA_DIR / f"{team.lower()}-roster.csv", hdrs, rows)

    # Pick changes
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "pick":
                for p in picks:
                    if (int(p["YEAR"]) == asset.year and int(p["ROUND"]) == asset.round
                            and p["ORIG"].upper() == asset.orig):
                        p["OWNER"] = xfer.to_team
                        if asset.protection is not None:
                            p["PROTECTED"] = str(asset.protection)
                        if asset.swap_with is not None:
                            p["SWAP_OWNER"] = asset.swap_with
    save_picks(picks)

    teams = sorted({xfer.from_team for xfer in details.transfers} | {xfer.to_team for xfer in details.transfers})
    log_write(info, f"TXN trade — {' / '.join(teams)}: {len(seen_players)} players, {len(seen_picks)} picks")
    return teams


@app.post("/api/transactions")
def create_transaction(body: TransactionIn, info: dict = Depends(require_role("rosters"))):
    try:
        datetime.strptime(body.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date; use YYYY-MM-DD")

    if body.type not in ("sign", "pick", "option", "release", "trade", "convert_twoway"):
        raise HTTPException(status_code=422, detail=f"Unsupported transaction type: {body.type!r}")

    with _txn_lock:
        if body.type == "sign":
            try:
                details = SignDetails(**body.details)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid sign details: {e}")
            _apply_sign(details, info)
            stored_details = details.model_dump()
        elif body.type == "pick":
            try:
                details = PickDetails(**body.details)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid pick details: {e}")
            _apply_pick(details, info)
            stored_details = details.model_dump()
        elif body.type == "option":
            try:
                details = OptionDetails(**body.details)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid option details: {e}")
            team = _apply_option(details, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "release":
            try:
                details = ReleaseDetails(**body.details)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid release details: {e}")
            team, dead_cap = _apply_release(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
            stored_details["dead_cap"] = dead_cap
        elif body.type == "trade":
            try:
                details = TradeIn(**body.details)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid trade details: {e}")
            teams = _apply_trade(details, info)
            stored_details = details.model_dump()
            stored_details["teams"] = teams
        elif body.type == "convert_twoway":
            try:
                details = ConvertTwoWayDetails(**body.details)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid convert_twoway details: {e}")
            team = _apply_convert_twoway(details, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        else:
            raise HTTPException(status_code=422, detail=f"Unsupported transaction type: {body.type!r}")

        txn = {
            "id": secrets.token_hex(8),
            "type": body.type,
            "date": body.date,
            "created_by": info.get("name", "unknown"),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": body.description,
            "details": stored_details,
        }
        _append_transaction(txn)

    return txn


@app.get("/api/transactions")
def list_transactions(
    team: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    txns = _load_transactions()

    if team:
        t_upper = team.upper()
        def _team_match(t):
            d = t.get("details", {})
            if d.get("team", "").upper() == t_upper:
                return True
            return t_upper in [x.upper() for x in d.get("teams", [])]
        txns = [t for t in txns if _team_match(t)]

    if type:
        txns = [t for t in txns if t.get("type") == type]

    txns = sorted(txns, key=lambda t: (t.get("date", ""), t.get("created_at", "")), reverse=True)
    total = len(txns)
    return {"transactions": txns[offset: offset + limit], "total": total}


@app.get("/api/transactions/{txn_id}")
def get_transaction(txn_id: str):
    for t in _load_transactions():
        if t.get("id") == txn_id:
            return t
    raise HTTPException(status_code=404, detail="Transaction not found")


@app.delete("/api/transactions/{txn_id}")
def delete_transaction(txn_id: str, info: dict = Depends(require_role("rosters"))):
    with _txn_lock:
        txns = _load_transactions()
        filtered = [t for t in txns if t.get("id") != txn_id]
        if len(filtered) == len(txns):
            raise HTTPException(status_code=404, detail="Transaction not found")
        TRANSACTIONS_FILE.write_text(json.dumps(filtered, indent=2))
    log_write(info, f"TXN delete — {txn_id}")
    return {"deleted": txn_id}


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/api/me")
def me(info: dict = Depends(get_token_info)):
    return {"name": info.get("name", ""), "roles": info.get("roles", [])}


# ── Token management (admin only) ────────────────────────────────────────────

class TokenCreate(BaseModel):
    name: str
    roles: list[str]


@app.get("/api/tokens")
def list_tokens(_: dict = Depends(require_admin)):
    tokens = load_tokens()
    return [{"token": k, **v} for k, v in tokens.items()]


@app.post("/api/tokens")
def create_token(body: TokenCreate, info: dict = Depends(require_admin)):
    invalid = [r for r in body.roles if r not in VALID_ROLES]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
    token = secrets.token_hex(32)
    tokens = load_tokens()
    tokens[token] = {"name": body.name, "roles": body.roles}
    save_tokens(tokens)
    log_write(info, f"POST tokens — created token for {body.name!r} roles={body.roles}")
    return {"token": token, "name": body.name, "roles": body.roles}


@app.delete("/api/tokens/{token}")
def delete_token(token: str, info: dict = Depends(require_admin)):
    tokens = load_tokens()
    if token not in tokens:
        raise HTTPException(status_code=404, detail="Token not found")
    deleted_name = tokens[token].get("name", "?")
    del tokens[token]
    save_tokens(tokens)
    log_write(info, f"DELETE tokens — removed token for {deleted_name!r}")
    return {"ok": True}

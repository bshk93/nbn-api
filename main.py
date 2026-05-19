import csv
import io
import json
import logging
import secrets
import sys
import threading
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
CAP_LEVELS_FILE    = DATA_DIR / "cap-levels.json"
PICKS_FILE         = DATA_DIR / "draft-picks.csv"

PICKS_HEADERS = ["YEAR", "ROUND", "ORIG", "OWNER", "PICK", "PROTECTED", "SWAP_OWNER", "NOTES"]
_picks_lock = threading.Lock()

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
def delete_pick(year: int, rnd: int, orig: str, info: dict = Depends(require_admin)):
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


class PlayerCreate(PlayerBio):
    slug: str


@app.get("/api/players")
def get_players():
    return load_player_bios()


@app.post("/api/players")
def create_player(body: PlayerCreate, info: dict = Depends(require_admin)):
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

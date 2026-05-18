import csv
import io
import json
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_DIR = Path("/var/lib/nothing-but-stats")
TOKENS_FILE = DATA_DIR / "tokens.json"
TRADING_BLOCK_FILE = DATA_DIR / "trading-block.json"
PLAYER_BIOS_FILE   = DATA_DIR / "player-bios.json"

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
def put_roster(team: str, body: dict, _: dict = Depends(require_role("rosters"))):
    path = team_path(team, "roster")
    existing_headers, _ = read_csv(path)
    headers = body.get("headers") or existing_headers
    write_csv(path, headers, body.get("rows", []))
    return {"ok": True}


# ── Picks ────────────────────────────────────────────────────────────────────

@app.get("/api/picks/{team}")
def get_picks(team: str):
    path = team_path(team, "picks")
    headers, rows = read_csv(path)
    return {"headers": headers, "rows": rows}


@app.put("/api/picks/{team}")
def put_picks(team: str, body: dict, _: dict = Depends(require_role("rosters"))):
    path = team_path(team, "picks")
    headers, _ = read_csv(path)
    write_csv(path, headers, body.get("rows", []))
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


class PlayerCreate(PlayerBio):
    slug: str


@app.get("/api/players")
def get_players():
    return load_player_bios()


@app.post("/api/players")
def create_player(body: PlayerCreate, _: dict = Depends(require_admin)):
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
    return {"ok": True, "slug": slug}


@app.put("/api/players/{slug}")
def update_player(slug: str, body: PlayerBio, _: dict = Depends(require_admin)):
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    bios = load_player_bios()
    bios[slug] = body.model_dump()
    save_player_bios(bios)
    return {"ok": True}


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
def create_token(body: TokenCreate, _: dict = Depends(require_admin)):
    invalid = [r for r in body.roles if r not in VALID_ROLES]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
    token = secrets.token_hex(32)
    tokens = load_tokens()
    tokens[token] = {"name": body.name, "roles": body.roles}
    save_tokens(tokens)
    return {"token": token, "name": body.name, "roles": body.roles}


@app.delete("/api/tokens/{token}")
def delete_token(token: str, _: dict = Depends(require_admin)):
    tokens = load_tokens()
    if token not in tokens:
        raise HTTPException(status_code=404, detail="Token not found")
    del tokens[token]
    save_tokens(tokens)
    return {"ok": True}

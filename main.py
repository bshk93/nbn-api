import base64
import csv
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
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

DATA_DIR  = Path("/var/lib/nothing-but-stats")
RULES_DIR = DATA_DIR / "rules"
PENDING_BOXSCORES_DIR = DATA_DIR / "pending-boxscores"
BUILD_STATUS_FILE  = DATA_DIR / "build-status.json"
BUILD_SCRIPT       = Path("/home/skim/projects/nothing-but-stats/refresh/nbs.sh")
TOKENS_FILE        = DATA_DIR / "tokens.json"
TRADING_BLOCK_FILE = DATA_DIR / "trading-block.json"
PLAYER_BIOS_FILE   = DATA_DIR / "player-bios.json"
OVR_FILE           = DATA_DIR / "ovr-history.json"
CAP_LEVELS_FILE    = DATA_DIR / "cap-levels.json"
ROOKIE_SCALE_FILE  = DATA_DIR / "rookie-scale.json"
PICKS_FILE         = DATA_DIR / "draft-picks.csv"
TRANSACTIONS_FILE  = DATA_DIR / "transactions.json"
TEAM_STATE_FILE    = DATA_DIR / "team-state.json"
AWARDS_CONFIG_FILE = DATA_DIR / "awards-config.json"

PICKS_HEADERS = ["YEAR", "ROUND", "ORIG", "OWNER", "PICK", "PLAYER", "PROTECTED", "SWAP_OWNER", "NOTES"]
_rules_lock    = threading.Lock()
_picks_lock    = threading.Lock()
_txn_lock      = threading.Lock()
_ovr_lock      = threading.Lock()
_state_lock    = threading.Lock()
_deadcap_lock  = threading.Lock()

VALID_TEAMS = {
    "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

VALID_ROLES = {"admin", "rosters", "bod", "curator", "stats"} | {t.lower() for t in VALID_TEAMS}

# Roles that are implicitly granted by holding another role
ROLE_IMPLIES: dict[str, set[str]] = {
    "bod": {"rosters"},
}

CURATOR_FIELDS = {
    "name", "pos", "dob", "college", "country",
    "draft_year", "draft_round", "draft_pick",
    "photo_url", "height", "weight", "wingspan", "jersey_number", "retired",
}

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


def _resolve_token(authorization: Optional[str]) -> Optional[dict]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return load_tokens().get(authorization[7:])


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
    roles = set(info.get("roles", []))
    if role in roles:
        return True
    return any(role in ROLE_IMPLIES.get(r, set()) for r in roles)


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


def require_any_role(*roles: str):
    def check(info: dict = Depends(get_token_info)) -> dict:
        if not has_role(info, "admin") and not any(has_role(info, r) for r in roles):
            raise HTTPException(status_code=403, detail=f"One of {list(roles)} role required")
        return info
    return check


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


# ── Dead Cap ─────────────────────────────────────────────────────────────────

@app.get("/api/deadcap/{team}")
def get_deadcap(team: str):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = DATA_DIR / f"{team.lower()}-deadcap.csv"
    if not path.exists():
        return []
    _, rows = read_csv(path)
    return rows


@app.put("/api/deadcap/{team}")
def put_deadcap(
    team: str,
    body: list[dict],
    info: dict = Depends(require_role("rosters")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    season_keys = sorted({
        k for row in body for k in row
        if k != "SLUG" and re.fullmatch(r'\d{2}-\d{2}', k)
    })
    headers = ["SLUG"] + season_keys
    path = DATA_DIR / f"{team.lower()}-deadcap.csv"
    with _deadcap_lock:
        write_csv(path, headers, body)
    log_write(info, f"PUT deadcap/{team} — {len(body)} rows")
    return {"ok": True}


# ── Rules ────────────────────────────────────────────────────────────────────

RULE_SLUGS = {
    "overview":    "README.md",
    "trades":      "trades.md",
    "free-agency": "free-agency.md",
    "extensions":  "extensions.md",
    "options":     "options.md",
    "releases":    "releases.md",
    "two-way":     "two-way.md",
    "draft":       "draft.md",
}

RULE_LABELS = {
    "overview":    "League Overview",
    "trades":      "Trades",
    "free-agency": "Free Agency",
    "extensions":  "Extensions",
    "options":     "Options",
    "releases":    "Releases / Waivers",
    "two-way":     "Two-Way Contracts",
    "draft":       "Draft Picks",
}


@app.get("/api/rules")
def list_rules():
    return [{"slug": s, "label": RULE_LABELS[s]} for s in RULE_SLUGS]


@app.get("/api/rules/{slug}")
def get_rule(slug: str):
    if slug not in RULE_SLUGS:
        raise HTTPException(status_code=404, detail="Unknown rule slug")
    path = RULES_DIR / RULE_SLUGS[slug]
    if not path.exists():
        return {"slug": slug, "content": ""}
    return {"slug": slug, "content": path.read_text()}


class RuleUpdate(BaseModel):
    content: str


@app.put("/api/rules/{slug}")
def put_rule(slug: str, body: RuleUpdate, info: dict = Depends(require_role("rosters"))):
    if slug not in RULE_SLUGS:
        raise HTTPException(status_code=404, detail="Unknown rule slug")
    path = RULES_DIR / RULE_SLUGS[slug]
    with _rules_lock:
        path.write_text(body.content)
    log_write(info, f"PUT rules/{slug}")
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
    cap_holds: dict[str, str] = {}
    salaries: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}  # season → "YYYY-MM-DD" after which salary is fully guaranteed
    jersey_number: Optional[str] = None
    retired: bool = False


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
def update_player(slug: str, body: PlayerBio, info: dict = Depends(require_any_role("rosters", "curator"))):
    invalid_pos = [p for p in body.pos if p not in VALID_POSITIONS]
    if invalid_pos:
        raise HTTPException(status_code=422, detail=f"Invalid positions: {invalid_pos}")
    bios = load_player_bios()
    if has_role(info, "curator") and not has_role(info, "rosters") and not has_role(info, "admin"):
        existing = bios.get(slug, {})
        update_data = body.model_dump()
        for field in CURATOR_FIELDS:
            existing[field] = update_data[field]
        bios[slug] = existing
    else:
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
def put_ovr(slug: str, body: OvrEntry, info: dict = Depends(require_any_role("rosters", "curator"))):
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
def put_ovr_history(slug: str, body: list[OvrEntry], info: dict = Depends(require_any_role("rosters", "curator"))):
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


# ── Trading block helpers ─────────────────────────────────────────────────────

def _display_name(canonical: str) -> str:
    """Convert 'LAST, FIRST' bio name to title-case 'First Last', mirroring JS displayNameFromBio."""
    if not canonical:
        return ''
    if ',' in canonical:
        last, _, first = canonical.partition(',')
        return f"{first.strip()} {last.strip()}".title()
    return canonical.title()


def _scrub_trading_block(removals: dict[str, set[str]], bios: dict) -> None:
    """Remove players from their source teams' trading block entries after a trade or release.

    removals: {team_abbr: {slug, ...}}
    """
    data = load_trading_block()
    changed = False
    for team, slugs in removals.items():
        display_names = {_display_name(bios[s].get('name', '')) for s in slugs if s in bios}
        display_names.discard('')
        if not display_names:
            continue
        original = data.get(team, [])
        updated = [e for e in original if e.get('player') not in display_names]
        if len(updated) != len(original):
            data[team] = updated
            changed = True
    if changed:
        save_trading_block(data)


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
    ntmle_amount: int = 0
    tmle_amount: int = 0
    bae_amount: int = 0

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
    log_write(info, f"PUT cap-levels/{season} — cap={body.cap} apron1={body.apron1} apron2={body.apron2} ntmle={body.ntmle_amount} tmle={body.tmle_amount} bae={body.bae_amount}")
    return levels[season]


class AwardsSeasonConfig(BaseModel):
    revealed: bool = False


@app.get("/api/awards-config")
def get_awards_config():
    if not AWARDS_CONFIG_FILE.exists():
        return {}
    return json.loads(AWARDS_CONFIG_FILE.read_text())


@app.put("/api/awards-config/{season}")
def put_awards_config(season: str, body: AwardsSeasonConfig, info: dict = Depends(require_admin)):
    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    config[season] = body.model_dump()
    AWARDS_CONFIG_FILE.write_text(json.dumps(config, indent=2))
    log_write(info, f"PUT awards-config/{season} — revealed={body.revealed}")
    return config[season]


# ── Awards ballots ────────────────────────────────────────────────────────────

def _awards_ballots_path(season: str) -> Path:
    if not re.fullmatch(r'\d{2}-\d{2}', season):
        raise HTTPException(status_code=422, detail="season must be YY-YY format")
    return DATA_DIR / f"awards-ballots-{season}.json"


@app.get("/api/awards-ballots/{season}")
def get_awards_ballots(season: str, info: Optional[dict] = None, authorization: Optional[str] = Header(None)):
    path = _awards_ballots_path(season)

    # Check reveal status; unrevealed requires bod/admin
    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    revealed = config.get(season, {}).get("revealed", False)
    if not revealed:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=403, detail="Awards not yet revealed")
        token = authorization[7:]
        tokens = load_tokens()
        token_info = tokens.get(token)
        if not token_info or not (has_role(token_info, "bod") or has_role(token_info, "admin")):
            raise HTTPException(status_code=403, detail="Awards not yet revealed")

    if not path.exists():
        raise HTTPException(status_code=404, detail="No ballot data for this season")
    return json.loads(path.read_text())


@app.put("/api/awards-ballots/{season}")
def put_awards_ballots(season: str, body: dict, info: dict = Depends(require_role("rosters"))):
    path = _awards_ballots_path(season)
    path.write_text(json.dumps(body, indent=2))
    log_write(info, f"PUT awards-ballots/{season}")
    return {"ok": True}


@app.get("/api/awards-ballots/{season}/{team}")
def get_team_ballot(season: str, team: str, authorization: Optional[str] = Header(None)):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = _awards_ballots_path(season)
    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    revealed = config.get(season, {}).get("revealed", False)
    if not revealed:
        info = _resolve_token(authorization)
        if not info:
            raise HTTPException(status_code=403, detail="Unauthorized")
        roles = set(info.get("roles", []))
        if not (has_role(info, "admin") or has_role(info, "bod") or team.lower() in roles):
            raise HTTPException(status_code=403, detail="Unauthorized")
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get(team, {})


@app.put("/api/awards-ballots/{season}/{team}")
def put_team_ballot(season: str, team: str, body: dict, authorization: Optional[str] = Header(None)):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    info = _resolve_token(authorization)
    if not info:
        raise HTTPException(status_code=403, detail="Unauthorized")
    roles = set(info.get("roles", []))
    if not (has_role(info, "admin") or has_role(info, "rosters") or team.lower() in roles):
        raise HTTPException(status_code=403, detail="Unauthorized")
    config = json.loads(AWARDS_CONFIG_FILE.read_text()) if AWARDS_CONFIG_FILE.exists() else {}
    if config.get(season, {}).get("revealed", False):
        raise HTTPException(status_code=403, detail="Cannot edit a revealed season")
    path = _awards_ballots_path(season)
    ballots = json.loads(path.read_text()) if path.exists() else {}
    ballots[team] = body
    path.write_text(json.dumps(ballots, indent=2))
    log_write(info, f"PUT awards-ballots/{season}/{team}")
    return {"ok": True, "team": team}


@app.delete("/api/awards-ballots/{season}/{team}")
def delete_team_ballot(season: str, team: str, info: dict = Depends(require_role("rosters"))):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    path = _awards_ballots_path(season)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No ballot data for this season")
    ballots = json.loads(path.read_text())
    if team not in ballots:
        raise HTTPException(status_code=404, detail=f"No ballot for {team}")
    del ballots[team]
    path.write_text(json.dumps(ballots, indent=2))
    log_write(info, f"DELETE awards-ballots/{season}/{team}")
    return {"ok": True}


# ── Rookie scale ─────────────────────────────────────────────────────────────

@app.get("/api/rookie-scale")
def get_rookie_scale():
    if not ROOKIE_SCALE_FILE.exists():
        return {}
    return json.loads(ROOKIE_SCALE_FILE.read_text())

@app.put("/api/rookie-scale/{year}")
def put_rookie_scale(year: int, body: list[list[int]], info: dict = Depends(require_role("rosters"))):
    scale = json.loads(ROOKIE_SCALE_FILE.read_text()) if ROOKIE_SCALE_FILE.exists() else {}
    scale[str(year)] = body
    ROOKIE_SCALE_FILE.write_text(json.dumps(scale, indent=2))
    log_write(info, f"PUT rookie-scale/{year} — {len(body)} picks")
    return scale[str(year)]


# ── Team state ────────────────────────────────────────────────────────────────

DEFAULT_SEASON_STATE: dict = {
    "hard_cap": None, "hard_cap_reason": "", "mle_used": 0, "bae_used": False,
}

CAP_RANK = {None: 0, "first_apron": 1, "second_apron": 2}


def load_team_state() -> dict:
    if not TEAM_STATE_FILE.exists():
        return {}
    return json.loads(TEAM_STATE_FILE.read_text())


def save_team_state(data: dict):
    TEAM_STATE_FILE.write_text(json.dumps(data, indent=2))


def get_season_state(state: dict, team: str, season: str) -> dict:
    return state.get(team, {}).get(season, dict(DEFAULT_SEASON_STATE))


def _bae_available(state: dict, team: str, cur_season: str) -> bool:
    seasons = sorted(state.get(team, {}).keys())
    prior = [s for s in seasons if s < cur_season]
    if not prior:
        return True
    return not state[team][prior[-1]].get("bae_used", False)


def _maybe_set_hard_cap(ts: dict, new_cap: str, reason: str):
    if CAP_RANK.get(new_cap, 0) > CAP_RANK.get(ts.get("hard_cap"), 0):
        ts["hard_cap"] = new_cap
        ts["hard_cap_reason"] = reason


class TeamSeasonState(BaseModel):
    hard_cap: Optional[str] = None
    hard_cap_reason: str = ""
    mle_used: int = 0
    bae_used: bool = False


@app.get("/api/team-state")
def get_all_team_state():
    state = load_team_state()
    cur = _current_season_str()
    return {
        team: {
            "seasons": state.get(team, {}),
            "current": get_season_state(state, team, cur),
            "bae_available": _bae_available(state, team, cur),
        }
        for team in sorted(VALID_TEAMS)
    }


@app.get("/api/team-state/{team}")
def get_team_state(team: str, season: Optional[str] = None):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    state = load_team_state()
    cur = season or _current_season_str()
    return {
        "season": cur,
        **get_season_state(state, team, cur),
        "bae_available": _bae_available(state, team, cur),
    }


@app.put("/api/team-state/{team}")
def put_team_state(
    team: str,
    body: TeamSeasonState,
    season: Optional[str] = None,
    info: dict = Depends(require_role("rosters")),
):
    team = team.upper()
    if team not in VALID_TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team")
    if body.hard_cap not in (None, "first_apron", "second_apron"):
        raise HTTPException(status_code=422, detail="hard_cap must be null, 'first_apron', or 'second_apron'")
    cur = season or _current_season_str()
    with _state_lock:
        state = load_team_state()
        if team not in state:
            state[team] = {}
        state[team][cur] = body.model_dump()
        save_team_state(state)
    log_write(info, f"PUT team-state/{team}/{cur} — hard_cap={body.hard_cap} mle_used={body.mle_used} bae_used={body.bae_used}")
    return {"season": cur, **state[team][cur], "bae_available": _bae_available(state, team, cur)}


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
    cap_holds: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}


class SignDetails(BaseModel):
    player: str
    team: str
    contract: ContractIn
    signing_method: Optional[str] = None   # cap_space | bird_rights | ntmle | tmle | room_exception | bae | sign_and_trade
    bird_rights_type: Optional[str] = None  # QVFA | EQVFA | Non-QVFA


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
    force: bool = False   # override warning-level failures


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



def _season_start(s: str) -> int:
    try:
        return int(s.split('-')[0])
    except Exception:
        return 0


def _current_season_str() -> str:
    now = datetime.now(timezone.utc)
    y = now.year % 100
    if now.month < 7:
        return f"{y-1:02d}-{y:02d}"
    return f"{y:02d}-{(y+1) % 100:02d}"


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
    holds = bio.get("cap_holds") or {}

    if holds.get(details.year) != details.option_type:
        raise HTTPException(
            status_code=422,
            detail=f"Player {details.player!r} has no {details.option_type} for year {details.year!r}",
        )

    if details.decision == "accept":
        # Remove the option entry; salary for that year is now fully guaranteed
        bio["cap_holds"] = {yr: typ for yr, typ in holds.items()
                            if yr != details.year}
    else:
        # Decline: wipe option year + all future years from salaries
        key = _season_start(details.year)
        bio["salaries"] = {yr: amt for yr, amt in bio.get("salaries", {}).items()
                           if _season_start(yr) < key}
        # Write cap hold salary for the option year if provided
        if details.cap_hold_amount:
            bio["salaries"][details.year] = details.cap_hold_amount
        # Remove option entry and any cap_holds at or after the option year, then add new hold
        bio["cap_holds"] = {yr: typ for yr, typ in holds.items()
                            if yr != details.year and _season_start(yr) < key}
        bio["cap_holds"][details.year] = details.cap_hold_type

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

    cur_season = _current_season_str()

    bio = bios[details.player]
    holds_map = bio.get("cap_holds") or {}
    guaranteed = bio.get("guaranteed", {})
    guarantee_dates = bio.get("guarantee_dates", {})

    dead_cap: dict[str, str] = {}
    for season, salary in bio.get("salaries", {}).items():
        if season < cur_season:
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

    bio["salaries"] = {k: v for k, v in bio["salaries"].items() if k < cur_season}
    bio["cap_holds"] = {}
    bio["guaranteed"] = {}
    bio["guarantee_dates"] = {}
    bio["type"] = ""
    save_player_bios(bios)

    # Remove from roster CSV
    path = DATA_DIR / f"{team.lower()}-roster.csv"
    headers, rows = read_csv(path)
    rows = [r for r in rows if r.get("SLUG", "").strip() != details.player]
    write_csv(path, headers, rows)

    # Write dead cap to team's deadcap CSV
    if dead_cap:
        dc_path = DATA_DIR / f"{team.lower()}-deadcap.csv"
        with _deadcap_lock:
            if dc_path.exists():
                _, dc_rows = read_csv(dc_path)
            else:
                dc_rows = []
            dc_rows = [r for r in dc_rows if r.get("SLUG", "").strip() != details.player]
            dc_rows.append({"SLUG": details.player, **dead_cap})
            season_keys = sorted({
                k for row in dc_rows for k in row
                if k != "SLUG" and re.fullmatch(r'\d{2}-\d{2}', k)
            })
            write_csv(dc_path, ["SLUG"] + season_keys, dc_rows)

    _scrub_trading_block({team: {details.player}}, bios)
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
    cur_season = _current_season_str()
    past = {k: v for k, v in bio.get("salaries", {}).items() if k < cur_season}
    past_gtd = {k: v for k, v in bio.get("guaranteed", {}).items() if k < cur_season}
    past_gtd_dates = {k: v for k, v in bio.get("guarantee_dates", {}).items() if k < cur_season}
    bio["salaries"] = {**past, **details.contract.salaries}
    bio["cap_holds"] = details.contract.cap_holds
    bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
    bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
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
        bio["salaries"] = {}
    cur_season = _current_season_str()
    past = {k: v for k, v in bio.get("salaries", {}).items() if k < cur_season}
    past_gtd = {k: v for k, v in bio.get("guaranteed", {}).items() if k < cur_season}
    past_gtd_dates = {k: v for k, v in bio.get("guarantee_dates", {}).items() if k < cur_season}
    bio["salaries"] = {**past, **details.contract.salaries}
    bio["cap_holds"] = details.contract.cap_holds
    bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
    bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
    bio["type"] = details.contract.type
    save_player_bios(bios)

    if details.signing_method in ("ntmle", "tmle", "room_exception", "bae"):
        cur = _current_season_str()
        with _state_lock:
            state = load_team_state()
            if team not in state:
                state[team] = {}
            ts = state[team].get(cur, dict(DEFAULT_SEASON_STATE))

            if details.signing_method in ("ntmle", "tmle", "room_exception"):
                yr1 = 0
                for yr in sorted(details.contract.salaries.keys()):
                    if yr >= cur:
                        yr1 = int(str(details.contract.salaries[yr])
                                  .replace("$", "").replace(",", "").strip() or 0)
                        break
                ts["mle_used"] = ts.get("mle_used", 0) + yr1
                if details.signing_method == "ntmle":
                    _maybe_set_hard_cap(ts, "first_apron", f"NTMLE signing: {details.player}")
                elif details.signing_method == "tmle":
                    _maybe_set_hard_cap(ts, "second_apron", f"TMLE signing: {details.player}")
            elif details.signing_method == "bae":
                ts["bae_used"] = True
                _maybe_set_hard_cap(ts, "first_apron", f"BAE signing: {details.player}")

            state[team][cur] = ts
            save_team_state(state)

    log_write(info, f"TXN sign — {details.player} → {team} [{details.signing_method or 'cap_space'}]")


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
        bio["salaries"] = {}
    if details.contract.salaries:
        cur_season = _current_season_str()
        past = {k: v for k, v in bio.get("salaries", {}).items() if k < cur_season}
        past_gtd = {k: v for k, v in bio.get("guaranteed", {}).items() if k < cur_season}
        past_gtd_dates = {k: v for k, v in bio.get("guarantee_dates", {}).items() if k < cur_season}
        bio["salaries"] = {**past, **details.contract.salaries}
        bio["cap_holds"] = details.contract.cap_holds
        bio["guaranteed"] = {**past_gtd, **details.contract.guaranteed}
        bio["guarantee_dates"] = {**past_gtd_dates, **details.contract.guarantee_dates}
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

    trade_removals: dict[str, set[str]] = {}
    for xfer in details.transfers:
        for asset in xfer.assets:
            if asset.type == "player":
                trade_removals.setdefault(xfer.from_team, set()).add(asset.slug)
    _scrub_trading_block(trade_removals, bios)

    teams = sorted({xfer.from_team for xfer in details.transfers} | {xfer.to_team for xfer in details.transfers})
    log_write(info, f"TXN trade — {' / '.join(teams)}: {len(seen_players)} players, {len(seen_picks)} picks")
    return teams


# ── Transaction validation ────────────────────────────────────────────────────
# Each _validate_* function receives the parsed details object and returns a
# list of CheckResult. level="error" → hard block. level="warning" → soft,
# can be overridden with force=True on the request.

def _validate_sign(details: SignDetails) -> list[CheckResult]:
    checks = []
    # TODO: roster size, cap space / exception eligibility, contract length limits,
    #       apron restrictions, Bird Rights eligibility, sign-and-trade rules
    return checks


def _validate_release(details: ReleaseDetails) -> list[CheckResult]:
    checks = []
    # TODO: player exists on a roster, release window rules
    return checks


def _validate_trade(details: TradeIn) -> list[CheckResult]:
    checks = []
    # TODO: salary matching tiers, Stepien Rule, 7-year advance limit,
    #       apron restrictions, roster size after trade, aggregation rules
    return checks


def _validate_option(details: OptionDetails) -> list[CheckResult]:
    checks = []
    # TODO: option year exists on contract, decision window
    return checks


def _validate_pick(details: PickDetails) -> list[CheckResult]:
    checks = []
    # TODO: player not already on a roster, pick exists and is owned by team,
    #       rookie scale contract limits
    return checks


def _validate_convert_twoway(details: ConvertTwoWayDetails) -> list[CheckResult]:
    checks = []
    # TODO: player is on a two-way, roster size after conversion,
    #       cap space / exception for the new contract
    return checks


_VALIDATORS = {
    "sign":           _validate_sign,
    "release":        _validate_release,
    "trade":          _validate_trade,
    "option":         _validate_option,
    "pick":           _validate_pick,
    "convert_twoway": _validate_convert_twoway,
}


def _run_validation(txn_type: str, details) -> list[CheckResult]:
    fn = _VALIDATORS.get(txn_type)
    return fn(details) if fn else []


@app.post("/api/transactions")
def create_transaction(body: TransactionIn, info: dict = Depends(require_role("rosters"))):
    try:
        datetime.strptime(body.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date; use YYYY-MM-DD")

    if body.type not in ("sign", "pick", "option", "release", "trade", "convert_twoway"):
        raise HTTPException(status_code=422, detail=f"Unsupported transaction type: {body.type!r}")

    # ── Parse details early so validators get typed objects ───────────────────
    _detail_models = {
        "sign":           (SignDetails,           "Invalid sign details"),
        "pick":           (PickDetails,           "Invalid pick details"),
        "option":         (OptionDetails,         "Invalid option details"),
        "release":        (ReleaseDetails,        "Invalid release details"),
        "trade":          (TradeIn,               "Invalid trade details"),
        "convert_twoway": (ConvertTwoWayDetails,  "Invalid convert_twoway details"),
    }
    model_cls, err_prefix = _detail_models[body.type]
    try:
        parsed_details = model_cls(**body.details)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"{err_prefix}: {e}")

    # ── Run rubric checks ─────────────────────────────────────────────────────
    checks = _run_validation(body.type, parsed_details)
    errors   = [c for c in checks if not c.passed and c.level == "error"]
    warnings = [c for c in checks if not c.passed and c.level == "warning"]

    if errors or (warnings and not body.force):
        raise HTTPException(status_code=422, detail={
            "validation": True,
            "checks": [c.model_dump() for c in checks],
            "can_force": len(errors) == 0,
        })

    with _txn_lock:
        details = parsed_details  # already parsed above
        if body.type == "sign":
            _apply_sign(details, info)
            stored_details = details.model_dump()
        elif body.type == "pick":
            _apply_pick(details, info)
            stored_details = details.model_dump()
        elif body.type == "option":
            team = _apply_option(details, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
        elif body.type == "release":
            team, dead_cap = _apply_release(details, body.date, info)
            stored_details = details.model_dump()
            stored_details["team"] = team
            stored_details["dead_cap"] = dead_cap
        elif body.type == "trade":
            teams = _apply_trade(details, info)
            stored_details = details.model_dump()
            stored_details["teams"] = teams
        elif body.type == "convert_twoway":
            team = _apply_convert_twoway(details, info)
            stored_details = details.model_dump()
            stored_details["team"] = team

        if body.force and warnings:
            stored_details["_forced_warnings"] = [c.check for c in warnings]

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
    player: Optional[str] = None,
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

    if player:
        def _player_match(t):
            d = t.get("details", {})
            if d.get("player") == player:
                return True
            for tr in d.get("transfers", []):
                for asset in tr.get("assets", []):
                    if asset.get("type") == "player" and asset.get("slug") == player:
                        return True
            return False
        txns = [t for t in txns if _player_match(t)]

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


# ── Trade validation ─────────────────────────────────────────────────────────

ROSTER_MAX = 15


class TradeValidateInput(BaseModel):
    transfers: list[TradeTransfer]
    is_sign_and_trade: bool = False
    exceptions: dict[str, Optional[str]] = {}  # team → exception being used


class CheckResult(BaseModel):
    check: str
    passed: bool
    level: str = "error"   # "error" blocks; "warning" allows force-through
    message: str


class TradeValidationResult(BaseModel):
    legal: bool
    checks: list[CheckResult]
    fact_sheet: dict


def _build_trade_context(trade: TradeValidateInput) -> dict:
    bios = load_player_bios()

    players_out: dict[str, list[str]] = {}
    players_in: dict[str, list[str]] = {}
    for xfer in trade.transfers:
        from_team = xfer.from_team.upper()
        to_team = xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type == "player" and asset.slug:
                players_out.setdefault(from_team, []).append(asset.slug)
                players_in.setdefault(to_team, []).append(asset.slug)

    teams: dict[str, dict] = {}
    for team in set(players_out) | set(players_in):
        path = DATA_DIR / f"{team.lower()}-roster.csv"
        rows = read_csv(path)[1] if path.exists() else []

        non_standard_on_roster = {
            r["SLUG"].strip() for r in rows
            if r.get("SLUG", "").strip() and r.get("TYPE", "").strip() in ("two-way", "dead")
        }
        count_before = sum(
            1 for r in rows
            if r.get("SLUG", "").strip() and r.get("TYPE", "").strip() not in ("two-way", "dead")
        )

        out_slugs = players_out.get(team, [])
        in_slugs = players_in.get(team, [])

        out_standard = sum(1 for s in out_slugs if s not in non_standard_on_roster)
        in_standard = sum(
            1 for s in in_slugs
            if bios.get(s, {}).get("type", "") not in ("two-way", "dead")
        )

        teams[team] = {
            "team": team,
            "standard_count_before": count_before,
            "standard_count_after": count_before - out_standard + in_standard,
            "players_out": out_slugs,
            "players_in": in_slugs,
        }

    return {
        "teams": teams,
        "is_sign_and_trade": trade.is_sign_and_trade,
        "exceptions": trade.exceptions,
    }


def _check_roster_size(ctx: dict) -> CheckResult:
    violations = [
        f"{tc['team']}: would have {tc['standard_count_after']} players (max {ROSTER_MAX})"
        for tc in ctx["teams"].values()
        if tc["standard_count_after"] > ROSTER_MAX
    ]
    if violations:
        return CheckResult(
            check="roster_size",
            passed=False,
            message="Roster limit exceeded — release a player first: " + "; ".join(violations),
        )
    return CheckResult(
        check="roster_size",
        passed=True,
        message=f"All rosters within the {ROSTER_MAX}-player limit",
    )


_TRADE_CHECKS = [
    _check_roster_size,
]


@app.post("/api/validate/trade")
def validate_trade(body: TradeValidateInput):
    ctx = _build_trade_context(body)
    results = [fn(ctx) for fn in _TRADE_CHECKS]
    return TradeValidationResult(
        legal=all(r.passed for r in results),
        checks=results,
        fact_sheet=ctx,
    )


# ── Trivia scores ────────────────────────────────────────────────────────────

TRIVIA_SCORES_PATH = DATA_DIR / "trivia-scores.json"


def load_trivia_scores() -> list:
    if not TRIVIA_SCORES_PATH.exists():
        return []
    return json.loads(TRIVIA_SCORES_PATH.read_text())


def save_trivia_scores(scores: list):
    TRIVIA_SCORES_PATH.write_text(json.dumps(scores, indent=2))


class TriviaScoreSubmit(BaseModel):
    score: int


@app.get("/api/trivia/scores")
def get_trivia_scores():
    scores = load_trivia_scores()
    best: dict[str, dict] = {}
    for s in scores:
        name = s.get("name", "?")
        if name not in best or s["score"] > best[name]["score"]:
            best[name] = s
    return sorted(best.values(), key=lambda s: s["score"], reverse=True)[:20]


@app.post("/api/trivia/scores")
def post_trivia_score(body: TriviaScoreSubmit, info: dict = Depends(get_token_info)):
    if not info.get("name"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if body.score < 0:
        raise HTTPException(status_code=422, detail="Score must be non-negative")
    scores = load_trivia_scores()
    scores.append({
        "name": info["name"],
        "score": body.score,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    save_trivia_scores(scores)
    log_write(info, f"POST trivia/scores — score={body.score}")
    return {"ok": True}


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/api/me")
def me(info: dict = Depends(get_token_info)):
    return {"name": info.get("name", ""), "roles": info.get("roles", [])}


# ── Token management (admin only) ────────────────────────────────────────────

# ── Boxscore ─────────────────────────────────────────────────────────────────

REG_ALLSTATS_HEADERS = [
    "TEAM","DATE","OPP","OPP_RAW","PLAYER","M","P","R","OR","DR","A","S","B","TO",
    "FGM","FGA","FGPCT","3PM","3PA","3PPCT","FTM","FTA","FTPCT","PF","OPP_TEAM",
    "TD","BOX"," ","TEAM_PTS","OPP_TEAM_PTS","AGE","WL","gametype",
]
PLAYOFF_ALLSTATS_HEADERS = [
    "TEAM","DATE","OPP","OPP_RAW","PLAYER","M","P","R","OR","DR","A","S","B","TO",
    "FGM","FGA","FGPCT","3PM","3PA","3PPCT","FTM","FTA","FTPCT","PF","OPP_TEAM",
    "TD","BOX"," ","TEAM_PTS","OPP_TEAM_PTS","AGE","WL","GAME","ROUND","gametype",
]


def allstats_path(season: str, game_type: str) -> Path:
    if game_type.upper() == "PLAYOFF":
        year = season.split("-")[-1]
        return DATA_DIR / f"allstats-playoffs-{year}.csv"
    return DATA_DIR / f"allstats-{season}.csv"


def _safe_pct(made: int, att: int) -> str:
    return str(made / att) if att > 0 else "NA"


def _triple_double(pts, reb, ast, stl, blk) -> str:
    return "1" if sum(1 for v in [pts, reb, ast, stl, blk] if v >= 10) >= 3 else "0"


def _calc_age(dob_str: str, game_date_str: str) -> str:
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d")
        gd = datetime.strptime(game_date_str, "%Y-%m-%d")
        return f"{(gd - dob).days / 365.25:.5f}"
    except Exception:
        return "NA"


def _build_allstats_row(
    team: str, opp: str, date: str, player_name: str, slug: str,
    min_: int, pts: int, reb: int, oreb: int, dreb: int,
    ast: int, stl: int, blk: int, tov: int, pf: int,
    fgm: int, fga: int, tpm: int, tpa: int, ftm: int, fta: int,
    team_pts: int, opp_pts: int, wl: str, game_type: str,
    game_num: Optional[int], round_num: Optional[int],
    bios: dict,
) -> dict:
    age = _calc_age(bios.get(slug, {}).get("dob", ""), date)
    row = {
        "TEAM": team.upper(),
        "DATE": date,
        "OPP": opp.upper(),
        "OPP_RAW": opp.upper(),
        "PLAYER": player_name,
        "M": str(min_),
        "P": str(pts),
        "R": str(reb),
        "OR": str(oreb),
        "DR": str(dreb),
        "A": str(ast),
        "S": str(stl),
        "B": str(blk),
        "TO": str(tov),
        "FGM": str(fgm),
        "FGA": str(fga),
        "FGPCT": _safe_pct(fgm, fga),
        "3PM": str(tpm),
        "3PA": str(tpa),
        "3PPCT": _safe_pct(tpm, tpa),
        "FTM": str(ftm),
        "FTA": str(fta),
        "FTPCT": _safe_pct(ftm, fta),
        "PF": str(pf),
        "OPP_TEAM": opp.upper(),
        "TD": _triple_double(pts, reb, ast, stl, blk),
        "BOX": "0",
        " ": "",
        "TEAM_PTS": str(team_pts),
        "OPP_TEAM_PTS": str(opp_pts),
        "AGE": age,
        "WL": wl,
    }
    if game_type.upper() == "PLAYOFF":
        row["GAME"] = str(game_num) if game_num is not None else ""
        row["ROUND"] = str(round_num) if round_num is not None else ""
    row["gametype"] = "PLAYOFF" if game_type.upper() == "PLAYOFF" else "REG"
    return row


def _parse_one_screenshot(
    image_bytes: bytes, media_type: str,
    team: str, opp: str, date: str,
    roster_context: list[dict],
) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured on server")

    client = anthropic.Anthropic(api_key=api_key)
    roster_json = json.dumps(roster_context, indent=2)
    prompt = f"""You are parsing an NBA 2K simulation game box score screenshot.
This screenshot shows the {team} box score from a game against {opp} on {date}.

Here are the known {team} players with their slugs:
{roster_json}

Extract ALL players who played (skip DNP). For minutes shown as MM:SS, convert to integer minutes (round down).
Match each player name to the closest known player above. Set confidence:
- "high": obvious match
- "medium": somewhat uncertain
- "low": best guess only

Return ONLY valid JSON — no markdown, no explanation:
{{
  "team_pts": <integer or null if not visible>,
  "opp_pts": <integer or null if not visible>,
  "rows": [
    {{
      "player": "LAST, FIRST",
      "slug": "slug-from-roster",
      "confidence": "high|medium|low",
      "min": <integer>,
      "pts": <integer>,
      "reb": <integer total>,
      "oreb": <integer, 0 if not shown>,
      "dreb": <integer, equals reb if not split>,
      "ast": <integer>,
      "stl": <integer>,
      "blk": <integer>,
      "tov": <integer>,
      "pf": <integer>,
      "fgm": <integer>,
      "fga": <integer>,
      "tpm": <integer 3PM>,
      "tpa": <integer 3PA>,
      "ftm": <integer>,
      "fta": <integer>,
      "concern": "<string if uncertain, else null>"
    }}
  ],
  "concerns": ["<any general readability issues>"]
}}"""

    img_b64 = base64.standard_b64encode(image_bytes).decode()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _load_roster_context(team: str, bios: dict) -> list[dict]:
    roster_path = DATA_DIR / f"{team.lower()}-roster.csv"
    if not roster_path.exists():
        return []
    _, rows = read_csv(roster_path)
    result = []
    for r in rows:
        slug = r.get("SLUG") or r.get("PLAYER", "")
        if not slug:
            continue
        bio = bios.get(slug, {})
        name = bio.get("name", slug)
        result.append({"slug": slug, "name": name})
    return result


@app.post("/api/boxscore/parse")
async def parse_boxscore(
    home_image: UploadFile = File(...),
    away_image: UploadFile = File(...),
    date: str = Form(...),
    home_team: str = Form(...),
    away_team: str = Form(...),
    season: str = Form(...),
    game_type: str = Form(...),
    game_num: Optional[int] = Form(None),
    round_num: Optional[int] = Form(None),
    info: dict = Depends(require_any_role("rosters", "stats")),
):
    home_team = home_team.upper()
    away_team = away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}
    home_roster = _load_roster_context(home_team, bios)
    away_roster = _load_roster_context(away_team, bios)

    home_bytes = await home_image.read()
    away_bytes = await away_image.read()
    home_mt = home_image.content_type or "image/png"
    away_mt = away_image.content_type or "image/png"

    home_result = _parse_one_screenshot(home_bytes, home_mt, home_team, away_team, date, home_roster)
    away_result = _parse_one_screenshot(away_bytes, away_mt, away_team, home_team, date, away_roster)

    # Reconcile scores from both screenshots
    home_pts = home_result.get("team_pts") or away_result.get("opp_pts")
    away_pts = away_result.get("team_pts") or home_result.get("opp_pts")

    concerns = home_result.get("concerns", []) + away_result.get("concerns", [])
    logger.info("[%s] POST boxscore/parse — %s vs %s on %s", info.get("name"), home_team, away_team, date)
    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_pts": home_pts,
        "away_pts": away_pts,
        "home_rows": home_result.get("rows", []),
        "away_rows": away_result.get("rows", []),
        "concerns": concerns,
    }


class BoxscorePlayerRow(BaseModel):
    player: str
    slug: str
    min: int
    pts: int
    reb: int
    oreb: int
    dreb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int
    fgm: int
    fga: int
    tpm: int
    tpa: int
    ftm: int
    fta: int


class BoxscoreCommitRequest(BaseModel):
    date: str
    home_team: str
    away_team: str
    season: str
    game_type: str
    home_pts: int
    away_pts: int
    game_num: Optional[int] = None
    round_num: Optional[int] = None
    home_rows: list[BoxscorePlayerRow]
    away_rows: list[BoxscorePlayerRow]


@app.post("/api/boxscore/commit")
def commit_boxscore(body: BoxscoreCommitRequest, info: dict = Depends(require_any_role("rosters", "stats"))):
    home_team = body.home_team.upper()
    away_team = body.away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    path = allstats_path(body.season, body.game_type)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Allstats file not found: {path.name}")

    headers, existing = read_csv(path)
    is_playoff = body.game_type.upper() == "PLAYOFF"
    expected_headers = PLAYOFF_ALLSTATS_HEADERS if is_playoff else REG_ALLSTATS_HEADERS

    bios = json.loads(PLAYER_BIOS_FILE.read_text()) if PLAYER_BIOS_FILE.exists() else {}

    new_rows = []
    for r in body.home_rows:
        new_rows.append(_build_allstats_row(
            home_team, away_team, body.date, r.player, r.slug,
            r.min, r.pts, r.reb, r.oreb, r.dreb,
            r.ast, r.stl, r.blk, r.tov, r.pf,
            r.fgm, r.fga, r.tpm, r.tpa, r.ftm, r.fta,
            body.home_pts, body.away_pts,
            "W" if body.home_pts > body.away_pts else "L",
            body.game_type, body.game_num, body.round_num, bios,
        ))
    for r in body.away_rows:
        new_rows.append(_build_allstats_row(
            away_team, home_team, body.date, r.player, r.slug,
            r.min, r.pts, r.reb, r.oreb, r.dreb,
            r.ast, r.stl, r.blk, r.tov, r.pf,
            r.fgm, r.fga, r.tpm, r.tpa, r.ftm, r.fta,
            body.away_pts, body.home_pts,
            "W" if body.away_pts > body.home_pts else "L",
            body.game_type, body.game_num, body.round_num, bios,
        ))

    write_csv(path, expected_headers, existing + new_rows)
    logger.info("[%s] POST boxscore/commit — %s vs %s on %s (%d rows)", info.get("name"), home_team, away_team, body.date, len(new_rows))
    building = _trigger_build()
    return {"ok": True, "rows_added": len(new_rows), "building": building}


# ── Stats build ───────────────────────────────────────────────────────────────

def _read_build_status() -> dict:
    if BUILD_STATUS_FILE.exists():
        try:
            return json.loads(BUILD_STATUS_FILE.read_text())
        except Exception:
            pass
    return {"status": "idle"}


def _trigger_build():
    status = _read_build_status()
    if status.get("status") == "running":
        return False

    now = datetime.now(timezone.utc).isoformat()
    BUILD_STATUS_FILE.write_text(json.dumps({"status": "running", "started_at": now}))

    def _run():
        try:
            result = subprocess.run(
                ["bash", str(BUILD_SCRIPT), "build"],
                capture_output=True, text=True,
            )
            finished = datetime.now(timezone.utc).isoformat()
            if result.returncode == 0:
                BUILD_STATUS_FILE.write_text(json.dumps({
                    "status": "done",
                    "started_at": now,
                    "finished_at": finished,
                }))
            else:
                BUILD_STATUS_FILE.write_text(json.dumps({
                    "status": "error",
                    "started_at": now,
                    "finished_at": finished,
                    "error": (result.stderr or result.stdout or "unknown error")[-500:],
                }))
        except Exception as e:
            BUILD_STATUS_FILE.write_text(json.dumps({
                "status": "error",
                "started_at": now,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            }))

    threading.Thread(target=_run, daemon=True).start()
    return True


@app.get("/api/build/status")
def get_build_status():
    return _read_build_status()


# ── Boxscore pending queue ────────────────────────────────────────────────────

@app.post("/api/boxscore/upload")
async def upload_boxscore(
    home_image: UploadFile = File(...),
    away_image: UploadFile = File(...),
    date: str = Form(...),
    home_team: str = Form(...),
    away_team: str = Form(...),
    season: str = Form(...),
    game_type: str = Form(...),
    game_num: Optional[int] = Form(None),
    round_num: Optional[int] = Form(None),
    info: dict = Depends(require_any_role("rosters", "stats")),
):
    home_team = home_team.upper()
    away_team = away_team.upper()
    if home_team not in VALID_TEAMS or away_team not in VALID_TEAMS:
        raise HTTPException(status_code=422, detail="Invalid team abbreviation")

    item_id = str(uuid.uuid4())[:8]
    item_dir = PENDING_BOXSCORES_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    home_bytes = await home_image.read()
    away_bytes = await away_image.read()
    home_ext = (home_image.filename or "home.png").rsplit(".", 1)[-1].lower()
    away_ext = (away_image.filename or "away.png").rsplit(".", 1)[-1].lower()
    (item_dir / f"home.{home_ext}").write_bytes(home_bytes)
    (item_dir / f"away.{away_ext}").write_bytes(away_bytes)

    meta = {
        "id": item_id,
        "date": date,
        "home_team": home_team,
        "away_team": away_team,
        "season": season,
        "game_type": game_type,
        "game_num": game_num,
        "round_num": round_num,
        "uploaded_by": info.get("name"),
        "home_image": f"home.{home_ext}",
        "away_image": f"away.{away_ext}",
    }
    (item_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    logger.info("[%s] POST boxscore/upload — %s vs %s on %s (id=%s)", info.get("name"), home_team, away_team, date, item_id)
    return {"ok": True, "id": item_id}


@app.get("/api/boxscore/pending")
def list_pending_boxscores(info: dict = Depends(require_any_role("rosters", "stats"))):
    if not PENDING_BOXSCORES_DIR.exists():
        return []
    items = []
    for item_dir in sorted(PENDING_BOXSCORES_DIR.iterdir()):
        meta_path = item_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        items.append(meta)
    return items


@app.delete("/api/boxscore/pending/{item_id}")
def delete_pending_boxscore(item_id: str, info: dict = Depends(require_any_role("rosters", "stats"))):
    item_dir = PENDING_BOXSCORES_DIR / item_id
    if not item_dir.exists():
        raise HTTPException(status_code=404, detail="Pending item not found")
    shutil.rmtree(item_dir)
    logger.info("[%s] DELETE boxscore/pending/%s", info.get("name"), item_id)
    return {"ok": True}


# ── Tokens ───────────────────────────────────────────────────────────────────

class TokenCreate(BaseModel):
    name: str
    roles: list[str]


class TokenUpdate(BaseModel):
    name: Optional[str] = None
    roles: Optional[list[str]] = None


@app.get("/api/tokens/public")
def list_tokens_public():
    tokens = load_tokens()
    return [{"name": v.get("name"), "roles": v.get("roles", [])} for v in tokens.values()]


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


@app.patch("/api/tokens/{token}")
def update_token(token: str, body: TokenUpdate, info: dict = Depends(require_admin)):
    tokens = load_tokens()
    if token not in tokens:
        raise HTTPException(status_code=404, detail="Token not found")
    entry = tokens[token]
    if body.name is not None:
        entry["name"] = body.name
    if body.roles is not None:
        invalid = [r for r in body.roles if r not in VALID_ROLES]
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
        entry["roles"] = body.roles
    tokens[token] = entry
    save_tokens(tokens)
    log_write(info, f"PATCH tokens — updated token for {entry['name']!r} roles={entry['roles']}")
    return {"token": token, **entry}


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

import io
import re
import secrets
import threading
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .constants import (
    MEMBERS_FILE, VALID_ROLES, VALID_TEAMS, ROLE_IMPLIES,
    MEMBER_SEEN_FILE, AVATARS_DIR,
)
from .storage import _load_json, _save_json, log_write

_seen_lock = threading.Lock()
_SEEN_MAX = 20  # max unique values stored per member per field


def _record_member_seen(name: str, ip: Optional[str], user_agent: Optional[str],
                         timezone: Optional[str] = None, screen: Optional[str] = None,
                         language: Optional[str] = None):
    with _seen_lock:
        seen = _load_json(MEMBER_SEEN_FILE, {})
        entry = seen.setdefault(name, {})
        for field, value in [
            ("ips", ip), ("user_agents", user_agent),
            ("timezones", timezone), ("screens", screen), ("languages", language),
        ]:
            if not value:
                continue
            lst = entry.setdefault(field, [])
            if value not in lst:
                lst.append(value)
                if len(lst) > _SEEN_MAX:
                    lst.pop(0)
        _save_json(MEMBER_SEEN_FILE, seen)

router = APIRouter()

VALID_MEMBER_POSITIONS = {"owner", "gm", "coach", "none"}


def load_members() -> dict:
    return _load_json(MEMBERS_FILE, {})


def save_members(members: dict):
    _save_json(MEMBERS_FILE, members)


def load_tokens() -> dict:
    """Compatibility shim — reads members.json in the old {hex: {name, roles}} format."""
    return {
        m["token"]: {"name": name, "roles": m.get("roles", [])}
        for name, m in load_members().items()
        if m.get("token")
    }


def _resolve_token(authorization: Optional[str]) -> Optional[dict]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    hex_token = authorization[7:]
    for name, member in load_members().items():
        if member.get("token") == hex_token:
            return {"name": name, "roles": member.get("roles", [])}
    return None


def get_token_info(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    info = _resolve_token(authorization)
    if info is None:
        raise HTTPException(status_code=403, detail="Invalid token")
    return info


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


# ── Pydantic models ───────────────────────────────────────────────────────────

class TenureEntry(BaseModel):
    team: str
    start: str
    end: Optional[str] = None
    position: str


class MemberCreate(BaseModel):
    name: str
    roles: list[str] = []
    tenures: list[TenureEntry] = []


class MemberUpdate(BaseModel):
    roles: Optional[list[str]] = None
    tenures: Optional[list[TenureEntry]] = None


class TokenCreate(BaseModel):
    name: str
    roles: list[str]


class TokenUpdate(BaseModel):
    name: Optional[str] = None
    roles: Optional[list[str]] = None


class MemberSelfUpdate(BaseModel):
    dob: Optional[str] = None


_COSMETIC_COST = 500.0
_VALID_COLORS = {"#f59e0b", "#ef4444", "#a855f7", "#60a5fa", "#34d399", "#f472b6", "#fb923c", "#2dd4bf"}


class CosmeticsUpdate(BaseModel):
    name_color: Optional[str] = None   # hex from _VALID_COLORS or "" to clear
    status_text: Optional[str] = None  # max 40 chars or "" to clear


_AVATAR_COST = 5000.0
_AVATAR_MAX_BYTES = 512 * 1024  # 512 KB
_AVATAR_MAX_PX = 512
_AVATAR_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


# ── Auth identity ────────────────────────────────────────────────────────────

@router.get("/api/me")
def me(request: Request, info: dict = Depends(get_token_info)):
    name = info.get("name", "")
    if name:
        fwd = request.headers.get("x-forwarded-for", "")
        ip  = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
        _record_member_seen(name, ip, request.headers.get("user-agent"))
    return {"name": name, "roles": info.get("roles", [])}


class MemberSignal(BaseModel):
    timezone: Optional[str] = None
    screen:   Optional[str] = None
    language: Optional[str] = None


@router.post("/api/me/signal")
def post_member_signal(body: MemberSignal, info: dict = Depends(get_token_info)):
    name = info.get("name", "")
    if name:
        _record_member_seen(name, None, None,
                            timezone=body.timezone, screen=body.screen, language=body.language)
    return {"ok": True}


@router.get("/api/auth/me")
def get_me(authorization: Optional[str] = Header(None)):
    """Returns the current token's member name and roles. Always 200 — empty if no/invalid token."""
    info = _resolve_token(authorization)
    if not info:
        return {"name": None, "roles": []}
    return {"name": info["name"], "roles": info["roles"]}


# ── Token management (compatibility shims) ────────────────────────────────────

@router.get("/api/tokens/public")
def list_tokens_public():
    members = load_members()
    return [{"name": name, "roles": m.get("roles", [])} for name, m in members.items()]


@router.get("/api/tokens")
def list_tokens(_: dict = Depends(require_admin)):
    members = load_members()
    return [
        {"token": m.get("token", ""), "name": name, "roles": m.get("roles", [])}
        for name, m in members.items() if m.get("token")
    ]


@router.post("/api/tokens")
def create_token(body: TokenCreate, info: dict = Depends(require_admin)):
    invalid = [r for r in body.roles if r not in VALID_ROLES]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
    members = load_members()
    if body.name in members:
        if not members[body.name].get("token"):
            members[body.name]["token"] = secrets.token_hex(32)
        members[body.name]["roles"] = body.roles
        token = members[body.name]["token"]
    else:
        token = secrets.token_hex(32)
        members[body.name] = {"token": token, "roles": body.roles, "tenures": []}
    save_members(members)
    log_write(info, f"POST tokens — upserted member {body.name!r} roles={body.roles}")
    return {"token": token, "name": body.name, "roles": body.roles}


@router.patch("/api/tokens/{token}")
def update_token(token: str, body: TokenUpdate, info: dict = Depends(require_admin)):
    members = load_members()
    target = next((name for name, m in members.items() if m.get("token") == token), None)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")
    if body.roles is not None:
        invalid = [r for r in body.roles if r not in VALID_ROLES]
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
        members[target]["roles"] = body.roles
    save_members(members)
    log_write(info, f"PATCH tokens — updated {target!r} roles={members[target]['roles']}")
    return {"token": token, "name": target, "roles": members[target]["roles"]}


@router.delete("/api/tokens/{token}")
def delete_token(token: str, info: dict = Depends(require_admin)):
    members = load_members()
    target = next((name for name, m in members.items() if m.get("token") == token), None)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")
    members[target].pop("token", None)
    save_members(members)
    log_write(info, f"DELETE tokens — revoked token for {target!r}")
    return {"ok": True}


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/api/members/me")
def get_my_member_info(info: dict = Depends(get_token_info)):
    """Return the authenticated member's own name, roles, current tenure positions, and profile."""
    members = load_members()
    m = members.get(info["name"], {})
    tenures = m.get("tenures", [])
    current_positions = list({
        t["position"] for t in tenures
        if not t.get("end") and t.get("position") and t["position"] != "none"
    })
    avatar_url = f"/api/members/{info['name']}/avatar" if m.get("has_avatar") else None
    return {"name": info["name"], "roles": info.get("roles", []), "positions": current_positions, "dob": m.get("dob"), "cosmetics": m.get("cosmetics", {}), "avatar_url": avatar_url}


@router.get("/api/members/public")
def list_members_public():
    members = load_members()
    return [
        {"name": name, "roles": m.get("roles", []), "tenures": m.get("tenures", []), "dob": m.get("dob"), "cosmetics": m.get("cosmetics", {}), "avatar_url": f"/api/members/{name}/avatar" if m.get("has_avatar") else None}
        for name, m in members.items()
    ]


@router.patch("/api/members/me/profile")
def update_my_profile(body: MemberSelfUpdate, info: dict = Depends(get_token_info)):
    if body.dob is not None and body.dob != "":
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', body.dob):
            raise HTTPException(status_code=422, detail="dob must be YYYY-MM-DD or empty string to clear")
    members = load_members()
    name = info["name"]
    if name not in members:
        raise HTTPException(status_code=404, detail="Member not found")
    if body.dob is not None:
        if body.dob == "":
            members[name].pop("dob", None)
        else:
            members[name]["dob"] = body.dob
    save_members(members)
    log_write(info, f"PATCH members/me/profile — {name!r} updated profile")
    return {"dob": members[name].get("dob")}


@router.patch("/api/members/me/cosmetics")
def update_my_cosmetics(body: CosmeticsUpdate, info: dict = Depends(get_token_info)):
    from .bets import _load_balances, _save_balances, _init_bal, _append_ledger, _balances_lock

    name = info["name"]
    if body.name_color is not None and body.name_color != "" and body.name_color not in _VALID_COLORS:
        raise HTTPException(status_code=422, detail=f"Invalid color. Must be one of: {sorted(_VALID_COLORS)}")
    if body.status_text is not None:
        body.status_text = body.status_text.strip()
        if len(body.status_text) > 40:
            raise HTTPException(status_code=422, detail="status_text must be 40 characters or fewer")

    from datetime import datetime, timezone as tz
    ts = datetime.now(tz.utc).isoformat()

    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, name)
        if balances[name] < _COSMETIC_COST:
            raise HTTPException(status_code=402, detail=f"Insufficient NB¥ balance (need {_COSMETIC_COST:.0f})")
        balances[name] = round(balances[name] - _COSMETIC_COST, 2)
        new_balance = balances[name]
        _save_balances(balances)

    _append_ledger([{"ts": ts, "member": name, "delta": -_COSMETIC_COST,
                     "new_balance": new_balance, "reason": "Cosmetics update"}])

    members = load_members()
    if name not in members:
        raise HTTPException(status_code=404, detail="Member not found")
    cosmetics = members[name].get("cosmetics", {})
    if body.name_color is not None:
        if body.name_color == "":
            cosmetics.pop("name_color", None)
        else:
            cosmetics["name_color"] = body.name_color
    if body.status_text is not None:
        if body.status_text == "":
            cosmetics.pop("status_text", None)
        else:
            cosmetics["status_text"] = body.status_text
    members[name]["cosmetics"] = cosmetics
    save_members(members)
    log_write(info, f"PATCH members/me/cosmetics — {name!r} updated cosmetics")
    return {"cosmetics": cosmetics, "new_balance": new_balance}


@router.post("/api/members/me/avatar")
async def upload_my_avatar(info: dict = Depends(get_token_info), file: UploadFile = File(...)):
    from .bets import _load_balances, _save_balances, _init_bal, _append_ledger, _balances_lock
    from PIL import Image

    name = info["name"]
    if file.content_type not in _AVATAR_ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="Image must be JPEG, PNG, or WebP")

    contents = await file.read()
    if len(contents) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Image must be {_AVATAR_MAX_BYTES // 1024} KB or smaller")

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
        img.thumbnail((_AVATAR_MAX_PX, _AVATAR_MAX_PX), Image.LANCZOS)
    except Exception:
        raise HTTPException(status_code=422, detail="Could not process image")

    from datetime import datetime, timezone as tz
    ts = datetime.now(tz.utc).isoformat()

    with _balances_lock:
        balances = _load_balances()
        _init_bal(balances, name)
        if balances[name] < _AVATAR_COST:
            raise HTTPException(status_code=402, detail=f"Insufficient NB¥ balance (need {_AVATAR_COST:.0f})")
        balances[name] = round(balances[name] - _AVATAR_COST, 2)
        new_balance = balances[name]
        _save_balances(balances)

    _append_ledger([{"ts": ts, "member": name, "delta": -_AVATAR_COST,
                     "new_balance": new_balance, "reason": "Avatar upload"}])

    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AVATARS_DIR / f"{name}.jpg"
    img.save(out_path, "JPEG", quality=88, optimize=True)

    members = load_members()
    if name not in members:
        raise HTTPException(status_code=404, detail="Member not found")
    members[name]["has_avatar"] = True
    save_members(members)
    log_write(info, f"POST members/me/avatar — {name!r} uploaded avatar")
    return {"avatar_url": f"/api/members/{name}/avatar", "new_balance": new_balance}


@router.get("/api/members/{name}/avatar")
def get_member_avatar(name: str):
    path = AVATARS_DIR / f"{name}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No avatar set")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/api/members")
def list_members_admin(info: dict = Depends(require_admin)):
    members = load_members()
    return [
        {"name": name, "token": m.get("token"), "roles": m.get("roles", []), "tenures": m.get("tenures", []), "cosmetics": m.get("cosmetics", {}), "avatar_url": f"/api/members/{name}/avatar" if m.get("has_avatar") else None}
        for name, m in members.items()
    ]


@router.post("/api/members")
def create_member(body: MemberCreate, info: dict = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be empty")
    members = load_members()
    if name in members:
        raise HTTPException(status_code=409, detail=f"Member '{name}' already exists")
    invalid = [r for r in body.roles if r not in VALID_ROLES]
    if invalid:
        raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
    for t in body.tenures:
        if t.team.upper() not in VALID_TEAMS:
            raise HTTPException(status_code=422, detail=f"Invalid team: {t.team}")
        if t.position not in VALID_MEMBER_POSITIONS:
            raise HTTPException(status_code=422, detail=f"Invalid position: {t.position}")
    token = secrets.token_hex(32)
    members[name] = {"token": token, "roles": body.roles, "tenures": [t.model_dump() for t in body.tenures]}
    save_members(members)
    log_write(info, f"POST members — created {name!r} roles={body.roles}")
    return {"name": name, "token": token, "roles": body.roles, "tenures": members[name]["tenures"]}


@router.patch("/api/members/{name}")
def update_member(name: str, body: MemberUpdate, info: dict = Depends(get_token_info)):
    is_admin = has_role(info, "admin")
    is_bod   = has_role(info, "bod")
    if not is_admin and not is_bod:
        raise HTTPException(status_code=403, detail="'bod' role required")
    if body.roles is not None and not is_admin:
        raise HTTPException(status_code=403, detail="Only admin can update roles")
    members = load_members()
    if name not in members:
        raise HTTPException(status_code=404, detail=f"Member '{name}' not found")
    member = members[name]
    if body.roles is not None:
        invalid = [r for r in body.roles if r not in VALID_ROLES]
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid roles: {invalid}")
        member["roles"] = body.roles
    if body.tenures is not None:
        for t in body.tenures:
            if t.team.upper() not in VALID_TEAMS:
                raise HTTPException(status_code=422, detail=f"Invalid team: {t.team}")
            if t.position not in VALID_MEMBER_POSITIONS:
                raise HTTPException(status_code=422, detail=f"Invalid position: {t.position}")
        member["tenures"] = [t.model_dump() for t in body.tenures]
    members[name] = member
    save_members(members)
    log_write(info, f"PATCH members — updated {name!r}")
    return {"name": name, "roles": member.get("roles", []), "tenures": member.get("tenures", [])}


@router.post("/api/members/{name}/rotate-token")
def rotate_member_token(name: str, info: dict = Depends(require_admin)):
    members = load_members()
    if name not in members:
        raise HTTPException(status_code=404, detail=f"Member '{name}' not found")
    new_token = secrets.token_hex(32)
    members[name]["token"] = new_token
    save_members(members)
    log_write(info, f"POST members/{name}/rotate-token")
    return {"name": name, "token": new_token}


@router.delete("/api/members/{name}")
def delete_member(name: str, info: dict = Depends(require_admin)):
    members = load_members()
    if name not in members:
        raise HTTPException(status_code=404, detail=f"Member '{name}' not found")
    del members[name]
    save_members(members)
    log_write(info, f"DELETE members — removed {name!r}")
    return {"ok": True}

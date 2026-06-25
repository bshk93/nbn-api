import secrets
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from .constants import (
    MEMBERS_FILE, VALID_ROLES, VALID_TEAMS, ROLE_IMPLIES,
    MEMBER_SEEN_FILE,
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
    """Return the authenticated member's own name, roles, and current tenure positions."""
    members = load_members()
    m = members.get(info["name"], {})
    tenures = m.get("tenures", [])
    current_positions = list({
        t["position"] for t in tenures
        if not t.get("end") and t.get("position") and t["position"] != "none"
    })
    return {"name": info["name"], "roles": info.get("roles", []), "positions": current_positions}


@router.get("/api/members/public")
def list_members_public():
    members = load_members()
    return [
        {"name": name, "roles": m.get("roles", []), "tenures": m.get("tenures", [])}
        for name, m in members.items()
    ]


@router.get("/api/members")
def list_members_admin(info: dict = Depends(require_admin)):
    members = load_members()
    return [
        {"name": name, "token": m.get("token"), "roles": m.get("roles", []), "tenures": m.get("tenures", [])}
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

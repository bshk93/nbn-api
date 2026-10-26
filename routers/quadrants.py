"""Saved quadrant charts — the gallery on /stats/frivolities#quadrant.

A chart is the page's own config object (stat per axis, filters, the four
quadrant captions, which players), stored as-is. The page draws it from live
stats every time it is opened, so a saved chart is a recipe, not a picture —
it moves when the numbers do.

Stored as {"seq": int, "items": [...]} like suggestions.json, so a chart's
number is a permanent reference even after older ones are deleted.

    GET    /api/quadrants          public, newest first, no configs
    GET    /api/quadrants/{n}      public, one chart
    POST   /api/quadrants          any member
    DELETE /api/quadrants/{n}      its author, or admin

Pinned by tests/test_quadrants.py.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .auth import get_token_info, has_role
from .constants import QUADRANTS_FILE
from .storage import _load_json, _save_json, log_write

router = APIRouter()

_lock = threading.Lock()

# Mirrors AXIS_VARS in nbn-today/stats/frivolities/index.html, plus "ovr". A key the
# page doesn't know would load as a chart with no axis.
STAT_KEYS = {
    "age", "salary", "ppg", "rpg", "apg", "spg", "bpg", "topg", "pfpg",
    "fg%", "2p%", "3p%", "ts%", "gmsc", "ovr",
}
MAX_PLAYERS = 60
LABEL_MAX = 60


def _stat(v: str) -> str:
    if v not in STAT_KEYS:
        raise ValueError(f"unknown stat {v!r}")
    return v


class Labels(BaseModel):
    tl: str = Field("", max_length=LABEL_MAX)
    tr: str = Field("", max_length=LABEL_MAX)
    bl: str = Field("", max_length=LABEL_MAX)
    br: str = Field("", max_length=LABEL_MAX)


class Split(BaseModel):
    mode: Literal["median", "mean", "league", "custom"] = "median"
    x: Optional[float] = None
    y: Optional[float] = None


class Pick(BaseModel):
    mode: Literal["top", "list"] = "top"
    by: str = "gmsc"
    n: int = Field(20, ge=1, le=MAX_PLAYERS)
    slugs: list[str] = Field(default_factory=list, max_length=MAX_PLAYERS)

    _by = field_validator("by")(_stat)

    @field_validator("slugs")
    @classmethod
    def _slugs(cls, v):
        if any(len(s) > 80 for s in v):
            raise ValueError("slug too long")
        return v


class Config(BaseModel):
    v: int = 1
    x: str
    y: str
    dataset: Literal["reg", "po", "all"] = "reg"
    season: str = Field("", max_length=12)
    team: str = Field("", max_length=4)
    minG: int = Field(0, ge=0, le=200)
    invertX: bool = False
    invertY: bool = False
    names: bool = True
    split: Split = Field(default_factory=Split)
    labels: Labels = Field(default_factory=Labels)
    pick: Pick = Field(default_factory=Pick)

    _x = field_validator("x")(_stat)
    _y = field_validator("y")(_stat)


class QuadrantCreate(BaseModel):
    title: str = Field(..., max_length=100)
    config: Config


def _load_store() -> dict:
    return _load_json(QUADRANTS_FILE, {"seq": 0, "items": []})


def _save_store(store: dict):
    _save_json(QUADRANTS_FILE, store)


def _summary(item: dict) -> dict:
    c = item["config"]
    return {k: item[k] for k in ("id", "title", "author", "created_at")} | {"x": c["x"], "y": c["y"]}


def _find(store: dict, quadrant_id: int) -> int:
    idx = next((i for i, q in enumerate(store["items"]) if q["id"] == quadrant_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Chart not found")
    return idx


@router.get("/api/quadrants")
def list_quadrants():
    items = sorted(_load_store()["items"], key=lambda q: q["id"], reverse=True)
    return [_summary(q) for q in items]


@router.get("/api/quadrants/{quadrant_id}")
def get_quadrant(quadrant_id: int):
    store = _load_store()
    return store["items"][_find(store, quadrant_id)]


@router.post("/api/quadrants")
def create_quadrant(body: QuadrantCreate, info: dict = Depends(get_token_info)):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    with _lock:
        store = _load_store()
        store["seq"] += 1
        item = {
            "id": store["seq"],
            "title": title,
            "author": info["name"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": body.config.model_dump(),
        }
        store["items"].append(item)
        _save_store(store)
    log_write(info, f"POST quadrants — #{item['id']} {title!r}")
    return item


@router.delete("/api/quadrants/{quadrant_id}")
def delete_quadrant(quadrant_id: int, info: dict = Depends(get_token_info)):
    with _lock:
        store = _load_store()
        idx = _find(store, quadrant_id)
        if store["items"][idx]["author"] != info["name"] and not has_role(info, "admin"):
            raise HTTPException(status_code=403, detail="Only the chart's author can delete it")
        store["items"].pop(idx)
        _save_store(store)
    log_write(info, f"DELETE quadrants/{quadrant_id}")
    return {"ok": True}

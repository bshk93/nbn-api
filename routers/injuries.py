"""The 2K injury report: uploaded as screenshots, parsed, tracked, announced.

Each stream day the streamer shows MyNBA's league-wide injury screen (player,
team, Hurt/Out, OVR and the injury's hit to it, injury, duration). This module
is that screen's whole pipeline, shaped like the box score one:

- **Upload** — `POST /api/injuries/upload`, one image at a time, into
  `pending-injury-reports/<id>/`. One item per report date; a second image the
  same day joins it, because the list scrolls and a long one is two or three
  screenshots. Originals byte for byte, since a parse reads them.
- **Parse** — `/parse-injuries` (nbn-today/.claude/commands) reads the pending
  item, resolves each name to a slug and posts the rows to
  `POST /api/injuries/commit`.
- **Track** — `injuries.json` is who is injured *now*; `injury-history.jsonl`
  is append-only, one line per change. The committed images move to
  `injury-report-screenshots/` as WebP and are deleted 14 days later, the same
  retention as box score screenshots.
- **Announce** — the changes, and only the changes, go to
  `DISCORD_INJURY_CHANNEL` (`#injury-report`), pinging each affected team's
  role on that player's own line. Unset, nothing is posted.

## Off the report means healed

The report is the whole league's list, so a player on `injuries.json` who is
missing from today's report is healthy again (league rule, 2026-10-22). That
makes every report the full list: a long one is uploaded as several
screenshots of the same day, and `/parse-injuries` reads them all before it
commits.

## A long injury that vanishes is confirmed, not assumed

The likeliest way a player drops off a report is a parse that missed a row, so
a heal is checked before anything is written. A player whose last reported
duration was at least `CONFIRM_HEAL_DAYS` at its low end ("2-4 weeks",
"Career"), or one that can't be read, is not healed silently: the commit is a
409 listing them and writes nothing, and goes through only when the same
request names each of them in `confirm_heals`. Per player, not a blanket flag,
so a newly flagged one can't ride along on an earlier yes. A short injury
("1-2 weeks", days, day-to-day) heals with no question.

## The team is the roster's, not the logo's

A player's team comes from the roster CSVs at commit time, the same source
every other page uses. The parser sends the logo it saw as `team_shown`, and a
disagreement is returned as a warning rather than trusted either way: a stale
2K roster is far likelier than a wrong NBN one. A player on no roster falls
back to `team_shown`, with a warning.

injuries.json:

    {"last_report": {"id", "date", "committed_by", "committed_at"},
     "players": {slug: {"slug", "team", "status", "injury", "duration",
                        "ovr", "ovr_delta", "since", "updated"}}}
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import discord_transport as transport
from .auth import require_any_role
from .boxscore_shots import _to_webp, MAX_IMAGE_BYTES
from .boxscores import _checked_image_ext
from .constants import DATA_DIR, VALID_TEAMS, logger
from .discord_notify import TEAM_ROLE_IDS
from .league_time import league_today_str
from .names import player_name
from .players import load_player_bios
from .schedule import DATE_RE
from .storage import _load_json, _save_json, log_write

router = APIRouter()

PENDING_DIR = DATA_DIR / "pending-injury-reports"
KEPT_DIR = DATA_DIR / "injury-report-screenshots"
INJURIES_FILE = DATA_DIR / "injuries.json"
HISTORY_FILE = DATA_DIR / "injury-history.jsonl"
ROSTERS_DIR = DATA_DIR

DISCORD_INJURY_CHANNEL = os.environ.get("DISCORD_INJURY_CHANNEL", "").strip()
# One report a day, split into a few messages at most. Well above that while
# still catching a loop.
MAX_BURST = 20
BURST_WINDOW = 900   # seconds
# Discord refuses content over 2,000 characters; leave room for the pings.
MAX_MESSAGE_CHARS = 1700

KEEP_DAYS = 14
MAX_IMAGES = 6

# Upload, remove, commit and sweep all read-modify-write the same files.
_lock = threading.Lock()

_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_FILE_RE = re.compile(r"^shot-\d+\.(png|jpg|gif|webp)$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_date(date: str) -> str:
    if not DATE_RE.match(date or ""):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    return date


# ── Screenshots ───────────────────────────────────────────────────────────────

def _read_meta(item_dir: Path) -> Optional[dict]:
    try:
        return json.loads((item_dir / "meta.json").read_text())
    except Exception:
        return None


def _write_meta(item_dir: Path, meta: dict) -> None:
    (item_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def _items(root: Path) -> list[tuple[Path, dict]]:
    if not root.exists():
        return []
    out = []
    for d in root.iterdir():
        if d.is_dir() and (meta := _read_meta(d)):
            out.append((d, meta))
    return out


def _pending_for(date: str) -> Optional[tuple[Path, dict]]:
    return next(((d, m) for d, m in _items(PENDING_DIR) if m.get("date") == date), None)


def _public(meta: dict, status: str) -> dict:
    base = f"/api/injuries/screenshots/{meta['id']}"
    return {
        "id": meta["id"],
        "status": status,
        "date": meta.get("date"),
        "uploaded_by": meta.get("uploaded_by"),
        "committed_at": meta.get("committed_at"),
        "expires_at": meta.get("expires_at"),
        "images": [{**i, "url": f"{base}/{i['file']}"} for i in meta.get("images", [])],
    }


def _archive(item_id: str) -> None:
    """Move a committed report's screenshots to the kept folder as WebP, with a
    14-day expiry. The caller holds `_lock`. Never raises: keeping a screenshot
    must never be why a commit fails."""
    try:
        src_dir = PENDING_DIR / item_id
        meta = _read_meta(src_dir)
        if not meta:
            return
        dst_dir = KEPT_DIR / item_id
        dst_dir.mkdir(parents=True, exist_ok=True)
        for img in meta.get("images", []):
            src = src_dir / img["file"]
            if not src.exists():
                continue
            webp = Path(img["file"]).with_suffix(".webp").name
            if _to_webp(src, dst_dir / webp):
                img["file"] = webp
            else:
                shutil.copy2(src, dst_dir / img["file"])
        now = _now()
        meta["committed_at"] = now.isoformat()
        meta["expires_at"] = (now + timedelta(days=KEEP_DAYS)).isoformat()
        _write_meta(dst_dir, meta)
        shutil.rmtree(src_dir, ignore_errors=True)
    except Exception as exc:
        logger.warning("Archiving injury report %s failed: %s", item_id, exc)


def _sweep_expired() -> None:
    """Delete kept screenshots past their expiry. Run on commit and on list.
    The caller holds `_lock`. Never raises."""
    try:
        now = _now().isoformat()
        for d, meta in _items(KEPT_DIR):
            if (meta.get("expires_at") or "") < now:
                shutil.rmtree(d, ignore_errors=True)
    except Exception as exc:
        logger.warning("Injury screenshot sweep failed: %s", exc)


# ── State ─────────────────────────────────────────────────────────────────────

def load_injuries() -> dict:
    data = _load_json(INJURIES_FILE, {})
    return {"last_report": data.get("last_report"), "players": data.get("players") or {}}


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _append_history(events: list[dict]) -> None:
    if not events:
        return
    with HISTORY_FILE.open("a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _roster_teams() -> dict:
    """slug → team abbr, from every team's roster CSV."""
    out = {}
    for team in VALID_TEAMS:
        path = ROSTERS_DIR / f"{team.lower()}-roster.csv"
        if not path.exists():
            continue
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                slug = (row.get("SLUG") or "").strip()
                if slug:
                    out[slug] = team
    return out


# ── The diff ──────────────────────────────────────────────────────────────────

# What makes an injury "changed" enough to announce. OVR is not in it: the
# injury's hit to OVR moves as a player recovers, and a daily post for that
# would bury the real news.
_TRACKED = ("status", "injury", "duration")

# A heal from an injury this long, or longer, at its low end needs confirming.
CONFIRM_HEAL_DAYS = 14
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}
_OPEN_ENDED = re.compile(r"career|season|indefinite", re.I)
_SHORT = re.compile(r"day[\s-]*to[\s-]*day|game[\s-]*time", re.I)


def min_duration_days(duration: str) -> Optional[float]:
    """The low end of a 2K duration in days: "4-6 weeks" → 28, "3 days" → 3,
    "Day-To-Day" → 0, "Career" → inf. None when it can't be read."""
    d = (duration or "").strip()
    if _OPEN_ENDED.search(d):
        return float("inf")
    if _SHORT.search(d):
        return 0
    m = re.match(r"^(\d+(?:\.\d+)?)(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*(day|week|month)s?\b", d, re.I)
    if not m:
        return None
    return float(m.group(1)) * _UNIT_DAYS[m.group(2).lower()]


def needs_confirming(prev: dict) -> bool:
    """Whether healing this entry should be confirmed rather than assumed."""
    days = min_duration_days(prev.get("duration") or "")
    return days is None or days >= CONFIRM_HEAL_DAYS


def diff_report(current: dict, rows: list[dict], date: str) -> tuple[dict, list[dict]]:
    """`(players_after, events)` for one report against the current list.

    `rows` are already resolved: each has slug, team, status, injury, duration,
    ovr, ovr_delta. Anyone on the current list and not in `rows` is healed.
    Pure, so the rules are testable without any files."""
    after = {}
    events = []
    seen = set()
    for r in rows:
        slug = r["slug"]
        seen.add(slug)
        entry = {k: r.get(k) for k in ("slug", "team", "status", "injury", "duration", "ovr", "ovr_delta")}
        prev = current.get(slug)
        if not prev:
            entry["since"] = date
            events.append({"kind": "new", "slug": slug, "team": entry["team"], "after": _view(entry)})
        else:
            entry["since"] = prev.get("since") or date
            if any((prev.get(k) or "") != (entry.get(k) or "") for k in _TRACKED):
                events.append({"kind": "changed", "slug": slug, "team": entry["team"],
                               "before": _view(prev), "after": _view(entry)})
        entry["updated"] = date
        after[slug] = entry
    for slug, prev in current.items():
        if slug not in seen:
            events.append({"kind": "healed", "slug": slug, "team": prev.get("team"), "before": _view(prev)})
    return after, events


def _view(entry: dict) -> dict:
    return {k: entry.get(k) for k in ("status", "injury", "duration", "ovr", "ovr_delta")}


# ── Discord ───────────────────────────────────────────────────────────────────

_SECTIONS = (("new", "🚑 **New**"), ("changed", "🔁 **Updated**"), ("healed", "✅ **Cleared**"))


def _ovr_bit(v: dict) -> str:
    if v.get("ovr") is None:
        return ""
    d = v.get("ovr_delta")
    return f" - OVR {v['ovr']}" + (f" ({d:+d})" if d else "")


def _team_tag(team: Optional[str]) -> str:
    """The team's role mention, so the tag sits on the player's own line. A team
    with no role (or no team at all) is written out in bold instead."""
    rid = TEAM_ROLE_IDS.get((team or "").upper())
    return f"<@&{rid}>" if rid else f"**{team or '—'}**"


def _event_line(e: dict, names: dict) -> str:
    who = f"{_team_tag(e.get('team'))} {names.get(e['slug']) or e['slug']}"
    if e["kind"] == "healed":
        return f"{who} - back from {e['before'].get('injury') or 'injury'}"
    a = e["after"]
    status = a.get("status") or ""
    if e["kind"] == "changed" and (e["before"].get("status") or "") != status:
        status = f"{e['before'].get('status')} → {status}"
    return f"{who} - {status} - {a.get('injury')} - {a.get('duration')}{_ovr_bit(a)}"


def _render(date: str, events: list[dict], names: dict, part: str = "") -> str:
    d = datetime.strptime(date, "%Y-%m-%d")
    lines = [f"**Injury report — {d.strftime('%b')} {d.day}**{part}"]
    for kind, label in _SECTIONS:
        group = [e for e in events if e["kind"] == kind]
        if group:
            lines += ["", label] + [_event_line(e, names) for e in group]
    return "\n".join(lines)


def build_messages(date: str, events: list[dict], names: dict) -> list[dict]:
    """The Discord payloads for one report's changes: one message, or one per
    group of teams when a single message would be too long. Each line starts
    with its team's role mention, and each message pings exactly the teams it
    names — once per team, however many of its players are listed."""
    if not events:
        return []
    order = sorted({e.get("team") or "" for e in events})
    by_team = {t: sorted((e for e in events if (e.get("team") or "") == t),
                         key=lambda e: names.get(e["slug"]) or e["slug"]) for t in order}
    chunks, cur = [], []
    for t in order:
        trial = cur + by_team[t]
        if cur and len(_render(date, trial, names, " (99/99)")) > MAX_MESSAGE_CHARS:
            chunks.append(cur)
            cur = list(by_team[t])
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    out = []
    for i, chunk in enumerate(chunks):
        part = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        text = _render(date, chunk, names, part)
        # The tags are inline, one per line; `allowed_mentions` names exactly
        # those roles, so nothing else in the text (a stray @everyone) can ping.
        roles = sorted({TEAM_ROLE_IDS[t] for t in ((e.get("team") or "").upper() for e in chunk)
                        if t in TEAM_ROLE_IDS})
        out.append({"content": text, "allowed_mentions": {"parse": [], "roles": roles}})
    return out


def notify(date: str, events: list[dict], names: dict) -> None:
    """Post one report's changes. Never raises; the report is already saved."""
    try:
        if not transport.configured(DISCORD_INJURY_CHANNEL):
            return
        for payload in build_messages(date, events, names):
            transport.send(DISCORD_INJURY_CHANNEL, payload,
                           max_burst=MAX_BURST, burst_window=BURST_WINDOW)
    except Exception as exc:
        logger.warning("Injury report notify failed for %s: %s", date, exc)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/api/injuries/upload")
async def upload_injury_report(
    image: UploadFile = File(...),
    date: Optional[str] = Form(None),
    info: dict = Depends(require_any_role("stats", "rosters")),
):
    """One screenshot of the injury screen. The first of a date creates that
    date's pending report; later ones are added to it."""
    date = _check_date(date or league_today_str())
    data = await image.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="The screenshot is over 15MB.")
    ext = _checked_image_ext(data, "injury report")
    with _lock:
        found = _pending_for(date)
        if found:
            item_dir, meta = found
        else:
            item_id = uuid.uuid4().hex[:8]
            item_dir = PENDING_DIR / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            meta = {"id": item_id, "date": date, "uploaded_by": info.get("name"),
                    "uploaded_at": _now().isoformat(), "images": []}
        if len(meta["images"]) >= MAX_IMAGES:
            raise HTTPException(status_code=409, detail=f"This report already has {MAX_IMAGES} screenshots.")
        used = [int(m.group(1)) for i in meta["images"] if (m := re.match(r"^shot-(\d+)\.", i["file"]))]
        fname = f"shot-{max(used, default=0) + 1}.{ext}"
        (item_dir / fname).write_bytes(data)
        meta["images"].append({"file": fname, "by": info.get("name"), "at": _now().isoformat()})
        _write_meta(item_dir, meta)
        out = _public(meta, "pending")
    logger.info("[%s] POST injuries/upload — %s (id=%s, %s)", info.get("name"), date, meta["id"], fname)
    return {"ok": True, "item": out}


@router.get("/api/injuries/pending")
def list_pending(info: dict = Depends(require_any_role("stats", "rosters"))):
    """Pending reports, oldest first — what /parse-injuries reads."""
    items = [_public(m, "pending") for _, m in _items(PENDING_DIR)]
    return sorted(items, key=lambda m: m["date"] or "")


@router.delete("/api/injuries/pending/{item_id}")
def delete_pending(item_id: str, info: dict = Depends(require_any_role("stats", "rosters"))):
    # Checked against the pattern before it goes near a path: ".." would
    # otherwise rmtree the data directory.
    if not _ID_RE.match(item_id):
        raise HTTPException(status_code=404, detail="Pending report not found")
    with _lock:
        item_dir = PENDING_DIR / item_id
        if not item_dir.is_dir():
            raise HTTPException(status_code=404, detail="Pending report not found")
        shutil.rmtree(item_dir)
    logger.info("[%s] DELETE injuries/pending/%s", info.get("name"), item_id)
    return {"ok": True}


@router.delete("/api/injuries/pending/{item_id}/images/{fname}")
def delete_pending_image(item_id: str, fname: str,
                         info: dict = Depends(require_any_role("stats", "rosters"))):
    if not _ID_RE.match(item_id) or not _FILE_RE.match(fname):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    with _lock:
        item_dir = PENDING_DIR / item_id
        meta = _read_meta(item_dir)
        if not meta or not any(i["file"] == fname for i in meta["images"]):
            raise HTTPException(status_code=404, detail="Screenshot not found")
        meta["images"] = [i for i in meta["images"] if i["file"] != fname]
        (item_dir / fname).unlink(missing_ok=True)
        if meta["images"]:
            _write_meta(item_dir, meta)
            out = _public(meta, "pending")
        else:
            shutil.rmtree(item_dir, ignore_errors=True)
            out = None
    logger.info("[%s] DELETE injuries/pending/%s/images/%s", info.get("name"), item_id, fname)
    return {"ok": True, "item": out}


@router.get("/api/injuries/screenshots")
def list_screenshots(date: Optional[str] = Query(None)):
    """Every report with screenshots on file: pending and committed (kept 14
    days). Public — it is the same information the injury list shows."""
    with _lock:
        _sweep_expired()
    out = []
    for root, status in ((PENDING_DIR, "pending"), (KEPT_DIR, "committed")):
        out += [_public(m, status) for _, m in _items(root) if not date or m.get("date") == date]
    return sorted(out, key=lambda m: m["date"] or "", reverse=True)


@router.get("/api/injuries/screenshots/{item_id}/{fname}")
def get_screenshot(item_id: str, fname: str):
    if _ID_RE.match(item_id or "") and _FILE_RE.match(fname or ""):
        for root in (PENDING_DIR, KEPT_DIR):
            p = root / item_id / fname
            if p.is_file():
                return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(status_code=404, detail="Screenshot not found")


class InjuryRow(BaseModel):
    slug: str
    status: str
    injury: str
    duration: str
    ovr: Optional[int] = None
    ovr_delta: Optional[int] = None
    team_shown: Optional[str] = None
    player: Optional[str] = None   # the name as shown, for the warnings only


class InjuryCommit(BaseModel):
    date: str
    rows: list[InjuryRow]
    report_id: Optional[str] = None
    # Players a long injury would be healed from, confirmed by a person.
    confirm_heals: list[str] = []


def _clean(s: str) -> str:
    return " ".join((s or "").split())


@router.post("/api/injuries/commit")
def commit_injury_report(body: InjuryCommit, info: dict = Depends(require_any_role("stats", "rosters"))):
    date = _check_date(body.date)
    if body.report_id is not None and not _ID_RE.match(body.report_id):
        raise HTTPException(status_code=422, detail="report_id is not a report id")

    bios = load_player_bios()
    problems, rows, warnings, seen = [], [], [], set()
    rostered = _roster_teams()
    for r in body.rows:
        label = r.player or r.slug
        if r.slug not in bios:
            problems.append(f"{label}: {r.slug!r} is not a player bio")
            continue
        if r.slug in seen:
            problems.append(f"{label}: listed twice")
            continue
        seen.add(r.slug)
        status, injury, duration = _clean(r.status).title(), _clean(r.injury), _clean(r.duration)
        if not status or not injury or not duration:
            problems.append(f"{label}: status, injury and duration are all required")
            continue
        if r.ovr is not None and not 25 <= r.ovr <= 99:
            problems.append(f"{label}: OVR {r.ovr} is out of range")
        shown = (r.team_shown or "").upper() or None
        if shown and shown not in VALID_TEAMS:
            problems.append(f"{label}: team_shown {shown!r} is not a team")
            continue
        team = rostered.get(r.slug)
        name = player_name(bios[r.slug].get("name", ""))
        if not team:
            team = shown
            warnings.append(f"{name} is on no roster; filed under {shown or 'no team'} from the screenshot")
        elif shown and shown != team:
            warnings.append(f"{name} showed as {shown} in 2K but is rostered on {team}; filed under {team}")
        rows.append({"slug": r.slug, "team": team, "status": status, "injury": injury,
                     "duration": duration, "ovr": r.ovr, "ovr_delta": r.ovr_delta})
    if problems:
        raise HTTPException(status_code=422, detail=f"Injury report refused, {len(problems)} "
                            f"problem{'s' if len(problems) != 1 else ''}: " + "; ".join(problems))

    with _lock:
        if body.report_id and not (PENDING_DIR / body.report_id).is_dir():
            raise HTTPException(status_code=404, detail="Pending report not found")
        state = load_injuries()
        last = state["last_report"] or {}
        # Reports apply in order: an older one committed late would heal (or
        # re-injure) players against a list that has already moved past it.
        if (last.get("date") or "") > date:
            raise HTTPException(status_code=409, detail=f"A report for {last['date']} is already "
                                f"committed; a report for {date} is older than the current list.")
        after, events = diff_report(state["players"], rows, date)
        confirmed = set(body.confirm_heals)
        unconfirmed = []
        for e in events:
            prev = state["players"].get(e["slug"]) or {}
            if e["kind"] == "healed" and e["slug"] not in confirmed and needs_confirming(prev):
                unconfirmed.append({
                    "slug": e["slug"],
                    "name": player_name((bios.get(e["slug"]) or {}).get("name", "")) or e["slug"],
                    "team": prev.get("team"), "status": prev.get("status"),
                    "injury": prev.get("injury"), "duration": prev.get("duration"),
                    "since": prev.get("since"), "last_seen": prev.get("updated"),
                })
        if unconfirmed:
            raise HTTPException(status_code=409, detail={
                "error": "confirm_heals",
                "message": f"{len(unconfirmed)} player{'s' if len(unconfirmed) != 1 else ''} on a long "
                           f"injury {'are' if len(unconfirmed) != 1 else 'is'} missing from this report. "
                           f"Check the screenshots, then resend with each slug in confirm_heals if they "
                           f"really are healthy. Nothing was written.",
                "players": unconfirmed,
            })
        stamp = {"date": date, "report_id": body.report_id}
        events = [{**stamp, **e} for e in events]
        _save_json(INJURIES_FILE, {
            "last_report": {"id": body.report_id, "date": date,
                            "committed_by": info.get("name"), "committed_at": _now().isoformat()},
            "players": after,
        })
        _append_history(events)
        if body.report_id:
            _archive(body.report_id)
        _sweep_expired()

    names = {slug: player_name((bios.get(slug) or {}).get("name", "")) for slug in
             {e["slug"] for e in events}}
    notify(date, events, names)
    counts = {k: sum(1 for e in events if e["kind"] == k) for k in ("new", "changed", "healed")}
    log_write(info, f"injuries/commit {date}: {counts['new']} new, {counts['changed']} changed, "
                    f"{counts['healed']} healed")
    return {"ok": True, **counts, "injured": len(after), "warnings": warnings}


def _with_names(entries, bios: dict) -> list[dict]:
    return [{**e, "name": player_name((bios.get(e.get("slug")) or {}).get("name", "")) or e.get("slug")}
            for e in entries]


@router.get("/api/injuries")
def get_injuries():
    """Who is injured now, by team, with the last report's date."""
    state = load_injuries()
    players = _with_names(state["players"].values(), load_player_bios())
    players.sort(key=lambda p: (p.get("team") or "~", p["name"]))
    return {"last_report": state["last_report"], "players": players}


@router.get("/api/injuries/history")
def get_injury_history(team: Optional[str] = Query(None), slug: Optional[str] = Query(None),
                       since: Optional[str] = Query(None), limit: int = Query(500, ge=1, le=5000)):
    """Every change, newest first."""
    events = load_history()
    if team:
        events = [e for e in events if (e.get("team") or "") == team.upper()]
    if slug:
        events = [e for e in events if e.get("slug") == slug]
    if since:
        events = [e for e in events if (e.get("date") or "") >= since]
    events = list(reversed(events))[:limit]
    return _with_names(events, load_player_bios())

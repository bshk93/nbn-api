"""Background task that keeps the draft-picks ledger's horizon topped up the
instant the league year rolls over — no systemd timer, no separate process.

nbn-api already runs as one continuous process (Restart=always), so a single
asyncio task that sleeps until the next known rollover boundary and then calls
the idempotent `ensure_picks_horizon()` gets seconds-level precision without
giving the API any new privileges (no systemd/sudo access needed).

The "next boundary" is recomputed from scratch every time this fires or is
woken early — it's just `_season_start_date` of next season, whatever that
resolves to (default July 1 or a BOD override) — so BOD moving a rollover
earlier, later, or un-rolling an already-passed one all fall out of the same
calculation with no special-casing.
"""

import asyncio
import logging
from datetime import datetime

from .league_time import LEAGUE_TZ, league_now
from .roster_picks import ensure_picks_horizon
from .storage import _current_league_year, _league_rollovers, _season_start_date, _season_shift
from .transactions import snapshot_room_zone_baseline

logger = logging.getLogger("nbn-api")

_reschedule_event: asyncio.Event | None = None
_task: asyncio.Task | None = None


def _next_rollover_boundary() -> datetime:
    rollovers = _league_rollovers()
    current = _current_league_year()
    return _season_start_date(_season_shift(current, 1), rollovers)


async def _loop():
    global _reschedule_event
    while True:
        try:
            created = ensure_picks_horizon()
            if created:
                logger.info("picks horizon: created draft year(s) %s", created)
        except Exception:
            logger.exception("picks horizon: check failed")

        try:
            snapshotted = snapshot_room_zone_baseline(_current_league_year())
            if snapshotted:
                logger.info("room zone baseline: snapshotted %s for %s",
                            snapshotted, _current_league_year())
        except Exception:
            logger.exception("room zone baseline: snapshot failed")

        # _season_start_date returns a naive *civil* midnight. Comparing it to
        # utcnow() treated that as UTC midnight, so the horizon rolled over at
        # 8pm ET the evening before the league year actually started. Anchor it
        # to league time instead.
        target = _next_rollover_boundary().replace(tzinfo=LEAGUE_TZ)
        wait_seconds = max((target - league_now()).total_seconds(), 1)

        _reschedule_event = asyncio.Event()
        try:
            await asyncio.wait_for(_reschedule_event.wait(), timeout=wait_seconds)
            logger.info("picks horizon: rescheduled early (league-year override changed)")
        except asyncio.TimeoutError:
            pass  # reached the boundary naturally — loop around and run the check


def start_picks_horizon_scheduler():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def wake_picks_horizon_scheduler():
    """Call after anything that could move the next rollover boundary (a
    league-year override PUT/DELETE) so the loop recomputes immediately
    instead of waiting out a sleep computed against the old boundary."""
    if _reschedule_event is not None:
        _reschedule_event.set()

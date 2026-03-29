"""
Centralized Bluesky API rate limit manager.

All components call check_read() or check_write() before making Bluesky API calls.
Write budgets are tracked in Firestore (_system/rate_state) so the global 4-minute
write window is enforced across all Cloud Functions, not just within a single process.

Read tracking is in-memory per process. Bluesky read limits are IP-based (not
per-token), so cross-process coordination via Firestore adds thousands of ops without
benefit — each process tracks its own reads independently within the 5-min window.

Bluesky limits:
  Reads:  3,000 per 5 minutes (IP-based)
  Writes: 5,000 points/hour, 35,000 points/day
          CREATE=3pts, UPDATE=2pts, DELETE=1pt

Operational ceiling: 80% of limits.
  Reads:  2,400 per 5 minutes
  Writes: 4,000 points/hour, 28,000 points/day

4-minute global write window (public posts):
  No two public write actions (post reply, post comment) may occur within
  4 minutes of each other, regardless of which component triggered them.

60-second DM write window (independent):
  DM sends use a separate window tracked via last_dm_write_at in rate_state.
  DMs do not block public writes and public writes do not block DMs.

Active hours: 7am–10pm America/Los_Angeles — enforced by is_active_hours().
"""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from bluesky.shared.firestore_client import db

# Write point costs
WRITE_COSTS = {"create": 3, "update": 2, "delete": 1}

# Operational ceilings (80% of limits)
READ_CEILING_PER_5MIN = 2400
WRITE_CEILING_PER_HOUR = 4000
WRITE_CEILING_PER_DAY = 28000

# Global write window in seconds (public posts: replies, comments)
WRITE_WINDOW_SECONDS = 240  # 4 minutes

# DM-specific write window — independent of public write window
DM_WRITE_WINDOW_SECONDS = 60  # 1 minute

# Active hours gate
_ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
_ACTIVE_START = 7   # 7am
_ACTIVE_END = 22    # 10pm

_STATE_DOC = db.collection("_system").document("rate_state")


def is_active_hours():
    """Return True if current Pacific time is within 7am–10pm (active sending hours)."""
    now_local = datetime.now(_ACTIVE_TZ)
    return _ACTIVE_START <= now_local.hour < _ACTIVE_END


# In-memory read state — per-process, reset when window expires
_read_state = {
    "reads_this_window": 0,
    "window_start": None,
}


class RateLimitError(Exception):
    pass


def _now_ts():
    return datetime.now(timezone.utc).timestamp()


def _get_write_state():
    doc = _STATE_DOC.get()
    if doc.exists:
        return doc.to_dict()
    return {
        "last_write_at": 0.0,
        "writes_this_hour": 0,
        "writes_today": 0,
        "hour_window_start": _now_ts(),
        "day_window_start": _now_ts(),
    }


def check_read():
    """
    Call before any Bluesky read API call.
    Raises RateLimitError if the read ceiling is approaching.
    Tracked in-memory (reads are IP-based; Firestore coordination is unnecessary).
    """
    now = _now_ts()

    # Reset window if 5 minutes have passed
    if _read_state["window_start"] is None or now - _read_state["window_start"] >= 300:
        _read_state["reads_this_window"] = 0
        _read_state["window_start"] = now

    if _read_state["reads_this_window"] >= READ_CEILING_PER_5MIN:
        wait = 300 - (now - _read_state["window_start"])
        raise RateLimitError(
            f"Read ceiling reached ({_read_state['reads_this_window']}/{READ_CEILING_PER_5MIN}). "
            f"Window resets in {wait:.0f}s."
        )

    _read_state["reads_this_window"] += 1


def check_write(op_type="create"):
    """
    Call before any Bluesky write API call (post reply, send DM, post comment).
    op_type: "create" | "update" | "delete"
    Raises RateLimitError if any write limit or the 4-minute window blocks the action.
    """
    cost = WRITE_COSTS.get(op_type, 3)
    state = _get_write_state()
    now = _now_ts()

    # Enforce 4-minute global window
    last_write = state.get("last_write_at", 0.0)
    elapsed = now - last_write
    if elapsed < WRITE_WINDOW_SECONDS:
        wait = WRITE_WINDOW_SECONDS - elapsed
        raise RateLimitError(
            f"Global 4-minute write window active. Next write allowed in {wait:.0f}s."
        )

    # Reset hourly window
    hour_start = state.get("hour_window_start", now)
    writes_hour = state.get("writes_this_hour", 0)
    if now - hour_start >= 3600:
        writes_hour = 0
        hour_start = now

    # Reset daily window
    day_start = state.get("day_window_start", now)
    writes_day = state.get("writes_today", 0)
    if now - day_start >= 86400:
        writes_day = 0
        day_start = now

    if writes_hour + cost > WRITE_CEILING_PER_HOUR:
        wait = 3600 - (now - hour_start)
        raise RateLimitError(
            f"Hourly write ceiling reached ({writes_hour}/{WRITE_CEILING_PER_HOUR} pts). "
            f"Resets in {wait:.0f}s."
        )

    if writes_day + cost > WRITE_CEILING_PER_DAY:
        wait = 86400 - (now - day_start)
        raise RateLimitError(
            f"Daily write ceiling reached ({writes_day}/{WRITE_CEILING_PER_DAY} pts). "
            f"Resets in {wait:.0f}s."
        )

    _STATE_DOC.set({
        "last_write_at": now,
        "writes_this_hour": writes_hour + cost,
        "hour_window_start": hour_start,
        "writes_today": writes_day + cost,
        "day_window_start": day_start,
    }, merge=True)


def seconds_until_next_write():
    """Return seconds until the next public write is allowed (0 if allowed now)."""
    state = _get_write_state()
    elapsed = _now_ts() - state.get("last_write_at", 0.0)
    remaining = WRITE_WINDOW_SECONDS - elapsed
    return max(0.0, remaining)


def check_dm_write():
    """
    Call before any DM send. Uses a 60s window independent of the 4-min public write window.
    Updates last_dm_write_at only — does NOT touch last_write_at.
    Shares the same hourly/daily point budget as public writes.
    """
    cost = WRITE_COSTS.get("create", 3)
    state = _get_write_state()
    now = _now_ts()

    # Enforce 60-second DM window
    last_dm = state.get("last_dm_write_at", 0.0)
    elapsed = now - last_dm
    if elapsed < DM_WRITE_WINDOW_SECONDS:
        wait = DM_WRITE_WINDOW_SECONDS - elapsed
        raise RateLimitError(
            f"DM write window active. Next DM allowed in {wait:.0f}s."
        )

    # Reset hourly window
    hour_start = state.get("hour_window_start", now)
    writes_hour = state.get("writes_this_hour", 0)
    if now - hour_start >= 3600:
        writes_hour = 0
        hour_start = now

    # Reset daily window
    day_start = state.get("day_window_start", now)
    writes_day = state.get("writes_today", 0)
    if now - day_start >= 86400:
        writes_day = 0
        day_start = now

    if writes_hour + cost > WRITE_CEILING_PER_HOUR:
        wait = 3600 - (now - hour_start)
        raise RateLimitError(
            f"Hourly write ceiling reached ({writes_hour}/{WRITE_CEILING_PER_HOUR} pts). "
            f"Resets in {wait:.0f}s."
        )

    if writes_day + cost > WRITE_CEILING_PER_DAY:
        wait = 86400 - (now - day_start)
        raise RateLimitError(
            f"Daily write ceiling reached ({writes_day}/{WRITE_CEILING_PER_DAY} pts). "
            f"Resets in {wait:.0f}s."
        )

    _STATE_DOC.set({
        "last_dm_write_at": now,
        "writes_this_hour": writes_hour + cost,
        "hour_window_start": hour_start,
        "writes_today": writes_day + cost,
        "day_window_start": day_start,
    }, merge=True)


def seconds_until_next_dm_write():
    """Return seconds until the next DM send is allowed (0 if allowed now)."""
    state = _get_write_state()
    elapsed = _now_ts() - state.get("last_dm_write_at", 0.0)
    remaining = DM_WRITE_WINDOW_SECONDS - elapsed
    return max(0.0, remaining)

"""
Derived status computation for Threshold routines.

Implements the three-state absence model from threshold_spec.md:
    snoozed → observed → watching → unknown | missed

Status is NEVER stored — computed fresh on every scheduler tick.
The "unknown" state prevents escalating when device health is stale
(the device might be offline, not the routine missing).

Only "missed" triggers Urgent. "unknown" does NOT escalate.
"""

from datetime import datetime, timedelta, time as dt_time, timezone
from typing import Union
from zoneinfo import ZoneInfo

from src.threshold.models import Routine, DeviceHealthRecord


# The four possible derived statuses
STATUS_SNOOZED = "snoozed"
STATUS_OBSERVED = "observed"
STATUS_WATCHING = "watching"
STATUS_UNKNOWN = "unknown"
STATUS_MISSED = "missed"

# Day name mapping (routine uses lowercase 3-letter abbreviations)
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_time(time_str: str) -> dt_time:
    """Parse a time string like '07:00' or '09:30' into a time object."""
    parts = time_str.split(":")
    return dt_time(int(parts[0]), int(parts[1]))


def _get_today_window(routine: Routine, now: datetime) -> tuple[datetime, datetime]:
    """
    Compute today's window start and end in UTC, based on the routine's
    timezone and window times.

    Returns (window_start_utc, window_end_utc) for the current day
    in the routine's timezone.
    """
    tz = ZoneInfo(routine.timezone)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    local_date = local_now.date()

    start_time = _parse_time(routine.window_start)
    end_time = _parse_time(routine.window_end)

    if start_time <= end_time:
        window_start = datetime.combine(local_date, start_time, tzinfo=tz)
        window_end = datetime.combine(local_date, end_time, tzinfo=tz)
    else:
        # Window crosses midnight (e.g. 22:00 to 06:00)
        # Any time before 22:00 belongs to the window that started yesterday evening
        if local_now.time() < start_time:
            window_start = datetime.combine(local_date - timedelta(days=1), start_time, tzinfo=tz)
            window_end = datetime.combine(local_date, end_time, tzinfo=tz)
        else:
            window_start = datetime.combine(local_date, start_time, tzinfo=tz)
            window_end = datetime.combine(local_date + timedelta(days=1), end_time, tzinfo=tz)

    return window_start, window_end


def _is_routine_day(routine: Routine, now: datetime) -> bool:
    """Check if today is a day the routine is active."""
    tz = ZoneInfo(routine.timezone)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    start_time = _parse_time(routine.window_start)
    end_time = _parse_time(routine.window_end)
    local_date = local_now.date()
    if start_time > end_time and local_now.time() < start_time:
        day_date = local_date - timedelta(days=1)
    else:
        day_date = local_date
    day_name = DAY_NAMES[day_date.weekday()]
    return day_name in routine.days


def event_satisfies_routine(routine: Routine, occurred_at: Union[datetime, str, int, float]) -> bool:
    """
    Check if an event timestamp satisfies a routine.
    Returns True only if occurred_at:
      (a) falls on one of the routine's active days, and
      (b) lies within [window_start, window_end + escalation_delay_minutes]
          of the window that contains that event's own local date/time.
    For overnight windows, an event after midnight belongs to the window
    that started the previous evening.
    """
    if occurred_at is None:
        return False

    if isinstance(occurred_at, str):
        clean_str = occurred_at.replace("Z", "+00:00")
        occ_dt = datetime.fromisoformat(clean_str)
    elif isinstance(occurred_at, (int, float)):
        occ_dt = datetime.fromtimestamp(occurred_at / 1000.0 if occurred_at > 1e11 else occurred_at, tz=timezone.utc)
    else:
        occ_dt = occurred_at

    tz = ZoneInfo(routine.timezone)
    occ_aware = occ_dt if occ_dt.tzinfo else occ_dt.replace(tzinfo=tz)

    if not _is_routine_day(routine, occ_aware):
        return False

    window_start, window_end = _get_today_window(routine, occ_aware)
    grace_end = window_end + timedelta(minutes=routine.escalation_delay_minutes)

    return window_start <= occ_aware <= grace_end


def _last_satisfied_within_window(
    routine: Routine, window_start: datetime, window_end: datetime
) -> bool:
    """
    Check if last_satisfied_at falls within today's window.

    Uses event_satisfies_routine to verify satisfaction and ensures sat falls
    within today's window (not a previous day's).
    """
    if routine.last_satisfied_at is None:
        return False

    sat = routine.last_satisfied_at
    # Ensure timezone-aware comparison
    if sat.tzinfo is None:
        tz = ZoneInfo(routine.timezone)
        sat = sat.replace(tzinfo=tz)

    # Must be on or after today's window_start (not yesterday's satisfaction)
    if sat < window_start:
        return False

    return event_satisfies_routine(routine, sat)


def derive_status(
    routine: Routine,
    device_health: DeviceHealthRecord,
    now: datetime,
) -> str:
    """
    Derive the current status of a routine. Called on every scheduler tick.

    Implements the exact logic from threshold_spec.md:

        if snoozed_until and now < snoozed_until:
            return "snoozed"
        if last_satisfied_at falls within today's window:
            return "observed"
        if now > today's window_end + escalation_delay:
            if device_health.is_stale(device_id, now):
                return "unknown"     # do not escalate
            return "missed"          # fires Urgent
        return "watching"

    Returns one of: "snoozed", "observed", "watching", "unknown", "missed"
    """
    # Not a routine day → not watching
    if not _is_routine_day(routine, now):
        return STATUS_OBSERVED  # nothing expected today, treat as fine

    # Snoozed check
    if routine.snoozed_until is not None:
        snooze = routine.snoozed_until
        if snooze.tzinfo is None:
            snooze = snooze.replace(tzinfo=ZoneInfo(routine.timezone))
        now_aware = now if now.tzinfo else now.replace(tzinfo=ZoneInfo(routine.timezone))
        if now_aware < snooze:
            return STATUS_SNOOZED

    # Compute today's window
    window_start, window_end = _get_today_window(routine, now)

    # Check if routine was satisfied within today's window
    if _last_satisfied_within_window(routine, window_start, window_end):
        return STATUS_OBSERVED

    # Past the escalation deadline?
    escalation_deadline = window_end + timedelta(minutes=routine.escalation_delay_minutes)
    now_aware = now if now.tzinfo else now.replace(tzinfo=ZoneInfo(routine.timezone))
    if now_aware > escalation_deadline:
        # Three-state check: is the device data trustworthy?
        if device_health.is_stale(now):
            return STATUS_UNKNOWN  # do not escalate — data isn't trustworthy
        return STATUS_MISSED  # fires Urgent — this is the only case that escalates

    return STATUS_WATCHING

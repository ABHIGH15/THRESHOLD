"""
Derived status computation for Threshold routines.

Implements the three-state absence model from threshold_spec.md:
    snoozed → observed → watching → unknown | missed

Status is NEVER stored — computed fresh on every scheduler tick.
The "unknown" state prevents escalating when device health is stale
(the device might be offline, not the routine missing).

Only "missed" triggers Urgent. "unknown" does NOT escalate.
"""

from datetime import datetime, timedelta, time as dt_time
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

    window_start = datetime.combine(local_date, start_time, tzinfo=tz)
    window_end = datetime.combine(local_date, end_time, tzinfo=tz)

    return window_start, window_end


def _is_routine_day(routine: Routine, now: datetime) -> bool:
    """Check if today is a day the routine is active."""
    tz = ZoneInfo(routine.timezone)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    day_name = DAY_NAMES[local_now.weekday()]
    return day_name in routine.days


def _last_satisfied_within_window(
    routine: Routine, window_start: datetime, window_end: datetime
) -> bool:
    """
    Check if last_satisfied_at falls within today's window.

    Both the satisfaction timestamp and the window boundaries must be
    compared in the same timezone context.
    """
    if routine.last_satisfied_at is None:
        return False

    sat = routine.last_satisfied_at
    # Ensure timezone-aware comparison
    if sat.tzinfo is None:
        tz = ZoneInfo(routine.timezone)
        sat = sat.replace(tzinfo=tz)

    return window_start <= sat <= window_end


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

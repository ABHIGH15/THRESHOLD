"""
Deterministic triggers for identifying ambiguous events requiring Agent investigation.
Standard library only; no LLM.
"""

from datetime import datetime, time as dt_time, timedelta, timezone
import sqlite3
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo

from src.threshold.status import event_satisfies_routine
from src.threshold.store import row_to_routine


def _parse_time_str(t_str: str) -> dt_time:
    """Parse 'HH:MM' string into a time object."""
    parts = t_str.strip().split(":")
    return dt_time(int(parts[0]), int(parts[1]))


def is_time_in_range(target_time: dt_time, start_str: str, end_str: str) -> bool:
    """Check if target_time is inside [start, end), handling midnight crossing."""
    start_t = _parse_time_str(start_str)
    end_t = _parse_time_str(end_str)

    if start_t <= end_t:
        return start_t <= target_time < end_t
    else:
        # Crosses midnight (e.g. 23:00 to 05:00)
        return target_time >= start_t or target_time < end_t


def is_ambiguous_event(
    conn: sqlite3.Connection,
    event: Dict[str, Any],
    now: datetime,
    cfg: Optional[Union[Dict[str, Any], Any]] = None,
) -> Optional[str]:
    """
    Evaluate if an event is ambiguous and warrants agent investigation.

    Trigger: 'quiet_hours_unexpected_activity'
    Conditions:
    1. event_type in cfg.event_types (default: ['motion_detected'])
    2. device appears in at least one routine
    3. event_satisfies_routine is False for EVERY routine of that device
    4. event occurred during quiet_hours (default: ['23:00', '05:00'] in the routine's timezone)
    5. dedupe: no investigation exists for this device within cfg.dedupe_minutes (default: 30)

    Returns reason_code string if triggered, else None.
    """
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # Config extraction
    if cfg is None:
        cfg_dict: Dict[str, Any] = {}
    elif isinstance(cfg, dict):
        cfg_dict = cfg
    else:
        cfg_dict = getattr(cfg, "__dict__", {})

    event_types = cfg_dict.get("event_types", ["motion", "motion_detected"])
    quiet_hours = cfg_dict.get("quiet_hours", ["23:00", "05:00"])
    dedupe_minutes = int(cfg_dict.get("dedupe_minutes", 30))

    event_type = event.get("event_type")
    if event_type not in event_types:
        return None

    device_id = event.get("device_id")
    if not device_id:
        return None

    # Condition 2: device appears in at least one routine
    cur = conn.execute("SELECT * FROM routines WHERE device_id = ?", (device_id,))
    routine_rows = cur.fetchall()
    if not routine_rows:
        return None

    if routine_rows and not isinstance(routine_rows[0], (sqlite3.Row, dict)):
        col_names = [d[0] for d in cur.description]
        routine_rows = [dict(zip(col_names, r)) for r in routine_rows]

    routines = [row_to_routine(r) for r in routine_rows]

    # Parse occurred_at timestamp
    occ_str = event.get("occurred_at")
    if not occ_str:
        return None
    clean_occ = occ_str.replace("Z", "+00:00")
    occ_dt = datetime.fromisoformat(clean_occ)
    if occ_dt.tzinfo is None:
        occ_dt = occ_dt.replace(tzinfo=timezone.utc)

    # Condition 3: event_satisfies_routine is False for EVERY routine of that device
    for r in routines:
        if event_satisfies_routine(r, occ_dt):
            # Satisfies a routine -> not an unexpected event!
            return None

    # Condition 4: event's local time is inside quiet_hours (in primary routine timezone)
    primary_tz = ZoneInfo(routines[0].timezone)
    local_occ = occ_dt.astimezone(primary_tz)
    local_time = local_occ.time()

    if not is_time_in_range(local_time, quiet_hours[0], quiet_hours[1]):
        return None

    # Condition 5: Deduplication check
    cutoff_dt = now_utc - timedelta(minutes=dedupe_minutes)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    cur_inv = conn.execute(
        "SELECT 1 FROM investigations WHERE device_id = ? AND created_at >= ? LIMIT 1",
        (device_id, cutoff_iso),
    )
    if cur_inv.fetchone():
        # Already investigated recently -> deduped
        return None

    return "quiet_hours_unexpected_activity"

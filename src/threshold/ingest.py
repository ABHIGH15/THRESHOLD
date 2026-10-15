"""
Shared ingest pipeline for Threshold.

Unified entry point for all event sources:
- Ring Webhooks (verified HMAC)
- Ring Event History poller
- Signed simulator
"""

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Dict, Optional, Union

from src.threshold.status import event_satisfies_routine
from src.threshold.store import (
    row_to_routine,
    save_event,
    to_iso_timestamp,
    update_routine_last_satisfied,
    upsert_device_health,
)


def ingest_event(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    device_id: str,
    event_type: str,
    sub_type: Optional[str],
    occurred_at: Union[int, float, str, datetime],
    raw_json: Union[str, dict],
    ended_at: Optional[Union[int, float, str, datetime]] = None,
    received_at: Optional[Union[int, float, str, datetime]] = None,
) -> bool:
    """
    Ingest an event into Threshold storage.
    (a) Inserts into events table (idempotent on source, external_id).
    (b) If device_online/device_offline, updates device_health.
    (c) If event matches a routine, advances last_satisfied_at if newer.

    Returns:
        True if the event was newly inserted and processed.
        False if the event was ignored as a duplicate.
    """
    if received_at is not None:
        rec_iso = to_iso_timestamp(received_at)
    else:
        rec_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    occ_iso = to_iso_timestamp(occurred_at)

    # 1. Insert into events table (idempotent on source, external_id)
    inserted = save_event(
        conn=conn,
        source=source,
        external_id=external_id,
        device_id=device_id,
        event_type=event_type,
        occurred_at=occ_iso,
        received_at=rec_iso,
        raw_json=raw_json,
        sub_type=sub_type,
        ended_at=ended_at,
    )

    if not inserted:
        # Duplicate delivery: change nothing
        return False

    # 2. Update device_health on online/offline events
    if event_type in ("device_online", "device_offline"):
        is_online = (event_type == "device_online")
        upsert_device_health(
            conn=conn,
            device_id=device_id,
            online=is_online,
            fetched_at=rec_iso,
            source=source,
            reported_at=None,
        )

    # 3. Check for matching routines and advance last_satisfied_at if newer and in-window
    cur = conn.execute(
        "SELECT * FROM routines "
        "WHERE device_id = ? AND expected_event_type = ?",
        (device_id, event_type),
    )
    matching_routines = cur.fetchall()

    clean_occ = occ_iso.replace("Z", "+00:00")
    occ_dt = datetime.fromisoformat(clean_occ)
    if occ_dt.tzinfo is None:
        occ_dt = occ_dt.replace(tzinfo=timezone.utc)

    for r_row in matching_routines:
        expected_sub = r_row["expected_sub_type"]
        # Match if expected_sub_type is None/empty OR matches event sub_type
        if expected_sub is None or expected_sub == "" or expected_sub == sub_type:
            routine = row_to_routine(r_row)
            if event_satisfies_routine(routine, occ_dt):
                current_sat_dt = routine.last_satisfied_at
                # Compare timestamps as datetimes, never as strings
                if current_sat_dt is None:
                    update_routine_last_satisfied(conn, r_row["routine_id"], occ_iso)
                else:
                    if current_sat_dt.tzinfo is None:
                        current_sat_dt = current_sat_dt.replace(tzinfo=timezone.utc)
                    if occ_dt > current_sat_dt:
                        update_routine_last_satisfied(conn, r_row["routine_id"], occ_iso)

    return True

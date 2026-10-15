"""
Pure, bounded tools for Threshold Agent.
Whitelist args validation; deterministic text rendering.
Never invent Ring fields; only reads our tables or RingClient.
"""

from datetime import datetime, timedelta, timezone
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from src.threshold.agent.schemas import ToolResult
from src.threshold.models import Routine
from src.threshold.ring_client import (
    MediaUnavailable,
    RateLimited,
    RingAPIError,
    TokenExpired,
)
from src.threshold.scheduler import build_device_health_record
from src.threshold.status import derive_status
from src.threshold.store import get_routine, list_routines, row_to_routine, to_iso_timestamp
import re

DEVICE_ID_REGEX = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
IDENTIFIER_REGEX = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _is_known_valid_device_id(conn: Optional[sqlite3.Connection], device_id: Any) -> bool:
    """
    Validate that device_id matches ^[A-Za-z0-9._:-]{1,200}$ and is present
    in routines or device_health table if database connection is available.
    """
    if not isinstance(device_id, str) or not DEVICE_ID_REGEX.match(device_id):
        return False
    if conn is not None:
        try:
            cur = conn.execute(
                """
                SELECT 1 FROM routines WHERE device_id = ?
                UNION
                SELECT 1 FROM device_health WHERE device_id = ?
                LIMIT 1
                """,
                (device_id, device_id),
            )
            if not cur.fetchone():
                return False
        except Exception:
            return False
    return True


def _validate_whitelist_args(args: Dict[str, Any], allowed_keys: set) -> bool:
    """Ensure args dictionary contains ONLY allowed keys."""
    if not isinstance(args, dict):
        return False
    return set(args.keys()).issubset(allowed_keys)


def get_recent_ring_events(
    conn: sqlite3.Connection,
    now: datetime,
    args: Dict[str, Any],
    call_id: str = "call_1",
) -> ToolResult:
    """
    Read our events table only for recent device events.
    Args whitelist: device_id (str), hours (int <= 24), limit (int <= 50).
    """
    if not _validate_whitelist_args(args, {"device_id", "hours", "limit"}):
        return ToolResult(
            call_id=call_id,
            tool="get_recent_ring_events",
            ok=False,
            data=None,
            text="Invalid arguments: unknown or disallowed arguments provided.",
            error="invalid_args",
        )

    device_id = args.get("device_id")
    if not _is_known_valid_device_id(conn, device_id):
        return ToolResult(
            call_id=call_id,
            tool="get_recent_ring_events",
            ok=False,
            data=None,
            text="Invalid arguments: invalid or unknown device_id.",
            error="invalid_args",
        )

    hours = args.get("hours", 24)
    if not isinstance(hours, int) or hours < 1 or hours > 24:
        return ToolResult(
            call_id=call_id,
            tool="get_recent_ring_events",
            ok=False,
            data=None,
            text="Invalid arguments: hours must be an integer between 1 and 24.",
            error="invalid_args",
        )

    limit = args.get("limit", 50)
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        return ToolResult(
            call_id=call_id,
            tool="get_recent_ring_events",
            ok=False,
            data=None,
            text="Invalid arguments: limit must be an integer between 1 and 50.",
            error="invalid_args",
        )

    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff = (now_utc - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = conn.execute(
        """
        SELECT source, external_id, device_id, event_type, sub_type, occurred_at
        FROM events
        WHERE device_id = ? AND occurred_at >= ?
        ORDER BY occurred_at DESC
        LIMIT ?
        """,
        (device_id, cutoff, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        text = f"No events recorded for device {device_id} in the last {hours} hour(s)."
    else:
        summary_items = [
            f"{r['event_type']}{' (' + r['sub_type'] + ')' if r.get('sub_type') else ''} at {r['occurred_at']}"
            for r in rows[:5]
        ]
        text = f"Found {len(rows)} event(s) for device {device_id} in the last {hours}h: " + "; ".join(summary_items)
        if len(rows) > 5:
            text += f"; ... and {len(rows) - 5} more."

    return ToolResult(
        call_id=call_id,
        tool="get_recent_ring_events",
        ok=True,
        data=rows,
        text=text,
        error=None,
    )


def pull_snapshot(
    conn: Optional[sqlite3.Connection],
    now: datetime,
    args: Dict[str, Any],
    ring_client: Any = None,
    call_id: str = "call_2",
    snapshots_dir: str = "snapshots",
) -> ToolResult:
    """
    Download latest snapshot for device using injectable RingClient.
    Args whitelist: device_id (str).
    Never puts raw image bytes or URLs into results.
    """
    if not _validate_whitelist_args(args, {"device_id"}):
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text="Invalid arguments: unknown or disallowed arguments provided.",
            error="invalid_args",
        )

    device_id = args.get("device_id")
    if not _is_known_valid_device_id(conn, device_id):
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text="Invalid arguments: invalid or unknown device_id.",
            error="invalid_args",
        )

    if ring_client is None:
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text="Media unavailable: no Ring client configured.",
            error="media_unavailable",
        )

    try:
        image_bytes, captured_at_ms = ring_client.get_latest_snapshot(
            device_id=device_id,
            lookback_hours=24,
            deadline_seconds=5.0,
        )
        if not image_bytes:
            return ToolResult(
                call_id=call_id,
                tool="pull_snapshot",
                ok=False,
                data=None,
                text="Media unavailable: empty response received from camera.",
                error="media_unavailable",
            )

        os.makedirs(snapshots_dir, exist_ok=True)
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        sanitized_device = re.sub(r"[^A-Za-z0-9._-]", "_", device_id)
        sanitized_device = os.path.basename(sanitized_device)
        file_path = os.path.join(snapshots_dir, f"snapshot_{sanitized_device}_{ts_str}.jpg")

        abs_dest = os.path.abspath(file_path)
        abs_dir = os.path.abspath(snapshots_dir)
        if not abs_dest.startswith(abs_dir + os.sep) and abs_dest != abs_dir:
            return ToolResult(
                call_id=call_id,
                tool="pull_snapshot",
                ok=False,
                data=None,
                text="Invalid arguments: path traversal attempt detected.",
                error="invalid_args",
            )

        with open(file_path, "wb") as f:
            f.write(image_bytes)

        captured_iso = None
        if captured_at_ms:
            captured_dt = datetime.fromtimestamp(captured_at_ms / 1000.0, tz=timezone.utc)
            captured_iso = captured_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        text = f"Snapshot retrieved successfully for device {device_id} saved to {file_path}"
        if captured_iso:
            text += f" (captured at {captured_iso})."
        else:
            text += "."

        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=True,
            data={"path": file_path, "captured_at": captured_iso},
            text=text,
            error=None,
        )

    except MediaUnavailable as e:
        reason = "416" if "416" in str(e) else ("425" if "425" in str(e) else "media_unavailable")
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text=f"Media unavailable: camera returned {reason} (no recording in range).",
            error="media_unavailable",
        )
    except RateLimited:
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text="Media unavailable: rate limit 429 from Ring API.",
            error="media_unavailable",
        )
    except TokenExpired:
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text="Media unavailable: authentication token expired 401.",
            error="media_unavailable",
        )
    except TimeoutError:
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text="Media unavailable: snapshot request timed out.",
            error="media_unavailable",
        )
    except Exception as e:
        return ToolResult(
            call_id=call_id,
            tool="pull_snapshot",
            ok=False,
            data=None,
            text=f"Media unavailable: {e}",
            error="media_unavailable",
        )


def check_routine_status(
    conn: sqlite3.Connection,
    now: datetime,
    args: Dict[str, Any],
    call_id: str = "call_3",
) -> ToolResult:
    """
    Derive routine status as of now.
    Args whitelist: routine_id (str) or device_id (str).
    """
    if not _validate_whitelist_args(args, {"routine_id", "device_id"}):
        return ToolResult(
            call_id=call_id,
            tool="check_routine_status",
            ok=False,
            data=None,
            text="Invalid arguments: unknown or disallowed arguments provided.",
            error="invalid_args",
        )

    routine_id = args.get("routine_id")
    device_id = args.get("device_id")

    if not routine_id and not device_id:
        return ToolResult(
            call_id=call_id,
            tool="check_routine_status",
            ok=False,
            data=None,
            text="Invalid arguments: must provide either routine_id or device_id.",
            error="invalid_args",
        )

    if device_id is not None and not _is_known_valid_device_id(conn, device_id):
        return ToolResult(
            call_id=call_id,
            tool="check_routine_status",
            ok=False,
            data=None,
            text="Invalid arguments: invalid or unknown device_id.",
            error="invalid_args",
        )

    if routine_id is not None and (not isinstance(routine_id, str) or not IDENTIFIER_REGEX.match(routine_id)):
        return ToolResult(
            call_id=call_id,
            tool="check_routine_status",
            ok=False,
            data=None,
            text="Invalid arguments: invalid routine_id format.",
            error="invalid_args",
        )

    routines_to_check: List[Routine] = []
    if routine_id:
        r_row = get_routine(conn, routine_id)
        if not r_row:
            return ToolResult(
                call_id=call_id,
                tool="check_routine_status",
                ok=False,
                data=None,
                text=f"Routine not found: {routine_id}",
                error="not_found",
            )
        routines_to_check.append(row_to_routine(r_row))
    else:
        cur = conn.execute("SELECT * FROM routines WHERE device_id = ?", (device_id,))
        for r_row in cur.fetchall():
            routines_to_check.append(row_to_routine(r_row))

    if not routines_to_check:
        return ToolResult(
            call_id=call_id,
            tool="check_routine_status",
            ok=True,
            data=[],
            text=f"No routines configured for device {device_id}.",
            error=None,
        )

    statuses = []
    text_summaries = []
    for routine in routines_to_check:
        health = build_device_health_record(conn, routine.device_id, now)
        status = derive_status(routine, health, now)
        statuses.append({
            "routine_id": routine.routine_id,
            "name": routine.name,
            "status": status,
            "window": f"{routine.window_start}-{routine.window_end}",
            "timezone": routine.timezone,
        })
        text_summaries.append(
            f"Routine '{routine.name}' ({routine.routine_id}): {status} (window {routine.window_start}-{routine.window_end} {routine.timezone})"
        )

    return ToolResult(
        call_id=call_id,
        tool="check_routine_status",
        ok=True,
        data=statuses,
        text="; ".join(text_summaries),
        error=None,
    )


def get_threshold_history(
    conn: sqlite3.Connection,
    now: datetime,
    args: Dict[str, Any],
    call_id: str = "call_4",
) -> ToolResult:
    """
    Retrieve past escalations and acks.
    Args whitelist: device_id (str) or routine_id (str); days (int <= 30).
    """
    if not _validate_whitelist_args(args, {"device_id", "routine_id", "days"}):
        return ToolResult(
            call_id=call_id,
            tool="get_threshold_history",
            ok=False,
            data=None,
            text="Invalid arguments: unknown or disallowed arguments provided.",
            error="invalid_args",
        )

    device_id = args.get("device_id")
    routine_id = args.get("routine_id")
    if not device_id and not routine_id:
        return ToolResult(
            call_id=call_id,
            tool="get_threshold_history",
            ok=False,
            data=None,
            text="Invalid arguments: must provide device_id or routine_id.",
            error="invalid_args",
        )

    if device_id is not None and not _is_known_valid_device_id(conn, device_id):
        return ToolResult(
            call_id=call_id,
            tool="get_threshold_history",
            ok=False,
            data=None,
            text="Invalid arguments: invalid or unknown device_id.",
            error="invalid_args",
        )

    if routine_id is not None and (not isinstance(routine_id, str) or not IDENTIFIER_REGEX.match(routine_id)):
        return ToolResult(
            call_id=call_id,
            tool="get_threshold_history",
            ok=False,
            data=None,
            text="Invalid arguments: invalid routine_id format.",
            error="invalid_args",
        )

    days = args.get("days", 30)
    if not isinstance(days, int) or days < 1 or days > 30:
        return ToolResult(
            call_id=call_id,
            tool="get_threshold_history",
            ok=False,
            data=None,
            text="Invalid arguments: days must be an integer between 1 and 30.",
            error="invalid_args",
        )

    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff = (now_utc - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if routine_id:
        cur = conn.execute(
            """
            SELECT routine_id, local_date, fired_at, notified_at, acked_at, reminded_at
            FROM escalations
            WHERE routine_id = ? AND fired_at >= ?
            ORDER BY fired_at DESC
            """,
            (routine_id, cutoff),
        )
    else:
        cur = conn.execute(
            """
            SELECT e.routine_id, e.local_date, e.fired_at, e.notified_at, e.acked_at, e.reminded_at
            FROM escalations e
            JOIN routines r ON e.routine_id = r.routine_id
            WHERE r.device_id = ? AND e.fired_at >= ?
            ORDER BY e.fired_at DESC
            """,
            (device_id, cutoff),
        )

    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        text = f"No escalations recorded in the last {days} days."
    else:
        acked_count = sum(1 for r in rows if r.get("acked_at"))
        text = f"Found {len(rows)} escalation(s) in last {days} days ({acked_count} acknowledged)."

    return ToolResult(
        call_id=call_id,
        tool="get_threshold_history",
        ok=True,
        data=rows,
        text=text,
        error=None,
    )


def check_household_memory(
    conn: sqlite3.Connection,
    now: datetime,
    args: Dict[str, Any],
    call_id: str = "call_5",
) -> ToolResult:
    """
    Counts of corrections labelled false_alarm in the last 30 days, structured only.
    Args whitelist: device_id (str), event_type (str), days (int <= 30).
    """
    if not _validate_whitelist_args(args, {"device_id", "event_type", "days"}):
        return ToolResult(
            call_id=call_id,
            tool="check_household_memory",
            ok=False,
            data=None,
            text="Invalid arguments: unknown or disallowed arguments provided.",
            error="invalid_args",
        )

    device_id = args.get("device_id")
    event_type = args.get("event_type")
    if not _is_known_valid_device_id(conn, device_id):
        return ToolResult(
            call_id=call_id,
            tool="check_household_memory",
            ok=False,
            data=None,
            text="Invalid arguments: invalid or unknown device_id.",
            error="invalid_args",
        )

    if not isinstance(event_type, str) or not IDENTIFIER_REGEX.match(event_type):
        return ToolResult(
            call_id=call_id,
            tool="check_household_memory",
            ok=False,
            data=None,
            text="Invalid arguments: invalid event_type format.",
            error="invalid_args",
        )

    days = args.get("days", 30)
    if not isinstance(days, int) or days < 1 or days > 30:
        return ToolResult(
            call_id=call_id,
            tool="check_household_memory",
            ok=False,
            data=None,
            text="Invalid arguments: days must be an integer between 1 and 30.",
            error="invalid_args",
        )

    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff = (now_utc - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = conn.execute(
        """
        SELECT
            COUNT(*) as total_corrections,
            SUM(CASE WHEN label = 'false_alarm' THEN 1 ELSE 0 END) as false_alarm_count
        FROM corrections
        WHERE device_id = ? AND event_type = ? AND created_at >= ?
        """,
        (device_id, event_type, cutoff),
    )
    row = cur.fetchone()
    total = row[0] or 0
    false_alarms = row[1] or 0

    data = {
        "device_id": device_id,
        "event_type": event_type,
        "false_alarm_count": false_alarms,
        "total_corrections": total,
        "days": days,
    }
    text = (
        f"Household memory for device {device_id} ({event_type}): "
        f"{false_alarms} false alarm(s) out of {total} correction(s) in last {days} days."
    )

    return ToolResult(
        call_id=call_id,
        tool="check_household_memory",
        ok=True,
        data=data,
        text=text,
        error=None,
    )


TOOL_REGISTRY = {
    "get_recent_ring_events": get_recent_ring_events,
    "pull_snapshot": pull_snapshot,
    "check_routine_status": check_routine_status,
    "get_threshold_history": get_threshold_history,
    "check_household_memory": check_household_memory,
}


def execute_tool(
    name: str,
    args: Dict[str, Any],
    call_id: str,
    conn: sqlite3.Connection,
    now: datetime,
    ring_client: Any = None,
    snapshots_dir: str = "snapshots",
) -> ToolResult:
    """Execute a tool by name with arguments and context."""
    if name not in TOOL_REGISTRY:
        return ToolResult(
            call_id=call_id,
            tool=name,
            ok=False,
            data=None,
            text=f"Unknown tool: {name}",
            error="unknown_tool",
        )

    func = TOOL_REGISTRY[name]
    if name == "pull_snapshot":
        return func(
            conn=conn,
            now=now,
            args=args,
            ring_client=ring_client,
            call_id=call_id,
            snapshots_dir=snapshots_dir,
        )
    return func(conn=conn, now=now, args=args, call_id=call_id)

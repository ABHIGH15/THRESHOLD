"""
SQLite storage layer for Threshold.
Replaces DynamoDB single-table design with a local relational schema.

Stdlib sqlite3 only.
WAL mode enabled. Foreign keys enforced.
All timestamps stored as UTC ISO-8601 strings.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union


DEFAULT_DB_PATH = "threshold.db"


def get_db_path() -> str:
    """Return database file path from environment or default."""
    return os.environ.get("THRESHOLD_DB", DEFAULT_DB_PATH)


def ms_to_iso(ms: Union[int, float]) -> str:
    """
    Convert a millisecond integer epoch timestamp to a UTC ISO-8601 string.
    Example: 1791814643626 -> '2026-10-12T14:17:23Z'
    """
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso_timestamp(ts: Union[int, float, str, datetime, None]) -> Optional[str]:
    """Convert supported timestamp representations to UTC ISO-8601 string."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e11:  # Milliseconds epoch
            return ms_to_iso(ts)
        # Seconds epoch
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, str):
        # Return cleanly if already string
        return ts
    raise ValueError(f"Unsupported timestamp type: {type(ts)}")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Create and configure a SQLite connection with WAL mode and foreign keys enabled.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    source TEXT NOT NULL CHECK(source IN ('webhook', 'history', 'simulator')),
    external_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sub_type TEXT,
    occurred_at TEXT NOT NULL,
    ended_at TEXT,
    received_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS device_health (
    device_id TEXT NOT NULL PRIMARY KEY,
    online INTEGER NOT NULL,
    reported_at TEXT,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routines (
    routine_id TEXT NOT NULL PRIMARY KEY,
    property_id TEXT NOT NULL,
    name TEXT NOT NULL,
    device_id TEXT NOT NULL,
    expected_event_type TEXT NOT NULL,
    expected_sub_type TEXT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    timezone TEXT NOT NULL,
    days TEXT NOT NULL,
    escalation_delay_minutes INTEGER NOT NULL DEFAULT 30,
    last_satisfied_at TEXT,
    snoozed_until TEXT
);

CREATE TABLE IF NOT EXISTS escalations (
    routine_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    acked_at TEXT,
    notified_at TEXT,
    reminded_at TEXT,
    UNIQUE(routine_id, local_date),
    FOREIGN KEY(routine_id) REFERENCES routines(routine_id)
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT NOT NULL PRIMARY KEY,
    device_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    time_bucket TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db(conn_or_path: Optional[Union[sqlite3.Connection, str]] = None) -> sqlite3.Connection:
    """Initialize database tables and indexes."""
    if isinstance(conn_or_path, sqlite3.Connection):
        conn = conn_or_path
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
    else:
        conn = get_connection(conn_or_path)

    conn.executescript(SCHEMA_SQL)
    # Ensure notified_at and reminded_at columns exist for existing escalations tables
    try:
        conn.execute("ALTER TABLE escalations ADD COLUMN notified_at TEXT;")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE escalations ADD COLUMN reminded_at TEXT;")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


# --- Store Operations ---

def save_event(
    conn: sqlite3.Connection,
    source: str,
    external_id: str,
    device_id: str,
    event_type: str,
    occurred_at: Union[int, float, str, datetime],
    received_at: Union[int, float, str, datetime],
    raw_json: Union[str, dict],
    sub_type: Optional[str] = None,
    ended_at: Optional[Union[int, float, str, datetime]] = None,
) -> bool:
    """
    Save an event with deduplication on (source, external_id).
    Uses INSERT OR IGNORE so the same (source, external_id) results in exactly one row.
    Returns True if a new row was inserted, False if ignored as a duplicate.
    """
    raw_str = raw_json if isinstance(raw_json, str) else json.dumps(raw_json)
    occurred_iso = to_iso_timestamp(occurred_at)
    received_iso = to_iso_timestamp(received_at)
    ended_iso = to_iso_timestamp(ended_at) if ended_at is not None else None

    sql = """
    INSERT OR IGNORE INTO events (
        source, external_id, device_id, event_type, sub_type,
        occurred_at, ended_at, received_at, raw_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    cur = conn.execute(
        sql,
        (
            source,
            external_id,
            device_id,
            event_type,
            sub_type,
            occurred_iso,
            ended_iso,
            received_iso,
            raw_str,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def get_event(conn: sqlite3.Connection, source: str, external_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve an event by (source, external_id)."""
    cur = conn.execute(
        "SELECT * FROM events WHERE source = ? AND external_id = ?",
        (source, external_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def upsert_device_health(
    conn: sqlite3.Connection,
    device_id: str,
    online: Union[bool, int],
    fetched_at: Union[int, float, str, datetime],
    source: str,
    reported_at: Optional[Union[int, float, str, datetime]] = None,
) -> None:
    """
    Insert or update device health status.
    """
    online_int = 1 if online else 0
    fetched_iso = to_iso_timestamp(fetched_at)
    reported_iso = to_iso_timestamp(reported_at) if reported_at is not None else None

    sql = """
    INSERT INTO device_health (device_id, online, reported_at, fetched_at, source)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(device_id) DO UPDATE SET
        online = excluded.online,
        reported_at = excluded.reported_at,
        fetched_at = excluded.fetched_at,
        source = excluded.source
    """
    conn.execute(sql, (device_id, online_int, reported_iso, fetched_iso, source))
    conn.commit()


def get_device_health(conn: sqlite3.Connection, device_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve current health record for a device."""
    cur = conn.execute("SELECT * FROM device_health WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["online"] = bool(data["online"])
    return data


def save_routine(
    conn: sqlite3.Connection,
    routine_id: str,
    property_id: str,
    name: str,
    device_id: str,
    expected_event_type: str,
    window_start: str,
    window_end: str,
    timezone_name: str,
    days: Union[List[str], str],
    expected_sub_type: Optional[str] = None,
    escalation_delay_minutes: int = 30,
    last_satisfied_at: Optional[Union[int, float, str, datetime]] = None,
    snoozed_until: Optional[Union[int, float, str, datetime]] = None,
) -> None:
    """Save or update routine configuration."""
    days_str = days if isinstance(days, str) else json.dumps(days)
    sat_iso = to_iso_timestamp(last_satisfied_at) if last_satisfied_at is not None else None
    snooze_iso = to_iso_timestamp(snoozed_until) if snoozed_until is not None else None

    sql = """
    INSERT INTO routines (
        routine_id, property_id, name, device_id, expected_event_type, expected_sub_type,
        window_start, window_end, timezone, days, escalation_delay_minutes,
        last_satisfied_at, snoozed_until
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(routine_id) DO UPDATE SET
        property_id = excluded.property_id,
        name = excluded.name,
        device_id = excluded.device_id,
        expected_event_type = excluded.expected_event_type,
        expected_sub_type = excluded.expected_sub_type,
        window_start = excluded.window_start,
        window_end = excluded.window_end,
        timezone = excluded.timezone,
        days = excluded.days,
        escalation_delay_minutes = excluded.escalation_delay_minutes,
        last_satisfied_at = excluded.last_satisfied_at,
        snoozed_until = excluded.snoozed_until
    """
    conn.execute(
        sql,
        (
            routine_id,
            property_id,
            name,
            device_id,
            expected_event_type,
            expected_sub_type,
            window_start,
            window_end,
            timezone_name,
            days_str,
            escalation_delay_minutes,
            sat_iso,
            snooze_iso,
        ),
    )
    conn.commit()


def get_routine(conn: sqlite3.Connection, routine_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve routine record by id."""
    cur = conn.execute("SELECT * FROM routines WHERE routine_id = ?", (routine_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def update_routine_last_satisfied(
    conn: sqlite3.Connection, routine_id: str, last_satisfied_at: Union[int, float, str, datetime]
) -> None:
    """Update last_satisfied_at fact on routine."""
    sat_iso = to_iso_timestamp(last_satisfied_at)
    conn.execute(
        "UPDATE routines SET last_satisfied_at = ? WHERE routine_id = ?",
        (sat_iso, routine_id),
    )
    conn.commit()


def list_routines(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Retrieve all routines from routines table."""
    cur = conn.execute("SELECT * FROM routines ORDER BY routine_id")
    return [dict(row) for row in cur.fetchall()]


def record_escalation(
    conn: sqlite3.Connection,
    routine_id: str,
    local_date: str,
    fired_at: Union[int, float, str, datetime],
    acked_at: Optional[Union[int, float, str, datetime]] = None,
    notified_at: Optional[Union[int, float, str, datetime]] = None,
    reminded_at: Optional[Union[int, float, str, datetime]] = None,
    ignore_conflicts: bool = False,
) -> bool:
    """
    Record an escalation.
    If ignore_conflicts=True, uses INSERT OR IGNORE and returns True if inserted, False if ignored.
    If ignore_conflicts=False, uses INSERT and raises sqlite3.IntegrityError if (routine_id, local_date) already exists.
    """
    fired_iso = to_iso_timestamp(fired_at)
    acked_iso = to_iso_timestamp(acked_at) if acked_at is not None else None
    notified_iso = to_iso_timestamp(notified_at) if notified_at is not None else None
    reminded_iso = to_iso_timestamp(reminded_at) if reminded_at is not None else None

    if ignore_conflicts:
        sql = "INSERT OR IGNORE INTO escalations (routine_id, local_date, fired_at, acked_at, notified_at, reminded_at) VALUES (?, ?, ?, ?, ?, ?)"
        cur = conn.execute(sql, (routine_id, local_date, fired_iso, acked_iso, notified_iso, reminded_iso))
        conn.commit()
        return cur.rowcount > 0
    else:
        sql = "INSERT INTO escalations (routine_id, local_date, fired_at, acked_at, notified_at, reminded_at) VALUES (?, ?, ?, ?, ?, ?)"
        conn.execute(sql, (routine_id, local_date, fired_iso, acked_iso, notified_iso, reminded_iso))
        conn.commit()
        return True


def mark_escalation_notified(
    conn: sqlite3.Connection,
    routine_id: str,
    local_date: str,
    notified_at: Union[int, float, str, datetime],
) -> None:
    """Mark an escalation as notified."""
    iso = to_iso_timestamp(notified_at)
    conn.execute(
        "UPDATE escalations SET notified_at = ? WHERE routine_id = ? AND local_date = ?",
        (iso, routine_id, local_date),
    )
    conn.commit()


def mark_escalation_reminded(
    conn: sqlite3.Connection,
    routine_id: str,
    local_date: str,
    reminded_at: Union[int, float, str, datetime],
) -> None:
    """Mark an escalation as reminded."""
    iso = to_iso_timestamp(reminded_at)
    conn.execute(
        "UPDATE escalations SET reminded_at = ? WHERE routine_id = ? AND local_date = ?",
        (iso, routine_id, local_date),
    )
    conn.commit()


def ack_escalation(
    conn: sqlite3.Connection,
    routine_id: str,
    local_date: str,
    acked_at: Union[int, float, str, datetime],
) -> None:
    """Acknowledge an escalation."""
    iso = to_iso_timestamp(acked_at)
    conn.execute(
        "UPDATE escalations SET acked_at = ? WHERE routine_id = ? AND local_date = ?",
        (iso, routine_id, local_date),
    )
    conn.commit()


def get_unnotified_escalations(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Retrieve escalations where notified_at IS NULL."""
    cur = conn.execute("SELECT * FROM escalations WHERE notified_at IS NULL ORDER BY fired_at")
    return [dict(row) for row in cur.fetchall()]


def get_escalations_due_for_reminder(
    conn: sqlite3.Connection,
    now: Union[int, float, str, datetime],
    reminder_delay_minutes: int = 15,
) -> List[Dict[str, Any]]:
    """
    Retrieve escalations that have been notified, have not been acknowledged,
    have not already received a reminder, and where now >= notified_at + reminder_delay_minutes.
    """
    now_iso = to_iso_timestamp(now)
    now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

    cur = conn.execute(
        "SELECT * FROM escalations WHERE notified_at IS NOT NULL AND acked_at IS NULL AND reminded_at IS NULL ORDER BY fired_at"
    )
    rows = [dict(row) for row in cur.fetchall()]
    due = []
    for r in rows:
        notified_iso = r["notified_at"].replace("Z", "+00:00")
        try:
            notified_dt = datetime.fromisoformat(notified_iso)
            if now_dt >= notified_dt + timedelta(minutes=reminder_delay_minutes):
                due.append(r)
        except Exception:
            continue
    return due


def get_escalation(conn: sqlite3.Connection, routine_id: str, local_date: str) -> Optional[Dict[str, Any]]:
    """Retrieve escalation record for routine and local date."""
    cur = conn.execute(
        "SELECT * FROM escalations WHERE routine_id = ? AND local_date = ?",
        (routine_id, local_date),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def record_correction(
    conn: sqlite3.Connection,
    id: str,
    device_id: str,
    event_type: str,
    time_bucket: str,
    label: str,
    created_at: Union[int, float, str, datetime],
    ignore_conflicts: bool = True,
) -> bool:
    """Save a caretaker correction record (idempotent by default)."""
    created_iso = to_iso_timestamp(created_at)
    cmd = "INSERT OR IGNORE" if ignore_conflicts else "INSERT"
    sql = f"""
    {cmd} INTO corrections (id, device_id, event_type, time_bucket, label, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """
    cur = conn.execute(sql, (id, device_id, event_type, time_bucket, label, created_iso))
    conn.commit()
    return cur.rowcount > 0


def get_app_state(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve a persisted key-value string from app_state table."""
    cur = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_app_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Store or update a key-value string in app_state table."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), now_iso),
    )
    conn.commit()


def get_correction(conn: sqlite3.Connection, id: str) -> Optional[Dict[str, Any]]:
    """Retrieve correction record by ID."""
    cur = conn.execute("SELECT * FROM corrections WHERE id = ?", (id,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_corrections(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """List all correction records ordered by created_at."""
    cur = conn.execute("SELECT * FROM corrections ORDER BY created_at")
    return [dict(row) for row in cur.fetchall()]


def set_snooze(conn: sqlite3.Connection, routine_id: str, until: Union[int, float, str, datetime]) -> None:
    """Set or update snooze until timestamp for routine directly on the routines table."""
    until_iso = to_iso_timestamp(until)
    conn.execute("UPDATE routines SET snoozed_until = ? WHERE routine_id = ?", (until_iso, routine_id))
    conn.commit()


def get_snooze(conn: sqlite3.Connection, routine_id: str) -> Optional[str]:
    """Get active snooze until timestamp string directly from the routines table."""
    cur = conn.execute("SELECT snoozed_until FROM routines WHERE routine_id = ?", (routine_id,))
    row = cur.fetchone()
    return row["snoozed_until"] if row else None


def clear_snooze(conn: sqlite3.Connection, routine_id: str) -> None:
    """Clear snooze on the routines table."""
    conn.execute("UPDATE routines SET snoozed_until = NULL WHERE routine_id = ?", (routine_id,))
    conn.commit()


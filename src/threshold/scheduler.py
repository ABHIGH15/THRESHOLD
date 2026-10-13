"""
Deterministic absence detection scheduler engine for Threshold.
Standard library only: abc, datetime, json, logging, os, sqlite3, threading, time, traceback, typing, zoneinfo.
No LLM anywhere in this path.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
import json
import logging
import os
import sqlite3
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo

from src.threshold.models import DeviceHealthRecord, Routine
from src.threshold.status import (
    derive_status,
    STATUS_MISSED,
    STATUS_OBSERVED,
    STATUS_SNOOZED,
    STATUS_UNKNOWN,
    STATUS_WATCHING,
)
from src.threshold.store import (
    get_connection,
    get_device_health,
    get_routine,
    get_unnotified_escalations,
    init_db,
    list_routines,
    mark_escalation_notified,
    record_escalation,
)

logger = logging.getLogger("threshold.scheduler")


# --- 1. Clock Interfaces ---

class Clock(ABC):
    """Abstract clock interface for deterministic time control."""

    @abstractmethod
    def now(self) -> datetime:
        """Return current datetime, guaranteed timezone-aware in UTC."""
        pass


class RealClock(Clock):
    """Real wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """Controllable and advanceable clock for testing and simulation."""

    def __init__(self, initial_time: Optional[datetime] = None):
        if initial_time is not None:
            self._current_time = initial_time if initial_time.tzinfo else initial_time.replace(tzinfo=timezone.utc)
        else:
            self._current_time = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        self._current_time = new_time if new_time.tzinfo else new_time.replace(tzinfo=timezone.utc)

    def advance(self, delta: Union[timedelta, int, float]) -> datetime:
        if isinstance(delta, (int, float)):
            delta = timedelta(seconds=delta)
        self._current_time += delta
        return self._current_time


class AcceleratedClock(Clock):
    """
    Accelerated clock for demo mode:
    N simulated seconds advance per 1 real elapsed second.
    """

    def __init__(self, start_sim_time: datetime, scale: float = 60.0):
        self.start_sim_time = start_sim_time if start_sim_time.tzinfo else start_sim_time.replace(tzinfo=timezone.utc)
        self.scale = float(scale)
        self._start_mono = time.monotonic()

    def now(self) -> datetime:
        elapsed_real = time.monotonic() - self._start_mono
        sim_seconds = elapsed_real * self.scale
        return self.start_sim_time + timedelta(seconds=sim_seconds)


# --- 2. Notifier Interfaces ---

class Notifier(ABC):
    """Abstract notification delivery interface."""

    @abstractmethod
    def send_escalation(self, routine: Routine, local_date: str, fired_at: str) -> None:
        """Send escalation notification. Must raise exception on failure."""
        pass


class RecordingNotifier(Notifier):
    """In-memory recording notifier for tests and simulated failures."""

    def __init__(self, fail_first_n: int = 0):
        self.sent: List[Dict[str, Any]] = []
        self.fail_first_n = fail_first_n
        self.attempts = 0

    def send_escalation(self, routine: Routine, local_date: str, fired_at: str) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError(f"Simulated notification failure (attempt {self.attempts})")
        self.sent.append({
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
        })


# --- 3. Model Helpers ---

def row_to_routine(row: Dict[str, Any]) -> Routine:
    """Convert SQLite routines row dictionary to Routine dataclass instance."""
    days_val = row["days"]
    if isinstance(days_val, str):
        try:
            days_list = json.loads(days_val)
        except Exception:
            days_list = [d.strip() for d in days_val.split(",") if d.strip()]
    else:
        days_list = list(days_val)

    sat_dt = None
    if row.get("last_satisfied_at"):
        sat_str = row["last_satisfied_at"].replace("Z", "+00:00")
        sat_dt = datetime.fromisoformat(sat_str)

    snooze_dt = None
    if row.get("snoozed_until"):
        snooze_str = row["snoozed_until"].replace("Z", "+00:00")
        snooze_dt = datetime.fromisoformat(snooze_str)

    routine = Routine(
        property_id=row["property_id"],
        name=row["name"],
        device_id=row["device_id"],
        expected_event_type=row["expected_event_type"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        timezone=row["timezone"],
        days=days_list,
        escalation_delay_minutes=row.get("escalation_delay_minutes", 30),
        last_satisfied_at=sat_dt,
        snoozed_until=snooze_dt,
    )
    routine.routine_id = row["routine_id"]  # Attach routine_id for convenience
    return routine


def build_device_health_record(
    conn: sqlite3.Connection,
    device_id: str,
    now: datetime,
    staleness_threshold_minutes: int = 120,
) -> DeviceHealthRecord:
    """
    Read device health from store and construct DeviceHealthRecord.
    If device has never reported, or was marked offline, or last fetched_at is stale,
    returns a record where is_stale(now) evaluates to True.
    """
    health_row = get_device_health(conn, device_id)
    if not health_row:
        # Never heard from device -> stale
        return DeviceHealthRecord(
            device_id=device_id,
            last_event_at=None,
            staleness_threshold_minutes=staleness_threshold_minutes,
        )

    online = bool(health_row["online"])
    fetched_iso = health_row["fetched_at"].replace("Z", "+00:00")
    fetched_dt = datetime.fromisoformat(fetched_iso)

    # If device was marked offline by webhook, or fetched_at is older than threshold
    if not online or (now - fetched_dt) > timedelta(minutes=staleness_threshold_minutes):
        return DeviceHealthRecord(
            device_id=device_id,
            last_event_at=None,
            staleness_threshold_minutes=staleness_threshold_minutes,
        )

    return DeviceHealthRecord(
        device_id=device_id,
        last_event_at=fetched_dt,
        staleness_threshold_minutes=staleness_threshold_minutes,
    )


# --- 4. Tick Function ---

def run_scheduler_tick(
    conn: sqlite3.Connection,
    now: datetime,
    notifier: Notifier,
    staleness_threshold_minutes: int = 120,
) -> Dict[str, Any]:
    """
    Execute a single absence detection evaluation tick across all routines:
    1. For every routine, compute status via derive_status().
    2. If missed: INSERT OR IGNORE into escalations table for (routine_id, local_date).
    3. If unknown / snoozed / observed: never escalate.
    4. For any escalations where notified_at IS NULL: call notifier.send_escalation().
       If send succeeds, mark notified_at = now. If it raises, leave notified_at NULL.
    """
    routines = list_routines(conn)
    statuses: Dict[str, str] = {}
    new_escalations = 0

    for r_row in routines:
        routine_id = r_row["routine_id"]
        routine = row_to_routine(r_row)
        health = build_device_health_record(
            conn=conn,
            device_id=routine.device_id,
            now=now,
            staleness_threshold_minutes=staleness_threshold_minutes,
        )

        status = derive_status(routine, health, now)
        statuses[routine_id] = status

        if status == STATUS_MISSED:
            tz = ZoneInfo(routine.timezone)
            local_now = now.astimezone(tz)
            local_date = local_now.strftime("%Y-%m-%d")
            fired_at_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            inserted = record_escalation(
                conn=conn,
                routine_id=routine_id,
                local_date=local_date,
                fired_at=fired_at_iso,
                ignore_conflicts=True,
            )
            if inserted:
                new_escalations += 1

    # Notification delivery with retry
    unnotified = get_unnotified_escalations(conn)
    delivered_count = 0

    for esc in unnotified:
        esc_routine_id = esc["routine_id"]
        local_date = esc["local_date"]
        fired_at = esc["fired_at"]

        r_row = get_routine(conn, esc_routine_id)
        if not r_row:
            continue
        routine_obj = row_to_routine(r_row)

        try:
            notifier.send_escalation(routine_obj, local_date, fired_at)
            mark_escalation_notified(conn, esc_routine_id, local_date, now)
            delivered_count += 1
        except Exception as e:
            logger.warning(
                "Escalation notification failed for routine %s (%s): %s",
                esc_routine_id,
                local_date,
                e,
            )
            # Leave notified_at NULL to retry next tick

    return {
        "tick_time": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "routines_checked": len(routines),
        "statuses": statuses,
        "new_escalations": new_escalations,
        "delivered_notifications": delivered_count,
        "pending_notifications": len(get_unnotified_escalations(conn)),
    }


# --- 5. Scheduler Class ---

class Scheduler:
    """
    Background scheduler process driven by a threading.Event.
    Periodically executes tick() and manages failure resilience.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        clock: Optional[Clock] = None,
        notifier: Optional[Notifier] = None,
        interval_seconds: float = 1.0,
        staleness_threshold_minutes: int = 120,
    ):
        self.db_path = db_path
        self.clock = clock or RealClock()
        self.notifier = notifier or RecordingNotifier()
        self.interval_seconds = interval_seconds
        self.staleness_threshold_minutes = staleness_threshold_minutes

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[Exception] = None

    def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Execute a single deterministic scheduler tick."""
        tick_time = now or self.clock.now()
        conn = get_connection(self.db_path)
        try:
            result = run_scheduler_tick(
                conn=conn,
                now=tick_time,
                notifier=self.notifier,
                staleness_threshold_minutes=self.staleness_threshold_minutes,
            )
            self.last_tick_at = tick_time
            return result
        finally:
            conn.close()

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the scheduler background thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        """Internal loop executing tick every interval_seconds."""
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                self.last_error = e
                logger.error("Exception in scheduler tick:\n%s", traceback.format_exc())
            self._stop_event.wait(timeout=self.interval_seconds)

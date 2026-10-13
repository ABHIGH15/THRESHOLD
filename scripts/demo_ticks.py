#!/usr/bin/env python3
"""
scripts/demo_ticks.py — Demonstration of Threshold Absence Detection Engine.
Runs a FakeClock through a missed-routine day, showing the status progression,
single-fire escalation deduplication, notification delivery, and post-miss late event handling.
"""

from datetime import datetime, timezone
import os
import sqlite3
import sys

# Ensure repository root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.threshold.scheduler import FakeClock, RecordingNotifier, Scheduler
from src.threshold.store import (
    get_connection,
    init_db,
    save_routine,
    update_routine_last_satisfied,
    upsert_device_health,
)


def run_demo(db_path: str = "demo_absence.db") -> None:
    # 1. Initialize SQLite database
    conn = init_db(db_path)

    # 2. Configure routine: Morning Front Door (07:00-09:00 America/Chicago, 30m escalation delay)
    save_routine(
        conn=conn,
        routine_id="rt_morning_door",
        property_id="prop_oak_street",
        name="Morning Front Door",
        device_id="cam_front_porch",
        expected_event_type="motion_detected",
        window_start="07:00",
        window_end="09:00",
        timezone_name="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri"],
        escalation_delay_minutes=30,
    )

    # 3. Healthy device status
    upsert_device_health(
        conn=conn,
        device_id="cam_front_porch",
        online=True,
        fetched_at="2026-10-12T14:10:00Z",
        source="probe",
    )
    conn.close()

    # 4. Initialize FakeClock and Scheduler
    # Monday 2026-10-12: America/Chicago is in CDT (UTC-5)
    clock = FakeClock(datetime(2026, 10, 12, 11, 45, 0, tzinfo=timezone.utc))  # 06:45 CDT
    notifier = RecordingNotifier()
    scheduler = Scheduler(db_path=db_path, clock=clock, notifier=notifier)

    # Simulation timeline
    timeline_steps = [
        ("11:45 UTC", "06:45 CDT", "Before window starts (07:00)", None),
        ("12:30 UTC", "07:30 CDT", "Inside active window (07:00-09:00)", None),
        ("14:15 UTC", "09:15 CDT", "Past window_end, inside 30m delay", None),
        ("14:35 UTC", "09:35 CDT", "Past deadline (09:30 CDT) -> Miss detected!", None),
        ("14:45 UTC", "09:45 CDT", "Consecutive tick past miss (deduped)", None),
        ("15:00 UTC", "10:00 CDT", "Late event arrives satisfying window", "2026-10-12T13:30:00Z"),
    ]

    print("=" * 105)
    print(f"{'THRESHOLD DETERMINISTIC ABSENCE DETECTION ENGINE — SIMULATED DAY':^105}")
    print("=" * 105)
    print(f"| {'Tick (UTC)':<12} | {'Local (CDT)':<12} | {'Routine Status':<15} | {'Escalations':<11} | {'Notified':<10} | {'Context Description':<38} |")
    print("|" + "-" * 14 + "|" + "-" * 14 + "|" + "-" * 17 + "|" + "-" * 13 + "|" + "-" * 12 + "|" + "-" * 40 + "|")

    for utc_str, cdt_str, desc, late_sat_time in timeline_steps:
        # Advance clock to matching UTC time
        target_hour = int(utc_str.split(":")[0])
        target_min = int(utc_str.split(":")[1].split()[0])
        clock.set_time(datetime(2026, 10, 12, target_hour, target_min, 0, tzinfo=timezone.utc))

        # If a late event arrived, update store
        if late_sat_time:
            c = get_connection(db_path)
            update_routine_last_satisfied(c, "rt_morning_door", late_sat_time)
            c.close()

        # Tick scheduler
        res = scheduler.tick()
        status = res["statuses"]["rt_morning_door"]

        # Check DB escalations count
        c = get_connection(db_path)
        cur = c.execute("SELECT COUNT(*), COUNT(notified_at) FROM escalations WHERE routine_id = 'rt_morning_door'")
        esc_count, notified_count = cur.fetchone()
        c.close()

        notified_display = "Yes" if notified_count > 0 else "No"
        if status == "observed" and esc_count > 0:
            notified_display = "Preserved"

        print(f"| {utc_str:<12} | {cdt_str:<12} | {status:<15} | {esc_count:<11} | {notified_display:<10} | {desc:<38} |")

    print("=" * 105)


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "demo_absence.db"
    run_demo(db)

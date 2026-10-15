#!/usr/bin/env python3
"""
Seed Dashboard States and Real-Browser Verification Script (scripts/seed_dashboard_states.py).
Standard library only; zero third-party dependencies.

Builds a TEMP database using the real engine with a simulated clock for 6 dashboard states:
1. Calm (observed) -> evidence/dashboard/01_calm_observed.png
2. Watching -> evidence/dashboard/02_watching.png
3. Unknown (device offline) -> evidence/dashboard/03_unknown_offline.png
4. Missed (with alert) -> evidence/dashboard/04_missed_alert.png
5. Check-needed with snapshot -> evidence/dashboard/05_check_needed_snapshot.png
6. Check-needed with no snapshot -> evidence/dashboard/06_check_needed_no_snapshot.png

Opens each page in a REAL browser (Google Chrome headless), takes a high-resolution screenshot,
and counts JavaScript browser console errors per page.
"""

from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.threshold.dashboard import DashboardServer, start_dashboard_server
from src.threshold.notifier import RecordingNotifier
from src.threshold.scheduler import run_scheduler_tick
from src.threshold.store import (
    init_db,
    save_event,
    save_investigation,
    save_routine,
    set_app_state,
    upsert_device_health,
)


CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def find_chrome() -> Optional[str]:
    """Locate Google Chrome binary."""
    if os.path.exists(CHROME_PATH):
        return CHROME_PATH
    import shutil
    for name in ("google-chrome", "chromium", "google-chrome-stable", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def get_real_sample_jpeg() -> bytes:
    """Return real JPEG binary bytes from existing snapshots or ring evidence."""
    search_paths = [
        "snapshots/rt_morning_medicine_latest.jpg",
        "evidence/ring/c2_case_a2_latest_in_range_recording.jpg",
        "evidence/ring/snapshot_success_20261013T132000Z.jpg",
    ]
    for p in search_paths:
        if os.path.exists(p) and os.path.getsize(p) > 1000:
            with open(p, "rb") as f:
                return f.read()
    # Minimal valid JPEG fallback
    return (
        b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB\x00C\x00"
        + b"\x08" * 64
        + b"\xFF\xC0\x00\x0B\x08\x00\x10\x00\x10\x01\x01\x11\x00\xFF\xDA\x00\x08\x01\x01\x00\x00?\x00"
        + b"\x00" * 32
        + b"\xFF\xD9"
    )


def build_state(
    state_name: str,
    db_path: str,
    snapshot_dir: str,
) -> datetime:
    """
    Populate SQLite database using real Threshold store & engine for the specified state.
    Returns the simulated clock time (UTC datetime).
    """
    conn = init_db(db_path)
    sample_jpeg = get_real_sample_jpeg()

    # Clear prior data
    conn.execute("DELETE FROM routines")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM escalations")
    conn.execute("DELETE FROM corrections")
    conn.execute("DELETE FROM device_health")
    conn.execute("DELETE FROM investigations")
    conn.execute("DELETE FROM app_state")
    conn.commit()

    set_app_state(conn, "daemon_status", "Active")
    set_app_state(conn, "daemon_started_at", "2026-10-12T12:00:00Z")
    set_app_state(conn, "daemon_tick_count", "42")
    set_app_state(conn, "last_poll_at", "2026-10-12T13:20:00Z")

    if state_name == "calm_observed":
        # State 1: 100% Routines Observed, Calm, No Alerts
        sim_now = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)  # 08:30 CDT

        save_routine(
            conn,
            routine_id="rt_morning_medicine",
            property_id="prop_mom",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            escalation_delay_minutes=30,
            last_satisfied_at="2026-10-12T13:15:00Z",
        )
        upsert_device_health(conn, "sensor_cabinet", 1, "2026-10-12T13:20:00Z", "webhook")
        save_event(
            conn,
            "webhook",
            "ev_med_01",
            "sensor_cabinet",
            "motion_detected",
            "2026-10-12T13:15:00Z",
            "2026-10-12T13:15:01Z",
            {"test": True},
        )
        run_scheduler_tick(conn=conn, now=sim_now, notifier=RecordingNotifier())

    elif state_name == "watching":
        # State 2: Active Watching inside open window (07:00–09:00 CDT), waiting for activity
        sim_now = datetime(2026, 10, 12, 12, 30, tzinfo=timezone.utc)  # 07:30 CDT

        save_routine(
            conn,
            routine_id="rt_morning_medicine",
            property_id="prop_mom",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(conn, "sensor_cabinet", 1, "2026-10-12T12:20:00Z", "webhook")
        run_scheduler_tick(conn=conn, now=sim_now, notifier=RecordingNotifier())

    elif state_name == "unknown_offline":
        # State 3: Device Offline / Stale -> "Unknown — device offline, not escalated"
        sim_now = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)  # 08:30 CDT

        save_routine(
            conn,
            routine_id="rt_front_door",
            property_id="prop_mom",
            name="Front Door Motion",
            device_id="cam_front_yard",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            escalation_delay_minutes=30,
        )
        # Device stale (>120m ago) and offline
        upsert_device_health(conn, "cam_front_yard", 0, "2026-10-12T05:00:00Z", "poll")
        run_scheduler_tick(conn=conn, now=sim_now, notifier=RecordingNotifier())

    elif state_name == "missed_alert":
        # State 4: Window Missed + Escalation Delay Passed -> MISSED + Red Emergency Banner
        sim_now = datetime(2026, 10, 12, 14, 35, tzinfo=timezone.utc)  # 09:35 CDT (deadline was 09:30)

        save_routine(
            conn,
            routine_id="rt_morning_medicine",
            property_id="prop_mom",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            escalation_delay_minutes=30,
            snapshot_device_id="cam_front",
        )
        upsert_device_health(conn, "sensor_cabinet", 1, "2026-10-12T14:30:00Z", "webhook")
        upsert_device_health(conn, "cam_front", 1, "2026-10-12T14:30:00Z", "webhook")

        # Save snapshot file
        snap_file = os.path.join(snapshot_dir, "rt_morning_medicine_latest.jpg")
        with open(snap_file, "wb") as f:
            f.write(sample_jpeg)

        notifier = RecordingNotifier()
        run_scheduler_tick(conn=conn, now=sim_now, notifier=notifier)

    elif state_name == "check_needed_snapshot":
        # State 5: Check Needed Investigation with Camera Snapshot Thumbnail
        sim_now = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)  # 08:00 CDT

        save_routine(
            conn,
            routine_id="rt_day_routine",
            property_id="prop_mom",
            name="Morning Routine",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        upsert_device_health(conn, "cam_front", 1, "2026-10-12T12:50:00Z", "webhook")

        snap_filename = "snapshot_cam_front_20261012_021000.jpg"
        snap_path = os.path.join(snapshot_dir, snap_filename)
        with open(snap_path, "wb") as f:
            f.write(sample_jpeg)

        save_investigation(
            conn,
            event_key="evt_cn_snap_01",
            device_id="cam_front",
            created_at="2026-10-12T07:10:00Z",
            mode="replay (scripted)",
            provider_name="mock",
            tier="check_needed",
            decision="check_needed",
            reason_code="quiet_hours_unexpected_activity",
            recommendation="(replay, scripted) Unexpected motion at 02:10 outside routine window. Routine status is watching. Snapshot attached for the caregiver to review.",
            evidence_json=[
                {"tool": "check_routine_status", "ok": True, "text": "Routine rt_day_routine status is watching."},
                {"tool": "pull_snapshot", "ok": True, "text": f"Snapshot retrieved successfully for device cam_front saved to {snap_path} (captured at 2026-10-12T02:10:00Z)."},
            ],
        )
        run_scheduler_tick(conn=conn, now=sim_now, notifier=RecordingNotifier())

    elif state_name == "check_needed_no_snapshot":
        # State 6: Check Needed Investigation with No Camera Image Available
        sim_now = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)  # 08:00 CDT

        save_routine(
            conn,
            routine_id="rt_day_routine",
            property_id="prop_mom",
            name="Morning Routine",
            device_id="sensor_hallway",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        upsert_device_health(conn, "sensor_hallway", 1, "2026-10-12T12:50:00Z", "webhook")

        save_investigation(
            conn,
            event_key="evt_cn_nosnap_01",
            device_id="sensor_hallway",
            created_at="2026-10-12T08:40:00Z",
            mode="replay (scripted)",
            provider_name="mock",
            tier="check_needed",
            decision="check_needed",
            reason_code="quiet_hours_unexpected_activity",
            recommendation="(replay, scripted) Unexpected motion at 03:40 outside routine window, but camera snapshot returned media unavailable.",
            evidence_json=[
                {"tool": "check_routine_status", "ok": True, "text": "Routine rt_day_routine status is watching."},
                {"tool": "pull_snapshot", "ok": False, "text": "Media unavailable: camera returned 416 (no recording in range)."},
            ],
        )
        run_scheduler_tick(conn=conn, now=sim_now, notifier=RecordingNotifier())

    conn.close()
    return sim_now


def capture_state_screenshot(
    state_key: str,
    output_png: str,
    chrome_bin: str,
) -> Tuple[int, int, str]:
    """
    Launch ephemeral server for state, open in real Chrome browser, take screenshot,
    and parse browser console error count.
    Returns (exit_code, console_error_count, stderr_log).
    """
    temp_dir = tempfile.mkdtemp(prefix=f"threshold_state_{state_key}_")
    db_path = os.path.join(temp_dir, "threshold.db")
    snap_dir = os.path.join(temp_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    user_data_dir = os.path.join(temp_dir, "chrome_user_data")
    os.makedirs(user_data_dir, exist_ok=True)

    try:
        sim_now = build_state(state_key, db_path, snap_dir)

        # Boot local dashboard server on ephemeral port
        server = start_dashboard_server(
            db_path=db_path,
            port=0,
            host="127.0.0.1",
            household_name="Mom's House — Dallas",
            timezone_name="America/Chicago",
            snapshot_dir=snap_dir,
            simulated_now=sim_now,
            fallback_ports=False,
        )
        port = server.server_address[1]
        target_url = f"http://127.0.0.1:{port}/"

        # Give server thread a moment to start
        time.sleep(0.2)

        os.makedirs(os.path.dirname(os.path.abspath(output_png)), exist_ok=True)

        cmd = [
            chrome_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={user_data_dir}",
            "--window-size=1280,1024",
            "--enable-logging=stderr",
            "--v=1",
            target_url,
            f"--screenshot={output_png}",
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        server.shutdown()
        server.server_close()

        # Parse browser console errors from Chrome stderr
        # Chrome formats page console messages as: [pid:tid:ts:LEVEL:CONSOLE:line] "msg"
        console_errors = []
        for line in proc.stderr.splitlines():
            if ":CONSOLE:" in line:
                # Check if it's an error level
                if "ERROR" in line or "Uncaught" in line or "error" in line.lower():
                    console_errors.append(line)

        return proc.returncode, len(console_errors), proc.stderr

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    chrome_bin = find_chrome()
    if not chrome_bin:
        print("ERROR: Google Chrome binary not found on system.", file=sys.stderr)
        return 1

    output_dir = os.path.join("evidence", "dashboard")
    os.makedirs(output_dir, exist_ok=True)

    states = [
        ("calm_observed", "01_calm_observed.png", "Calm (Routine Observed, No Alerts)"),
        ("watching", "02_watching.png", "Active Watching (Window Open)"),
        ("unknown_offline", "03_unknown_offline.png", "Unknown (Device Offline, Not Escalated)"),
        ("missed_alert", "04_missed_alert.png", "Missed (Alert Fired, Emergency Banner)"),
        ("check_needed_snapshot", "05_check_needed_snapshot.png", "Check Needed (with Snapshot Preview)"),
        ("check_needed_no_snapshot", "06_check_needed_no_snapshot.png", "Check Needed (No Camera Image Available)"),
    ]

    print("=" * 70)
    print("THRESHOLD DASHBOARD: REAL-BROWSER VERIFICATION")
    print(f"Browser:  {chrome_bin}")
    print(f"Output:   {output_dir}")
    print("=" * 70)

    all_passed = True
    results = []

    for state_key, filename, description in states:
        dest_png = os.path.join(output_dir, filename)
        code, err_count, stderr_log = capture_state_screenshot(state_key, dest_png, chrome_bin)

        if code == 0 and os.path.exists(dest_png):
            fsize = os.path.getsize(dest_png)
            print(f"✓ [{state_key}] {description}")
            print(f"  ↳ Screenshot: {dest_png} ({fsize:,} bytes)")
            print(f"  ↳ Console Errors: {err_count}")
            results.append((filename, fsize, err_count, "OK"))
        else:
            print(f"✗ [{state_key}] FAILED (exit code {code})")
            print(f"  ↳ Stderr: {stderr_log[:200]}")
            results.append((filename, 0, err_count, "FAIL"))
            all_passed = False

    print("-" * 70)
    print("VERIFICATION SUMMARY:")
    for fn, sz, errs, status in results:
        print(f"  • {fn:<32} {sz:>8} bytes | console errors: {errs} [{status}]")
    print("=" * 70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

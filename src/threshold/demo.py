"""
src/threshold/demo.py — End-to-End Interactive & Automated Hackathon Demo Runner.

Orchestrates the complete 3-beat emotional narrative:
  Beat 1: Empathy & Morning Routine Baseline (07:30 CDT)
          Normal front door activity observed, routine satisfied, dashboard green.
  Beat 2: The Silence & Pattern-Absence Detection (09:31 CDT)
          Medicine cabinet window + 30m grace buffer closes with no activity.
          Edge-transition fires: watching -> missed.
          Dashboard turns red with emergency alert banner.
          Notifier captures snapshot and dispatches photo alert with [False alarm] [Got it] buttons.
  Beat 3: Caretaker Resolution & Graceful Recovery (09:35 - 10:00 CDT)
          Caretaker taps [False alarm]. Correction recorded, escalation acknowledged.
          Late morning activity arrives, system updates to observed with full audit trail preserved.
"""

from datetime import datetime, timezone
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.threshold.dashboard import start_dashboard_server, DashboardServer
from src.threshold.ingest import ingest_event
from src.threshold.models import DeviceHealthRecord, Routine
from src.threshold.notifier import (
    format_escalation_text,
    format_expected_by,
    format_health_state,
    format_last_activity,
    make_callback_data,
    Notifier,
    TelegramNotifier,
)
from src.threshold.scheduler import FakeClock, Scheduler
from src.threshold.store import (
    ack_escalation,
    get_connection,
    init_db,
    record_correction,
    save_routine,
    upsert_device_health,
)

logger = logging.getLogger("threshold.demo")

# Minimal 1x1 valid JPEG fallback if no fixture is found on disk
FALLBACK_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00"
    b"\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19"
    b"\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9"
    b"=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00"
    b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03"
    b"\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
)


class DemoNotifier(Notifier):
    """
    Demo notifier that captures notifications in-memory, renders rich ASCII smartphone
    cards in terminal output, and optionally dispatches real Telegram photo alerts if configured.
    """

    def __init__(
        self,
        snapshot_path: Optional[str] = None,
        live_telegram: bool = False,
        telegram_notifier: Optional[TelegramNotifier] = None,
    ):
        self.snapshot_path = snapshot_path
        self.live_telegram = live_telegram
        self.telegram_notifier = telegram_notifier
        self.sent_escalations: List[Dict[str, Any]] = []
        self.sent_reminders: List[Dict[str, Any]] = []

    def send_escalation(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        r_id = getattr(routine, "routine_id", getattr(routine, "name", "routine")).lower().replace(" ", "_")
        expected_by = format_expected_by(routine)
        health_state = format_health_state(routine, device_health)
        last_act = format_last_activity(routine, device_health)
        snap_id = getattr(routine, "snapshot_device_id", None) or routine.device_id
        alert_text = format_escalation_text(routine, fired_at, device_health)

        record = {
            "routine_id": r_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
            "snapshot_path": self.snapshot_path,
            "alert_text": alert_text,
            "expected_by": expected_by,
            "health_state": health_state,
            "last_activity": last_act,
            "snapshot_device_id": snap_id,
        }
        self.sent_escalations.append(record)

        if self.live_telegram and self.telegram_notifier:
            try:
                self.telegram_notifier.send_escalation(
                    routine=routine,
                    local_date=local_date,
                    fired_at=fired_at,
                    device_health=device_health,
                )
            except Exception as exc:
                logger.warning("Live Telegram escalation delivery failed: %s", exc)

        return {
            "channel": "demo",
            "delivered": True,
            "type": "photo" if self.snapshot_path else "text",
            "has_photo": bool(self.snapshot_path),
            "snapshot_path": self.snapshot_path,
        }

    def send_reminder(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        r_id = getattr(routine, "routine_id", getattr(routine, "name", "routine")).lower().replace(" ", "_")
        record = {
            "routine_id": r_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
        }
        self.sent_reminders.append(record)
        return {
            "channel": "demo",
            "delivered": True,
            "type": "text",
            "has_photo": False,
            "snapshot_path": None,
        }


def prepare_demo_snapshots(snapshot_dir: str = "snapshots") -> str:
    """
    Ensure demonstration snapshot assets are available in snapshot_dir.
    Copies real Ring JPEG evidence if present, or writes valid JPEG fallback.
    Returns the path to the primary demo snapshot.
    """
    os.makedirs(snapshot_dir, exist_ok=True)
    target_latest = os.path.join(snapshot_dir, "rt_morning_medicine_latest.jpg")
    target_date = os.path.join(snapshot_dir, "rt_morning_medicine_2026-10-12.jpg")

    evidence_candidates = [
        os.path.join("evidence", "ring", "snapshot_success_20261013T132000Z.jpg"),
        os.path.join("snapshots", "rt_morning_door_latest.jpg"),
    ]

    source_bytes = None
    for cand in evidence_candidates:
        if os.path.isfile(cand) and os.path.getsize(cand) > 32:
            try:
                with open(cand, "rb") as f:
                    source_bytes = f.read()
                break
            except Exception:
                pass

    if not source_bytes:
        source_bytes = FALLBACK_JPEG_BYTES

    for target in (target_latest, target_date):
        with open(target, "wb") as f:
            f.write(source_bytes)

    return target_latest


def print_header() -> None:
    """Print top-level ASCII demo header banner."""
    print()
    print("=" * 86)
    print("  THRESHOLD — PATTERN-ABSENCE DETECTION FOR ELDERLY CARE")
    print("  'Ring tells you when something happens. Threshold tells you when something doesn't.'")
    print("=" * 86)
    print("  Caregiver-support notification tool. Not an emergency or medical device.")
    print("  Does not contact emergency services.")
    print("  SIMULATED EVENTS. Times are simulated. The camera image is a real image from the")
    print("  Ring Partner API sandbox.")
    print("-" * 86)
    print("  Dallas, TX  |  Resident: Margaret (Age 82, living independently)")
    print("  Hardware:   Ring Motion Sensor (Medicine Cabinet) + Playground Device (Camera)")
    print("              Ring Contact Sensor (Front Door)       + Ring Doorbell Cam (Porch)")
    print("  Stack:      Python Standard Library only + SQLite + Telegram + Ring Partner API")
    print("  Cloud:      Runs on your own machine; no Threshold-operated cloud.")
    print("              Data goes only to Ring (the source) and Telegram (alerts).")
    print("-" * 86)


def print_beat_header(beat_num: int, title: str, local_time: str, subtitle: str) -> None:
    """Print stylized beat announcement block."""
    print()
    print("┌" + "─" * 84 + "┐")
    print(f"│  BEAT {beat_num}: {title.upper():<56} [Simulated: {local_time}] │")
    print(f"│  {subtitle:<80} │")
    print("└" + "─" * 84 + "┘")


def print_timeline_row(
    utc_time: str,
    cdt_time: str,
    med_status: str,
    door_status: str,
    escalation_desc: str,
    notes: str,
) -> None:
    """Print formatted timeline tick row."""
    # Status badges
    med_badge = (
        "🟢 OBSERVED" if med_status == "observed"
        else ("🔴 MISSED" if med_status == "missed"
        else ("🟡 WATCHING" if med_status == "watching" else med_status))
    )
    door_badge = (
        "🟢 OBSERVED" if door_status == "observed"
        else ("🔴 MISSED" if door_status == "missed"
        else ("🟡 WATCHING" if door_status == "watching" else door_status))
    )
    print(
        f"  [{cdt_time}] Medicine: {med_badge:<13} | Door: {door_badge:<13} | "
        f"Alerts: {escalation_desc:<10} | {notes}"
    )


def print_phone_alert_card(alert_record: Dict[str, Any]) -> None:
    """Print smartphone card showing the ACTUAL alert text from the notifier formatting function."""
    alert_text = alert_record.get("alert_text", "")
    snap_path = alert_record.get("snapshot_path", "snapshots/rt_morning_medicine_latest.jpg")

    # Strip HTML tags for clean terminal display
    plain_text = re.sub(r"<[^>]+>", "", alert_text) if alert_text else ""

    snap_size_kb = 0.0
    if snap_path and os.path.exists(snap_path):
        snap_size_kb = os.path.getsize(snap_path) / 1024.0

    print()
    print("  ╔" + "═" * 74 + "╗")
    print("  ║  📱 TELEGRAM NOTIFICATION RECEIVED ON CAREGIVER PHONE (Sarah)         ║")
    print("  ╠" + "═" * 74 + "╣")
    if snap_path and os.path.exists(snap_path):
        photo_label = f"📷 [most recent Ring camera image: {snap_path} ({snap_size_kb:.1f} KB JPEG)]"
        print(f"  ║  {photo_label:<72} ║")
        print("  ║" + " " * 74 + "║")
    for line in plain_text.strip().split("\n"):
        print(f"  ║  {line:<72} ║")
    print("  ║" + " " * 74 + "║")
    print("  ║  ┌────────────────────────────────┐    ┌───────────────────────────────┐ ║")
    print("  ║  │       [ False alarm ]          │    │          [ Got it ]           │ ║")
    print("  ║  └────────────────────────────────┘    └───────────────────────────────┘ ║")
    print("  ╚" + "═" * 74 + "╝")
    print()


def sync_device_poll(conn: sqlite3.Connection, now_iso: str) -> None:
    """Simulate regular background polling (RingClient.poll_once) keeping device health fresh."""
    devices = [
        "sensor_front_door",
        "sensor_medicine_cabinet",
        "Playground Device",
        "cam_front_porch",
    ]
    for dev in devices:
        upsert_device_health(
            conn=conn,
            device_id=dev,
            online=True,
            fetched_at=now_iso,
            source="history",
            reported_at=now_iso,
        )


def run_demo(
    db_path: Optional[str] = None,
    dashboard_port: int = 8080,
    dashboard_host: str = "0.0.0.0",
    enable_dashboard: bool = True,
    open_browser: bool = False,
    interactive: bool = False,
    speed: float = 1.0,
    live_telegram: bool = False,
    cleanup: bool = False,
) -> Dict[str, Any]:
    """
    Execute the scripted 3-beat demonstration end-to-end.

    Args:
        db_path: Path to demo SQLite database. If None, uses demo_threshold.db.
        dashboard_port: Preferred port for web dashboard (defaults to 8080 with auto-fallback).
        dashboard_host: Network host for dashboard (0.0.0.0 binds all interfaces for LAN).
        enable_dashboard: If True, boots embedded HTTP dashboard server.
        open_browser: If True, launches default web browser to dashboard URL.
        interactive: If True, pauses for user input between beats.
        speed: Pacing delay multiplier (0.0 for instant/tests, 1.0 for live presentation).
        live_telegram: If True and credentials exist in .env, sends real Telegram photo.
        cleanup: If True, removes demo SQLite database upon exit.

    Returns:
        Structured execution result dictionary.
    """
    def pace_pause(seconds: float) -> None:
        if speed > 0.0 and seconds > 0.0:
            time.sleep(seconds * speed)

    def prompt_step(msg: str) -> None:
        if interactive:
            print(f"\n  👉 [PAUSE] {msg}")
            try:
                input("     Press [Enter] to proceed to next beat...")
            except (KeyboardInterrupt, EOFError):
                print()

    # 1. Database Setup
    target_db = db_path or "demo_threshold.db"
    if os.path.exists(target_db):
        try:
            os.remove(target_db)
        except Exception:
            pass

    conn = init_db(target_db)

    # 2. Seed Hardware Devices
    sync_device_poll(conn, "2026-10-12T11:45:00Z")

    # 3. Seed Routines:
    # Medicine Cabinet: 07:00-09:00 CDT, 30m escalation delay (deadline 09:30 CDT)
    # Front Door Morning Walk: 07:00-10:00 CDT, 30m escalation delay
    save_routine(
        conn=conn,
        routine_id="rt_morning_medicine",
        property_id="prop_dallas_house",
        name="Morning Medicine Cabinet",
        device_id="sensor_medicine_cabinet",
        expected_event_type="motion_detected",
        window_start="07:00",
        window_end="09:00",
        timezone_name="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        escalation_delay_minutes=30,
        snapshot_device_id="Playground Device",
    )
    save_routine(
        conn=conn,
        routine_id="rt_morning_door",
        property_id="prop_dallas_house",
        name="Front Door Morning Walk",
        device_id="sensor_front_door",
        expected_event_type="motion_detected",
        window_start="07:00",
        window_end="10:00",
        timezone_name="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        escalation_delay_minutes=30,
        snapshot_device_id="cam_front_porch",
    )
    conn.close()

    # 4. Prepare Snapshots
    snapshot_path = prepare_demo_snapshots("snapshots")

    # 5. Boot Dashboard Server
    dash_server: Optional[DashboardServer] = None
    dash_url = ""
    if enable_dashboard:
        dash_server = start_dashboard_server(
            host=dashboard_host,
            port=dashboard_port,
            db_path=target_db,
            household_name="Margaret's Home — Dallas",
            timezone_name="America/Chicago",
            snapshot_dir="snapshots",
            fallback_ports=True,
        )
        actual_port = dash_server.server_address[1]
        disp_host = "localhost" if dashboard_host == "0.0.0.0" else dashboard_host
        dash_url = f"http://{disp_host}:{actual_port}"

        if open_browser:
            try:
                import webbrowser
                webbrowser.open(dash_url)
            except Exception:
                pass

    # 6. Initialize Clock & Scheduler
    # Monday Oct 12, 2026: America/Chicago is in CDT (UTC-5)
    # Start at 06:45 CDT (11:45 UTC)
    clock = FakeClock(datetime(2026, 10, 12, 11, 45, 0, tzinfo=timezone.utc))

    # Optional live Telegram notifier
    tg_notifier: Optional[TelegramNotifier] = None
    if live_telegram:
        bot_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if bot_tok and chat_id:
            tg_notifier = TelegramNotifier(bot_token=bot_tok, chat_id=chat_id)

    demo_notifier = DemoNotifier(
        snapshot_path=snapshot_path,
        live_telegram=live_telegram,
        telegram_notifier=tg_notifier,
    )
    scheduler = Scheduler(db_path=target_db, clock=clock, notifier=demo_notifier)

    # -------------------------------------------------------------
    # PRESENTATION RUNNER
    # -------------------------------------------------------------
    print_header()
    if dash_url:
        print(f"  🖥️  Live Status Dashboard: {dash_url}")
        print(f"  📊 Programmatic JSON API: {dash_url}/api/status")
        print("-" * 86)

    # Initial baseline tick at 06:45 CDT
    res_0 = scheduler.tick()
    pace_pause(1.0)

    # =============================================================
    # BEAT 1: EMPATHY & MORNING ROUTINE BASELINE (07:30 CDT)
    # =============================================================
    print_beat_header(
        beat_num=1,
        title="Empathy & Normal Morning Routine",
        local_time="07:30 CDT (12:30 UTC)",
        subtitle="Resident wakes up and steps outside for the morning newspaper.",
    )

    # Advance clock to 07:30 CDT (12:30 UTC)
    clock.set_time(datetime(2026, 10, 12, 12, 30, 0, tzinfo=timezone.utc))

    # Ingest front door sensor event and sync device freshness
    c_beat1 = get_connection(target_db)
    sync_device_poll(c_beat1, "2026-10-12T12:30:00Z")
    ingest_event(
        conn=c_beat1,
        source="history",
        external_id="ev_demo_door_0730",
        device_id="sensor_front_door",
        event_type="motion_detected",
        sub_type="human",
        occurred_at="2026-10-12T12:30:00Z",
        raw_json={"event": "motion", "door": "front"},
    )
    c_beat1.close()

    # Scheduler tick
    res_1 = scheduler.tick()
    st_med_1 = res_1["statuses"]["rt_morning_medicine"]
    st_door_1 = res_1["statuses"]["rt_morning_door"]

    print_timeline_row("12:30 UTC", "07:30 CDT", st_med_1, st_door_1, "0 active", "Front door motion detected (Margaret got paper)")
    print("     ↳ Front Door Walk satisfied -> 🟢 OBSERVED. Medicine cabinet window is open -> 🟡 WATCHING.")
    print("     ↳ Dashboard shows 100% routines observed today, 1 routine satisfied, 1 routine watching.")

    pace_pause(1.5)
    prompt_step("Beat 1 complete. Margaret has completed her front door walk.")

    # =============================================================
    # BEAT 2: THE SILENCE & PATTERN-ABSENCE DETECTION (09:00 -> 09:31 CDT)
    # =============================================================
    print_beat_header(
        beat_num=2,
        title="The Silence & Absence Detection",
        local_time="09:00 -> 09:31 CDT (14:00 -> 14:31 UTC)",
        subtitle="Medicine cabinet window ends at 09:00. 30m grace buffer expires at 09:30. Silence.",
    )

    # 1. Advance to 09:00 CDT (Window ends, 30m grace buffer active)
    clock.set_time(datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc))
    c_sync_9 = get_connection(target_db)
    sync_device_poll(c_sync_9, "2026-10-12T14:00:00Z")
    c_sync_9.close()

    res_2a = scheduler.tick()
    print_timeline_row("14:00 UTC", "09:00 CDT", res_2a["statuses"]["rt_morning_medicine"], res_2a["statuses"]["rt_morning_door"], "0 active", "Window closed, 30m grace buffer active (no panic)")

    pace_pause(1.2)

    # 2. Advance to 09:31 CDT (Deadline 09:30 passed -> ABSENCE DETECTED!)
    clock.set_time(datetime(2026, 10, 12, 14, 31, 0, tzinfo=timezone.utc))
    c_sync_931 = get_connection(target_db)
    sync_device_poll(c_sync_931, "2026-10-12T14:31:00Z")
    c_sync_931.close()

    res_2b = scheduler.tick()
    st_med_2 = res_2b["statuses"]["rt_morning_medicine"]
    st_door_2 = res_2b["statuses"]["rt_morning_door"]

    # Verify escalation was created in DB
    c_beat2 = get_connection(target_db)
    cur = c_beat2.execute("SELECT COUNT(*) FROM escalations WHERE routine_id = 'rt_morning_medicine'")
    esc_count = cur.fetchone()[0]
    c_beat2.close()

    print_timeline_row("14:31 UTC", "09:31 CDT", st_med_2, st_door_2, f"{esc_count} fired", "⚠️ DEADLINE PASSED: Edge-transition into MISSED!")
    print("     ↳ Deterministic edge-trigger fires strictly once! Alert dispatched with camera snapshot.")
    print("     ↳ Web Dashboard displays pulsing MISSED ROUTINE banner at top.")

    # Render smartphone notification card
    if demo_notifier.sent_escalations:
        print_phone_alert_card(demo_notifier.sent_escalations[-1])

    pace_pause(1.5)

    # 3. Subsequent tick at 09:45 CDT (Demonstrating deduplication)
    clock.set_time(datetime(2026, 10, 12, 14, 45, 0, tzinfo=timezone.utc))
    c_sync_945 = get_connection(target_db)
    sync_device_poll(c_sync_945, "2026-10-12T14:45:00Z")
    c_sync_945.close()

    res_2c = scheduler.tick()
    print_timeline_row("14:45 UTC", "09:45 CDT", res_2c["statuses"]["rt_morning_medicine"], res_2c["statuses"]["rt_morning_door"], f"{esc_count} (deduped)", "Subsequent tick: deduplication prevents re-alerting")

    pace_pause(1.2)
    prompt_step("Beat 2 complete. Caregiver has received snapshot alert on phone.")

    # =============================================================
    # BEAT 3: CARETAKER RESOLUTION & PEACE OF MIND (09:35 -> 10:00 CDT)
    # =============================================================
    print_beat_header(
        beat_num=3,
        title="Caretaker Resolution & Late Activity",
        local_time="09:35 -> 10:00 CDT (14:35 -> 15:00 UTC)",
        subtitle="Sarah views the camera snapshot, confirms Mom is resting, and taps [False alarm].",
    )

    # 1. Caregiver taps False alarm at 09:35 CDT (14:35 UTC)
    c_beat3 = get_connection(target_db)
    record_correction(
        conn=c_beat3,
        id="corr_rt_morning_medicine_2026-10-12",
        device_id="sensor_medicine_cabinet",
        event_type="motion_detected",
        time_bucket="07:00-09:00",
        label="false_alarm",
        created_at="2026-10-12T14:35:00Z",
        ignore_conflicts=True,
    )
    ack_escalation(
        conn=c_beat3,
        routine_id="rt_morning_medicine",
        local_date="2026-10-12",
        acked_at="2026-10-12T14:35:00Z",
    )
    sync_device_poll(c_beat3, "2026-10-12T14:35:00Z")
    c_beat3.close()

    # Advance clock to 09:35 CDT and tick
    clock.set_time(datetime(2026, 10, 12, 14, 35, 0, tzinfo=timezone.utc))
    res_3a = scheduler.tick()

    print_timeline_row("14:35 UTC", "09:35 CDT", res_3a["statuses"]["rt_morning_medicine"], res_3a["statuses"]["rt_morning_door"], "Acked (0 active)", "Sarah tapped [False alarm] -> correction recorded")
    print("     ↳ Dashboard routine card updates to: 🟢 Acknowledged (False alarm). Emergency banner clears.")

    pace_pause(1.5)

    # 2. Delayed Ring event from 08:30 CDT (13:30 UTC) backfills at 10:00 CDT
    clock.set_time(datetime(2026, 10, 12, 15, 0, 0, tzinfo=timezone.utc))
    c_late = get_connection(target_db)
    sync_device_poll(c_late, "2026-10-12T15:00:00Z")
    ingest_event(
        conn=c_late,
        source="history",
        external_id="ev_demo_med_late_1000",
        device_id="sensor_medicine_cabinet",
        event_type="motion_detected",
        sub_type="motion",
        occurred_at="2026-10-12T13:30:00Z",
        received_at="2026-10-12T15:00:00Z",
        raw_json={"event": "motion", "cabinet": "medicine"},
    )
    c_late.close()

    res_3b = scheduler.tick()
    st_med_3 = res_3b["statuses"]["rt_morning_medicine"]
    st_door_3 = res_3b["statuses"]["rt_morning_door"]

    print_timeline_row("15:00 UTC", "10:00 CDT", st_med_3, st_door_3, "Preserved", "Late event arrives (occurred 08:30 CDT) -> Routine satisfied (OBSERVED)")
    print("     ↳ Status updates cleanly to 🟢 OBSERVED. Full audit log of escalation and correction preserved.")

    pace_pause(1.2)

    # Compile final execution results
    result_data = {
        "success": True,
        "dashboard_url": dash_url,
        "database": target_db,
        "server": dash_server,
        "beat1": {
            "status_door": st_door_1,
            "status_medicine": st_med_1,
        },
        "beat2": {
            "status_medicine": st_med_2,
            "escalations_count": esc_count,
            "alert_delivered": len(demo_notifier.sent_escalations) > 0,
            "snapshot_used": snapshot_path,
        },
        "beat3": {
            "status_medicine_final": st_med_3,
            "correction_id": "corr_rt_morning_medicine_2026-10-12",
            "acked": True,
        },
        "total_escalations": esc_count,
    }

    if enable_dashboard:
        print(f"  💡 Dashboard remains running at {dash_url}. Press Ctrl+C to terminate.")

    if cleanup and os.path.exists(target_db):
        try:
            os.remove(target_db)
        except Exception:
            pass

    return result_data


def main() -> int:
    """CLI entry point for scripts/run_demo.py and python3 -m src.threshold demo."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="threshold demo",
        description="Threshold 3-Beat End-to-End Hackathon Demonstration Runner",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Prompt user between narrative beats")
    parser.add_argument("--fast", "-f", action="store_true", help="Run without artificial pacing delays")
    parser.add_argument("--speed", type=float, default=1.0, help="Pacing speed multiplier (default: 1.0)")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Dashboard HTTP port (default: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard HTTP host (default: 0.0.0.0)")
    parser.add_argument("--open", "-o", action="store_true", help="Open web dashboard in default browser")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable embedded web status dashboard")
    parser.add_argument("--telegram", action="store_true", help="Send live Telegram photo alert if configured in .env")
    parser.add_argument("--db", default=None, help="Path to demo SQLite database (default: demo_threshold.db)")
    parser.add_argument("--keep-running", action="store_true", help="Keep dashboard running after demo concludes")

    args = parser.parse_args()

    # Load environment variables if available
    from scripts.probe_ring import load_env
    load_env(".env")

    speed = 0.0 if args.fast else args.speed

    try:
        res = run_demo(
            db_path=args.db,
            dashboard_port=args.port,
            dashboard_host=args.host,
            enable_dashboard=not args.no_dashboard,
            open_browser=args.open,
            interactive=args.interactive,
            speed=speed,
            live_telegram=args.telegram,
            cleanup=False,
        )

        if not args.no_dashboard:
            if args.interactive or args.keep_running:
                try:
                    while True:
                        time.sleep(1.0)
                except KeyboardInterrupt:
                    print("\nDemo stopped cleanly.")
            else:
                # Give a short graceful window for dashboard inspection
                if speed > 0:
                    time.sleep(2.0)

        return 0 if res["success"] else 1
    except KeyboardInterrupt:
        print("\nDemo aborted by user.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

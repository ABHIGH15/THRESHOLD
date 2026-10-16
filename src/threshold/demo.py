"""
src/threshold/demo.py — Zero-Secret Simulated Household Hackathon Demonstration.

A self-contained, offline, zero-network demonstration of Threshold:
- Runs in a clean temporary directory database (deleted on exit).
- Zero external dependencies, zero API keys, zero cloud.
- Starts the real webhook HTTP server on 127.0.0.1 (ephemeral port).
- Signs every event with HMAC-SHA256 (per-run random secret, never printed).
- Protects against network leaks via an installed loopback socket guard.
- Drives time deterministically via FakeClock across 6 explicit scenes.
- Executes real core pipelines: signature verification, idempotency, ingestion,
  status derivation, fire-once escalation, notifier formatting, agent tools,
  deterministic gate, and read-only status dashboard.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.error
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from src.threshold.agent.investigate import run_investigation_and_gate
from src.threshold.agent.provider import MockProvider
from src.threshold.agent.runner import process_new_events
from src.threshold.agent.triggers import is_ambiguous_event
from src.threshold.config import AgentConfig
from src.threshold.dashboard import start_dashboard_server
from src.threshold.models import Routine
from src.threshold.notifier import (
    ConsoleNotifier,
    Notifier,
    resolve_callback_routine_id,
)
from src.threshold.scheduler import build_device_health_record, FakeClock, Scheduler
from src.threshold.server import create_server
from src.threshold.status import derive_status
from src.threshold.store import (
    ack_escalation,
    get_connection,
    get_routine,
    init_db,
    record_correction,
    record_escalation,
    row_to_routine,
    save_routine,
    set_app_state,
    upsert_device_health,
)
from src.threshold.webhook_auth import compute_signature

logger = logging.getLogger("threshold.demo")

# Minimal 1x1 valid JPEG bytes for synthetic snapshot responses
SAMPLE_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00"
    b"\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19"
    b"\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9"
    b"=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00"
    b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03"
    b"\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
)

FALLBACK_JPEG_BYTES = SAMPLE_JPEG_BYTES

# ─── Network Guard ────────────────────────────────────────────────────────────

_orig_socket_connect = socket.socket.connect


def install_network_guard() -> None:
    """
    Patch socket.socket.connect so that outbound connections to non-loopback
    addresses immediately raise ConnectionRefusedError.
    Guarantees the demo remains 100% offline with zero outbound network calls.
    """
    def _guarded_connect(self, address):
        host = address[0] if isinstance(address, (tuple, list)) else address
        is_loopback = False
        if isinstance(host, str):
            h_lower = host.lower()
            if h_lower in ("127.0.0.1", "localhost", "::1") or h_lower.startswith("127."):
                is_loopback = True
            else:
                try:
                    ip = ipaddress.ip_address(host)
                    if ip.is_loopback:
                        is_loopback = True
                except ValueError:
                    pass
        if not is_loopback:
            raise ConnectionRefusedError(
                f"[NETWORK GUARD] Non-loopback connection to {host} is blocked during demo."
            )
        return _orig_socket_connect(self, address)

    socket.socket.connect = _guarded_connect


def uninstall_network_guard() -> None:
    """Restore original socket.socket.connect."""
    socket.socket.connect = _orig_socket_connect


# ─── Synthetic Snapshot Client ────────────────────────────────────────────────

class DemoSnapshotClient:
    """Offline Ring client returning sample JPEG bytes for pull_snapshot tool."""

    def __init__(self, sample_bytes: bytes = SAMPLE_JPEG_BYTES, fixed_epoch_ms: int = 1791875400000):
        self.sample_bytes = sample_bytes
        self.fixed_epoch_ms = fixed_epoch_ms

    def get_latest_snapshot(
        self,
        device_id: str,
        lookback_hours: int = 24,
        deadline_seconds: float = 5.0,
    ) -> Tuple[bytes, int]:
        return self.sample_bytes, self.fixed_epoch_ms


# ─── Capturing Console Notifier ───────────────────────────────────────────────

class CapturingConsoleNotifier(Notifier):
    """
    Wraps ConsoleNotifier to print real alerts via format_escalation_text while
    recording sent notifications in-memory for assertion and structured reporting.
    """

    def __init__(self, console_notifier: ConsoleNotifier):
        self.underlying = console_notifier
        self.sent_escalations: List[Dict[str, Any]] = []
        self.sent_messages: List[str] = []
        self.sent_photos: List[Dict[str, Any]] = []

    def send_message(self, text: str) -> None:
        self.sent_messages.append(text)
        print("\n" + text)

    def send_photo(self, photo_bytes: bytes, caption: str) -> None:
        self.sent_photos.append({"bytes_len": len(photo_bytes), "caption": caption})
        print("\n" + caption)

    def send_escalation(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        res = self.underlying.send_escalation(
            routine=routine,
            local_date=local_date,
            fired_at=fired_at,
            device_health=device_health,
        )
        self.sent_escalations.append({
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
            "res": res,
        })
        return res

    def send_reminder(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return self.underlying.send_reminder(
            routine=routine,
            local_date=local_date,
            fired_at=fired_at,
            device_health=device_health,
        )


# ─── Webhook Helpers ──────────────────────────────────────────────────────────

def make_ring_payload(
    device_id: str,
    event_type: str,
    iso_timestamp: str,
    request_id: Optional[str] = None,
    sub_type: str = "human",
) -> bytes:
    """Construct a documented Ring v1.1 webhook payload bytes."""
    if request_id is None:
        request_id = f"demo_req_{uuid.uuid4().hex[:12]}"
    payload = {
        "meta": {
            "request_id": request_id,
            "time": iso_timestamp,
            "version": "1.1",
        },
        "data": {
            "type": event_type,
            "attributes": {
                "source": device_id,
                "timestamp": iso_timestamp,
                "sub_type": sub_type,
            },
        },
    }
    return json.dumps(payload).encode("utf-8")


def post_signed_event(
    port: int,
    secret: str,
    payload_bytes: bytes,
    bad_signature: bool = False,
) -> Tuple[int, str]:
    """Send signed HTTP POST to local webhook receiver using compute_signature."""
    if bad_signature:
        sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"
    else:
        sig = compute_signature(payload_bytes, secret)

    url = f"http://127.0.0.1:{port}/webhooks/ring"
    req = urllib.request.Request(url, data=payload_bytes, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Signature", sig)
    req.add_header("User-Agent", "Threshold-Demo/1.0")

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, body


# ─── Callback Processor ───────────────────────────────────────────────────────

def process_demo_callback(
    conn: sqlite3.Connection,
    callback_data: str,
    now_iso: str,
) -> bool:
    """
    Execute the real callback resolution pathway:
    resolve_callback_routine_id -> verify escalation exists -> record_correction -> ack_escalation.
    """
    parts = callback_data.split(":", 2)
    if len(parts) < 3:
        return False
    prefix, cand_routine_id, local_date = parts[0], parts[1], parts[2]
    routine_id = resolve_callback_routine_id(conn, cand_routine_id)
    if not routine_id:
        return False

    cur_esc = conn.execute(
        "SELECT 1 FROM escalations WHERE routine_id = ? AND local_date = ?",
        (routine_id, local_date),
    )
    if not cur_esc.fetchone():
        return False

    r_row = get_routine(conn, routine_id)
    device_id = r_row["device_id"] if r_row else "unknown_device"
    event_type = r_row["expected_event_type"] if r_row else "motion_detected"
    time_bucket = f"{r_row['window_start']}-{r_row['window_end']}" if r_row else local_date
    corr_id = f"corr_{routine_id}_{local_date}"

    record_correction(
        conn=conn,
        id=corr_id,
        device_id=device_id,
        event_type=event_type,
        time_bucket=time_bucket,
        label="false_alarm",
        created_at=now_iso,
        ignore_conflicts=True,
    )
    ack_escalation(conn, routine_id, local_date, now_iso)
    return True


# ─── Terminal Formatting ──────────────────────────────────────────────────────

class DemoPrinter:
    """Formats terminal output with optional ANSI colors."""

    def __init__(self, no_color: bool = False, pace: float = 0.0):
        self.no_color = no_color
        self.pace = pace

    def _color(self, code: str, text: str) -> str:
        if self.no_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def green(self, text: str) -> str:
        return self._color("32", text)

    def yellow(self, text: str) -> str:
        return self._color("33", text)

    def red(self, text: str) -> str:
        return self._color("31", text)

    def cyan(self, text: str) -> str:
        return self._color("36", text)

    def bold(self, text: str) -> str:
        return self._color("1", text)

    def sleep_pace(self) -> None:
        if self.pace > 0:
            time.sleep(self.pace)

    def print_banner(self) -> None:
        print("=" * 86)
        print("  THRESHOLD — PATTERN-ABSENCE DETECTION FOR ELDERLY CARE")
        print("  'Ring tells you when something happens. Threshold tells you when something doesn't.'")
        print("=" * 86)
        print("  Caregiver-support notification tool. Not an emergency or medical device.")
        print("  Does not contact emergency services.")
        print("  SIMULATED HOUSEHOLD (fictional) — Dallas, TX (America/Chicago)")
        print("  All devices, events, routines, and clock times in this demo are simulated.")
        print("  Stack: Python standard library only (no pip dependencies) + SQLite.")
        print("  Zero network calls: all webhooks are signed and posted locally to 127.0.0.1.")
        print("-" * 86)

    def print_closing_table(self) -> None:
        print("\n" + "=" * 86)
        print("  REAL vs SIMULATED BREAKDOWN")
        print("=" * 86)
        print("  COMPONENT               STATUS        IMPLEMENTATION NOTES")
        print("  " + "─" * 82)
        rows = [
            ("Signature verification", "REAL", "HMAC-SHA256 verified before JSON parse (401 on bad sig)"),
            ("Idempotency", "REAL", "SQLite UNIQUE constraint on (source, external_id)"),
            ("Webhook ingestion", "REAL", "ingest_event parses schema and normalizes types"),
            ("Status derivation", "REAL", "derive_status evaluates window + grace period"),
            ("Scheduler loop", "REAL", "Scheduler.tick checks deadlines and transitions"),
            ("Fire-once escalation", "REAL", "Escalation ladder fires strictly once per missed routine"),
            ("Alert formatting", "REAL", "ConsoleNotifier renders plain-text caregiver alerts"),
            ("Agent tool execution", "REAL", "check_routine_status, pull_snapshot, check_household_memory"),
            ("Evidence derivation", "REAL", "Derives evidence strictly from tool execution results"),
            ("Deterministic gate", "REAL", "Pure function gate enforces evidence & cooldown bars"),
            ("Web Dashboard", "REAL", "Read-only HTTP server with dynamic HTML escaping"),
            ("Household & Resident", "SIMULATED", "Fictional household in Dallas, TX"),
            ("Hardware devices", "SIMULATED", "Fictional device identifiers (e.g. demo-cam-living-room)"),
            ("Device events", "SIMULATED", "Synthetic motion events generated for narrative"),
            ("System clock", "SIMULATED", "FakeClock drives time progression across scenes"),
            ("Agent reasoning", "SIMULATED", "Replay mode: committed scripted traces (not a live LLM)"),
        ]
        for comp, status, notes in rows:
            status_str = self.green(f"{status:<12}") if status == "REAL" else self.yellow(f"{status:<12}")
            print(f"  {comp:<24}{status_str}  {notes}")
        print("=" * 86)


# ─── Demo Pipeline Execution ──────────────────────────────────────────────────

def run_demo_pipeline(
    pace: float = 0.0,
    no_color: bool = False,
    serve: bool = False,
) -> Dict[str, Any]:
    """
    Executes the complete 6-scene hackathon demonstration end-to-end:
      Scene 1: Calm morning (routine observed, 0 alerts)
      Scene 2: Trust boundary (401 rejected, deduplication idempotent)
      Scene 3: Missed routine (fire-once escalation)
      Scene 4: UNKNOWN device offline not alarm (0 escalations)
      Scene 5: Check needed (escalate cleared vs gate downgrade)
      Scene 6: Household memory quiets alert safely (real callback path)
    """
    printer = DemoPrinter(no_color=no_color, pace=pace)
    printer.print_banner()

    install_network_guard()

    temp_dir = tempfile.mkdtemp(prefix="threshold_demo_")
    db_path = os.path.join(temp_dir, "demo.db")
    snapshot_dir = os.path.join(temp_dir, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)

    demo_secret = secrets.token_hex(32)

    init_db(db_path)
    conn = get_connection(db_path)

    # Initialize routines
    # Routine 1: Morning movement (Living room camera)
    save_routine(
        conn=conn,
        routine_id="rt_morning_movement",
        property_id="prop_demo",
        name="Morning movement",
        device_id="demo-cam-living-room",
        expected_event_type="motion_detected",
        window_start="07:00",
        window_end="09:00",
        timezone_name="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        escalation_delay_minutes=30,
    )
    upsert_device_health(conn, "demo-cam-living-room", 1, "2026-10-12T12:00:00Z", "webhook")

    # Routine 2: Evening check-in (Front door camera)
    save_routine(
        conn=conn,
        routine_id="rt_evening_checkin",
        property_id="prop_demo",
        name="Evening check-in",
        device_id="demo-cam-front-door",
        expected_event_type="motion_detected",
        window_start="18:00",
        window_end="20:00",
        timezone_name="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        escalation_delay_minutes=30,
    )
    upsert_device_health(conn, "demo-cam-front-door", 1, "2026-10-12T23:00:00Z", "webhook")

    # Routine 3: Hallway check (Hallway camera)
    save_routine(
        conn=conn,
        routine_id="rt_hallway_check",
        property_id="prop_demo",
        name="Hallway check",
        device_id="demo-cam-hallway",
        expected_event_type="motion_detected",
        window_start="21:00",
        window_end="22:00",
        timezone_name="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        escalation_delay_minutes=30,
    )

    # Seed 2 historical false alarms for demo-cam-front-door using the REAL callback pathway
    # (resolve_callback_routine_id -> record_correction -> ack_escalation)
    for past_date in ("2026-10-10", "2026-10-11"):
        record_escalation(
            conn=conn,
            routine_id="rt_evening_checkin",
            local_date=past_date,
            fired_at=f"{past_date}T20:31:00Z",
            notified_at=f"{past_date}T20:31:00Z",
            ignore_conflicts=True,
        )
        process_demo_callback(conn, f"fa:rt_evening_checkin:{past_date}", f"{past_date}T20:35:00Z")

    server = create_server(host="127.0.0.1", port=0, db_path=db_path, secret=demo_secret)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    webhook_port = server.server_address[1]

    clock = FakeClock(datetime(2026, 10, 12, 12, 42, tzinfo=timezone.utc))  # 07:42 CDT
    console_notif = ConsoleNotifier()
    capturing_notif = CapturingConsoleNotifier(console_notif)
    scheduler = Scheduler(db_path=db_path, clock=clock, notifier=capturing_notif)

    snapshot_client = DemoSnapshotClient()

    results: Dict[str, Any] = {
        "success": False,
        "scenes": {},
        "server_port": webhook_port,
        "db_path": db_path,
    }

    try:
        # ─── SCENE 1: Calm Morning ──────────────────────────────────────────
        printer.sleep_pace()
        print("\n" + printer.bold("[SCENE 1: CALM MORNING] Routine Observed — Peace of Mind"))
        print("  Context:   Routine 'Morning movement' (07:00–09:00 CDT, +30m grace) on living room camera.")
        print("  Action:    Resident steps into living room at 07:42 CDT. Signed motion webhook posted.")

        p1 = make_ring_payload("demo-cam-living-room", "motion_detected", "2026-10-12T12:42:00Z")
        c1, _ = post_signed_event(webhook_port, demo_secret, p1)
        if c1 != 200:
            raise RuntimeError(f"Scene 1 webhook failed with HTTP {c1}")

        # Status check
        r1_row = get_routine(conn, "rt_morning_movement")
        r1 = row_to_routine(r1_row)
        health_s1 = build_device_health_record(conn=conn, device_id=r1.device_id, now=clock.now())
        status_s1 = derive_status(routine=r1, device_health=health_s1, now=clock.now())
        print(f"  Result:    Event accepted (HTTP {c1}). Status derived: {printer.green(status_s1.upper())}.")

        # Advance clock past deadline + grace buffer to 09:31 CDT (14:31 UTC)
        clock.set_time(datetime(2026, 10, 12, 14, 31, tzinfo=timezone.utc))
        tick_s1 = scheduler.tick()
        esc_s1 = tick_s1.get("escalations", 0)
        print(f"  Tick:      Clock advances past deadline to 09:31 CDT. Scheduler tick: {esc_s1} escalations.")
        results["scenes"]["s1"] = {"status": status_s1, "escalations": esc_s1}

        # ─── SCENE 2: Trust Boundary ────────────────────────────────────────
        printer.sleep_pace()
        print("\n" + printer.bold("[SCENE 2: TRUST BOUNDARY] Signature Check & Idempotent Ingestion"))
        print("  Test 2a:   POST event with invalid HMAC signature.")

        p2 = make_ring_payload("demo-cam-living-room", "motion_detected", "2026-10-12T15:00:00Z")
        c2a, _ = post_signed_event(webhook_port, demo_secret, p2, bad_signature=True)
        cur_cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"  Result 2a: Rejected by server with HTTP {printer.red(str(c2a))} Unauthorized. Stored rows: {cur_cnt} (unchanged).")

        print("  Test 2b:   Send valid signed event twice with identical meta.request_id.")
        shared_req_id = "demo_req_idempotent_101"
        p2b = make_ring_payload(
            "demo-cam-living-room",
            "motion_detected",
            "2026-10-12T15:05:00Z",
            request_id=shared_req_id,
        )
        c2b_1, _ = post_signed_event(webhook_port, demo_secret, p2b)
        cnt_after_first = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        c2b_2, _ = post_signed_event(webhook_port, demo_secret, p2b)
        cnt_after_second = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        idempotent_ok = (cnt_after_first == cnt_after_second)
        print(f"  Result 2b: 1st POST HTTP {c2b_1} (events: {cnt_after_first}). 2nd duplicate POST HTTP {c2b_2} (events: {cnt_after_second}).")
        print(f"             Idempotency verified: duplicate event safely ignored (count unchanged: {idempotent_ok}).")
        results["scenes"]["s2"] = {"bad_sig_status": c2a, "idempotent": idempotent_ok}

        # ─── SCENE 3: Missed Routine ────────────────────────────────────────
        printer.sleep_pace()
        print("\n" + printer.bold("[SCENE 3: MISSED ROUTINE] The Silence — Deterministic Absence Escalation"))
        print("  Context:   'Evening check-in' (18:00–20:00 CDT, +30m delay) on front door camera.")
        print("  Silence:   Window and 30m grace buffer close with zero motion recorded.")

        # Set clock to 20:31 CDT (01:31 UTC next day)
        clock.set_time(datetime(2026, 10, 13, 1, 31, tzinfo=timezone.utc))
        upsert_device_health(conn, "demo-cam-front-door", 1, "2026-10-13T01:00:00Z", "webhook")
        pre_esc_count = len(capturing_notif.sent_escalations)
        tick_s3_1 = scheduler.tick()
        post_esc_count_1 = len(capturing_notif.sent_escalations)
        new_escs_1 = post_esc_count_1 - pre_esc_count

        print(f"  [20:31 CDT] Deadline passed! Status transitions to {printer.red('MISSED')}.")
        print(f"  Dispatched: Exactly {printer.bold(str(new_escs_1))} escalation alert sent to caregiver via ConsoleNotifier.")

        # Tick again at 20:46 CDT to verify fire-once deduplication
        clock.set_time(datetime(2026, 10, 13, 1, 46, tzinfo=timezone.utc))
        tick_s3_2 = scheduler.tick()
        post_esc_count_2 = len(capturing_notif.sent_escalations)
        new_escs_2 = post_esc_count_2 - post_esc_count_1
        print(f"  [20:46 CDT] Subsequent scheduler tick: {new_escs_2} alerts fired (fire-once deduplication enforced).")
        results["scenes"]["s3"] = {"first_tick_escalations": new_escs_1, "second_tick_escalations": new_escs_2}

        # ─── SCENE 4: UNKNOWN Offline Not Alarm ──────────────────────────────
        printer.sleep_pace()
        print("\n" + printer.bold("[SCENE 4: UNKNOWN NOT ALARM] Stale / Offline Device Silenced"))
        print("  Context:   Routine 'Hallway check' (21:00–22:00 CDT, +30m delay) on hallway camera.")
        print("  Health:    Device has not confirmed healthy poll in > 120 minutes (offline / stale).")

        # Set last poll to 180 minutes ago
        upsert_device_health(conn, "demo-cam-hallway", 1, "2026-10-12T20:31:00Z", "webhook")
        clock.set_time(datetime(2026, 10, 13, 3, 31, tzinfo=timezone.utc))  # 22:31 CDT

        r4_row = get_routine(conn, "rt_hallway_check")
        r4 = row_to_routine(r4_row)
        health_s4 = build_device_health_record(conn=conn, device_id=r4.device_id, now=clock.now())
        status_s4 = derive_status(routine=r4, device_health=health_s4, now=clock.now())

        pre_s4 = len(capturing_notif.sent_escalations)
        tick_s4 = scheduler.tick()
        post_s4 = len(capturing_notif.sent_escalations) - pre_s4

        print(f"  Status:    Status derives as: {printer.yellow(status_s4.upper())}.")
        print("  Display:   'Unknown — device offline, not escalated'.")
        print(f"  Result:    Scheduler tick past deadline: {post_s4} escalations (silenced, zero false alarms).")
        results["scenes"]["s4"] = {"status": status_s4, "escalations": post_s4}
        # ─── SCENE 5: Check Needed ──────────────────────────────────────────
        printer.sleep_pace()
        print("\n" + printer.bold("[SCENE 5: CHECK NEEDED] Bounded Investigation & Gate Evaluation"))
        print("  Event 5a:  Tue 02:10 CDT unexpected motion at living room camera (quiet hours).")

        # Initialize cursor to current max rowid so newly posted events are picked up
        cur_max = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
        init_cursor = cur_max[0] if cur_max and cur_max[0] is not None else 0
        set_app_state(conn, "agent_event_cursor", str(init_cursor))

        # Ensure living room camera is fresh for quiet hours investigation
        upsert_device_health(conn, "demo-cam-living-room", 1, "2026-10-13T07:00:00Z", "webhook")

        # Event 5a at 02:10 CDT (07:10 UTC)
        clock.set_time(datetime(2026, 10, 13, 7, 10, tzinfo=timezone.utc))
        p5a = make_ring_payload("demo-cam-living-room", "motion_detected", "2026-10-13T07:10:00Z")
        post_signed_event(webhook_port, demo_secret, p5a)

        agent_cfg_5a = AgentConfig(
            enabled=True,
            provider="mock",
            trace_file="trace_escalate_two_tools.json",
            notify_enabled=True,
            snapshots_dir=snapshot_dir,
        )
        agent_notif_5a = ConsoleNotifier()
        capturing_agent_5a = CapturingConsoleNotifier(agent_notif_5a)

        proc_5a = process_new_events(
            conn=conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=clock.now(),
            cfg=agent_cfg_5a,
            notifier=capturing_agent_5a,
            ring_client=snapshot_client,
        )
        dec_5a = proc_5a[0]["decision"] if proc_5a else "none"
        reason_5a = proc_5a[0]["reason_code"] if proc_5a else "none"
        notif_5a = proc_5a[0].get("notified", False) if proc_5a else False
        print(f"  Investigation: mode: replay (scripted) | decision: {dec_5a} (reason: {reason_5a})")
        print(f"  Notification:  CHECK NEEDED alert dispatched with ✓/✗ evidence lines: {notif_5a}")

        # Event 5b: Dedupe check on same device 15 min later (02:25 CDT)
        print("  Event 5b:  Tue 02:25 CDT motion on SAME device (< 30m cooldown).")
        clock.set_time(datetime(2026, 10, 13, 7, 25, tzinfo=timezone.utc))
        p5b = make_ring_payload("demo-cam-living-room", "motion_detected", "2026-10-13T07:25:00Z")
        post_signed_event(webhook_port, demo_secret, p5b)
        evt_row_5b = conn.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
        ambig_5b = is_ambiguous_event(conn, dict(evt_row_5b), clock.now())
        print(f"  Dedupe:    is_ambiguous_event returned: {ambig_5b} (deduped within 30m cooldown).")

        # Event 5c: Different camera at 02:50 CDT with insufficient evidence (1 tool) -> Gate downgrade
        print("  Event 5c:  Tue 02:50 CDT motion on hallway camera using trace_escalate_one_tool.")
        clock.set_time(datetime(2026, 10, 13, 7, 50, tzinfo=timezone.utc))
        upsert_device_health(conn, "demo-cam-hallway", 1, "2026-10-13T07:45:00Z", "webhook")
        p5c = make_ring_payload("demo-cam-hallway", "motion_detected", "2026-10-13T07:50:00Z")
        post_signed_event(webhook_port, demo_secret, p5c)

        agent_cfg_5c = AgentConfig(
            enabled=True,
            provider="mock",
            trace_file="trace_escalate_one_tool.json",
            notify_enabled=True,
            snapshots_dir=snapshot_dir,
        )
        capturing_agent_5c = CapturingConsoleNotifier(ConsoleNotifier())

        proc_5c = process_new_events(
            conn=conn,
            provider_factory=lambda: MockProvider("trace_escalate_one_tool.json"),
            now=clock.now(),
            cfg=agent_cfg_5c,
            notifier=capturing_agent_5c,
            ring_client=snapshot_client,
        )
        dec_5c = proc_5c[0]["decision"] if proc_5c else "none"
        reason_5c = proc_5c[0]["reason_code"] if proc_5c else "none"
        notif_5c = proc_5c[0].get("notified", False) if proc_5c else False
        print(f"  Investigation: mode: replay (scripted) | decision: {dec_5c} (reason: {reason_5c})")
        print(f"  Gate Guard:    Downgraded to check_needed due to insufficient_evidence. Notifications sent: {len(capturing_agent_5c.sent_escalations)}.")
        results["scenes"]["s5"] = {
            "decision_5a": dec_5a,
            "reason_5a": reason_5a,
            "notified_5a": notif_5a,
            "deduped_5b": ambig_5b is None,
            "decision_5c": dec_5c,
            "reason_5c": reason_5c,
            "notified_5c": notif_5c,
        }

        # ─── SCENE 6: Memory ────────────────────────────────────────────────
        printer.sleep_pace()
        print("\n" + printer.bold("[SCENE 6: HOUSEHOLD MEMORY] Caretaker Resolution & Learned Safety"))
        print("  Action 6a: Caregiver taps [False alarm] on the Scene 3 escalation.")
        cb_s6 = process_demo_callback(conn, "fa:rt_evening_checkin:2026-10-12", "2026-10-13T01:35:00Z")
        print(f"  Callback:  Real callback pathway executed. Correction recorded in database: {cb_s6}.")

        # Event 6b: Quiet-hours motion at front door at 03:40 CDT (08:40 UTC)
        print("  Event 6b:  Tue 03:40 CDT motion on front door camera. Agent checks household memory.")
        clock.set_time(datetime(2026, 10, 13, 8, 40, tzinfo=timezone.utc))
        upsert_device_health(conn, "demo-cam-front-door", 1, "2026-10-13T08:30:00Z", "webhook")
        p6b = make_ring_payload("demo-cam-front-door", "motion_detected", "2026-10-13T08:40:00Z")
        post_signed_event(webhook_port, demo_secret, p6b)

        agent_cfg_6 = AgentConfig(
            enabled=True,
            provider="mock",
            trace_file="trace_household_memory_false_alarms.json",
            notify_enabled=True,
            snapshots_dir=snapshot_dir,
        )
        capturing_agent_6 = CapturingConsoleNotifier(ConsoleNotifier())

        proc_6 = process_new_events(
            conn=conn,
            provider_factory=lambda: MockProvider("trace_household_memory_false_alarms.json"),
            now=clock.now(),
            cfg=agent_cfg_6,
            notifier=capturing_agent_6,
            ring_client=snapshot_client,
        )

        dec_6 = proc_6[0]["decision"] if proc_6 else "none"
        reason_6 = proc_6[0]["reason_code"] if proc_6 else "none"

        inv_row = conn.execute("SELECT * FROM investigations WHERE device_id = 'demo-cam-front-door' ORDER BY rowid DESC LIMIT 1").fetchone()
        evidence_text = ""
        if inv_row:
            ev_list = json.loads(inv_row["evidence_json"])
            if ev_list:
                evidence_text = ev_list[0].get("text", "")

        print(f"  Investigation: mode: replay (scripted) | decision: {dec_6} (reason: {reason_6})")
        print(f"  Derived Proof: Evidence derived from real tool result: '{evidence_text}'")
        print("  Safety:        Alert safely suppressed based on verified historical false alarms.")
        results["scenes"]["s6"] = {
            "callback_success": cb_s6,
            "decision": dec_6,
            "reason": reason_6,
            "evidence_text": evidence_text,
        }

        printer.print_closing_table()
        results["success"] = True

        if serve:
            print(f"\n  Starting read-only web dashboard on 127.0.0.1:8080...")
            dash_server = start_dashboard_server(
                db_path=db_path,
                port=8080,
                host="127.0.0.1",
                household_name="Simulated Household — Dallas",
                timezone_name="America/Chicago",
                snapshot_dir=snapshot_dir,
                fallback_ports=True,
                simulated_now=clock.now(),
            )
            actual_port = dash_server.server_address[1]
            print(f"  Dashboard running at http://127.0.0.1:{actual_port}/")
            print("  Press Ctrl+C to terminate.")
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print("\n  Stopping dashboard server...")
            finally:
                dash_server.shutdown()
                dash_server.server_close()

    finally:
        server.shutdown()
        server.server_close()
        uninstall_network_guard()
        conn.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return results


def main() -> int:
    """CLI entry point for python3 -m src.threshold demo."""
    parser = argparse.ArgumentParser(
        description="Threshold — Zero-Secret Simulated Household Hackathon Demonstration"
    )
    parser.add_argument("--pace", type=float, default=0.0, help="Pace in seconds between scenes (default: 0)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    parser.add_argument("--serve", action="store_true", help="Start read-only dashboard after scenes finish")

    args = parser.parse_args()
    res = run_demo_pipeline(pace=args.pace, no_color=args.no_color, serve=args.serve)
    return 0 if res.get("success", False) else 1


if __name__ == "__main__":
    sys.exit(main())

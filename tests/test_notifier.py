"""
Unit and integration tests for Threshold Notification System & Caretaker Loop (src/threshold/notifier.py).
Tests TelegramNotifier, ConsoleNotifier, RecordingNotifier, and reminder ladder against a local HTTP server.
Covers all 7 specified behaviors:
1. Keyboard structure: inline keyboard has ['False alarm', 'Got it'] buttons with correct callback_data.
2. Foreign chat ignored: updates with chat ID not matching TELEGRAM_CHAT_ID are ignored.
3. 'False alarm' writes a corrections row and sets escalations.acked_at.
4. 'Got it' sets escalations.acked_at without creating a correction.
5. Exactly one reminder after N minutes if unacknowledged.
6. No reminders after ack.
7. Token never in logged errors or exception messages.
8. Offset tracking prevents duplicate update processing.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import http.server
import json
import logging
import os
import tempfile
import threading
import unittest

from src.threshold.models import DeviceHealthRecord, Routine
from src.threshold.notifier import (
    ConsoleNotifier,
    Notifier,
    RecordingNotifier,
    TelegramNotifier,
    detect_image_mime,
    encode_multipart_form_data,
    format_escalation_text,
    format_fired_at,
    format_last_activity,
    format_snapshot_caption_prefix,
    make_callback_data,
    redact_telegram_token,
    resolve_callback_routine_id,
    truncate_caption,
)
from src.threshold.ring_client import MediaUnavailable, RateLimited
from src.threshold.scheduler import FakeClock, Scheduler, run_scheduler_tick
from src.threshold.store import (
    ack_escalation,
    get_connection,
    get_correction,
    get_escalation,
    init_db,
    list_corrections,
    record_escalation,
    save_routine,
    upsert_device_health,
)


class FakeTelegramHandler(http.server.BaseHTTPRequestHandler):
    """Minimal fake Telegram Bot API HTTP handler for integration tests."""

    def log_message(self, format, *args):
        # Suppress standard HTTP request stderr logging
        pass

    def do_POST(self):
        server = self.server
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type and body:
            payload = json.loads(body.decode("utf-8"))
        else:
            payload = {}

        server.requests_log.append({
            "path": self.path,
            "headers": dict(self.headers),
            "payload": payload,
            "body": body,
        })

        if getattr(server, "fail_send_photo_with_500", False) and self.path.endswith("/sendPhoto"):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": False, "error_code": 500, "description": "SendPhoto Failed"}).encode("utf-8")
            self.wfile.write(resp)
            return

        if getattr(server, "fail_with_500", False):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": False, "error_code": 500, "description": "Internal Server Error"}).encode("utf-8")
            self.wfile.write(resp)
            return

        if self.path.endswith("/sendMessage"):
            server.sent_messages.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({
                "ok": True,
                "result": {"message_id": 999, "date": 123456789, "chat": payload.get("chat_id")},
            }).encode("utf-8")
            self.wfile.write(resp)

        elif self.path.endswith("/sendPhoto"):
            server.sent_photos.append({
                "headers": dict(self.headers),
                "body": body,
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({
                "ok": True,
                "result": {"message_id": 1000, "date": 123456789, "photo": []},
            }).encode("utf-8")
            self.wfile.write(resp)

        elif self.path.endswith("/getUpdates"):
            offset = payload.get("offset", 0)
            server.requested_offsets.append(offset)
            # Filter queued updates with update_id >= offset
            matching = [u for u in server.queued_updates if u.get("update_id", 0) >= offset]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": True, "result": matching}).encode("utf-8")
            self.wfile.write(resp)

        elif self.path.endswith("/answerCallbackQuery"):
            server.answered_callbacks.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": True, "result": True}).encode("utf-8")
            self.wfile.write(resp)

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        server = self.server
        server.requests_log.append({
            "path": self.path,
            "headers": dict(self.headers),
            "payload": {},
        })

        if getattr(server, "fail_with_500", False):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": False, "error_code": 500, "description": "Internal Server Error"}).encode("utf-8")
            self.wfile.write(resp)
            return

        if "/getUpdates" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = json.dumps({"ok": True, "result": server.queued_updates}).encode("utf-8")
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()


class TestNotifierSystem(unittest.TestCase):
    """Test suite for Notifier implementations and Telegram interaction loops."""

    def setUp(self):
        # Temp database for tests
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(self.db_path)
        conn.close()

        # Start background fake Telegram HTTP server on ephemeral port
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegramHandler)
        self.server.requests_log = []
        self.server.sent_messages = []
        self.server.sent_photos = []
        self.server.queued_updates = []
        self.server.answered_callbacks = []
        self.server.requested_offsets = []
        self.server.fail_with_500 = False
        self.server.fail_send_photo_with_500 = False

        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self.temp_snap_dir = tempfile.TemporaryDirectory()

        self.sample_routine = Routine(
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        self.sample_routine.routine_id = "rt_morning_door"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp_snap_dir.cleanup()

        for suffix in ("", "-wal", "-shm"):
            p = f"{self.db_path}{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # 1. Keyboard structure
    def test_keyboard_structure_inline_buttons(self):
        """TelegramNotifier sends message with inline keyboard ['False alarm', 'Got it']."""
        notifier = TelegramNotifier(
            bot_token="test_bot_token_123",
            chat_id="5551234",
            base_url=self.base_url,
        )

        notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
        )

        self.assertEqual(len(self.server.sent_messages), 1)
        msg = self.server.sent_messages[0]
        self.assertEqual(msg["chat_id"], "5551234")
        self.assertIn("THRESHOLD ESCALATION", msg["text"])
        self.assertIn("Morning Door", msg["text"])

        reply_markup = msg.get("reply_markup", {})
        inline_keyboard = reply_markup.get("inline_keyboard", [])
        self.assertEqual(len(inline_keyboard), 1)
        row = inline_keyboard[0]
        self.assertEqual(len(row), 2)

        # Button 0: False alarm
        self.assertEqual(row[0]["text"], "False alarm")
        self.assertEqual(row[0]["callback_data"], "fa:rt_morning_door:2026-10-12")

        # Button 1: Got it
        self.assertEqual(row[1]["text"], "Got it")
        self.assertEqual(row[1]["callback_data"], "ak:rt_morning_door:2026-10-12")

    # 2. Foreign chat ignored
    def test_foreign_chat_ignored_chat_isolation(self):
        """Callbacks from chat IDs other than TELEGRAM_CHAT_ID are strictly ignored."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_morning_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            conn,
            routine_id="rt_morning_door",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_token",
            chat_id="5551234",  # Authorized chat
            base_url=self.base_url,
        )

        # Foreign update from unauthorized chat "9998887"
        self.server.queued_updates = [
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb_foreign_1",
                    "data": "ack:rt_morning_door:2026-10-12",
                    "message": {"chat": {"id": 9998887}},
                    "from": {"id": 9998887},
                },
            }
        ]

        processed = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed), 0)

        # Verify escalation was NOT acknowledged
        esc = get_escalation(conn, "rt_morning_door", "2026-10-12")
        self.assertIsNotNone(esc)
        self.assertIsNone(esc["acked_at"])

        # Verify no callback response sent
        self.assertEqual(len(self.server.answered_callbacks), 0)
        conn.close()

    # 3. 'False alarm' writes a corrections row and sets escalations.acked_at
    def test_false_alarm_writes_correction_and_acks(self):
        """'False alarm' callback creates a row in corrections and sets acked_at on the escalation."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_morning_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            conn,
            routine_id="rt_morning_door",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_token",
            chat_id="5551234",
            base_url=self.base_url,
        )

        self.server.queued_updates = [
            {
                "update_id": 10,
                "callback_query": {
                    "id": "cb_false_alarm_1",
                    "data": "false_alarm:rt_morning_door:2026-10-12",
                    "message": {"chat": {"id": 5551234}},
                    "from": {"id": 5551234},
                },
            }
        ]

        processed = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["type"], "false_alarm")

        # Verify escalation acknowledged
        esc = get_escalation(conn, "rt_morning_door", "2026-10-12")
        self.assertIsNotNone(esc["acked_at"])

        # Verify corrections row written
        corrs = list_corrections(conn)
        self.assertEqual(len(corrs), 1)
        self.assertEqual(corrs[0]["device_id"], "cam_front")
        self.assertEqual(corrs[0]["event_type"], "motion_detected")
        self.assertEqual(corrs[0]["label"], "false_alarm")
        self.assertEqual(corrs[0]["time_bucket"], "07:00-09:00")

        # Verify answerCallbackQuery was called
        self.assertEqual(len(self.server.answered_callbacks), 1)
        self.assertEqual(self.server.answered_callbacks[0]["callback_query_id"], "cb_false_alarm_1")
        conn.close()

    # 4. 'Got it' sets escalations.acked_at without creating a correction
    def test_got_it_acks_without_creating_correction(self):
        """'Got it' callback sets acked_at on the escalation but does NOT write to corrections."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_morning_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            conn,
            routine_id="rt_morning_door",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_token",
            chat_id="5551234",
            base_url=self.base_url,
        )

        self.server.queued_updates = [
            {
                "update_id": 20,
                "callback_query": {
                    "id": "cb_ack_1",
                    "data": "ack:rt_morning_door:2026-10-12",
                    "message": {"chat": {"id": 5551234}},
                    "from": {"id": 5551234},
                },
            }
        ]

        processed = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["type"], "ack")

        # Verify escalation acknowledged
        esc = get_escalation(conn, "rt_morning_door", "2026-10-12")
        self.assertIsNotNone(esc["acked_at"])

        # Verify NO corrections row written
        corrs = list_corrections(conn)
        self.assertEqual(len(corrs), 0)

        # Verify answerCallbackQuery was called
        self.assertEqual(len(self.server.answered_callbacks), 1)
        self.assertEqual(self.server.answered_callbacks[0]["callback_query_id"], "cb_ack_1")
        conn.close()

    # 5. Exactly one reminder after N minutes if unacknowledged
    def test_exactly_one_reminder_after_n_minutes(self):
        """If unacknowledged, exactly one reminder is sent after N minutes (default 15)."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_morning_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(
            conn,
            device_id="cam_front",
            online=True,
            fetched_at="2026-10-12T14:45:00Z",
            source="probe",
        )
        conn.close()

        # Miss deadline: 09:30 CDT = 14:30 UTC
        # Tick 1: 14:35 UTC (miss detected -> initial escalation)
        clock = FakeClock(datetime(2026, 10, 12, 14, 35, 0, tzinfo=timezone.utc))
        notifier = RecordingNotifier()
        scheduler = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier, reminder_delay_minutes=15)

        res1 = scheduler.tick()
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(len(notifier.reminders), 0)
        self.assertEqual(res1["delivered_notifications"], 1)
        self.assertEqual(res1["reminded_notifications"], 0)

        # Tick 2: 10 minutes later (14:45 UTC) -> under 15 min -> 0 reminders
        clock.advance(600)
        res2 = scheduler.tick()
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(len(notifier.reminders), 0)
        self.assertEqual(res2["reminded_notifications"], 0)

        # Tick 3: 15 minutes after initial escalation (14:50 UTC) -> reminder fires!
        clock.advance(300)
        res3 = scheduler.tick()
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(len(notifier.reminders), 1)
        self.assertEqual(res3["reminded_notifications"], 1)

        # Tick 4: 5 minutes later (14:55 UTC) -> already reminded -> 0 additional reminders
        clock.advance(300)
        res4 = scheduler.tick()
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(len(notifier.reminders), 1)
        self.assertEqual(res4["reminded_notifications"], 0)

        # Tick 5: 30 minutes later (15:25 UTC) -> still exactly 1 reminder
        clock.advance(1800)
        res5 = scheduler.tick()
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(len(notifier.reminders), 1)
        self.assertEqual(res5["reminded_notifications"], 0)

        # Check SQLite row state: both notified_at and reminded_at are recorded
        conn = get_connection(self.db_path)
        esc = get_escalation(conn, "rt_morning_door", "2026-10-12")
        self.assertIsNotNone(esc["notified_at"])
        self.assertIsNotNone(esc["reminded_at"])
        self.assertIsNone(esc["acked_at"])
        conn.close()

    # 6. No reminders after ack
    def test_no_reminders_after_ack(self):
        """Acknowledging an escalation prevents any reminders from being sent."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_morning_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(
            conn,
            device_id="cam_front",
            online=True,
            fetched_at="2026-10-12T14:45:00Z",
            source="probe",
        )
        conn.close()

        clock = FakeClock(datetime(2026, 10, 12, 14, 35, 0, tzinfo=timezone.utc))
        notifier = RecordingNotifier()
        scheduler = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier, reminder_delay_minutes=15)

        # Tick 1: escalation fired & sent
        scheduler.tick()
        self.assertEqual(len(notifier.sent), 1)

        # Caretaker acknowledges escalation at 14:40 (5 minutes in)
        conn = get_connection(self.db_path)
        ack_escalation(conn, "rt_morning_door", "2026-10-12", "2026-10-12T14:40:00Z")
        conn.close()

        # Advance to 14:55 UTC (20 minutes past miss, past the 15 min reminder threshold)
        clock.advance(1200)
        res = scheduler.tick()

        # NO reminder sent because escalation is acknowledged
        self.assertEqual(len(notifier.reminders), 0)
        self.assertEqual(res["reminded_notifications"], 0)

    # 7. Token never in logged errors
    def test_token_never_in_logged_errors(self):
        """Even when API calls fail with HTTP 500, bot token is redacted from logs and exceptions."""
        secret_token = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_SECRET"
        self.server.fail_with_500 = True

        notifier = TelegramNotifier(
            bot_token=secret_token,
            chat_id="5551234",
            base_url=self.base_url,
        )

        with self.assertLogs("src.threshold.notifier", level="ERROR") as cm:
            with self.assertRaises(RuntimeError) as exc_ctx:
                notifier.send_escalation(
                    routine=self.sample_routine,
                    local_date="2026-10-12",
                    fired_at="2026-10-12T14:35:00Z",
                )

        # Ensure the secret token was never in the exception string
        exc_str = str(exc_ctx.exception)
        self.assertNotIn(secret_token, exc_str)
        self.assertIn("[REDACTED]", exc_str)

        # Ensure the secret token was never in any log message
        for log_line in cm.output:
            self.assertNotIn(secret_token, log_line)
            self.assertIn("[REDACTED]", log_line)

    # 8. Offset tracking prevents duplicate updates
    def test_offset_prevents_duplicate_updates(self):
        """TelegramNotifier updates last_offset so processed updates are not re-requested."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_morning_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            conn,
            routine_id="rt_morning_door",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_token",
            chat_id="5551234",
            base_url=self.base_url,
        )

        # Queue two updates (update_id 101 and 102)
        self.server.queued_updates = [
            {
                "update_id": 101,
                "callback_query": {
                    "id": "cb_1",
                    "data": "ack:rt_morning_door:2026-10-12",
                    "message": {"chat": {"id": 5551234}},
                },
            },
            {
                "update_id": 102,
                "callback_query": {
                    "id": "cb_2",
                    "data": "ack:rt_morning_door:2026-10-12",
                    "message": {"chat": {"id": 5551234}},
                },
            },
        ]

        processed1 = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed1), 2)
        self.assertEqual(notifier.last_offset, 103)

        # On second poll, offset 103 is requested -> empty result
        processed2 = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed2), 0)
        self.assertIn(103, self.server.requested_offsets)
        conn.close()

    # 9. Console notifier smoke test
    def test_console_notifier_smoke_test(self):
        """ConsoleNotifier outputs without raising exceptions."""
        console = ConsoleNotifier()
        console.send_escalation(self.sample_routine, "2026-10-12", "2026-10-12T14:35:00Z")
        console.send_reminder(self.sample_routine, "2026-10-12", "2026-10-12T14:35:00Z")

    # 10. telegram_whoami script integration test
    def test_telegram_whoami_script(self):
        """scripts/telegram_whoami.py resolves chat ID and formats output correctly."""
        from io import StringIO
        import sys
        from scripts.telegram_whoami import get_whoami

        self.server.queued_updates = [
            {
                "update_id": 500,
                "message": {
                    "message_id": 1,
                    "from": {"id": 1234567, "first_name": "Jane", "last_name": "Doe", "username": "janedoe"},
                    "chat": {"id": 1234567, "first_name": "Jane", "last_name": "Doe", "username": "janedoe", "type": "private"},
                    "date": 1791814643,
                    "text": "Hello",
                },
            }
        ]

        old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        old_base = os.environ.get("TELEGRAM_API_BASE")
        os.environ["TELEGRAM_BOT_TOKEN"] = "dummy_token_987"
        os.environ["TELEGRAM_API_BASE"] = self.base_url

        captured_stdout = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured_stdout
            ret = get_whoami()
        finally:
            sys.stdout = old_stdout
            if old_token is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            else:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            if old_base is not None:
                os.environ["TELEGRAM_API_BASE"] = old_base
            else:
                os.environ.pop("TELEGRAM_API_BASE", None)

        self.assertEqual(ret, 0)
        output = captured_stdout.getvalue()
        self.assertIn("Chat ID:   1234567", output)
        self.assertIn("TELEGRAM_CHAT_ID=1234567", output)
        self.assertNotIn("dummy_token_987", output)

    # 11. Console notifier message format (Step 0 a)
    def test_console_notifier_message_content_and_phone_call_disclaimer(self):
        """ConsoleNotifier output contains Escalation label, expected-by time, health, last activity, and plain sentence."""
        from io import StringIO
        import sys

        console = ConsoleNotifier()
        captured = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            console.send_escalation(
                self.sample_routine,
                local_date="2026-10-12",
                fired_at="2026-10-12T14:35:00Z",
            )
            console.send_reminder(
                self.sample_routine,
                local_date="2026-10-12",
                fired_at="2026-10-12T14:35:00Z",
            )
        finally:
            sys.stdout = old_stdout

        out = captured.getvalue()
        # Label Escalation & Reminder
        self.assertIn("ESCALATION", out)
        self.assertIn("REMINDER", out)
        # Routine name
        self.assertIn("Morning Door", out)
        # Expected-by local time
        self.assertIn("09:30 America/Chicago", out)
        # Device state & last activity
        self.assertIn("Device State:", out)
        self.assertIn("Last Activity:", out)
        # Plain sentence that this is a message, not a phone call
        self.assertIn("This is an automated text message notification, not a phone call.", out)

    # 12. callback_data <= 64 bytes with 36-char UUID routine id (Step 0 d)
    def test_callback_data_under_64_bytes_with_uuid(self):
        """callback_data stays strictly at or under 64 bytes even with a 36-character UUID routine id."""
        uuid_routine = Routine(
            property_id="prop_uuid",
            name="UUID Routine",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        uuid_routine.routine_id = "c4b8b6a2-6f29-4d6b-8526-778899aabbcc"  # 36 chars

        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
        )
        notifier.send_escalation(uuid_routine, "2026-10-12", "2026-10-12T14:35:00Z")

        msg = self.server.sent_messages[-1]
        keyboard = msg["reply_markup"]["inline_keyboard"][0]
        for button in keyboard:
            data_bytes = button["callback_data"].encode("utf-8")
            self.assertLessEqual(len(data_bytes), 64, f"callback_data '{button['callback_data']}' exceeds 64 bytes ({len(data_bytes)})")

    # 13. Persisting getUpdates offset & idempotent False alarm across restart (Step 0 b)
    def test_replayed_callback_after_restart_creates_no_second_correction(self):
        """Replaying the same callback after a simulated restart creates NO second correction row."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_restart_test",
            property_id="prop_1",
            name="Restart Routine",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            conn,
            routine_id="rt_restart_test",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        notifier1 = TelegramNotifier(bot_token="tok1", chat_id="5551234", base_url=self.base_url)
        self.server.queued_updates = [
            {
                "update_id": 77,
                "callback_query": {
                    "id": "cb_fa_replay",
                    "data": "fa:rt_restart_test:2026-10-12",
                    "message": {"chat": {"id": 5551234}},
                },
            }
        ]

        # First delivery: creates 1 correction row and records offset 78 in DB
        notifier1.poll_and_process_callbacks(conn)
        corrs = list_corrections(conn)
        self.assertEqual(len(corrs), 1)

        # Simulate process restart: create a new notifier instance on same db
        notifier2 = TelegramNotifier(bot_token="tok1", chat_id="5551234", base_url=self.base_url)
        # Even if the server delivers or replays the same callback update:
        self.server.queued_updates = [
            {
                "update_id": 77,
                "callback_query": {
                    "id": "cb_fa_replay",
                    "data": "fa:rt_restart_test:2026-10-12",
                    "message": {"chat": {"id": 5551234}},
                },
            }
        ]

        # Re-polling should ignore update_id 77 because persisted offset is 78
        notifier2.poll_and_process_callbacks(conn)
        self.assertEqual(len(list_corrections(conn)), 1)
        conn.close()

    # 14. Short poll / zero timeout during scheduler tick (Step 0 c)
    def test_getupdates_uses_zero_timeout_during_scheduler_tick(self):
        """getUpdates uses timeout=0 so scheduler tick can never block."""
        clock = FakeClock(datetime(2026, 10, 12, 14, 35, 0, tzinfo=timezone.utc))
        notifier = TelegramNotifier(bot_token="tok_timeout", chat_id="5551234", base_url=self.base_url)
        scheduler = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier)

        scheduler.tick()

        # Find the getUpdates request sent during tick
        get_updates_reqs = [
            r for r in self.server.requests_log
            if r["path"].endswith("/getUpdates")
        ]
        self.assertGreaterEqual(len(get_updates_reqs), 1)
        last_req = get_updates_reqs[-1]
        self.assertEqual(last_req["payload"].get("timeout"), 0, "Scheduler tick must use timeout=0 for getUpdates")

    # 15. Priority 3: Multipart encoding & sendPhoto tests
    def test_encode_multipart_form_data(self):
        """encode_multipart_form_data formats string fields and binary files into standard multipart."""
        fields = {"chat_id": "12345", "caption": "Hello World"}
        files = {"photo": ("test.jpg", b"\xFF\xD8\xFF\xE0JFIF_TEST", "image/jpeg")}
        boundary = "CustomTestBoundary123"

        body, ctype = encode_multipart_form_data(fields, files, boundary=boundary)

        self.assertEqual(ctype, "multipart/form-data; boundary=CustomTestBoundary123")
        self.assertTrue(body.startswith(b"--CustomTestBoundary123\r\n"))
        self.assertTrue(body.endswith(b"--CustomTestBoundary123--\r\n"))
        self.assertIn(b'name="chat_id"\r\n\r\n12345\r\n', body)
        self.assertIn(b'name="caption"\r\n\r\nHello World\r\n', body)
        self.assertIn(b'name="photo"; filename="test.jpg"\r\n', body)
        self.assertIn(b"Content-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xe0JFIF_TEST\r\n", body)

    def test_send_photo_with_multipart(self):
        """send_photo executes multipart POST to Telegram /sendPhoto endpoint with inline buttons."""
        notifier = TelegramNotifier(bot_token="tok_photo", chat_id="5551234", base_url=self.base_url)
        keyboard = {
            "inline_keyboard": [[{"text": "False alarm", "callback_data": "fa:1"}, {"text": "Got it", "callback_data": "ak:1"}]]
        }
        test_jpeg = b"\xFF\xD8\xFF\xE0" + b"_MOCK_IMAGE_BYTES_LONG_ENOUGH_FOR_VALIDATION_12345_"

        res = notifier.send_photo(
            photo_bytes=test_jpeg,
            caption="🚨 <b>Alert: Morning Window Missed</b>",
            reply_markup=keyboard,
        )

        self.assertEqual(len(self.server.sent_photos), 1)
        received = self.server.sent_photos[0]
        body = received["body"]
        self.assertIn(b"_MOCK_IMAGE_BYTES_", body)
        self.assertIn(b"Morning Window Missed", body)
        self.assertIn(b"False alarm", body)
        self.assertIn(b"Got it", body)
        self.assertIn("multipart/form-data", received["headers"].get("Content-Type", ""))

    def test_send_escalation_with_snapshot_enrichment(self):
        """Escalation with configured camera fetches Ring snapshot and delivers sendPhoto with inline buttons."""
        mock_ring = MockRingSnapshotClient(snapshot_data=b"\xFF\xD8\xFF\xE0" + b"_LIVE_RING_SNAPSHOT_JPEG_LONG_PAYLOAD_")
        notifier = TelegramNotifier(
            bot_token="tok_live_esc",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_ring,
            snapshot_dir=self.temp_snap_dir.name,
        )

        routine_with_cam = Routine(
            property_id="prop_1",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
            snapshot_device_id="cam_kitchen_counter",
        )
        routine_with_cam.routine_id = "rt_medicine"

        res = notifier.send_escalation(
            routine=routine_with_cam,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
        )

        # Snapshot was requested from camera device
        self.assertEqual(mock_ring.requested_devices, ["cam_kitchen_counter"])

        # Photo was sent, NOT text message
        self.assertEqual(len(self.server.sent_photos), 1)
        self.assertEqual(len(self.server.sent_messages), 0)

        photo_body = self.server.sent_photos[0]["body"]
        self.assertIn(b"_LIVE_RING_SNAPSHOT_JPEG_", photo_body)
        self.assertIn(b"THRESHOLD ESCALATION", photo_body)
        self.assertIn(b"Morning Medicine", photo_body)
        self.assertIn(b"False alarm", photo_body)
        self.assertIn(b"Got it", photo_body)

        # Check metadata dict
        self.assertIsInstance(res, dict)
        self.assertEqual(res["type"], "photo")
        self.assertTrue(res["has_photo"])
        self.assertEqual(res["channel"], "telegram")

    def test_send_escalation_falls_back_to_text_when_snapshot_unavailable(self):
        """When RingClient.get_snapshot raises MediaUnavailable, falls back cleanly to sendMessage."""
        mock_ring = MockRingSnapshotClient(raise_error=MediaUnavailable("Camera offline / recording not ready"))
        notifier = TelegramNotifier(
            bot_token="tok_fallback",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_ring,
        )

        res = notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
        )

        # Photo failed, so sendMessage was called as fallback
        self.assertEqual(len(self.server.sent_photos), 0)
        self.assertEqual(len(self.server.sent_messages), 1)
        msg = self.server.sent_messages[0]
        self.assertIn("THRESHOLD ESCALATION", msg.get("text", ""))
        self.assertIn("Morning Door", msg.get("text", ""))

        self.assertIsInstance(res, dict)
        self.assertEqual(res["type"], "text")
        self.assertFalse(res["has_photo"])

    def test_send_escalation_falls_back_to_text_when_send_photo_fails(self):
        """When sendPhoto endpoint fails with HTTP 500, falls back gracefully to sendMessage."""
        mock_ring = MockRingSnapshotClient(snapshot_data=b"\xFF\xD8\xFF\xE0" + b"\x00" * 40)
        self.server.fail_send_photo_with_500 = True

        notifier = TelegramNotifier(
            bot_token="tok_photo_fail",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_ring,
        )

        res = notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
        )

        # sendPhoto was attempted and failed, sendMessage delivered the alert
        self.assertEqual(len(self.server.sent_messages), 1)
        self.assertIn("THRESHOLD ESCALATION", self.server.sent_messages[0].get("text", ""))
        self.assertEqual(res["type"], "text")

    def test_token_redacted_in_multipart_failure(self):
        """Telegram bot token is never exposed in multipart error messages."""
        secret_token = "SECRET_TELEGRAM_TOKEN_999"
        self.server.fail_send_photo_with_500 = True

        notifier = TelegramNotifier(
            bot_token=secret_token,
            chat_id="5551234",
            base_url=self.base_url,
        )

        valid_jpeg = b"\xFF\xD8\xFF\xE0" + b"\x00" * 40
        with self.assertRaises(RuntimeError) as ctx:
            notifier.send_photo(photo_bytes=valid_jpeg, caption="Test")

        err_str = str(ctx.exception)
        self.assertNotIn(secret_token, err_str)
        self.assertIn("[REDACTED]", err_str)

    def test_console_notifier_displays_snapshot_device(self):
        """ConsoleNotifier displays snapshot camera ID when present on routine."""
        from io import StringIO
        import sys

        routine = Routine(
            property_id="prop_1",
            name="Porch Routine",
            device_id="sensor_1",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon"],
            snapshot_device_id="cam_porch_view",
        )
        routine.routine_id = "rt_porch"

        console = ConsoleNotifier()
        captured = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            res = console.send_escalation(routine, local_date="2026-10-12", fired_at="2026-10-12T14:35:00Z")
        finally:
            sys.stdout = old_stdout

        out = captured.getvalue()
        self.assertIn("Snapshot Camera: cam_porch_view", out)
        self.assertEqual(res["channel"], "console")
        self.assertEqual(res["type"], "text")

    def test_detect_image_mime(self):
        """detect_image_mime detects JPEG, PNG, WebP, and application/octet-stream."""
        jpeg_magic = b"\xFF\xD8\xFF\xE0_test"
        png_magic = b"\x89PNG\r\n\x1a\n_test"
        webp_magic = b"RIFF\x00\x00\x00\x00WEBPVP8 "
        other = b"PLAIN_TEXT_NOT_AN_IMAGE"

        self.assertEqual(detect_image_mime(jpeg_magic), "image/jpeg")
        self.assertEqual(detect_image_mime(png_magic), "image/png")
        self.assertEqual(detect_image_mime(webp_magic), "image/webp")
        self.assertEqual(detect_image_mime(other), "application/octet-stream")
        self.assertEqual(detect_image_mime(b""), "application/octet-stream")

    def test_truncate_caption(self):
        """truncate_caption respects 1024-char limit and appends ellipsis when exceeded."""
        short_caption = "<b>Alert</b>: Routine missed."
        self.assertEqual(truncate_caption(short_caption, max_chars=1024), short_caption)

        long_caption = "A" * 1200
        truncated = truncate_caption(long_caption, max_chars=1024)
        self.assertLessEqual(len(truncated), 1024)
        self.assertTrue(truncated.endswith("..."))

        long_with_tag = "<i>" + "B" * 1200
        truncated_tag = truncate_caption(long_with_tag, max_chars=1024)
        self.assertLessEqual(len(truncated_tag), 1024)
        self.assertTrue(truncated_tag.endswith("...</i>"))

    def test_send_photo_rejects_corrupt_or_empty_bytes(self):
        """send_photo raises ValueError for empty, tiny (<32 bytes), or non-image payloads."""
        notifier = TelegramNotifier(bot_token="tok_val", chat_id="5551234", base_url=self.base_url)

        with self.assertRaises(ValueError):
            notifier.send_photo(photo_bytes=b"", caption="Empty")

        with self.assertRaises(ValueError):
            notifier.send_photo(photo_bytes=b"too_short", caption="Short")

        with self.assertRaises(ValueError):
            notifier.send_photo(photo_bytes=b"A" * 40, caption="Not image")

    def test_send_escalation_honors_snapshot_timeout(self):
        """send_escalation passes snapshot_timeout to RingClient.get_snapshot."""
        mock_ring = MockRingSnapshotClient()
        notifier = TelegramNotifier(
            bot_token="tok_time",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_ring,
            snapshot_timeout=3.5,
            snapshot_dir=self.temp_snap_dir.name,
        )

        notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
        )

        self.assertEqual(mock_ring.requested_timeouts, [3.5])

    def test_send_escalation_archives_snapshot_to_disk(self):
        """send_escalation saves snapshot image to configured snapshot_dir."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample_bytes = b"\xFF\xD8\xFF\xE0_ARCHIVE_TEST_BYTES_FOR_SNAPSHOT_"
            mock_ring = MockRingSnapshotClient(snapshot_data=sample_bytes)
            notifier = TelegramNotifier(
                bot_token="tok_arch",
                chat_id="5551234",
                base_url=self.base_url,
                ring_client=mock_ring,
                snapshot_dir=tmp_dir,
            )

            res = notifier.send_escalation(
                routine=self.sample_routine,
                local_date="2026-10-12",
                fired_at="2026-10-12T14:35:00Z",
            )

            expected_file = os.path.join(tmp_dir, f"{self.sample_routine.routine_id}_2026-10-12.jpg")
            latest_file = os.path.join(tmp_dir, f"{self.sample_routine.routine_id}_latest.jpg")

            self.assertTrue(os.path.exists(expected_file))
            self.assertTrue(os.path.exists(latest_file))
            with open(expected_file, "rb") as f:
                self.assertEqual(f.read(), sample_bytes)
            self.assertEqual(res["snapshot_path"], expected_file)

    def test_cli_test_alert_command(self):
        """CLI test-alert command dispatches an alert without crashing."""
        import argparse
        from io import StringIO
        import sys
        from src.threshold.__main__ import run_test_alert_command

        test_args = argparse.Namespace(
            config="threshold.example.json",
            routine=None,
            camera=None,
            no_photo=True,
        )

        captured = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            exit_code = run_test_alert_command(test_args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(exit_code, 0)
        output = captured.getvalue()
        self.assertIn("THRESHOLD TEST ALERT DISPATCH", output)
        self.assertIn("Test alert successfully dispatched!", output)

    def test_hashed_routine_id_in_callback_data_resolves_to_correct_routine(self):
        """Hashed routine_id (>64 byte callback_data) resolves back to the original routine and acks escalation."""
        long_routine_id = "routine_morning_hygiene_and_medication_check_elderly_parent_daily"
        conn = get_connection(self.db_path)
        save_routine(
            conn=conn,
            routine_id=long_routine_id,
            property_id="prop_long",
            name="Extended Morning Check",
            device_id="sensor_cabinet_long",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )

        local_date = "2026-10-12"
        record_escalation(
            conn=conn,
            routine_id=long_routine_id,
            local_date=local_date,
            fired_at="2026-10-12T14:31:00Z",
            notified_at="2026-10-12T14:31:05Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
        )

        # Generate callback_data via make_callback_data
        cb_data = make_callback_data("fa", long_routine_id, local_date)
        expected_hash = hashlib.sha256(long_routine_id.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(cb_data, f"fa:{expected_hash}:{local_date}")
        self.assertLessEqual(len(cb_data.encode("utf-8")), 64)

        # Queue the callback update with the hashed data
        self.server.queued_updates = [
            {
                "update_id": 901,
                "callback_query": {
                    "id": "cb_hashed_fa_1",
                    "from": {"id": 5551234, "is_bot": False, "first_name": "Sarah"},
                    "message": {"message_id": 100, "chat": {"id": 5551234}},
                    "data": cb_data,
                },
            }
        ]

        # Process callbacks
        processed = notifier.poll_and_process_callbacks(conn)

        # Assert callback resolved to the full original routine_id
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["type"], "false_alarm")
        self.assertEqual(processed[0]["routine_id"], long_routine_id)
        self.assertEqual(processed[0]["correction_id"], f"corr_{long_routine_id}_{local_date}")

        # Assert escalation in DB was acknowledged
        cur = conn.execute("SELECT acked_at FROM escalations WHERE routine_id = ?", (long_routine_id,))
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["acked_at"])

        # Assert correction was recorded with the correct device_id
        cur = conn.execute("SELECT * FROM corrections WHERE id = ?", (f"corr_{long_routine_id}_{local_date}",))
        corr_row = cur.fetchone()
        self.assertIsNotNone(corr_row)
        self.assertEqual(corr_row["device_id"], "sensor_cabinet_long")
        self.assertEqual(corr_row["label"], "false_alarm")

        conn.close()

    def test_missed_routine_alert_never_has_last_activity_equal_to_fired_at(self):
        """For a missed routine with no events, Last Activity displays window context and never equals fired_at or health poll timestamp."""
        routine = Routine(
            property_id="prop_test",
            name="Morning Medicine",
            device_id="sensor_medicine_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        # Device health ping timestamp occurred at 14:31:00 UTC (09:31 CDT)
        health_poll_dt = datetime(2026, 10, 12, 14, 31, 0, tzinfo=timezone.utc)
        device_health = DeviceHealthRecord(
            device_id="sensor_medicine_cabinet",
            last_event_at=health_poll_dt,
        )

        fired_at_iso = "2026-10-12T14:31:00Z"
        alert_text = format_escalation_text(routine, fired_at_iso, device_health)

        # 1. Fired at must be formatted in routine's local timezone
        self.assertIn("Fired at: 09:31 CDT (2026-10-12)", alert_text)

        # 2. Last Activity must clearly state absence in window, NOT the health ping time or fired_at
        self.assertIn("Last Activity: None recorded in window (07:00-09:00 CDT)", alert_text)
        self.assertNotIn("Last Activity: 2026-10-12 14:31:00 UTC", alert_text)
        self.assertNotIn("Last Activity: 09:31", alert_text)

        # 3. Last Activity must strictly not equal fired_at or health_poll_dt
        last_act = format_last_activity(routine, device_health)
        fired_str = format_fired_at(fired_at_iso, routine.timezone)
        self.assertNotEqual(last_act, fired_str)
        self.assertEqual(last_act, "None recorded in window (07:00-09:00 CDT)")

    def test_bogus_callback_hash_acks_nothing(self):
        """An unresolvable or bogus callback hash mutates nothing in SQLite, logs a warning, and answers with inactive notice."""
        conn = get_connection(self.db_path)
        routine_id = "rt_valid_morning"
        save_routine(
            conn=conn,
            routine_id=routine_id,
            property_id="prop_valid",
            name="Valid Morning",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        local_date = "2026-10-12"
        record_escalation(
            conn=conn,
            routine_id=routine_id,
            local_date=local_date,
            fired_at="2026-10-12T14:31:00Z",
            notified_at="2026-10-12T14:31:05Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
        )

        # Bogus hash callback query
        bogus_data = f"fa:deadbeefcafebabe:{local_date}"
        self.server.queued_updates = [
            {
                "update_id": 902,
                "callback_query": {
                    "id": "cb_bogus_1",
                    "from": {"id": 5551234, "is_bot": False, "first_name": "Sarah"},
                    "message": {"message_id": 101, "chat": {"id": 5551234}},
                    "data": bogus_data,
                },
            }
        ]

        processed = notifier.poll_and_process_callbacks(conn)

        # Nothing processed
        self.assertEqual(len(processed), 0)

        # Escalation in DB remains untouched and unacknowledged
        cur = conn.execute("SELECT acked_at FROM escalations WHERE routine_id = ?", (routine_id,))
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["acked_at"])

        # Zero corrections recorded
        self.assertEqual(len(list_corrections(conn)), 0)

        # Callback query answered with alert inactive message
        self.assertTrue(any(
            ans.get("callback_query_id") == "cb_bogus_1" and ans.get("text") == "This alert is no longer active."
            for ans in self.server.answered_callbacks
        ))

        conn.close()

    def test_callback_for_valid_routine_without_escalation_writes_nothing(self):
        """A callback for a VALID routine whose local_date has NO escalation row writes nothing and answers 'This alert is no longer active.'"""
        conn = get_connection(self.db_path)
        routine_id = "rt_valid_morning"
        save_routine(
            conn=conn,
            routine_id=routine_id,
            property_id="prop_valid",
            name="Valid Morning",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        local_date = "2026-10-15"

        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
        )

        cb_data = f"fa:{routine_id}:{local_date}"
        self.server.queued_updates = [
            {
                "update_id": 905,
                "callback_query": {
                    "id": "cb_no_esc_1",
                    "from": {"id": 5551234, "is_bot": False, "first_name": "Sarah"},
                    "message": {"message_id": 105, "chat": {"id": 5551234}},
                    "data": cb_data,
                },
            }
        ]

        processed = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed), 0)
        self.assertEqual(len(list_corrections(conn)), 0)
        self.assertTrue(any(
            ans.get("callback_query_id") == "cb_no_esc_1" and ans.get("text") == "This alert is no longer active."
            for ans in self.server.answered_callbacks
        ))
        conn.close()

    def test_bogus_callback_with_two_unacked_escalations_acks_neither(self):
        """When multiple unacknowledged escalations exist, a bogus callback does not fall back to or mutate either escalation."""
        conn = get_connection(self.db_path)
        r1_id = "rt_medicine_box"
        r2_id = "rt_front_door"

        save_routine(
            conn=conn,
            routine_id=r1_id,
            property_id="prop_mult",
            name="Medicine Box",
            device_id="sensor_med",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        save_routine(
            conn=conn,
            routine_id=r2_id,
            property_id="prop_mult",
            name="Front Door",
            device_id="cam_door",
            expected_event_type="motion_detected",
            window_start="08:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )

        local_date = "2026-10-12"
        record_escalation(
            conn=conn,
            routine_id=r1_id,
            local_date=local_date,
            fired_at="2026-10-12T13:31:00Z",
            notified_at="2026-10-12T13:31:05Z",
        )
        record_escalation(
            conn=conn,
            routine_id=r2_id,
            local_date=local_date,
            fired_at="2026-10-12T14:31:00Z",
            notified_at="2026-10-12T14:31:05Z",
        )

        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
        )

        bogus_data = f"ak:0123456789abcdef:{local_date}"
        self.server.queued_updates = [
            {
                "update_id": 903,
                "callback_query": {
                    "id": "cb_bogus_2",
                    "from": {"id": 5551234, "is_bot": False, "first_name": "Sarah"},
                    "message": {"message_id": 102, "chat": {"id": 5551234}},
                    "data": bogus_data,
                },
            }
        ]

        processed = notifier.poll_and_process_callbacks(conn)
        self.assertEqual(len(processed), 0)

        # Neither escalation is acknowledged
        cur1 = conn.execute("SELECT acked_at FROM escalations WHERE routine_id = ?", (r1_id,))
        self.assertIsNone(cur1.fetchone()["acked_at"])

        cur2 = conn.execute("SELECT acked_at FROM escalations WHERE routine_id = ?", (r2_id,))
        self.assertIsNone(cur2.fetchone()["acked_at"])

        # No corrections
        self.assertEqual(len(list_corrections(conn)), 0)

        # Callback answered
        self.assertTrue(any(
            ans.get("callback_query_id") == "cb_bogus_2" and ans.get("text") == "This alert is no longer active."
            for ans in self.server.answered_callbacks
        ))

        conn.close()

    def test_caption_variants_header_present_absent_invalid(self):
        """Verify the three caption variants rendered by format_snapshot_caption_prefix."""
        # Variant 1: Header present and valid
        now = datetime(2026, 10, 12, 14, 32, 0, tzinfo=timezone.utc)
        cap_ms = 1791814643626  # 14:17:23 UTC -> 09:17 CDT (14m ago)
        cap1 = format_snapshot_caption_prefix(cap_ms, "America/Chicago", now=now)
        self.assertIn("Most recent camera image, taken 09:17 CDT (14 min ago). Not live.", cap1)

        # Variant 2: Header absent
        cap2 = format_snapshot_caption_prefix(None, "America/Chicago", now=now)
        self.assertEqual(cap2, "Most recent camera image (capture time unknown). Not live.")

        # Variant 3: Header invalid / unparseable
        cap3 = format_snapshot_caption_prefix(None, "America/Chicago")
        self.assertEqual(cap3, "Most recent camera image (capture time unknown). Not live.")

    def test_send_escalation_with_snapshot_latest_in_range(self):
        """send_escalation uses get_latest_snapshot, formats caption, and dispatches sendPhoto."""
        mock_client = MockRingSnapshotClient(captured_at_ms=1791814643626)
        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_client,
            snapshot_lookback_hours=24,
            snapshot_deadline_seconds=5.0,
        )

        res = notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:31:00Z",
        )

        self.assertEqual(res["type"], "photo")
        self.assertTrue(res["has_photo"])
        self.assertEqual(len(self.server.sent_photos), 1)
        self.assertEqual(mock_client.requested_lookbacks, [24])

        photo_req = self.server.sent_photos[0]
        # Body contains multipart form with caption
        body_str = photo_req["body"].decode("utf-8", errors="replace")
        self.assertIn("Most recent camera image", body_str)
        self.assertIn("Not live.", body_str)
        self.assertIn("🚨 <b>THRESHOLD ESCALATION</b>", body_str)

    def test_send_escalation_fallback_on_416_media_unavailable(self):
        """When get_latest_snapshot raises MediaUnavailable (416), notifier immediately sends text alert."""
        mock_client = MockRingSnapshotClient(raise_error=MediaUnavailable("No media found in range", status_code=416))
        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_client,
        )

        res = notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:31:00Z",
        )

        self.assertEqual(res["type"], "text")
        self.assertFalse(res["has_photo"])
        self.assertEqual(len(self.server.sent_photos), 0)
        self.assertEqual(len(self.server.sent_messages), 1)
        self.assertIn("🚨 <b>THRESHOLD ESCALATION</b>", self.server.sent_messages[0]["text"])

    def test_send_escalation_fallback_on_429_ratelimited_immediately(self):
        """When get_latest_snapshot raises RateLimited (429), notifier immediately sends text alert without sleeping."""
        mock_client = MockRingSnapshotClient(raise_error=RateLimited("Rate limited"))
        notifier = TelegramNotifier(
            bot_token="test_tok",
            chat_id="5551234",
            base_url=self.base_url,
            ring_client=mock_client,
        )

        res = notifier.send_escalation(
            routine=self.sample_routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:31:00Z",
        )

        self.assertEqual(res["type"], "text")
        self.assertEqual(len(self.server.sent_photos), 0)
        self.assertEqual(len(self.server.sent_messages), 1)


class MockRingSnapshotClient:
    """Mock RingClient providing configurable get_snapshot and get_latest_snapshot responses."""

    def __init__(
        self,
        snapshot_data: Optional[bytes] = None,
        raise_error: Optional[Exception] = None,
        captured_at_ms: Optional[int] = None,
    ):
        self.snapshot_data = snapshot_data or (b"\xFF\xD8\xFF\xE0" + b"\x00" * 40)
        self.raise_error = raise_error
        self.captured_at_ms = captured_at_ms
        self.requested_devices: List[str] = []
        self.requested_timestamps: List[int] = []
        self.requested_timeouts: List[Optional[float]] = []
        self.requested_lookbacks: List[int] = []

    def get_snapshot(
        self,
        device_id: str,
        timestamp_ms: int,
        timeout: Optional[float] = None,
    ) -> bytes:
        self.requested_devices.append(device_id)
        self.requested_timestamps.append(timestamp_ms)
        self.requested_timeouts.append(timeout)
        if self.raise_error:
            raise self.raise_error
        return self.snapshot_data

    def get_latest_snapshot(
        self,
        device_id: str,
        lookback_hours: int = 24,
        deadline_seconds: float = 5.0,
    ) -> Tuple[bytes, Optional[int]]:
        self.requested_devices.append(device_id)
        self.requested_lookbacks.append(lookback_hours)
        self.requested_timeouts.append(deadline_seconds)
        if self.raise_error:
            raise self.raise_error
        return self.snapshot_data, self.captured_at_ms


if __name__ == "__main__":
    unittest.main()

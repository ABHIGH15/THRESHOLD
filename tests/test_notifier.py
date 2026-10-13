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
import http.server
import json
import logging
import os
import tempfile
import threading
import unittest

from src.threshold.models import Routine
from src.threshold.notifier import (
    ConsoleNotifier,
    Notifier,
    RecordingNotifier,
    TelegramNotifier,
    redact_telegram_token,
)
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
        payload = json.loads(body.decode("utf-8")) if body else {}

        server.requests_log.append({
            "path": self.path,
            "headers": dict(self.headers),
            "payload": payload,
        })

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
        self.server.queued_updates = []
        self.server.answered_callbacks = []
        self.server.requested_offsets = []
        self.server.fail_with_500 = False

        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

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
        self.assertEqual(row[0]["callback_data"], "false_alarm:rt_morning_door:2026-10-12")

        # Button 1: Got it
        self.assertEqual(row[1]["text"], "Got it")
        self.assertEqual(row[1]["callback_data"], "ack:rt_morning_door:2026-10-12")

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


if __name__ == "__main__":
    unittest.main()

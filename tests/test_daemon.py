"""
Tests for Threshold Background Daemon (tests/test_daemon.py).
Verifies:
1. Daemon initialization and configuration defaults.
2. Startup banner formatting.
3. tick_once() execution and summary return.
4. run(max_ticks=N) clean termination without hanging.
5. poll_once() execution with client.
6. TokenExpired pause behavior without daemon crash.
7. Graceful stop() signaling.
8. Tick exception isolation and loop survival.
9. Heartbeat line formatting across multiple routine statuses.
"""

from datetime import datetime, timezone
import os
import sqlite3
import threading
import time
import unittest
from typing import Any, Dict, List, Optional

from src.threshold.daemon import DaemonConfig, ThresholdDaemon, format_local_time
from src.threshold.notifier import RecordingNotifier
from src.threshold.ring_client import RingClient, TokenExpired
from src.threshold.scheduler import FakeClock
from src.threshold.store import init_db, save_routine


class MockRingClient:
    """Mock RingClient simulating device list, status, and event history."""

    def __init__(self, raise_token_expired: bool = False):
        self.raise_token_expired = raise_token_expired
        self.poll_calls = 0
        self.access_token = "mock_token"

    def list_devices(self) -> List[Dict[str, Any]]:
        if self.raise_token_expired:
            raise TokenExpired("HTTP 401: Ring access token expired", status_code=401)
        self.poll_calls += 1
        return [
            {
                "id": "ava1.ring.device.TEST_CAM_1",
                "attributes": {"name": "Front Porch Cam"},
            }
        ]

    def get_device_status(self, device_id: str) -> Dict[str, Any]:
        return {
            "attributes": {
                "online": True,
                "reported_at": "2026-10-13T12:00:00Z",
            }
        }

    def get_event_history(self, device_id: str, event_types=None) -> List[Dict[str, Any]]:
        return [
            {
                "id": "ev_test_1",
                "attributes": {
                    "event_type": "on_demand",
                    "start": 1791814643626,
                },
            }
        ]


class TestThresholdDaemon(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_db(self.conn)
        self.clock = FakeClock(datetime(2026, 10, 13, 14, 0, 0, tzinfo=timezone.utc))
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.conn.close()

    def test_daemon_init_defaults(self):
        """Daemon initializes with sensible defaults and preserves provided DB."""
        daemon = ThresholdDaemon(conn=self.conn, notifier=self.notifier, clock=self.clock)
        self.assertIsNotNone(daemon.config)
        self.assertEqual(daemon.config.household_name, "Threshold Household")
        self.assertEqual(daemon.config.tick_interval_seconds, 60.0)
        self.assertEqual(daemon.config.poll_interval_seconds, 300.0)
        self.assertFalse(daemon.shutdown_requested)
        self.assertFalse(daemon.polling_paused)
        self.assertEqual(daemon.tick_count, 0)

    def test_daemon_startup_banner_content(self):
        """Startup banner formats correctly with household, routine count, and device count."""
        # Insert a routine
        save_routine(
            conn=self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Medicine",
            device_id="ava1.ring.device.TEST_CAM_1",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        config = DaemonConfig(household_name="Grandma's Cottage", timezone_name="America/Chicago")
        daemon = ThresholdDaemon(config=config, conn=self.conn, notifier=self.notifier, clock=self.clock)

        banner = daemon.build_startup_banner()
        self.assertIn("THRESHOLD v1.0", banner)
        self.assertIn("Grandma's Cottage", banner)
        self.assertIn("America/Chicago", banner)
        self.assertIn("1 loaded", banner)
        self.assertIn("RecordingNotifier", banner)

    def test_daemon_tick_once_executes_and_increments_count(self):
        """tick_once() runs scheduler tick, increments tick_count, and returns results."""
        daemon = ThresholdDaemon(conn=self.conn, notifier=self.notifier, clock=self.clock)
        res = daemon.tick_once()
        self.assertEqual(daemon.tick_count, 1)
        self.assertIn("tick_time", res)
        self.assertIn("routines_checked", res)
        self.assertIn("new_escalations", res)

    def test_daemon_run_with_max_ticks_terminates_cleanly(self):
        """daemon.run(max_ticks=2) completes exactly 2 ticks and exits with code 0."""
        config = DaemonConfig(tick_interval_seconds=0.01, poll_interval_seconds=10.0, enable_polling=False)
        daemon = ThresholdDaemon(config=config, conn=self.conn, notifier=self.notifier, clock=self.clock)

        exit_code = daemon.run(max_ticks=2)
        self.assertEqual(exit_code, 0)
        self.assertEqual(daemon.tick_count, 2)

    def test_daemon_poll_once_with_mock_client_updates_health_and_events(self):
        """poll_once() successfully queries mock client and updates store."""
        mock_client = MockRingClient()
        daemon = ThresholdDaemon(
            conn=self.conn,
            ring_client=mock_client,  # type: ignore
            notifier=self.notifier,
            clock=self.clock,
        )

        poll_res = daemon.poll_once()
        self.assertIsNotNone(poll_res)
        self.assertEqual(poll_res["devices_polled"], 1)
        self.assertEqual(poll_res["events_ingested"], 1)
        self.assertEqual(daemon.poll_count, 1)

        # Verify device_health in DB
        cur = self.conn.execute("SELECT online, source FROM device_health WHERE device_id = 'ava1.ring.device.TEST_CAM_1'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "poll")

    def test_daemon_token_expired_pauses_polling_without_crash(self):
        """When RingClient raises TokenExpired, daemon catches it, pauses polling, and survives."""
        mock_client = MockRingClient(raise_token_expired=True)
        daemon = ThresholdDaemon(
            conn=self.conn,
            ring_client=mock_client,  # type: ignore
            notifier=self.notifier,
            clock=self.clock,
        )

        poll_res = daemon.poll_once()
        self.assertIsNone(poll_res)
        self.assertTrue(daemon.polling_paused)
        self.assertEqual(daemon.poll_count, 0)

        # Subsequent poll_once call returns None immediately while paused
        self.assertIsNone(daemon.poll_once())

    def test_daemon_graceful_stop_signal(self):
        """Calling stop() breaks the daemon loop cleanly."""
        config = DaemonConfig(tick_interval_seconds=1.0, poll_interval_seconds=10.0, enable_polling=False)
        daemon = ThresholdDaemon(config=config, conn=self.conn, notifier=self.notifier, clock=self.clock)

        # Run daemon in separate thread
        t = threading.Thread(target=daemon.run, kwargs={"max_ticks": 100})
        t.start()

        # Let it perform first tick, then signal stop
        time.sleep(0.1)
        daemon.stop()
        t.join(timeout=3.0)

        self.assertFalse(t.is_alive(), "Daemon thread did not terminate within timeout after stop()")
        self.assertTrue(daemon.shutdown_requested)

    def test_daemon_heartbeat_formatting(self):
        """Heartbeat string correctly incorporates time, tick number, and routine statuses."""
        config = DaemonConfig(timezone_name="America/Chicago")
        daemon = ThresholdDaemon(config=config, conn=self.conn, notifier=self.notifier, clock=self.clock)
        daemon.tick_count = 5

        tick_res = {
            "statuses": {
                "rt_morning_medicine": "observed",
                "rt_evening_walk": "watching",
            },
            "new_escalations": 0,
        }
        heartbeat = daemon.format_heartbeat(tick_res, self.clock.now())
        self.assertIn("tick #5", heartbeat)
        self.assertIn("observed ✓", heartbeat)
        self.assertIn("watching", heartbeat)
        self.assertIn("0 escalations", heartbeat)

    def test_daemon_token_hot_reload_resumes_polling(self):
        """When token expires, updating .env allows daemon to reload token and resume polling."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".env") as tf:
            tf.write("RING_ACCESS_TOKEN=old_token\n")
            env_path = tf.name

        try:
            from unittest.mock import patch

            mock_client = MockRingClient(raise_token_expired=True)
            mock_client.access_token = "old_token"
            daemon = ThresholdDaemon(
                conn=self.conn,
                ring_client=mock_client,  # type: ignore
                notifier=self.notifier,
                clock=self.clock,
            )

            # First poll: raises TokenExpired -> pauses polling
            res1 = daemon.poll_once()
            self.assertIsNone(res1)
            self.assertTrue(daemon.polling_paused)

            # Update the temp .env file with fresh token and allow client to succeed
            with open(env_path, "w") as f:
                f.write("RING_ACCESS_TOKEN=fresh_valid_token\n")

            mock_client.raise_token_expired = False

            with patch("src.threshold.daemon.read_env_value", side_effect=lambda k: "fresh_valid_token" if k == "RING_ACCESS_TOKEN" else None):
                res2 = daemon.poll_once()
                self.assertIsNotNone(res2)
                self.assertFalse(daemon.polling_paused)
                self.assertEqual(mock_client.access_token, "fresh_valid_token")
                self.assertEqual(daemon.poll_count, 1)
        finally:
            if os.path.exists(env_path):
                os.remove(env_path)

    def test_daemon_fast_callback_polling(self):
        """poll_callbacks_fast invokes poll_and_process_callbacks on notifier if present."""
        class MockNotifierWithCallbacks(RecordingNotifier):
            def __init__(self):
                super().__init__()
                self.callback_polled = False

            def poll_and_process_callbacks(self, conn, timeout=0):
                self.callback_polled = True
                return [{"action": "false_alarm", "routine_id": "rt_test", "local_date": "2026-10-13"}]

        notifier_cb = MockNotifierWithCallbacks()
        daemon = ThresholdDaemon(conn=self.conn, notifier=notifier_cb, clock=self.clock)
        processed = daemon.poll_callbacks_fast()

        self.assertTrue(notifier_cb.callback_polled)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["action"], "false_alarm")

    def test_daemon_persists_health_state_in_app_state(self):
        """tick_once and run update app_state table with status and timestamps."""
        from src.threshold.store import get_app_state

        daemon = ThresholdDaemon(conn=self.conn, notifier=self.notifier, clock=self.clock)
        daemon.tick_once()

        last_tick = get_app_state(self.conn, "daemon_last_tick_at")
        tick_count = get_app_state(self.conn, "daemon_tick_count")
        self.assertIsNotNone(last_tick)
        self.assertEqual(tick_count, "1")

    def test_daemon_alert_box_formatting(self):
        """print_alert_box prints prominent notification on escalation and reminder."""
        import io
        from contextlib import redirect_stdout

        daemon = ThresholdDaemon(conn=self.conn, notifier=self.notifier, clock=self.clock)
        buf = io.StringIO()
        tick_result = {
            "new_escalations": 1,
            "delivered_notifications": 1,
            "reminded_notifications": 1,
        }
        with redirect_stdout(buf):
            daemon.print_alert_box(tick_result, self.clock.now())

        output = buf.getvalue()
        self.assertIn("PATTERN ABSENCE DETECTED", output)
        self.assertIn("Delivered 1 escalation alert(s)", output)
        self.assertIn("Sent 1 unacknowledged escalation reminder(s)", output)


if __name__ == "__main__":
    unittest.main()

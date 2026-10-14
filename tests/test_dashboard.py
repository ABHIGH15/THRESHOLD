"""
Unit and integration tests for Threshold Local Web Dashboard (src/threshold/dashboard.py).
Tests HTTP endpoints, JSON API, snapshot serving, directory traversal security, and HTML generation.
Zero third-party dependencies (Python standard library only).
"""

from datetime import datetime, timezone
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from src.threshold.daemon import DaemonConfig, ThresholdDaemon
from src.threshold.dashboard import (
    DashboardServer,
    get_dashboard_data,
    render_dashboard_html,
    start_dashboard_server,
)
from src.threshold.store import (
    init_db,
    record_escalation,
    save_event,
    save_routine,
    set_app_state,
    update_escalation_snapshot,
    upsert_device_health,
)


class TestDashboardSystem(unittest.TestCase):
    """Test suite for Threshold Local Dashboard server and API."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = init_db(self.db_path)

        # Temporary snapshot directory
        self.temp_snap_dir = tempfile.TemporaryDirectory()
        self.snapshot_dir = self.temp_snap_dir.name

        # Seed test data in SQLite
        save_routine(
            self.conn,
            routine_id="rt_med",
            property_id="prop_test",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            escalation_delay_minutes=30,
            snapshot_device_id="cam_kitchen",
        )

        upsert_device_health(
            self.conn,
            device_id="sensor_cabinet",
            online=True,
            fetched_at="2026-10-12T14:00:00Z",
            source="test",
        )

        save_event(
            self.conn,
            source="webhook",
            external_id="ev_001",
            device_id="sensor_cabinet",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T14:15:00Z",
            received_at="2026-10-12T14:15:01Z",
            raw_json={"test": True},
        )

        record_escalation(
            self.conn,
            routine_id="rt_med",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        set_app_state(self.conn, "daemon_status", "running")
        set_app_state(self.conn, "daemon_started_at", "2026-10-12T14:00:00Z")
        set_app_state(self.conn, "daemon_tick_count", "42")
        set_app_state(self.conn, "last_poll_at", "2026-10-12T14:30:00Z")

        # Start live background dashboard server on ephemeral port 0
        self.server = start_dashboard_server(
            db_path=self.db_path,
            port=0,
            host="127.0.0.1",
            household_name="Mom's House — Dallas",
            timezone_name="America/Chicago",
            snapshot_dir=self.snapshot_dir,
        )
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.conn.close()
        self.temp_snap_dir.cleanup()

        for suffix in ("", "-wal", "-shm"):
            p = f"{self.db_path}{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_get_dashboard_data_structure(self):
        """get_dashboard_data returns complete dictionary of household state."""
        data = get_dashboard_data(
            conn=self.conn,
            household_name="Mom's House — Dallas",
            timezone_name="America/Chicago",
            snapshot_dir=self.snapshot_dir,
        )

        self.assertEqual(data["household"], "Mom's House — Dallas")
        self.assertEqual(data["timezone"], "America/Chicago")
        self.assertIn("current_time_str", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total_routines"], 1)

        routines = data["routines"]
        self.assertEqual(len(routines), 1)
        self.assertEqual(routines[0]["name"], "Morning Medicine")
        self.assertEqual(routines[0]["device_id"], "sensor_cabinet")
        self.assertEqual(routines[0]["snapshot_device_id"], "cam_kitchen")

        devices = data["devices"]
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["device_id"], "sensor_cabinet")
        self.assertTrue(devices[0]["online"])

        events = data["recent_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "motion_detected")

        escalations = data["recent_escalations"]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["routine_id"], "rt_med")

    def test_render_dashboard_html_contains_critical_elements(self):
        """render_dashboard_html produces standalone HTML with header, badges, and auto-refresh."""
        data = get_dashboard_data(
            conn=self.conn,
            household_name="Mom's House — Dallas",
            timezone_name="America/Chicago",
            snapshot_dir=self.snapshot_dir,
        )
        html_out = render_dashboard_html(data)

        self.assertIn("<!DOCTYPE html>", html_out)
        self.assertIn('<meta http-equiv="refresh" content="10">', html_out)
        self.assertIn("THRESHOLD", html_out)
        self.assertIn("Mom&#x27;s House — Dallas", html_out)
        self.assertIn("Morning Medicine", html_out)
        self.assertIn("sensor_cabinet", html_out)
        self.assertIn("cam_kitchen", html_out)
        self.assertIn("Tick #42", html_out)
        self.assertIn("Today's Activity Timeline", html_out)
        self.assertIn("Escalation & Caretaker Log", html_out)

    def test_dashboard_home_page_returns_200_html(self):
        """GET / returns 200 OK with text/html content-type and valid markup."""
        req = urllib.request.Request(f"{self.base_url}/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")
            self.assertIn("THRESHOLD", body)
            self.assertIn("Morning Medicine", body)

    def test_api_status_returns_200_json(self):
        """GET /api/status returns 200 OK with valid JSON structure."""
        req = urllib.request.Request(f"{self.base_url}/api/status")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            data = json.loads(resp.read().decode("utf-8"))

            self.assertEqual(data["household"], "Mom's House — Dallas")
            self.assertEqual(data["daemon"]["ticks"], 42)
            self.assertEqual(len(data["routines"]), 1)
            self.assertEqual(data["routines"][0]["routine_id"], "rt_med")

    def test_healthz_endpoint_returns_200_ok(self):
        """GET /healthz returns 200 OK with status: ok."""
        req = urllib.request.Request(f"{self.base_url}/healthz")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")

    def test_snapshot_serving_valid_image(self):
        """GET /snapshots/<filename> serves local JPEG snapshot binary."""
        sample_jpeg = b"\xFF\xD8\xFF\xE0" + b"\x00" * 40
        snap_path = os.path.join(self.snapshot_dir, "rt_med_latest.jpg")
        with open(snap_path, "wb") as f:
            f.write(sample_jpeg)

        req = urllib.request.Request(f"{self.base_url}/snapshots/rt_med_latest.jpg")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("image/jpeg", resp.headers.get("Content-Type", ""))
            body = resp.read()
            self.assertEqual(body, sample_jpeg)

    def test_snapshot_missing_returns_404(self):
        """GET /snapshots/<nonexistent> returns 404 Not Found."""
        req = urllib.request.Request(f"{self.base_url}/snapshots/nonexistent.jpg")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_snapshot_directory_traversal_blocked(self):
        """Directory traversal attempts via /snapshots/ are rejected with 403 or 404."""
        # 1. Plain traversal
        traversal_urls = [
            f"{self.base_url}/snapshots/../../etc/passwd",
            f"{self.base_url}/snapshots/..%2F..%2Fetc%2Fpasswd",
            f"{self.base_url}/snapshots/subdir/image.jpg",
        ]
        for url in traversal_urls:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=5):
                    self.fail(f"Expected HTTP error for traversal URL {url}")
            except urllib.error.HTTPError as err:
                self.assertIn(err.code, (403, 404), f"Expected 403 or 404 for traversal, got {err.code}")

    def test_daemon_embeds_dashboard_server(self):
        """ThresholdDaemon automatically boots dashboard server and cleans it up."""
        daemon_cfg = DaemonConfig(
            db_path=self.db_path,
            dashboard_enabled=True,
            dashboard_port=0,
            dashboard_host="127.0.0.1",
            snapshot_dir=self.snapshot_dir,
            enable_polling=False,
            tick_interval_seconds=0.1,
        )
        daemon = ThresholdDaemon(config=daemon_cfg)
        exit_code = daemon.run(max_ticks=1)
        self.assertEqual(exit_code, 0)
        self.assertIsNone(daemon.dashboard_server)


if __name__ == "__main__":
    unittest.main()

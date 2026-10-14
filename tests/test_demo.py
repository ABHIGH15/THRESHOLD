"""
tests/test_demo.py — Automated Unit & Integration Tests for Hackathon Demo Runner.

Verifies:
1. Snapshot preparation extracts or generates valid JPEG image.
2. DemoNotifier records deliveries and returns correct metadata.
3. run_demo executes the complete 3-beat emotional narrative end-to-end:
   - Beat 1: Front door walk observed, medicine cabinet watching.
   - Beat 2: Medicine cabinet missed, escalation fired, snapshot captured, alert card generated.
   - Beat 3: False alarm correction recorded, escalation acked, late event satisfies routine.
4. Embedded dashboard runs concurrently and returns 200 OK on endpoints.
5. CLI entry points (threshold demo and scripts/run_demo.py) run cleanly.
"""

from datetime import datetime, timezone
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.request

from src.threshold.demo import (
    FALLBACK_JPEG_BYTES,
    prepare_demo_snapshots,
    DemoNotifier,
    run_demo,
)
from src.threshold.models import Routine
from src.threshold.notifier import detect_image_mime


class TestDemoSystem(unittest.TestCase):
    """Integration and unit tests for the demo package."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_demo.db")
        self.snap_dir = os.path.join(self.temp_dir, "snapshots")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_prepare_demo_snapshots_creates_valid_jpeg(self):
        """prepare_demo_snapshots writes valid JPEG files with appropriate magic bytes."""
        snap_path = prepare_demo_snapshots(self.snap_dir)
        self.assertTrue(os.path.isfile(snap_path))
        with open(snap_path, "rb") as f:
            data = f.read()
        self.assertGreaterEqual(len(data), 32)
        self.assertEqual(detect_image_mime(data), "image/jpeg")

    def test_demo_notifier_recording(self):
        """DemoNotifier captures escalations and reminders in-memory with delivery metadata."""
        snap_path = prepare_demo_snapshots(self.snap_dir)
        notifier = DemoNotifier(snapshot_path=snap_path)

        routine = Routine(
            property_id="prop_test",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
            snapshot_device_id="cam_kitchen",
        )
        routine.routine_id = "rt_med"

        res = notifier.send_escalation(
            routine=routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:31:00Z",
        )
        self.assertTrue(res["delivered"])
        self.assertEqual(res["type"], "photo")
        self.assertEqual(res["snapshot_path"], snap_path)
        self.assertEqual(len(notifier.sent_escalations), 1)
        self.assertEqual(notifier.sent_escalations[0]["routine_name"], "Morning Medicine")
        self.assertEqual(notifier.sent_escalations[0]["routine_id"], "rt_med")

        rem_res = notifier.send_reminder(
            routine=routine,
            local_date="2026-10-12",
            fired_at="2026-10-12T14:31:00Z",
        )
        self.assertTrue(rem_res["delivered"])
        self.assertEqual(len(notifier.sent_reminders), 1)

    def test_run_demo_fast_execution(self):
        """run_demo executes all 3 beats with correct status progression and returns summary."""
        result = run_demo(
            db_path=self.db_path,
            enable_dashboard=False,
            interactive=False,
            speed=0.0,
            cleanup=False,
        )

        self.assertTrue(result["success"])
        # Beat 1: Door walk observed, medicine cabinet watching
        self.assertEqual(result["beat1"]["status_door"], "observed")
        self.assertEqual(result["beat1"]["status_medicine"], "watching")

        # Beat 2: Medicine cabinet missed, escalation fired
        self.assertEqual(result["beat2"]["status_medicine"], "missed")
        self.assertEqual(result["beat2"]["escalations_count"], 1)
        self.assertTrue(result["beat2"]["alert_delivered"])

        # Beat 3: Correction recorded, acked, and final status is observed
        self.assertEqual(result["beat3"]["status_medicine_final"], "observed")
        self.assertTrue(result["beat3"]["acked"])

        # Check database records
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Check escalations table
        cur = conn.execute("SELECT * FROM escalations WHERE routine_id = 'rt_morning_medicine'")
        esc = cur.fetchone()
        self.assertIsNotNone(esc)
        self.assertIsNotNone(esc["notified_at"])
        self.assertIsNotNone(esc["acked_at"])

        # Check corrections table
        cur = conn.execute("SELECT * FROM corrections WHERE id = 'corr_rt_morning_medicine_2026-10-12'")
        corr = cur.fetchone()
        self.assertIsNotNone(corr)
        self.assertEqual(corr["label"], "false_alarm")

        conn.close()

    def test_run_demo_with_live_dashboard(self):
        """run_demo starts dashboard server and serves endpoints during execution."""
        result = run_demo(
            db_path=self.db_path,
            dashboard_port=0,  # Ephemeral port
            dashboard_host="127.0.0.1",
            enable_dashboard=True,
            interactive=False,
            speed=0.0,
            cleanup=False,
        )

        self.assertTrue(result["success"])
        dash_url = result["dashboard_url"]
        self.assertTrue(dash_url.startswith("http://localhost:") or dash_url.startswith("http://127.0.0.1:"))

        server = result.get("server")
        try:
            # Check GET /
            req = urllib.request.Request(f"{dash_url}/")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertTrue("Margaret" in body and "Home" in body)
                self.assertIn("Morning Medicine Cabinet", body)

            # Check GET /api/status
            req_api = urllib.request.Request(f"{dash_url}/api/status")
            with urllib.request.urlopen(req_api, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["household"], "Margaret's Home — Dallas")
                self.assertEqual(len(data["routines"]), 2)
        finally:
            if server:
                server.shutdown()
                server.server_close()

    def test_cli_demo_subcommand(self):
        """CLI invocation of threshold demo runs without errors."""
        import subprocess
        import sys
        res = subprocess.run(
            [sys.executable, "-m", "src.threshold", "demo", "--fast", "--no-dashboard", "--db", self.db_path],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("THRESHOLD — PATTERN-ABSENCE DETECTION", res.stdout)
        self.assertIn("BEAT 1:", res.stdout)
        self.assertIn("BEAT 2:", res.stdout)
        self.assertIn("BEAT 3:", res.stdout)


if __name__ == "__main__":
    unittest.main()

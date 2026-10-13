"""
Unit tests for shared ingest pipeline (src/threshold/ingest.py).
"""

import os
import tempfile
import unittest

from src.threshold.ingest import ingest_event
from src.threshold.store import (
    get_device_health,
    get_event,
    get_routine,
    init_db,
    save_routine,
)


class TestIngestPipeline(unittest.TestCase):
    """Test ingest_event behavior across idempotency, health, and routines."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        wal_path = f"{self.db_path}-wal"
        shm_path = f"{self.db_path}-shm"
        if os.path.exists(wal_path):
            os.remove(wal_path)
        if os.path.exists(shm_path):
            os.remove(shm_path)

    def test_duplicate_ingest_changes_nothing(self):
        """Duplicate ingest returns False and leaves events table at exactly 1 row."""
        first = ingest_event(
            self.conn,
            source="webhook",
            external_id="req_101",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T10:00:00Z",
            raw_json={"test": 1},
        )
        self.assertTrue(first)

        second = ingest_event(
            self.conn,
            source="webhook",
            external_id="req_101",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T10:00:00Z",
            raw_json={"test": 2},
        )
        self.assertFalse(second)

        cur = self.conn.execute("SELECT COUNT(*) FROM events WHERE external_id = 'req_101'")
        self.assertEqual(cur.fetchone()[0], 1)

    def test_offline_then_online_flips_health(self):
        """device_offline sets online=False, subsequent device_online flips to online=True."""
        ingest_event(
            self.conn,
            source="webhook",
            external_id="req_off",
            device_id="cam_front",
            event_type="device_offline",
            sub_type=None,
            occurred_at="2026-10-12T08:00:00Z",
            raw_json={"status": "offline"},
        )
        health = get_device_health(self.conn, "cam_front")
        self.assertIsNotNone(health)
        self.assertFalse(health["online"])

        ingest_event(
            self.conn,
            source="webhook",
            external_id="req_on",
            device_id="cam_front",
            event_type="device_online",
            sub_type=None,
            occurred_at="2026-10-12T08:05:00Z",
            raw_json={"status": "online"},
        )
        health = get_device_health(self.conn, "cam_front")
        self.assertTrue(health["online"])

    def test_matching_event_advances_last_satisfied_at(self):
        """A matching event advances last_satisfied_at on the routine."""
        save_routine(
            self.conn,
            routine_id="rt_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="door_sensor",
            expected_event_type="button_press",
            expected_sub_type=None,
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed"],
        )

        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_press_1",
            device_id="door_sensor",
            event_type="button_press",
            sub_type=None,
            occurred_at="2026-10-12T07:30:00Z",
            raw_json={"action": "ding"},
        )

        routine = get_routine(self.conn, "rt_door")
        self.assertEqual(routine["last_satisfied_at"], "2026-10-12T07:30:00Z")

    def test_older_event_does_not_move_last_satisfied_backwards(self):
        """An older event does not regress last_satisfied_at."""
        save_routine(
            self.conn,
            routine_id="rt_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="door_sensor",
            expected_event_type="button_press",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            last_satisfied_at="2026-10-12T08:00:00Z",
        )

        # Ingest an event with older timestamp
        ingest_event(
            self.conn,
            source="history",
            external_id="evt_older",
            device_id="door_sensor",
            event_type="button_press",
            sub_type=None,
            occurred_at="2026-10-12T07:15:00Z",
            raw_json={"delayed": True},
        )

        routine = get_routine(self.conn, "rt_door")
        self.assertEqual(routine["last_satisfied_at"], "2026-10-12T08:00:00Z")

    def test_non_matching_event_does_not_touch_routines(self):
        """Events for different devices or event_types do not update routine last_satisfied_at."""
        save_routine(
            self.conn,
            routine_id="rt_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="door_sensor",
            expected_event_type="button_press",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )

        # Different device
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_cam_motion",
            device_id="backyard_cam",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T07:45:00Z",
            raw_json={"diff_device": True},
        )

        routine = get_routine(self.conn, "rt_door")
        self.assertIsNone(routine["last_satisfied_at"])


if __name__ == "__main__":
    unittest.main()

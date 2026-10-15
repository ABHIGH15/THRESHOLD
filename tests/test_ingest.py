"""
Unit tests for shared ingest pipeline (src/threshold/ingest.py).
"""

from datetime import datetime, timezone
import os
import tempfile
import unittest
from zoneinfo import ZoneInfo

from src.threshold import ingest
from src.threshold.ingest import ingest_event
from src.threshold.models import Routine
from src.threshold.notifier import RecordingNotifier
from src.threshold.scheduler import run_scheduler_tick
from src.threshold.status import event_satisfies_routine
from src.threshold.store import (
    get_device_health,
    get_event,
    get_routine,
    init_db,
    row_to_routine,
    save_routine,
    upsert_device_health,
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
            timezone_name="UTC",
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
            timezone_name="UTC",
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

    def test_g2_step_0d_in_window_then_afternoon_event_stays_observed(self):
        """(Scenario a) 08:30 in-window event, then 14:00 afternoon event; tick at 14:00 -> observed, zero escalation rows."""
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(
            self.conn,
            device_id="cam_front",
            online=1,
            fetched_at="2026-10-12T19:00:00Z",
            source="webhook",
        )

        # 1. Ingest 08:30 CDT event (13:30:00Z)
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_0830",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T13:30:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_morning")
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:30:00Z")

        # 2. Ingest 14:00 CDT afternoon event (19:00:00Z)
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_1400",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T19:00:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_morning")
        # MUST NOT be overwritten by the 14:00 event
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:30:00Z")

        # 3. Tick at 14:00 CDT (19:00:00 UTC)
        now_1400 = datetime(2026, 10, 12, 19, 0, 0, tzinfo=timezone.utc)
        notifier = RecordingNotifier()
        result = run_scheduler_tick(self.conn, now=now_1400, notifier=notifier)

        self.assertEqual(result["statuses"]["rt_morning"], "observed")
        self.assertEqual(result["new_escalations"], 0)
        cur = self.conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_g2_only_afternoon_event_missed_then_tick_at_1430_dedupes(self):
        """(Scenario b) Only 14:00 event, no in-window; tick at 09:31 -> missed + 1 escalation; tick at 14:30 creates no 2nd row."""
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(
            self.conn,
            device_id="cam_front",
            online=1,
            fetched_at="2026-10-12T14:31:00Z",
            source="webhook",
        )

        # Tick at 09:31 CDT (14:31:00 UTC) -> missed, 1 escalation
        now_0931 = datetime(2026, 10, 12, 14, 31, 0, tzinfo=timezone.utc)
        notifier = RecordingNotifier()
        result1 = run_scheduler_tick(self.conn, now=now_0931, notifier=notifier)
        self.assertEqual(result1["statuses"]["rt_morning"], "missed")
        self.assertEqual(result1["new_escalations"], 1)

        cur = self.conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 1)

        # Event occurs at 14:00 CDT (19:00:00 UTC) - outside window
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_1400",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T19:00:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_morning")
        self.assertIsNone(r["last_satisfied_at"])

        # Tick at 14:30 CDT (19:30:00 UTC) -> still missed, but creates NO second escalation row
        upsert_device_health(
            self.conn,
            device_id="cam_front",
            online=1,
            fetched_at="2026-10-12T19:30:00Z",
            source="webhook",
        )
        now_1430 = datetime(2026, 10, 12, 19, 30, 0, tzinfo=timezone.utc)
        result2 = run_scheduler_tick(self.conn, now=now_1430, notifier=notifier)
        self.assertEqual(result2["statuses"]["rt_morning"], "missed")
        self.assertEqual(result2["new_escalations"], 0)

        cur = self.conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 1)

    def test_g2_event_inside_grace_zone_updates_last_satisfied_at(self):
        """(Scenario c) An event at 09:10 (inside the grace zone) updates last_satisfied_at."""
        save_routine(
            self.conn,
            routine_id="rt_grace",
            property_id="prop_1",
            name="Morning Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # 09:10 CDT is 14:10:00 UTC on Mon 2026-10-12
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_0910",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T14:10:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_grace")
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T14:10:00Z")

    def test_g2_event_after_deadline_does_not_update_last_satisfied_at(self):
        """(Scenario d) An event at 09:45 (after the deadline 09:30) does NOT update last_satisfied_at."""
        save_routine(
            self.conn,
            routine_id="rt_late",
            property_id="prop_1",
            name="Morning Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # 09:45 CDT is 14:45:00 UTC on Mon 2026-10-12
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_0945",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T14:45:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_late")
        self.assertIsNone(r["last_satisfied_at"])
        # Confirm event is still recorded in events table
        evt = get_event(self.conn, "webhook", "evt_0945")
        self.assertIsNotNone(evt)

    def test_g2_overnight_window_satisfaction_and_rejection(self):
        """(Scenario e) Overnight window 22:00-06:00: 23:30 and 02:00 (next calendar day) both satisfy; 12:00 does not."""
        save_routine(
            self.conn,
            routine_id="rt_night",
            property_id="prop_1",
            name="Night Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="22:00",
            window_end="06:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # 1. 23:30 CDT on Mon Oct 12 is 2026-10-13T04:30:00Z
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_2330",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-13T04:30:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_night")
        self.assertEqual(r["last_satisfied_at"], "2026-10-13T04:30:00Z")

        # 2. 02:00 CDT on Tue Oct 13 (next calendar day, same overnight window) is 2026-10-13T07:00:00Z
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_0200",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-13T07:00:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_night")
        self.assertEqual(r["last_satisfied_at"], "2026-10-13T07:00:00Z")

        # 3. 12:00 CDT on Tue Oct 13 is 2026-10-13T17:00:00Z (outside window)
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_1200",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-13T17:00:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_night")
        # Remains at 07:00:00Z
        self.assertEqual(r["last_satisfied_at"], "2026-10-13T07:00:00Z")

        # Also verify event_satisfies_routine directly
        r_obj = row_to_routine(r)
        self.assertTrue(event_satisfies_routine(r_obj, "2026-10-13T04:30:00Z"))
        self.assertTrue(event_satisfies_routine(r_obj, "2026-10-13T07:00:00Z"))
        self.assertFalse(event_satisfies_routine(r_obj, "2026-10-13T17:00:00Z"))

    def test_g2_event_on_non_routine_day_does_not_satisfy(self):
        """(Scenario f) An event on a non-routine day does not satisfy."""
        save_routine(
            self.conn,
            routine_id="rt_mon_wed",
            property_id="prop_1",
            name="MWF Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "wed"],
            escalation_delay_minutes=30,
        )
        # Tuesday 2026-10-13 at 08:00 CDT is 2026-10-13T13:00:00Z
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_tue",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-13T13:00:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_mon_wed")
        self.assertIsNone(r["last_satisfied_at"])

        r_obj = row_to_routine(r)
        self.assertFalse(event_satisfies_routine(r_obj, "2026-10-13T13:00:00Z"))

    def test_g2_out_of_order_arrival_newer_first_does_not_regress(self):
        """(Scenario g) Out-of-order arrival: 08:45 ingested before 08:30 -> last_satisfied_at stays 08:45."""
        save_routine(
            self.conn,
            routine_id="rt_order",
            property_id="prop_1",
            name="Morning Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # Ingest 08:45 CDT (13:45:00 UTC) first
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_0845",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T13:45:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_order")
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:45:00Z")

        # Ingest delayed 08:30 CDT (13:30:00 UTC) second
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_0830",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T13:30:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_order")
        # Must still be 13:45:00Z
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:45:00Z")

    def test_g2_ingest_imports_and_calls_event_satisfies_routine(self):
        """(Scenario h) ingest.py imports and calls event_satisfies_routine."""
        self.assertTrue(hasattr(ingest, "event_satisfies_routine"))
        self.assertIs(ingest.event_satisfies_routine, event_satisfies_routine)

        with open(ingest.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertIn("from src.threshold.status import event_satisfies_routine", source)
        self.assertIn("event_satisfies_routine(routine, occ_dt)", source)
        # Verify no private helpers from status.py are imported into ingest.py
        self.assertNotIn("_get_today_window", source)
        self.assertNotIn("_is_routine_day", source)
        self.assertNotIn("_last_satisfied_within_window", source)

    def test_g2_timestamp_comparison_mixed_iso_formats(self):
        """(Mixed ISO) Timestamp comparison parses datetimes supporting '...Z', '...+00:00', with/without fractional seconds."""
        save_routine(
            self.conn,
            routine_id="rt_iso",
            property_id="prop_1",
            name="Morning Motion",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # Event 1 with 'Z'
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_iso_1",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T13:30:00Z",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_iso")
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:30:00Z")

        # Event 2 with '+00:00' and fractional seconds (5.5 seconds later)
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_iso_2",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T13:30:05.500000+00:00",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_iso")
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:30:05.500000+00:00")

        # Event 3 with '+00:00' and fractional seconds that is chronologically older (13:30:04.999999+00:00)
        # String comparison would say "+00:00" < "Z", but chronologically it is older than Event 2
        ingest_event(
            self.conn,
            source="webhook",
            external_id="evt_iso_3",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T13:30:04.999999+00:00",
            raw_json={"motion": True},
        )
        r = get_routine(self.conn, "rt_iso")
        # Must not regress to 13:30:04.999999
        self.assertEqual(r["last_satisfied_at"], "2026-10-12T13:30:05.500000+00:00")


if __name__ == "__main__":
    unittest.main()

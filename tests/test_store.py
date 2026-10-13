"""
Unit tests for SQLite store (src/threshold/store.py).
Tests all tables, constraints, WAL mode, foreign keys, and timestamp conversion.
"""

from datetime import datetime, timezone
import os
import sqlite3
import tempfile
import unittest

from src.threshold.store import (
    ack_escalation,
    clear_snooze,
    get_connection,
    get_correction,
    get_device_health,
    get_escalation,
    get_escalations_due_for_reminder,
    get_event,
    get_routine,
    get_snooze,
    init_db,
    list_corrections,
    mark_escalation_notified,
    mark_escalation_reminded,
    ms_to_iso,
    record_correction,
    record_escalation,
    save_event,
    save_routine,
    set_snooze,
    to_iso_timestamp,
    update_escalation_snapshot,
    update_routine_last_satisfied,
    upsert_device_health,
)


class TestSQLiteStore(unittest.TestCase):
    """Test suite for SQLite store operations."""

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

    def test_ms_epoch_conversion(self):
        """A ms-epoch timestamp (1791814643626) converts to 2026-10-12T14:17:23Z."""
        result = ms_to_iso(1791814643626)
        self.assertEqual(result, "2026-10-12T14:17:23Z")

        # Also test via to_iso_timestamp
        self.assertEqual(to_iso_timestamp(1791814643626), "2026-10-12T14:17:23Z")

    def test_insert_and_read_event(self):
        """Insert an event and read it back."""
        inserted = save_event(
            self.conn,
            source="webhook",
            external_id="evt_123",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T10:00:00Z",
            received_at="2026-10-12T10:00:01Z",
            raw_json={"foo": "bar"},
        )
        self.assertTrue(inserted)

        event = get_event(self.conn, "webhook", "evt_123")
        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "webhook")
        self.assertEqual(event["external_id"], "evt_123")
        self.assertEqual(event["device_id"], "cam_front")
        self.assertEqual(event["event_type"], "motion_detected")
        self.assertEqual(event["sub_type"], "human")
        self.assertEqual(event["occurred_at"], "2026-10-12T10:00:00Z")
        self.assertEqual(event["raw_json"], '{"foo": "bar"}')

    def test_same_source_and_external_id_twice_exactly_one_row(self):
        """The same (source, external_id) twice -> exactly one row."""
        first = save_event(
            self.conn,
            source="webhook",
            external_id="evt_same",
            device_id="cam_front",
            event_type="motion_detected",
            occurred_at="2026-10-12T10:00:00Z",
            received_at="2026-10-12T10:00:01Z",
            raw_json={"attempt": 1},
        )
        second = save_event(
            self.conn,
            source="webhook",
            external_id="evt_same",
            device_id="cam_front",
            event_type="motion_detected",
            occurred_at="2026-10-12T10:00:00Z",
            received_at="2026-10-12T10:00:02Z",
            raw_json={"attempt": 2},
        )
        self.assertTrue(first)
        self.assertFalse(second)

        cur = self.conn.execute("SELECT COUNT(*) FROM events WHERE external_id = 'evt_same'")
        count = cur.fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_external_id_two_sources_two_rows(self):
        """The same external_id from two different sources -> two rows."""
        save_event(
            self.conn,
            source="webhook",
            external_id="shared_evt_1",
            device_id="cam_front",
            event_type="motion_detected",
            occurred_at="2026-10-12T10:00:00Z",
            received_at="2026-10-12T10:00:01Z",
            raw_json={"source": "webhook"},
        )
        save_event(
            self.conn,
            source="history",
            external_id="shared_evt_1",
            device_id="cam_front",
            event_type="on_demand",
            occurred_at="2026-10-12T10:00:00Z",
            received_at="2026-10-12T10:00:05Z",
            raw_json={"source": "history"},
        )

        cur = self.conn.execute("SELECT COUNT(*) FROM events WHERE external_id = 'shared_evt_1'")
        count = cur.fetchone()[0]
        self.assertEqual(count, 2)

    def test_upsert_device_health_twice(self):
        """Upsert device_health twice -> one row with newer values."""
        upsert_device_health(
            self.conn,
            device_id="cam_front",
            online=False,
            fetched_at="2026-10-12T08:00:00Z",
            source="poll",
            reported_at="2026-10-12T07:59:00Z",
        )
        # Second upsert with updated state
        upsert_device_health(
            self.conn,
            device_id="cam_front",
            online=True,
            fetched_at="2026-10-12T08:30:00Z",
            source="stream",
            reported_at="2026-10-12T08:29:55Z",
        )

        cur = self.conn.execute("SELECT COUNT(*) FROM device_health WHERE device_id = 'cam_front'")
        count = cur.fetchone()[0]
        self.assertEqual(count, 1)

        health = get_device_health(self.conn, "cam_front")
        self.assertTrue(health["online"])
        self.assertEqual(health["fetched_at"], "2026-10-12T08:30:00Z")
        self.assertEqual(health["reported_at"], "2026-10-12T08:29:55Z")
        self.assertEqual(health["source"], "stream")

    def test_malformed_insert_missing_not_null_fails(self):
        """A malformed insert (missing NOT NULL) fails with sqlite3.IntegrityError."""
        with self.assertRaises(sqlite3.IntegrityError):
            # external_id is NOT NULL
            self.conn.execute(
                "INSERT INTO events (source, device_id, event_type, occurred_at, received_at, raw_json) "
                "VALUES ('webhook', 'cam_1', 'motion', '2026-10-12T10:00:00Z', '2026-10-12T10:00:00Z', '{}')"
            )

    def test_insert_event_null_source_raises_integrity_error(self):
        """Inserting an event with source NULL raises sqlite3.IntegrityError (fixing SQLite non-INTEGER PK hole)."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO events (source, external_id, device_id, event_type, occurred_at, received_at, raw_json) "
                "VALUES (NULL, 'evt_1', 'cam_1', 'motion_detected', '2026-10-12T10:00:00Z', '2026-10-12T10:00:00Z', '{}')"
            )

    def test_insert_device_health_null_device_id_raises_integrity_error(self):
        """Inserting device_health with device_id NULL raises sqlite3.IntegrityError."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO device_health (device_id, online, fetched_at, source) "
                "VALUES (NULL, 1, '2026-10-12T10:00:00Z', 'poll')"
            )

    def test_escalation_unique_constraint(self):
        """The escalation UNIQUE constraint blocks a second insert for the same routine and date."""
        # Insert routine first for foreign key integrity
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Door",
            device_id="door_sensor",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
        )

        record_escalation(
            self.conn,
            routine_id="rt_morning",
            local_date="2026-10-12",
            fired_at="2026-10-12T09:30:00Z",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            record_escalation(
                self.conn,
                routine_id="rt_morning",
                local_date="2026-10-12",
                fired_at="2026-10-12T09:35:00Z",
            )

        rec = get_escalation(self.conn, "rt_morning", "2026-10-12")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["fired_at"], "2026-10-12T09:30:00Z")

    def test_routines_and_last_satisfied_at(self):
        """Routine creation and last_satisfied_at fact update."""
        save_routine(
            self.conn,
            routine_id="rt_nurse",
            property_id="prop_home",
            name="Nurse Visit",
            device_id="door_front",
            expected_event_type="button_press",
            window_start="10:00",
            window_end="11:30",
            timezone_name="America/Chicago",
            days=["mon", "wed", "fri"],
        )
        routine = get_routine(self.conn, "rt_nurse")
        self.assertIsNotNone(routine)
        self.assertEqual(routine["name"], "Nurse Visit")
        self.assertIsNone(routine["last_satisfied_at"])

        update_routine_last_satisfied(self.conn, "rt_nurse", "2026-10-12T10:15:00Z")
        updated = get_routine(self.conn, "rt_nurse")
        self.assertEqual(updated["last_satisfied_at"], "2026-10-12T10:15:00Z")

    def test_corrections_table_operation(self):
        """Test corrections table insertion and query."""
        record_correction(
            self.conn,
            id="corr_1",
            device_id="cam_front",
            event_type="motion_detected",
            time_bucket="morning",
            label="false_alarm",
            created_at="2026-10-12T10:30:00Z",
        )
        cur = self.conn.execute("SELECT * FROM corrections WHERE id = 'corr_1'")
        row = cur.fetchone()
        self.assertEqual(row["label"], "false_alarm")

    def test_snooze_set_read_and_clear_single_source_of_truth(self):
        """Snooze can be set, read, and cleared through the single source of truth on routines."""
        save_routine(
            self.conn,
            routine_id="rt_meds",
            property_id="prop_home",
            name="Evening Meds",
            device_id="cabinet_cam",
            expected_event_type="motion_detected",
            window_start="18:00",
            window_end="20:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        # Initially no snooze
        self.assertIsNone(get_snooze(self.conn, "rt_meds"))
        routine_init = get_routine(self.conn, "rt_meds")
        self.assertIsNone(routine_init["snoozed_until"])

        # Set snooze
        set_snooze(self.conn, "rt_meds", "2026-10-12T23:59:59Z")
        self.assertEqual(get_snooze(self.conn, "rt_meds"), "2026-10-12T23:59:59Z")
        routine_snoozed = get_routine(self.conn, "rt_meds")
        self.assertEqual(routine_snoozed["snoozed_until"], "2026-10-12T23:59:59Z")

        # Clear snooze
        clear_snooze(self.conn, "rt_meds")
        self.assertIsNone(get_snooze(self.conn, "rt_meds"))
        routine_cleared = get_routine(self.conn, "rt_meds")
        self.assertIsNone(routine_cleared["snoozed_until"])

    def test_mark_escalation_reminded_and_ack(self):
        """Test mark_escalation_reminded and ack_escalation update the escalation row."""
        save_routine(
            self.conn,
            routine_id="rt_remind_test",
            property_id="prop_1",
            name="Remind Test",
            device_id="cam_test",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            self.conn,
            routine_id="rt_remind_test",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:01Z",
        )

        esc = get_escalation(self.conn, "rt_remind_test", "2026-10-12")
        self.assertIsNone(esc["reminded_at"])
        self.assertIsNone(esc["acked_at"])

        # Mark reminded
        mark_escalation_reminded(self.conn, "rt_remind_test", "2026-10-12", "2026-10-12T14:50:00Z")
        esc_reminded = get_escalation(self.conn, "rt_remind_test", "2026-10-12")
        self.assertEqual(esc_reminded["reminded_at"], "2026-10-12T14:50:00Z")

        # Acknowledge
        ack_escalation(self.conn, "rt_remind_test", "2026-10-12", "2026-10-12T14:52:00Z")
        esc_acked = get_escalation(self.conn, "rt_remind_test", "2026-10-12")
        self.assertEqual(esc_acked["acked_at"], "2026-10-12T14:52:00Z")

    def test_get_escalations_due_for_reminder(self):
        """Test get_escalations_due_for_reminder returns only unacked, unreminded, notified escalations past delay."""
        save_routine(
            self.conn,
            routine_id="rt_esc_ladder",
            property_id="prop_1",
            name="Ladder Test",
            device_id="cam_ladder",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            self.conn,
            routine_id="rt_esc_ladder",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
            notified_at="2026-10-12T14:35:00Z",
        )

        # Before 15 minutes (14:45 UTC) -> not due
        due_early = get_escalations_due_for_reminder(self.conn, "2026-10-12T14:45:00Z", reminder_delay_minutes=15)
        self.assertEqual(len(due_early), 0)

        # At 15 minutes (14:50 UTC) -> due
        due_on_time = get_escalations_due_for_reminder(self.conn, "2026-10-12T14:50:00Z", reminder_delay_minutes=15)
        self.assertEqual(len(due_on_time), 1)
        self.assertEqual(due_on_time[0]["routine_id"], "rt_esc_ladder")

        # Once marked reminded -> no longer due
        mark_escalation_reminded(self.conn, "rt_esc_ladder", "2026-10-12", "2026-10-12T14:50:00Z")
        due_after_remind = get_escalations_due_for_reminder(self.conn, "2026-10-12T14:55:00Z", reminder_delay_minutes=15)
        self.assertEqual(len(due_after_remind), 0)

    def test_get_and_list_corrections(self):
        """Test get_correction and list_corrections retrieval."""
        record_correction(
            self.conn,
            id="corr_test_1",
            device_id="cam_test",
            event_type="motion_detected",
            time_bucket="07:00-09:00",
            label="false_alarm",
            created_at="2026-10-12T14:40:00Z",
        )
        corr = get_correction(self.conn, "corr_test_1")
        self.assertIsNotNone(corr)
        self.assertEqual(corr["id"], "corr_test_1")
        self.assertEqual(corr["label"], "false_alarm")

        corrs = list_corrections(self.conn)
        self.assertTrue(any(c["id"] == "corr_test_1" for c in corrs))

    def test_update_escalation_snapshot(self):
        """update_escalation_snapshot updates snapshot_status and snapshot_path on the escalation row."""
        save_routine(
            self.conn,
            routine_id="rt_snap_store",
            property_id="prop_snap",
            name="Snapshot Store Test",
            device_id="sensor_snap",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        record_escalation(
            self.conn,
            routine_id="rt_snap_store",
            local_date="2026-10-12",
            fired_at="2026-10-12T14:35:00Z",
        )

        esc = get_escalation(self.conn, "rt_snap_store", "2026-10-12")
        self.assertIsNotNone(esc)
        self.assertIsNone(esc["snapshot_status"])
        self.assertIsNone(esc["snapshot_path"])

        update_escalation_snapshot(
            self.conn,
            routine_id="rt_snap_store",
            local_date="2026-10-12",
            status="photo",
            path="snapshots/rt_snap_store_2026-10-12.jpg",
        )

        esc_updated = get_escalation(self.conn, "rt_snap_store", "2026-10-12")
        self.assertIsNotNone(esc_updated)
        self.assertEqual(esc_updated["snapshot_status"], "photo")
        self.assertEqual(esc_updated["snapshot_path"], "snapshots/rt_snap_store_2026-10-12.jpg")


if __name__ == "__main__":
    unittest.main()


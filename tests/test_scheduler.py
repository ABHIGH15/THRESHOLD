"""
Unit and integration tests for Threshold Scheduler & Absence Engine (src/threshold/scheduler.py).
Tests deterministic evaluation, retry with notification delivery, simulated restart, and timezone boundaries.
"""

from datetime import datetime, timedelta, timezone
import os
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

from src.threshold.agent.facts import build_gate_facts
from src.threshold.agent.gate import decide
from src.threshold.models import Routine
from src.threshold.scheduler import (
    AcceleratedClock,
    FakeClock,
    RealClock,
    RecordingNotifier,
    Scheduler,
    run_scheduler_tick,
)
from src.threshold.status import (
    STATUS_MISSED,
    STATUS_OBSERVED,
    STATUS_SNOOZED,
    STATUS_UNKNOWN,
    STATUS_WATCHING,
)
from src.threshold.store import (
    get_connection,
    get_escalation,
    init_db,
    save_event,
    save_routine,
    set_snooze,
    update_routine_last_satisfied,
    upsert_device_health,
)


class TestSchedulerAbsenceEngine(unittest.TestCase):
    """Test suite for absence engine evaluation, retry, and scheduler thread."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(self.db_path)
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = f"{self.db_path}{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # 1. 5 consecutive ticks past a miss -> exactly 1 escalation row
    def test_five_consecutive_ticks_past_miss_exactly_one_escalation_row(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_front_door",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        # Device is online and reported recently (healthy)
        upsert_device_health(
            conn,
            device_id="cam_front",
            online=True,
            fetched_at="2026-10-12T14:45:00Z",
            source="probe",
        )
        conn.close()

        # Monday 2026-10-12. In America/Chicago (CDT, UTC-5):
        # 09:30 CDT escalation deadline = 14:30 UTC.
        # Tick at 09:35 CDT = 14:35 UTC (past miss).
        clock = FakeClock(datetime(2026, 10, 12, 14, 35, 0, tzinfo=timezone.utc))
        notifier = RecordingNotifier()
        scheduler = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier)

        # Run 5 consecutive ticks past miss
        for i in range(5):
            clock.advance(60)  # advance 1 minute each tick
            res = scheduler.tick()
            self.assertEqual(res["statuses"]["rt_front_door"], STATUS_MISSED)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations WHERE routine_id = 'rt_front_door'")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

        # Notification sent exactly once
        self.assertEqual(len(notifier.sent), 1)

    # 2. Simulated restart on same DB -> still 1 escalation row
    def test_simulated_restart_on_same_db_still_one_escalation_row(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_meds",
            property_id="prop_1",
            name="Morning Meds",
            device_id="cam_kitchen",
            expected_event_type="motion_detected",
            window_start="08:00",
            window_end="09:00",
            timezone_name="America/New_York",
            days=["mon"],
            escalation_delay_minutes=15,
        )
        upsert_device_health(conn, "cam_kitchen", online=True, fetched_at="2026-10-12T13:10:00Z", source="probe")
        conn.close()

        # 09:15 EDT = 13:15 UTC. Tick at 13:20 UTC.
        clock = FakeClock(datetime(2026, 10, 12, 13, 20, 0, tzinfo=timezone.utc))
        notifier1 = RecordingNotifier()
        sched1 = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier1)
        sched1.tick()

        # Simulate app restart: new Scheduler object on same DB
        notifier2 = RecordingNotifier()
        sched2 = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier2)
        sched2.tick()

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations WHERE routine_id = 'rt_meds'")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

        # sched2 sees notified_at already set, so does not resend
        self.assertEqual(len(notifier2.sent), 0)

    # 3. Stale device -> unknown and 0 escalations
    def test_stale_device_returns_unknown_and_zero_escalations(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_stale",
            property_id="prop_1",
            name="Stale Check",
            device_id="cam_stale",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # Last fetched 5 hours ago (> 2 hour default threshold)
        upsert_device_health(conn, "cam_stale", online=True, fetched_at="2026-10-12T09:00:00Z", source="probe")
        conn.close()

        # Tick at 14:00 UTC (past window + delay)
        clock = FakeClock(datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()

        self.assertEqual(res["statuses"]["rt_stale"], STATUS_UNKNOWN)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 4. Offline device -> unknown and 0 escalations
    def test_offline_device_returns_unknown_and_zero_escalations(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_offline",
            property_id="prop_1",
            name="Offline Check",
            device_id="cam_offline",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # Device marked offline
        upsert_device_health(conn, "cam_offline", online=False, fetched_at="2026-10-12T13:45:00Z", source="webhook")
        conn.close()

        clock = FakeClock(datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()

        self.assertEqual(res["statuses"]["rt_offline"], STATUS_UNKNOWN)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 4b. Device with online=False and FRESH fetched_at/reported_at
    def test_offline_fresh_device_returns_unknown_zero_escalations_and_gate_checks_needed(self):
        """
        Prove that a device with online=False and fresh fetched_at/reported_at produces:
        1. derive_status returns STATUS_UNKNOWN past escalation deadline.
        2. Scheduler.tick produces exactly 0 escalations.
        3. Agent gate decide() returns check_needed with reason_code 'device_not_fresh'.
        """
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_idle_sandbox",
            property_id="prop_1",
            name="Morning Medicine",
            device_id="cam_idle",
            expected_event_type="motion_detected",
            window_start="08:00",
            window_end="09:00",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            timezone_name="UTC",
            escalation_delay_minutes=15,
        )
        now = datetime(2026, 10, 12, 9, 30, 0, tzinfo=timezone.utc)
        # Fresh timestamp: fetched 5 minutes ago, but online=False (idle sandbox behavior)
        fresh_time = "2026-10-12T09:25:00Z"
        upsert_device_health(
            conn,
            "cam_idle",
            online=False,
            fetched_at=fresh_time,
            reported_at=fresh_time,
            source="poll",
        )
        conn.close()

        # 1. Scheduler.tick evaluation
        clock = FakeClock(now)
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()

        self.assertEqual(res["statuses"]["rt_idle_sandbox"], STATUS_UNKNOWN)
        self.assertEqual(res["new_escalations"], 0)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 0)

        # 2. Agent gate evaluation
        facts = build_gate_facts(conn, "cam_idle", now)
        self.assertFalse(facts.device_online_fresh)
        gate_decision = decide(
            tier="escalate",
            ok_tool_results=3,
            facts=facts,
        )
        self.assertEqual(gate_decision.decision, "check_needed")
        self.assertEqual(gate_decision.reason_code, "device_not_fresh")
        conn.close()

    # 4c. Routine whose device has NO device_health row -> unknown and 0 escalations
    def test_device_with_no_health_row_returns_unknown_and_zero_escalations(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_no_health_data",
            property_id="prop_1",
            name="No Health Check",
            device_id="cam_unseen",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # Deliberately DO NOT insert any device_health row for cam_unseen
        conn.close()

        clock = FakeClock(datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()

        # No data is not absence of activity -> must be UNKNOWN
        self.assertEqual(res["statuses"]["rt_no_health_data"], STATUS_UNKNOWN)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 5. Snoozed -> 0 escalations
    def test_snoozed_routine_returns_zero_escalations(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_snooze",
            property_id="prop_1",
            name="Snooze Check",
            device_id="cam_snooze",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
            snoozed_until="2026-10-12T18:00:00Z",
        )
        upsert_device_health(conn, "cam_snooze", online=True, fetched_at="2026-10-12T13:45:00Z", source="probe")
        conn.close()

        clock = FakeClock(datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()

        self.assertEqual(res["statuses"]["rt_snooze"], STATUS_SNOOZED)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 6. Late satisfying event -> observed and still 1 row
    def test_late_event_after_miss_status_observed_still_one_escalation(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_late",
            property_id="prop_1",
            name="Late Check",
            device_id="cam_late",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(conn, "cam_late", online=True, fetched_at="2026-10-12T13:45:00Z", source="probe")
        conn.close()

        # Tick 1: missed -> creates escalation row
        clock = FakeClock(datetime(2026, 10, 12, 13, 40, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res1 = scheduler.tick()
        self.assertEqual(res1["statuses"]["rt_late"], STATUS_MISSED)

        # Late satisfying event arrives for within the window (07:30 CDT = 12:30 UTC)
        conn = get_connection(self.db_path)
        update_routine_last_satisfied(conn, "rt_late", "2026-10-12T12:30:00Z")
        conn.close()

        # Tick 2: now status is observed, and escalations row count remains exactly 1
        clock.advance(300)
        res2 = scheduler.tick()
        self.assertEqual(res2["statuses"]["rt_late"], STATUS_OBSERVED)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM escalations WHERE routine_id = 'rt_late'")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    # 7. Notifier fails once -> row stays pending, next tick delivers once, further ticks do not resend
    def test_notifier_fails_once_retries_and_delivers_once(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_retry",
            property_id="prop_1",
            name="Retry Check",
            device_id="cam_retry",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="08:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=10,
        )
        upsert_device_health(conn, "cam_retry", online=True, fetched_at="2026-10-12T13:15:00Z", source="probe")
        conn.close()

        # Notifier configured to fail on attempt 1
        notifier = RecordingNotifier(fail_first_n=1)
        clock = FakeClock(datetime(2026, 10, 12, 13, 20, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock, notifier=notifier)

        # Tick 1: escalation created, delivery fails -> notified_at is NULL
        res1 = scheduler.tick()
        self.assertEqual(res1["pending_notifications"], 1)
        self.assertEqual(len(notifier.sent), 0)

        conn = get_connection(self.db_path)
        esc = get_escalation(conn, "rt_retry", "2026-10-12")
        self.assertIsNotNone(esc)
        self.assertIsNone(esc["notified_at"])
        conn.close()

        # Tick 2: delivery succeeds -> notified_at set
        clock.advance(60)
        res2 = scheduler.tick()
        self.assertEqual(res2["delivered_notifications"], 1)
        self.assertEqual(res2["pending_notifications"], 0)
        self.assertEqual(len(notifier.sent), 1)

        conn = get_connection(self.db_path)
        esc2 = get_escalation(conn, "rt_retry", "2026-10-12")
        self.assertIsNotNone(esc2["notified_at"])
        conn.close()

        # Tick 3: does not resend
        clock.advance(60)
        res3 = scheduler.tick()
        self.assertEqual(res3["delivered_notifications"], 0)
        self.assertEqual(len(notifier.sent), 1)

    # 8. A tick exception does not kill the thread
    def test_tick_exception_does_not_kill_thread(self):
        scheduler = Scheduler(db_path=self.db_path, interval_seconds=0.05)

        call_count = 0

        def failing_tick(now=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Forced tick exception")
            return {"status": "ok"}

        scheduler.tick = failing_tick
        scheduler.start()

        # Let it run across multiple ticks
        time.sleep(0.2)
        scheduler.stop()

        self.assertGreater(call_count, 1)
        self.assertIsNotNone(scheduler.last_error)
        self.assertIsInstance(scheduler.last_error, RuntimeError)

    # 9. Timezones via zoneinfo
    def test_timezone_window_crossing_midnight(self):
        """Window crossing midnight (22:00 to 06:00)."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_night",
            property_id="prop_1",
            name="Night Watch",
            device_id="cam_porch",
            expected_event_type="motion_detected",
            window_start="22:00",
            window_end="06:00",
            timezone_name="America/New_York",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
        )
        upsert_device_health(conn, "cam_porch", online=True, fetched_at="2026-10-13T10:00:00Z", source="probe")
        conn.close()

        # In America/New_York (EDT, UTC-4):
        # Tuesday 06:30 EDT = 10:30 UTC is escalation deadline.
        # At 10:15 UTC (06:15 EDT, during delay) -> watching.
        clock = FakeClock(datetime(2026, 10, 13, 10, 15, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res1 = scheduler.tick()
        self.assertEqual(res1["statuses"]["rt_night"], STATUS_WATCHING)

        # At 10:35 UTC (06:35 EDT, past delay) -> missed.
        clock.advance(1200)
        res2 = scheduler.tick()
        self.assertEqual(res2["statuses"]["rt_night"], STATUS_MISSED)

    def test_us_fallback_dst_boundary(self):
        """US fall-back DST boundary: America/New_York on 2026-11-01."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_dst_fall",
            property_id="prop_1",
            name="DST Fall",
            device_id="cam_hall",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/New_York",
            days=["sun"],
            escalation_delay_minutes=30,
        )
        # On 2026-11-01, standard time (EST, UTC-5) is active at 09:30 EST.
        # 09:30 EST + 30m delay = 14:30 UTC.
        upsert_device_health(conn, "cam_hall", online=True, fetched_at="2026-11-01T14:00:00Z", source="probe")
        conn.close()

        clock = FakeClock(datetime(2026, 11, 1, 14, 35, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()
        self.assertEqual(res["statuses"]["rt_dst_fall"], STATUS_MISSED)

    def test_us_spring_forward_dst_boundary(self):
        """US spring-forward DST boundary: America/New_York on 2026-03-08."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_dst_spring",
            property_id="prop_1",
            name="DST Spring",
            device_id="cam_hall",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/New_York",
            days=["sun"],
            escalation_delay_minutes=30,
        )
        # On 2026-03-08, daylight saving time (EDT, UTC-4) is active at 09:30 EDT.
        # 09:30 EDT = 13:30 UTC.
        upsert_device_health(conn, "cam_hall", online=True, fetched_at="2026-03-08T13:00:00Z", source="probe")
        conn.close()

        clock = FakeClock(datetime(2026, 3, 8, 13, 35, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()
        self.assertEqual(res["statuses"]["rt_dst_spring"], STATUS_MISSED)

    def test_non_dst_zone_asia_kolkata(self):
        """Non-DST timezone: Asia/Kolkata (+05:30 throughout)."""
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_kolkata",
            property_id="prop_1",
            name="Morning Tea",
            device_id="cam_kitchen",
            expected_event_type="motion_detected",
            window_start="06:30",
            window_end="08:00",
            timezone_name="Asia/Kolkata",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        # 08:30 IST = 03:00 UTC.
        upsert_device_health(conn, "cam_kitchen", online=True, fetched_at="2026-10-12T02:45:00Z", source="probe")
        conn.close()

        # At 03:05 UTC (08:35 IST, past delay) -> missed
        clock = FakeClock(datetime(2026, 10, 12, 3, 5, 0, tzinfo=timezone.utc))
        scheduler = Scheduler(db_path=self.db_path, clock=clock)
        res = scheduler.tick()
        self.assertEqual(res["statuses"]["rt_kolkata"], STATUS_MISSED)

    # 10. Clock mechanics
    def test_clocks_mechanics(self):
        real_c = RealClock()
        self.assertIsNotNone(real_c.now().tzinfo)

        fake_c = FakeClock(datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(fake_c.now().hour, 10)
        fake_c.advance(3600)
        self.assertEqual(fake_c.now().hour, 11)

        accel_c = AcceleratedClock(datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc), scale=100.0)
        t0 = accel_c.now()
        time.sleep(0.05)
        t1 = accel_c.now()
        self.assertGreater(t1, t0)


if __name__ == "__main__":
    unittest.main()

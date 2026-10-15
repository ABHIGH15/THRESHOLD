"""
Tests for routine status derivation and grace period semantics (tests/test_status.py).

Verifies Task G1:
An event satisfies a routine if its occurred_at is within:
    [window_start, window_end + escalation_delay_minutes]
in the routine's timezone, overnight windows included.

Boundary conventions:
- Deadline boundary is window_end + escalation_delay_minutes.
- Activity at or before deadline (window_start <= sat <= deadline) satisfies routine -> OBSERVED.
- Activity strictly after deadline (sat > deadline) does not satisfy routine -> MISSED when past deadline.
- Activity strictly before window_start (sat < window_start) does not satisfy routine.
"""

from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from src.threshold.models import DeviceHealthRecord, Routine, Verdict
from src.threshold.status import (
    derive_status,
    STATUS_MISSED,
    STATUS_OBSERVED,
    STATUS_WATCHING,
)
from src.threshold.triage import triage_absence

TZ = ZoneInfo("America/Chicago")


def make_test_routine(
    routine_id: str = "rt_morning",
    name: str = "Morning Door",
    window_start: str = "07:00",
    window_end: str = "09:00",
    escalation_delay_minutes: int = 30,
    last_satisfied_at: datetime | None = None,
) -> Routine:
    """Helper to construct Routine instances for status testing."""
    return Routine(
        name=name,
        property_id="prop_home",
        device_id="front_door_cam",
        expected_event_type="motion_detected",
        window_start=window_start,
        window_end=window_end,
        timezone="America/Chicago",
        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        escalation_delay_minutes=escalation_delay_minutes,
        last_satisfied_at=last_satisfied_at,
    )


class TestGracePeriodSemantics(unittest.TestCase):
    """Unit tests for grace period extensions in routine status derivation."""

    def test_grace_period_event_at_window_end_plus_10m_observed(self):
        """Event at window_end + 10 min -> OBSERVED, no escalation."""
        # Window: 07:00-09:00, delay 30m -> deadline 09:30
        # Event occurred at 09:10 (10 min into grace buffer)
        routine = make_test_routine(
            window_start="07:00",
            window_end="09:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 28, 9, 10, tzinfo=TZ),
        )
        now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=5),
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_OBSERVED)

        # Absence triage must not produce an escalation
        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNone(result)

    def test_grace_period_event_at_exact_deadline_boundary_observed(self):
        """Event exactly at deadline boundary (window_end + delay) -> OBSERVED."""
        # Window: 07:00-09:00, delay 30m -> deadline exactly 09:30:00
        routine = make_test_routine(
            window_start="07:00",
            window_end="09:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 28, 9, 30, 0, tzinfo=TZ),
        )
        now = datetime(2026, 9, 28, 9, 35, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=5),
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_OBSERVED)

        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNone(result)

    def test_grace_period_event_at_deadline_plus_1m_missed(self):
        """Event at deadline + 1 min -> MISSED, fires escalation."""
        # Window: 07:00-09:00, delay 30m -> deadline 09:30:00
        # Event occurred at 09:31:00 (1 minute past deadline)
        routine = make_test_routine(
            window_start="07:00",
            window_end="09:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 28, 9, 31, 0, tzinfo=TZ),
        )
        now = datetime(2026, 9, 28, 9, 35, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=4),  # healthy
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_MISSED)

        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, Verdict.URGENT)

    def test_event_before_window_start_does_not_satisfy(self):
        """Event occurred before window_start -> does not satisfy routine."""
        # Window: 07:00-09:00, event at 06:55 (5 min before window_start)
        routine = make_test_routine(
            window_start="07:00",
            window_end="09:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 28, 6, 55, 0, tzinfo=TZ),
        )
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=datetime(2026, 9, 28, 6, 55, 0, tzinfo=TZ),
        )

        # During window -> WATCHING
        now_during = datetime(2026, 9, 28, 8, 0, 0, tzinfo=TZ)
        health_during = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now_during - timedelta(minutes=5),
        )
        self.assertEqual(derive_status(routine, health_during, now_during), STATUS_WATCHING)

        # Past deadline with fresh device health -> MISSED
        now_past = datetime(2026, 9, 28, 9, 35, 0, tzinfo=TZ)
        health_past = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now_past - timedelta(minutes=5),
        )
        self.assertEqual(derive_status(routine, health_past, now_past), STATUS_MISSED)

    def test_overnight_window_grace_period_event_at_window_end_plus_10m_observed(self):
        """Overnight window: event at window_end + 10 min -> OBSERVED, no escalation."""
        # Window: 22:00 to 06:00 (crosses midnight), delay 30m -> deadline 06:30
        # Event occurred at 06:10 the following morning
        routine = make_test_routine(
            routine_id="rt_night_sleep",
            name="Night Rest Routine",
            window_start="22:00",
            window_end="06:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 29, 6, 10, 0, tzinfo=TZ),
        )
        now = datetime(2026, 9, 29, 6, 35, 0, tzinfo=TZ)  # Past 06:30 deadline
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=5),
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_OBSERVED)

        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNone(result)

    def test_overnight_window_grace_period_event_at_exact_deadline_boundary_observed(self):
        """Overnight window: event exactly at deadline boundary (06:30:00) -> OBSERVED."""
        # Window: 22:00 to 06:00, delay 30m -> deadline 06:30:00
        routine = make_test_routine(
            routine_id="rt_night_sleep",
            name="Night Rest Routine",
            window_start="22:00",
            window_end="06:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 29, 6, 30, 0, tzinfo=TZ),
        )
        now = datetime(2026, 9, 29, 6, 35, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=5),
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_OBSERVED)

        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNone(result)

    def test_overnight_window_event_at_deadline_plus_1m_missed(self):
        """Overnight window: event at deadline + 1 min (06:31:00) -> MISSED."""
        # Window: 22:00 to 06:00, delay 30m -> deadline 06:30:00
        routine = make_test_routine(
            routine_id="rt_night_sleep",
            name="Night Rest Routine",
            window_start="22:00",
            window_end="06:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 29, 6, 31, 0, tzinfo=TZ),
        )
        now = datetime(2026, 9, 29, 6, 35, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=4),  # healthy
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_MISSED)

        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, Verdict.URGENT)

    def test_overnight_window_event_before_window_start_does_not_satisfy(self):
        """Overnight window: event before window_start (21:55 before 22:00) -> MISSED."""
        routine = make_test_routine(
            routine_id="rt_night_sleep",
            name="Night Rest Routine",
            window_start="22:00",
            window_end="06:00",
            escalation_delay_minutes=30,
            last_satisfied_at=datetime(2026, 9, 28, 21, 55, 0, tzinfo=TZ),
        )
        now = datetime(2026, 9, 29, 6, 35, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=5),
        )

        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_MISSED)


if __name__ == "__main__":
    unittest.main()

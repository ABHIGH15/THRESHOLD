"""
Phase 1 tests — rules-only triage engine against synthetic fixtures.

Definition of Done (EXECUTION_PLAN.md Section 6):
  [x] Every synthetic fixture scenario produces the tier the scenario was designed to produce
      — shown as a pass/fail table, not a verbal claim.
  [x] The "fire once, not on every tick" edge-detection is tested explicitly:
      run the derived-status function across multiple consecutive ticks past a miss
      and confirm only one Urgent fires.
  [x] The "unknown" state is tested explicitly: a routine past its window with a stale
      device health timestamp must NOT escalate — it must return "unknown" instead of "missed".
  [x] No AWS resource has been touched.
"""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.threshold.models import Routine, DeviceHealthRecord, RingEvent, Verdict
from src.threshold.status import (
    derive_status,
    STATUS_MISSED,
    STATUS_WATCHING,
    STATUS_OBSERVED,
    STATUS_SNOOZED,
    STATUS_UNKNOWN,
)
from src.threshold.triage import (
    triage_event,
    triage_absence,
    run_scheduler_tick,
    match_event_to_routine,
)
from tests.fixtures import (
    scenario_normal_day,
    scenario_missed_pickup,
    scenario_3am_door_open,
    scenario_silent_morning,
    make_routines,
    make_device_health_fresh,
    make_device_health_stale,
    make_device_health_never_seen,
    TZ,
)


class TestDerivedStatus(unittest.TestCase):
    """Test the three-state derived status computation."""

    def test_snoozed(self):
        """A snoozed routine returns 'snoozed' regardless of other state."""
        routine = make_routines()[0]  # Morning front door
        routine.snoozed_until = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)
        now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)  # past window, but snoozed
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=30),
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_SNOOZED)

    def test_observed(self):
        """A routine satisfied within its window returns 'observed'."""
        routine = make_routines()[0]
        routine.last_satisfied_at = datetime(2026, 9, 28, 7, 45, tzinfo=TZ)
        now = datetime(2026, 9, 28, 8, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_OBSERVED)

    def test_watching_within_window(self):
        """Within the window, not yet satisfied → 'watching'."""
        routine = make_routines()[0]
        now = datetime(2026, 9, 28, 8, 0, tzinfo=TZ)  # within 07:00-09:00
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_WATCHING)

    def test_watching_past_window_before_deadline(self):
        """Past window_end but before escalation deadline → still 'watching'."""
        routine = make_routines()[0]  # window_end=09:00, delay=30min
        now = datetime(2026, 9, 28, 9, 15, tzinfo=TZ)  # 09:15 — past window, before 09:30
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_WATCHING)

    def test_missed_healthy_device(self):
        """Past escalation deadline with healthy device → 'missed' (Urgent)."""
        routine = make_routines()[0]  # window_end=09:00, delay=30min → deadline 09:30
        now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),  # recently active
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_MISSED)

    def test_unknown_stale_device(self):
        """
        Past escalation deadline but device is stale → 'unknown' (NOT 'missed').
        This is the key safety behavior of the three-state model.
        """
        routine = make_routines()[0]
        now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(hours=5),  # 5 hours ago — stale
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_UNKNOWN)

    def test_unknown_never_seen_device(self):
        """Device never reported → 'unknown' (NOT 'missed')."""
        routine = make_routines()[0]
        now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
        health = DeviceHealthRecord(device_id="front_door_cam")  # last_event_at is None
        self.assertEqual(derive_status(routine, health, now), STATUS_UNKNOWN)

    def test_non_routine_day(self):
        """Saturday for a weekday-only routine → 'observed' (nothing expected)."""
        routine = make_routines()[1]  # Evening front door, Mon-Fri only
        # Saturday, September 26, 2026
        now = datetime(2026, 9, 26, 18, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=30),
        )
        self.assertEqual(derive_status(routine, health, now), STATUS_OBSERVED)


class TestEventTriaging(unittest.TestCase):
    """Test the webhook event → triage path."""

    def test_event_matches_routine(self):
        """An event matching a routine → Routine verdict, last_satisfied_at updated."""
        routines, events, health, now = scenario_normal_day()
        # Triage the morning motion event
        result = triage_event(events[0], routines, events[0].timestamp)
        self.assertEqual(result.verdict, Verdict.ROUTINE)
        self.assertIn("Morning front door", result.reason)
        # Verify last_satisfied_at was updated
        self.assertEqual(routines[0].last_satisfied_at, events[0].timestamp)

    def test_event_no_match(self):
        """An event outside all routine windows → Needs a look (agent path stub)."""
        routines, events, health, now = scenario_3am_door_open()
        result = triage_event(events[0], routines, events[0].timestamp)
        self.assertEqual(result.verdict, Verdict.NEEDS_A_LOOK)
        self.assertIn("does not match", result.reason)

    def test_doorbell_matches_routine(self):
        """A button_press event matching the doorbell routine → Routine."""
        routines, events, health, now = scenario_normal_day()
        result = triage_event(events[1], routines, events[1].timestamp)
        self.assertEqual(result.verdict, Verdict.ROUTINE)
        self.assertIn("Morning doorbell", result.reason)


class TestAbsenceDetection(unittest.TestCase):
    """Test the scheduler-driven absence detection path."""

    def test_missed_produces_urgent(self):
        """
        Scenario 2: Missed pickup — routine not satisfied, device healthy.
        watching → missed transition → Urgent verdict.
        """
        routines, health, now = scenario_missed_pickup()
        result = triage_absence(
            routines[0], health["front_door_cam"], now,
            previous_status=STATUS_WATCHING,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, Verdict.URGENT)
        self.assertIn("Morning front door", result.reason)

    def test_silent_morning_produces_unknown_not_urgent(self):
        """
        Scenario 4: Silent morning — device stale.
        watching → unknown transition → Needs a look (NOT Urgent).
        This is the three-state model's key safety test.
        """
        routines, health, now = scenario_silent_morning()
        result = triage_absence(
            routines[0], health["front_door_cam"], now,
            previous_status=STATUS_WATCHING,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.verdict, Verdict.NEEDS_A_LOOK)
        self.assertIn("stale", result.reason)
        # Crucially: NOT Urgent
        self.assertNotEqual(result.verdict, Verdict.URGENT)


class TestEdgeDetection(unittest.TestCase):
    """
    Test "fire once, not on every tick" — the edge detector.
    DoD requirement: run the derived-status function across multiple consecutive
    ticks past a miss and confirm only one Urgent fires.
    """

    def test_fire_once_on_transition(self):
        """
        Simulate 5 consecutive scheduler ticks past the missed deadline.
        Only the FIRST tick (watching → missed) should produce an Urgent.
        Subsequent ticks (missed → missed) should produce nothing.
        """
        routine = make_routines()[0]  # Morning front door, window 07:00-09:00, delay 30min
        # Start at 09:31 — just past the 09:30 deadline
        base_time = datetime(2026, 9, 28, 9, 31, tzinfo=TZ)

        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=base_time - timedelta(minutes=10),  # fresh device
        )

        results = []
        prev_status = STATUS_WATCHING  # initial state before any ticks

        # Simulate 5 ticks, 1 minute apart
        for tick in range(5):
            now = base_time + timedelta(minutes=tick)
            result = triage_absence(routine, health, now, previous_status=prev_status)
            if result is not None:
                results.append(result)
            # Update prev_status for edge detection
            prev_status = derive_status(routine, health, now)

        # Exactly ONE Urgent should have fired (on tick 0: watching → missed)
        urgent_results = [r for r in results if r.verdict == Verdict.URGENT]
        self.assertEqual(
            len(urgent_results), 1,
            f"Expected exactly 1 Urgent, got {len(urgent_results)}: {[r.reason for r in urgent_results]}"
        )

    def test_no_fire_when_already_missed(self):
        """
        If previous_status is already 'missed', no Urgent fires (missed → missed).
        """
        routine = make_routines()[0]
        now = datetime(2026, 9, 28, 9, 45, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),
        )
        result = triage_absence(routine, health, now, previous_status=STATUS_MISSED)
        self.assertIsNone(result)

    def test_no_fire_when_observed(self):
        """If routine is satisfied (observed), no action needed."""
        routine = make_routines()[0]
        routine.last_satisfied_at = datetime(2026, 9, 28, 7, 45, tzinfo=TZ)
        now = datetime(2026, 9, 28, 10, 0, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),
        )
        result = triage_absence(routine, health, now, previous_status=STATUS_OBSERVED)
        self.assertIsNone(result)


class TestSchedulerTick(unittest.TestCase):
    """Test the full scheduler tick across multiple routines."""

    def test_normal_day_no_actions(self):
        """All routines satisfied → no action-worthy transitions."""
        routines, events, health, now = scenario_normal_day()
        # Satisfy the routines first
        for event in events:
            triage_event(event, routines, event.timestamp)

        # Now run a scheduler tick — all should be observed
        prev_statuses = {r.name: STATUS_OBSERVED for r in routines}
        results, new_statuses = run_scheduler_tick(routines, health, prev_statuses, now)

        self.assertEqual(len(results), 0, f"Expected no actions, got: {[r.reason for r in results]}")

    def test_missed_pickup_one_urgent(self):
        """
        Missed pickup scenario: one routine should fire Urgent,
        others should not (different windows/not yet past deadline).
        """
        routines, health, now = scenario_missed_pickup()
        # Morning front door is past deadline; others may or may not be
        prev_statuses = {r.name: STATUS_WATCHING for r in routines}
        results, new_statuses = run_scheduler_tick(routines, health, prev_statuses, now)

        urgent_results = [r for r in results if r.verdict == Verdict.URGENT]
        # At minimum, "Morning front door" should be Urgent
        urgent_names = [r.routine_name for r in urgent_results]
        self.assertIn("Morning front door", urgent_names)


class TestFailureCases(unittest.TestCase):
    """
    DoD requirement: At least one failure/edge case tried on purpose
    and confirmed to fail the correct way.
    """

    def test_bad_event_type_no_match(self):
        """An event with a type not matching any routine → no routine match."""
        routines = make_routines()
        event = RingEvent(
            device_id="front_door_cam",
            event_type="device_online",  # valid type but no routine expects this
            timestamp=datetime(2026, 9, 28, 7, 45, tzinfo=TZ),
        )
        matched = match_event_to_routine(event, routines, event.timestamp)
        self.assertIsNone(matched)

    def test_wrong_device_no_match(self):
        """An event from an unknown device → no routine match."""
        routines = make_routines()
        event = RingEvent(
            device_id="unknown_camera",
            event_type="motion_detected",
            timestamp=datetime(2026, 9, 28, 7, 45, tzinfo=TZ),
        )
        matched = match_event_to_routine(event, routines, event.timestamp)
        self.assertIsNone(matched)

    def test_snoozed_routine_no_escalation(self):
        """A snoozed routine past its window → snoozed status, no escalation."""
        routine = make_routines()[0]
        routine.snoozed_until = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)
        now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
        health = DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=now - timedelta(minutes=10),
        )
        status = derive_status(routine, health, now)
        self.assertEqual(status, STATUS_SNOOZED)
        # And no Urgent fires
        result = triage_absence(routine, health, now, previous_status=STATUS_WATCHING)
        self.assertIsNone(result)  # snoozed is not missed, so no transition fires


if __name__ == "__main__":
    unittest.main()

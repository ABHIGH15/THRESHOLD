"""
Synthetic test fixtures for Phase 1.

Covers the four scenarios:
  1. Normal day — all routines satisfied
  2. Missed pickup — routine not satisfied within window
  3. 3am door-open — ambiguous event outside any routine window
  4. Silent morning — no events at all (device may be offline)

Uses ONLY confirmed event types and schema from the ground-truth table.
NO invented fields. NO package/vehicle events (unconfirmed schema).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.threshold.models import Routine, DeviceHealthRecord, RingEvent

TZ = ZoneInfo("America/Chicago")


def make_routines() -> list[Routine]:
    """Standard test routines for a caretaking scenario."""
    return [
        Routine(
            property_id="prop_dad",
            name="Morning front door",
            device_id="front_door_cam",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            escalation_delay_minutes=30,
        ),
        Routine(
            property_id="prop_dad",
            name="Evening front door",
            device_id="front_door_cam",
            expected_event_type="motion_detected",
            window_start="17:00",
            window_end="19:00",
            timezone="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=60,
        ),
        Routine(
            property_id="prop_dad",
            name="Morning doorbell",
            device_id="front_doorbell",
            expected_event_type="button_press",
            window_start="08:00",
            window_end="10:00",
            timezone="America/Chicago",
            days=["mon", "wed", "fri"],  # nurse visits MWF
            escalation_delay_minutes=45,
        ),
    ]


def make_device_health_fresh(base_time: datetime) -> dict[str, DeviceHealthRecord]:
    """Device health records where all devices reported recently — not stale."""
    return {
        "front_door_cam": DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=base_time - timedelta(minutes=30),
            last_event_type="motion_detected",
        ),
        "front_doorbell": DeviceHealthRecord(
            device_id="front_doorbell",
            last_event_at=base_time - timedelta(minutes=45),
            last_event_type="device_online",
        ),
    }


def make_device_health_stale(base_time: datetime) -> dict[str, DeviceHealthRecord]:
    """Device health records where the front door cam is stale (>2h since last event)."""
    return {
        "front_door_cam": DeviceHealthRecord(
            device_id="front_door_cam",
            last_event_at=base_time - timedelta(hours=5),  # 5 hours ago — well past 2h threshold
            last_event_type="device_offline",
        ),
        "front_doorbell": DeviceHealthRecord(
            device_id="front_doorbell",
            last_event_at=base_time - timedelta(minutes=45),
            last_event_type="device_online",
        ),
    }


def make_device_health_never_seen() -> dict[str, DeviceHealthRecord]:
    """Device health records where a device has NEVER reported — maximally stale."""
    return {
        "front_door_cam": DeviceHealthRecord(
            device_id="front_door_cam",
            # last_event_at is None — never seen
        ),
        "front_doorbell": DeviceHealthRecord(
            device_id="front_doorbell",
            # last_event_at is None — never seen
        ),
    }


# ============================================================
# Scenario 1: Normal day — all routines satisfied
# ============================================================
def scenario_normal_day() -> tuple[list[Routine], list[RingEvent], dict[str, DeviceHealthRecord], datetime]:
    """
    A normal Monday. Dad opens the front door at 7:45am, motion is detected.
    Nurse rings the doorbell at 8:30am.
    All routines should be satisfied → Routine verdict.
    """
    # Monday, September 28, 2026
    base_time = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)
    routines = make_routines()

    events = [
        RingEvent(
            device_id="front_door_cam",
            event_type="motion_detected",
            timestamp=datetime(2026, 9, 28, 7, 45, tzinfo=TZ),
            sub_type="human",
        ),
        RingEvent(
            device_id="front_doorbell",
            event_type="button_press",
            timestamp=datetime(2026, 9, 28, 8, 30, tzinfo=TZ),
        ),
    ]

    health = make_device_health_fresh(base_time)
    return routines, events, health, base_time


# ============================================================
# Scenario 2: Missed pickup — routine not satisfied
# ============================================================
def scenario_missed_pickup() -> tuple[list[Routine], dict[str, DeviceHealthRecord], datetime]:
    """
    Monday morning. No motion event at the front door within the 07:00–09:00 window.
    Devices are healthy (reporting normally). By 09:30 (past escalation_delay),
    derived status should be "missed" → Urgent verdict.

    Returns routines with NO last_satisfied_at set (simulating no events received).
    """
    # Monday, September 28, 2026, 09:35 — past window_end (09:00) + delay (30min)
    now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
    routines = make_routines()
    # Don't set last_satisfied_at — no event was received
    health = make_device_health_fresh(now)
    return routines, health, now


# ============================================================
# Scenario 3: 3am door-open — ambiguous event
# ============================================================
def scenario_3am_door_open() -> tuple[list[Routine], list[RingEvent], dict[str, DeviceHealthRecord], datetime]:
    """
    Motion detected at 3am at the front door. This doesn't match any routine
    window → should trigger the agent-investigated path (Needs a look in Phase 1).
    """
    now = datetime(2026, 9, 28, 3, 15, tzinfo=TZ)
    routines = make_routines()

    events = [
        RingEvent(
            device_id="front_door_cam",
            event_type="motion_detected",
            timestamp=datetime(2026, 9, 28, 3, 0, tzinfo=TZ),
            sub_type="human",
        ),
    ]

    health = make_device_health_fresh(now)
    return routines, events, health, now


# ============================================================
# Scenario 4: Silent morning — no events, device possibly offline
# ============================================================
def scenario_silent_morning() -> tuple[list[Routine], dict[str, DeviceHealthRecord], datetime]:
    """
    Monday morning. No events AND the front door camera's last activity was
    5 hours ago (stale). By 09:35 (past escalation deadline), derived status
    should be "unknown" — NOT "missed" — because we can't trust the absence
    conclusion when the device itself may be offline.

    This tests the three-state model's key safety behavior.
    """
    now = datetime(2026, 9, 28, 9, 35, tzinfo=TZ)
    routines = make_routines()
    health = make_device_health_stale(now)
    return routines, health, now

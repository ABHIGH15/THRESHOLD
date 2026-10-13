"""
Data models for Threshold — matches threshold_spec.md exactly.

Routine schema: Section "Routine Schema (Locked — Derived Status, Three-State Absence Model)"
Event types: Only confirmed types from the ground-truth table are included.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# --- Confirmed event types from ground-truth table ---
# Source: GA release notes, April 21, 2025
CONFIRMED_EVENT_TYPES = frozenset({
    "motion_detected",
    "button_press",
    "device_added",
    "device_removed",
    "device_online",
    "device_offline",
    "app_integration_added",
    "app_integration_removed",
    "subscription_activated",
    "subscription_deactivated",
})

# Only "human" is confirmed as a subType value.
# Source: motion-detection.html (user-verified)
CONFIRMED_SUB_TYPES = frozenset({"human"})


@dataclass
class RingEvent:
    """
    A webhook event from the Ring Partner API.

    Field structure: subType is camelCase, directly under data.
    DO NOT use attributes.sub_type (snake_case) — that's from the
    unconfirmed ring-sandbox PyPI package schema.
    """

    device_id: str
    event_type: str  # one of CONFIRMED_EVENT_TYPES
    timestamp: datetime
    sub_type: Optional[str] = None  # camelCase "subType" in the JSON payload, only "human" confirmed
    raw_payload: Optional[dict] = None  # preserve the full payload for debugging


@dataclass
class Routine:
    """
    Routine configuration + minimal mutable state.
    Matches threshold_spec.md Section "Routine Schema" exactly.

    Status is NEVER stored — it's derived fresh on every scheduler tick.
    Only last_satisfied_at and snoozed_until are mutable.
    """

    property_id: str
    name: str  # e.g., "Morning door open"
    device_id: str
    expected_event_type: str  # e.g., "motion_detected", "button_press"
    window_start: str  # "07:00" — local time in the routine's timezone
    window_end: str  # "09:00"
    timezone: str  # e.g., "America/Chicago"
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    escalation_delay_minutes: int = 30  # how long past window_end before firing
    last_satisfied_at: Optional[datetime] = None  # written by webhook path
    snoozed_until: Optional[datetime] = None  # written by caretaker action
    snapshot_device_id: Optional[str] = None  # camera device for snapshot-enriched alerts


@dataclass
class DeviceHealthRecord:
    """
    Tracks the last time we received ANY event from a device.

    Used by the three-state absence model: if the device's last known
    activity is stale (older than staleness_threshold_minutes), we return
    "unknown" instead of "missed" — because a silent device might mean
    the device is offline, not that the routine didn't happen.

    is_stale checks recency of the device's last known status, not just
    whether the last webhook said "online" — a stale timestamp is itself
    informative.
    """

    device_id: str
    last_event_at: Optional[datetime] = None
    last_event_type: Optional[str] = None
    staleness_threshold_minutes: int = 120  # 2 hours default — configurable

    def is_stale(self, now: datetime) -> bool:
        """
        Returns True if we haven't heard from this device recently enough
        to trust absence conclusions.
        """
        if self.last_event_at is None:
            return True  # never heard from this device — definitely stale
        from datetime import timedelta
        return (now - self.last_event_at) > timedelta(minutes=self.staleness_threshold_minutes)


# --- Triage verdicts ---
# Internal names (spec Section "Three-tier verdict")
# UI-facing labels: Routine / Check Needed / Escalation
class Verdict:
    ROUTINE = "routine"
    NEEDS_A_LOOK = "needs_a_look"
    URGENT = "urgent"

"""
Triage engine — rules-determined paths only (Phase 1).

From threshold_spec.md Section "Triage Engine — Bounded-Authority Agent":

Rules-determined paths (no agent involvement):
  - Event matches a known routine → Routine. Log silently. Update last_satisfied_at.
  - Scheduler derives watching → missed → Urgent. Escalate.

Agent-investigated path (ambiguous events):
  - Events that don't match any known routine trigger the bounded-authority agent.
  - NOT implemented in Phase 1 — this is Phase 5 work.

Edge detection for "fire once":
  The scheduler compares current derived status against last-tick status.
  Urgent fires only on the watching → missed transition, not on every
  subsequent tick where it's still missed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src.threshold.models import Routine, DeviceHealthRecord, RingEvent, Verdict
from src.threshold.status import (
    derive_status,
    STATUS_MISSED,
    STATUS_WATCHING,
    STATUS_OBSERVED,
    STATUS_SNOOZED,
    STATUS_UNKNOWN,
    _get_today_window,
    _parse_time,
)


@dataclass
class TriageResult:
    """The output of the triage engine for a single evaluation."""

    verdict: str  # One of Verdict.ROUTINE, Verdict.NEEDS_A_LOOK, Verdict.URGENT
    reason: str
    routine_name: Optional[str] = None
    device_id: Optional[str] = None
    event: Optional[RingEvent] = None
    derived_status: Optional[str] = None  # the status that produced this verdict


def match_event_to_routine(
    event: RingEvent,
    routines: list[Routine],
    now: datetime,
) -> Optional[Routine]:
    """
    Check if an incoming event satisfies any known routine.

    An event matches a routine if:
    1. The device_id matches
    2. The event_type matches expected_event_type
    3. The event falls within the routine's window (in the routine's timezone)
    4. Today is a routine day
    """
    for routine in routines:
        if routine.device_id != event.device_id:
            continue
        if routine.expected_event_type != event.event_type:
            continue

        # Check if today is a routine day
        tz = ZoneInfo(routine.timezone)
        local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        if day_names[local_now.weekday()] not in routine.days:
            continue

        # Check if event timestamp is within the routine window
        # (with some grace — events slightly before or after the window
        # should still satisfy the routine)
        window_start, window_end = _get_today_window(routine, now)

        # Allow events within the window + escalation delay period to satisfy
        grace_end = window_end + timedelta(minutes=routine.escalation_delay_minutes)
        event_ts = event.timestamp
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=tz)

        if window_start <= event_ts <= grace_end:
            return routine

    return None


def triage_event(
    event: RingEvent,
    routines: list[Routine],
    now: datetime,
) -> TriageResult:
    """
    Triage an incoming webhook event.

    Rules-determined paths (Phase 1):
    1. If the event matches a known routine → Routine (log silently, update last_satisfied_at)
    2. If the event doesn't match any routine → Needs a look (agent path, stubbed in Phase 1)

    The absence path (missed routines) is handled by triage_absence(), not here.
    This function only handles incoming events from the webhook path.
    """
    matched_routine = match_event_to_routine(event, routines, now)

    if matched_routine is not None:
        # Spec: "Event matches a known routine → Routine. Log silently. Update last_satisfied_at."
        matched_routine.last_satisfied_at = event.timestamp
        return TriageResult(
            verdict=Verdict.ROUTINE,
            reason=f"Event satisfies routine '{matched_routine.name}'",
            routine_name=matched_routine.name,
            device_id=event.device_id,
            event=event,
            derived_status=STATUS_OBSERVED,
        )

    # Spec: "Events that don't match any known routine trigger the bounded-authority agent."
    # Phase 1: stub as Needs a look — the agent layer is Phase 5 work.
    return TriageResult(
        verdict=Verdict.NEEDS_A_LOOK,
        reason="Event does not match any known routine — agent investigation required (Phase 5)",
        device_id=event.device_id,
        event=event,
    )


def triage_absence(
    routine: Routine,
    device_health: DeviceHealthRecord,
    now: datetime,
    previous_status: Optional[str] = None,
) -> Optional[TriageResult]:
    """
    Evaluate a single routine for absence detection on a scheduler tick.

    Implements edge detection for "fire once":
    - Urgent fires ONLY on the watching → missed transition
    - NOT on every subsequent tick where it's still missed
    - This prevents repeated escalation spam

    Returns a TriageResult only if an action-worthy transition occurred.
    Returns None if no action is needed this tick.
    """
    current_status = derive_status(routine, device_health, now)

    # Edge detection: only fire on the transition, not while staying in a state
    if current_status == STATUS_MISSED and previous_status == STATUS_WATCHING:
        # Spec: "Scheduler derives watching → missed → Urgent. Escalate.
        # The absence itself is the evidence; no LLM needed."
        return TriageResult(
            verdict=Verdict.URGENT,
            reason=f"Routine '{routine.name}' was not satisfied within its expected window. "
            f"No matching event received by escalation deadline.",
            routine_name=routine.name,
            device_id=routine.device_id,
            derived_status=current_status,
        )

    if current_status == STATUS_UNKNOWN and previous_status == STATUS_WATCHING:
        # Device health is stale — we can't conclude absence.
        # This is NOT an escalation, but it IS worth logging.
        return TriageResult(
            verdict=Verdict.NEEDS_A_LOOK,
            reason=f"Routine '{routine.name}' was not satisfied, but device '{routine.device_id}' "
            f"health data is stale — cannot confirm absence. Not escalating.",
            routine_name=routine.name,
            device_id=routine.device_id,
            derived_status=current_status,
        )

    # No action-worthy transition
    return None


def run_scheduler_tick(
    routines: list[Routine],
    device_health_map: dict[str, DeviceHealthRecord],
    previous_statuses: dict[str, str],
    now: datetime,
) -> tuple[list[TriageResult], dict[str, str]]:
    """
    Run a full scheduler tick across all routines.

    This is the top-level function the scheduler Lambda calls.

    Args:
        routines: All configured routines
        device_health_map: device_id → DeviceHealthRecord
        previous_statuses: routine_name → last-tick status (for edge detection)
        now: current time

    Returns:
        (results, new_statuses) where:
        - results is a list of TriageResults for action-worthy transitions
        - new_statuses is the updated status map for next tick's edge detection
    """
    results = []
    new_statuses = {}

    for routine in routines:
        # Get device health (default to a stale record if unknown device)
        health = device_health_map.get(
            routine.device_id,
            DeviceHealthRecord(device_id=routine.device_id),  # no last_event_at → stale
        )

        # Derive current status
        current_status = derive_status(routine, health, now)
        new_statuses[routine.name] = current_status

        # Get previous status for edge detection
        prev = previous_statuses.get(routine.name)

        # Check for action-worthy transitions
        result = triage_absence(routine, health, now, previous_status=prev)
        if result is not None:
            results.append(result)

    return results, new_statuses

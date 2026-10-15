"""
Deterministic, bounded gate for Threshold Agent recommendations.
Pure function: no DB, no clock, no free-text parsing, standard library only.
Authority only over agent recommendations, never absence escalations.
"""

from src.threshold.agent.schemas import GateDecision, GateFacts, TIERS


# Gate Policy Thresholds
MIN_OK_TOOLS_FOR_ESCALATE = 2
NOTIFICATION_COOLDOWN_MINUTES = 30
MAX_AGENT_ESCALATIONS_6H = 2


def decide(tier: str, ok_tool_results: int, facts: GateFacts) -> GateDecision:
    """
    Deterministic gate decision over model tier, tool results count, and physical facts.

    Rules in strict precedence order:
    1. tier not in TIERS -> check_needed ('invalid_tier')
    2. tier == 'routine' -> routine ('routine')
    3. not device_online_fresh -> check_needed ('device_not_fresh'). Never escalate on silent device.
    4. routine_snoozed -> check_needed ('snoozed')
    5. tier == 'check_needed' -> check_needed ('check_needed')
    6. tier == 'escalate':
       - ok_tool_results >= 2 ('insufficient_evidence')
       - minutes_since_last_notification is None or >= 30 ('notification_cooldown')
       - agent_escalations_last_6h < 2 ('escalation_rate_limit')
       - if all cleared -> escalate ('cleared')
    """
    # Rule 1: Tier must be valid
    if tier not in TIERS:
        return GateDecision(decision="check_needed", reason_code="invalid_tier")

    # Rule 2: Routine tier accepted without escalation
    if tier == "routine":
        return GateDecision(decision="routine", reason_code="routine")

    # Rule 3: Device health must be confirmed fresh and online
    if not facts.device_online_fresh:
        return GateDecision(decision="check_needed", reason_code="device_not_fresh")

    # Rule 4: Snoozed routine blocks escalation
    if facts.routine_snoozed:
        return GateDecision(decision="check_needed", reason_code="snoozed")

    # Rule 5: Explicit check_needed tier
    if tier == "check_needed":
        return GateDecision(decision="check_needed", reason_code="check_needed")

    # Rule 6: Escalate tier requirements
    if tier == "escalate":
        if ok_tool_results < MIN_OK_TOOLS_FOR_ESCALATE:
            return GateDecision(decision="check_needed", reason_code="insufficient_evidence")

        if (
            facts.minutes_since_last_notification is not None
            and facts.minutes_since_last_notification < NOTIFICATION_COOLDOWN_MINUTES
        ):
            return GateDecision(decision="check_needed", reason_code="notification_cooldown")

        if facts.agent_escalations_last_6h >= MAX_AGENT_ESCALATIONS_6H:
            return GateDecision(decision="check_needed", reason_code="escalation_rate_limit")

        return GateDecision(decision="escalate", reason_code="cleared")

    return GateDecision(decision="check_needed", reason_code="invalid_tier")

"""
Schemas and data structures for the Threshold bounded-authority agent.
No numeric confidence field anywhere.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


TIERS = ("routine", "check_needed", "escalate")


@dataclass
class ToolCall:
    """Represents a tool call requested by the provider."""
    name: str
    args: Dict[str, Any]
    call_id: str


@dataclass
class ToolResult:
    """Represents the execution result of a tool call."""
    call_id: str
    tool: str
    ok: bool
    data: Any
    text: str
    error: Optional[str] = None


@dataclass
class Evidence:
    """Evidence item cited by the agent from tool results."""
    tool: str
    call_id: str
    ok: bool
    text: str


@dataclass
class Final:
    """Final output from provider."""
    tier: str
    recommendation: str
    cited_call_ids: List[str] = field(default_factory=list)


@dataclass
class Investigation:
    """Structured output of an investigation."""
    event_key: str
    mode: str
    provider_name: str
    steps: int
    elapsed_ms: int
    tier: str
    recommendation: str
    evidence: List[Evidence]
    fallback: bool
    trace: List[Any]


@dataclass
class GateFacts:
    """Factual context for gate evaluation."""
    device_online_fresh: bool
    routine_snoozed: bool
    minutes_since_last_notification: Optional[float]
    agent_escalations_last_6h: int


@dataclass
class GateDecision:
    """Deterministic decision emitted by the gate."""
    decision: str
    reason_code: str

"""
Bounded-authority agent library for Threshold.
Offline, deterministic, standard library only.
"""

from src.threshold.agent.facts import build_gate_facts
from src.threshold.agent.gate import decide
from src.threshold.agent.investigate import investigate, run_investigation_and_gate
from src.threshold.agent.provider import LLMProvider, MockProvider
from src.threshold.agent.schemas import (
    Evidence,
    Final,
    GateDecision,
    GateFacts,
    Investigation,
    TIERS,
    ToolCall,
    ToolResult,
)
from src.threshold.agent.tools import (
    TOOL_REGISTRY,
    check_household_memory,
    check_routine_status,
    execute_tool,
    get_recent_ring_events,
    get_threshold_history,
    pull_snapshot,
)

__all__ = [
    "Evidence",
    "Final",
    "GateDecision",
    "GateFacts",
    "Investigation",
    "LLMProvider",
    "MockProvider",
    "TIERS",
    "TOOL_REGISTRY",
    "ToolCall",
    "ToolResult",
    "build_gate_facts",
    "check_household_memory",
    "check_routine_status",
    "decide",
    "execute_tool",
    "get_recent_ring_events",
    "get_threshold_history",
    "investigate",
    "pull_snapshot",
    "run_investigation_and_gate",
]

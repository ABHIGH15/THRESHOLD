"""
LLM Provider protocol and MockProvider replaying scripted traces.
Standard library only; offline, deterministic.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Protocol, Union

from src.threshold.agent.schemas import Final, ToolCall


class LLMProvider(Protocol):
    """Protocol for LLM providers generating agent steps."""

    name: str

    def next(
        self,
        state: Dict[str, Any],
        tool_specs: List[Dict[str, Any]],
        deadline: float,
    ) -> Union[ToolCall, Final]:
        """Produce the next tool call or final decision within deadline."""
        ...


class MockProvider:
    """
    Replays scripted steps from traces/*.json.
    Reports mode: 'replay (scripted)'.
    """

    def __init__(
        self,
        trace_source: Union[str, Dict[str, Any]],
        name: str = "mock_provider",
    ):
        self.name = name
        self.mode = "replay (scripted)"
        if isinstance(trace_source, dict):
            self.trace_data = trace_source
        else:
            # If path doesn't exist directly, check relative to traces directory
            if os.path.exists(trace_source):
                path = trace_source
            else:
                base_dir = os.path.dirname(__file__)
                path = os.path.join(base_dir, "traces", trace_source)
                if not path.endswith(".json"):
                    path += ".json"
            with open(path, "r", encoding="utf-8") as f:
                self.trace_data = json.load(f)

        self.scenario = self.trace_data.get("scenario", "")
        self.provenance = self.trace_data.get("provenance", "scripted")
        self.steps = self.trace_data.get("steps", [])

    def next(
        self,
        state: Dict[str, Any],
        tool_specs: List[Dict[str, Any]],
        deadline: float,
    ) -> Union[ToolCall, Final]:
        """Return the next scripted step based on current step index in state."""
        if time.monotonic() > deadline:
            raise TimeoutError("Provider deadline exceeded.")

        # Number of completed calls in state dictates which scripted step to emit
        actions_taken = [s for s in state.get("step_history", []) if s.get("type") == "call"]
        step_index = len(actions_taken)
        if step_index >= len(self.steps):
            # If steps exhausted, fallback to final check_needed
            return Final(
                tier="check_needed",
                recommendation="Check needed: script steps exhausted.",
                cited_call_ids=[],
            )

        step_def = self.steps[step_index]
        step_type = step_def.get("type")

        if step_type == "call":
            return ToolCall(
                name=step_def["tool"],
                args=dict(step_def.get("args", {})),
                call_id=step_def.get("call_id", f"call_{step_index + 1}"),
            )
        elif step_type == "final":
            rec = step_def["recommendation"]
            if "{local_time}" in rec:
                local_time_val = "02:10"
                event = state.get("event", {})
                occ_str = event.get("occurred_at")
                if occ_str:
                    try:
                        clean_occ = occ_str.replace("Z", "+00:00")
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        occ_dt = datetime.fromisoformat(clean_occ)
                        tz_name = event.get("timezone") or state.get("timezone", "America/Chicago")
                        local_dt = occ_dt.astimezone(ZoneInfo(tz_name))
                        local_time_val = local_dt.strftime("%H:%M")
                    except Exception:
                        pass
                rec = rec.replace("{local_time}", local_time_val)

            return Final(
                tier=step_def["tier"],
                recommendation=rec,
                cited_call_ids=list(step_def.get("cited_call_ids", [])),
            )
        else:
            raise ValueError(f"Unknown step type in script: {step_type}")

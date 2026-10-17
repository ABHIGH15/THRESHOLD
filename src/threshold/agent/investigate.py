"""
Agent investigation orchestration engine.
Executes bounded investigation loop with strict step/time limits and whitelisted tools.
Evidence is DERIVED from tool-rendered text, not model-authored.
"""

from datetime import datetime, timezone
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from src.threshold.agent.facts import build_gate_facts
from src.threshold.agent.gate import decide
from src.threshold.agent.provider import LLMProvider
from src.threshold.agent.schemas import (
    Evidence,
    Final,
    GateDecision,
    Investigation,
    TIERS,
    ToolCall,
    ToolResult,
)
from src.threshold.agent.tools import TOOL_REGISTRY, execute_tool
from src.threshold.store import save_investigation, to_iso_timestamp


def _clean_recommendation(text: str) -> str:
    """Strip control characters and clamp recommendation to <= 280 characters."""
    if not text:
        return ""
    # Strip control characters (ASCII 0-31, 127-159) except standard space
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:280]


def investigate(
    conn: sqlite3.Connection,
    event: Dict[str, Any],
    provider: LLMProvider,
    tools: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    limits: Optional[Dict[str, Any]] = None,
) -> Investigation:
    """
    Run a bounded agent investigation on an incoming event:
    - max 6 steps
    - 10s per provider step
    - 20s total deadline
    - whitelisted tools only
    - evidence is derived from model-cited tool results
    - free-text recommendation is display-only (<=280 chars)
    - timeout/error/step limit triggers fallback check_needed
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    tools_cfg = tools or {}
    ring_client = tools_cfg.get("ring_client")
    snapshots_dir = tools_cfg.get("snapshots_dir", "snapshots")

    limits_cfg = limits or {}
    max_steps = limits_cfg.get("max_steps", 6)
    step_timeout = limits_cfg.get("step_timeout", 10.0)
    total_timeout = limits_cfg.get("total_timeout", 20.0)

    start_mono = time.monotonic()
    total_deadline = start_mono + total_timeout

    step_history: List[Dict[str, Any]] = []
    tool_results_by_id: Dict[str, ToolResult] = {}
    fallback = False
    final_output: Optional[Final] = None

    tool_specs = [
        {"name": name, "schema": getattr(func, "__doc__", "")}
        for name, func in TOOL_REGISTRY.items()
    ]

    while len(step_history) < max_steps:
        # Check overall deadline
        if time.monotonic() >= total_deadline:
            fallback = True
            break

        step_deadline = min(total_deadline, time.monotonic() + step_timeout)
        state = {
            "event": event,
            "step_history": list(step_history),
            "tool_results": [
                {
                    "call_id": tr.call_id,
                    "tool": tr.tool,
                    "ok": tr.ok,
                    "data": tr.data,
                    "text": tr.text,
                    "error": tr.error,
                }
                for tr in tool_results_by_id.values()
            ],
        }

        try:
            next_action = provider.next(state, tool_specs, deadline=step_deadline)
        except Exception:
            fallback = True
            break

        if isinstance(next_action, ToolCall):
            call_id = next_action.call_id
            step_record = {
                "type": "call",
                "tool": next_action.name,
                "args": next_action.args,
                "call_id": call_id,
            }
            step_history.append(step_record)

            if next_action.name not in TOOL_REGISTRY:
                tr = ToolResult(
                    call_id=call_id,
                    tool=next_action.name,
                    ok=False,
                    data=None,
                    text=f"Unknown tool: {next_action.name}",
                    error="unknown_tool",
                )
            else:
                tr = execute_tool(
                    name=next_action.name,
                    args=next_action.args,
                    call_id=call_id,
                    conn=conn,
                    now=now,
                    ring_client=ring_client,
                    snapshots_dir=snapshots_dir,
                )

            tool_results_by_id[call_id] = tr
            result_step = {
                "type": "result",
                "call_id": call_id,
                "tool": tr.tool,
                "ok": tr.ok,
                "text": tr.text,
                "error": tr.error,
            }
            if tr.tool == "pull_snapshot" and tr.ok and isinstance(tr.data, dict):
                result_step["data"] = {
                    "path": tr.data.get("path"),
                    "captured_at": tr.data.get("captured_at"),
                }
            step_history.append(result_step)

        elif isinstance(next_action, Final):
            final_output = next_action
            step_history.append({
                "type": "final",
                "tier": final_output.tier,
                "recommendation": final_output.recommendation,
                "cited_call_ids": list(final_output.cited_call_ids),
            })
            break
        else:
            fallback = True
            break

    elapsed_ms = int((time.monotonic() - start_mono) * 1000)

    # Process Final decision or Fallback
    if final_output is not None and not fallback:
        # Validate tier
        if final_output.tier not in TIERS:
            tier = "check_needed"
        else:
            tier = final_output.tier

        recommendation = _clean_recommendation(final_output.recommendation)
        if "{local_time}" in recommendation:
            occ_str = event.get("occurred_at")
            local_time_val = "02:10"
            if occ_str:
                try:
                    clean_occ = occ_str.replace("Z", "+00:00")
                    from zoneinfo import ZoneInfo
                    occ_dt = datetime.fromisoformat(clean_occ)
                    tz_name = event.get("timezone", "America/Chicago")
                    local_dt = occ_dt.astimezone(ZoneInfo(tz_name))
                    local_time_val = local_dt.strftime("%H:%M")
                except Exception:
                    pass
            recommendation = recommendation.replace("{local_time}", local_time_val)

        # DERIVE evidence strictly from tool results cited by the model
        evidence: List[Evidence] = []
        for cid in final_output.cited_call_ids:
            if cid in tool_results_by_id:
                tr = tool_results_by_id[cid]
                evidence.append(Evidence(
                    tool=tr.tool,
                    call_id=tr.call_id,
                    ok=tr.ok,
                    text=tr.text,
                ))
    else:
        tier = "check_needed"
        recommendation = "Check needed: could not determine"
        fallback = True
        # Keep whatever evidence was gathered so far
        evidence = [
            Evidence(tool=tr.tool, call_id=tr.call_id, ok=tr.ok, text=tr.text)
            for tr in tool_results_by_id.values()
        ]

    mode = getattr(provider, "mode", "agent")
    provider_name = getattr(provider, "name", "unknown")

    # Enforce "(replay, scripted)" prefix for mock/replay mode across all consumers
    if mode == "replay (scripted)" or provider_name in ("mock", "mock_provider"):
        prefix = "(replay, scripted)"
        if not recommendation.startswith(prefix):
            recommendation = f"{prefix} {recommendation}"

    return Investigation(
        event_key=event["event_key"],
        mode=mode,
        provider_name=provider_name,
        steps=len(step_history),
        elapsed_ms=elapsed_ms,
        tier=tier,
        recommendation=recommendation,
        evidence=evidence,
        fallback=fallback,
        trace=step_history,
    )


def run_investigation_and_gate(
    conn: sqlite3.Connection,
    event: Dict[str, Any],
    provider: LLMProvider,
    tools: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    limits: Optional[Dict[str, Any]] = None,
    save_to_db: bool = True,
) -> Tuple[Investigation, GateDecision]:
    """
    Execute full investigation pipeline:
    1. Run investigate()
    2. Count ok_tool_results
    3. Build GateFacts
    4. Call Gate decide()
    5. Save record to investigations table
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    investigation = investigate(
        conn=conn,
        event=event,
        provider=provider,
        tools=tools,
        now=now,
        limits=limits,
    )

    # Count distinct successful tools from derived evidence (same tool twice counts once)
    ok_tool_results = len({e.tool for e in investigation.evidence if e.ok})

    facts = build_gate_facts(conn=conn, device_id=event["device_id"], now=now)
    gate_decision = decide(
        tier=investigation.tier,
        ok_tool_results=ok_tool_results,
        facts=facts,
    )

    if save_to_db:
        evidence_dicts = [
            {"tool": e.tool, "call_id": e.call_id, "ok": e.ok, "text": e.text}
            for e in investigation.evidence
        ]
        save_investigation(
            conn=conn,
            event_key=investigation.event_key,
            device_id=event["device_id"],
            created_at=now,
            mode=investigation.mode,
            provider_name=investigation.provider_name,
            tier=investigation.tier,
            decision=gate_decision.decision,
            reason_code=gate_decision.reason_code,
            recommendation=investigation.recommendation,
            evidence_json=evidence_dicts,
            trace_json=investigation.trace,
            notified_at=None,
        )

    return investigation, gate_decision

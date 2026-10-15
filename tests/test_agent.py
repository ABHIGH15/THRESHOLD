"""
Comprehensive unit tests for Bounded-Authority Agent Library (src/threshold/agent).
Standard library only; offline, deterministic.
"""

import ast
from datetime import datetime, timezone
import json
import os
import sqlite3
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

from src.threshold.agent.facts import build_gate_facts
from src.threshold.agent.gate import decide
from src.threshold.agent.investigate import investigate, run_investigation_and_gate
from src.threshold.agent.provider import MockProvider
from src.threshold.agent.schemas import (
    Final,
    GateDecision,
    GateFacts,
    Investigation,
    TIERS,
    ToolCall,
    ToolResult,
)
from src.threshold.agent.tools import (
    check_household_memory,
    check_routine_status,
    execute_tool,
    get_recent_ring_events,
    get_threshold_history,
    pull_snapshot,
)
from src.threshold.agent.runner import format_agent_notification_text, process_new_events
from src.threshold.agent.triggers import is_ambiguous_event
from src.threshold.config import AgentConfig
from src.threshold.daemon import DaemonConfig, ThresholdDaemon
from src.threshold.notifier import RecordingNotifier
from src.threshold.ring_client import MediaUnavailable, RateLimited, TokenExpired
from src.threshold.scheduler import FakeClock
from src.threshold.store import (
    get_app_state,
    get_investigation,
    init_db,
    list_investigations,
    record_correction,
    record_escalation,
    save_event,
    save_investigation,
    save_routine,
    set_app_state,
    upsert_device_health,
)


class FakeRingClient:
    """Mock RingClient for offline snapshot testing."""

    def __init__(self, mode: str = "success"):
        self.mode = mode

    def get_latest_snapshot(
        self,
        device_id: str,
        lookback_hours: int = 24,
        deadline_seconds: float = 5.0,
    ) -> Tuple[bytes, Optional[int]]:
        if self.mode == "success":
            return b"FAKE_JPEG_IMAGE_CONTENT_12345", 1791800000000
        elif self.mode == "416":
            raise MediaUnavailable("416 Range Not Satisfiable")
        elif self.mode == "429":
            raise RateLimited("429 Too Many Requests")
        elif self.mode == "401":
            raise TokenExpired("401 Unauthorized")
        elif self.mode == "timeout":
            raise TimeoutError("Snapshot deadline exceeded")
        elif self.mode == "empty":
            return b"", None
        raise ValueError(f"Unknown fake mode: {self.mode}")


class TestAgentLibrary(unittest.TestCase):
    """Test suite covering tools, provider, investigate, gate, and import guards."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        self.conn = init_db(self.db_path)
        self.temp_snapshots = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        wal_path = f"{self.db_path}-wal"
        shm_path = f"{self.db_path}-shm"
        if os.path.exists(wal_path):
            os.remove(wal_path)
        if os.path.exists(shm_path):
            os.remove(shm_path)
        self.temp_snapshots.cleanup()

    # --- 1. Tools Tests ---

    def test_tool_get_recent_ring_events_success_and_bad_args(self):
        """get_recent_ring_events reads events table with args validation."""
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        save_event(
            self.conn,
            source="webhook",
            external_id="evt_1",
            device_id="cam_front",
            event_type="motion_detected",
            sub_type="human",
            occurred_at="2026-10-12T11:30:00Z",
            received_at="2026-10-12T11:30:00Z",
            raw_json={"test": 1},
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # Success case
        res = get_recent_ring_events(self.conn, now, {"device_id": "cam_front", "hours": 24, "limit": 10})
        self.assertTrue(res.ok)
        self.assertEqual(len(res.data), 1)
        self.assertIn("motion_detected", res.text)

        # Bad args: unknown arg
        res_bad = get_recent_ring_events(self.conn, now, {"device_id": "cam_front", "malicious_arg": "hack"})
        self.assertFalse(res_bad.ok)
        self.assertEqual(res_bad.error, "invalid_args")

        # Bad args: hours > 24
        res_bad2 = get_recent_ring_events(self.conn, now, {"device_id": "cam_front", "hours": 25})
        self.assertFalse(res_bad2.ok)
        self.assertEqual(res_bad2.error, "invalid_args")

        # Bad args: missing device_id
        res_bad3 = get_recent_ring_events(self.conn, now, {})
        self.assertFalse(res_bad3.ok)
        self.assertEqual(res_bad3.error, "invalid_args")

    def test_tool_pull_snapshot_success_and_media_unavailable_and_bad_args(self):
        """pull_snapshot handles success, media errors, and bad args."""
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # Success with injected client
        client_ok = FakeRingClient("success")
        res_ok = pull_snapshot(
            self.conn,
            now,
            {"device_id": "cam_front"},
            ring_client=client_ok,
            snapshots_dir=self.temp_snapshots.name,
        )
        self.assertTrue(res_ok.ok)
        self.assertIn("path", res_ok.data)
        self.assertTrue(os.path.exists(res_ok.data["path"]))
        # Assert no raw image bytes in data
        self.assertNotIn("image_bytes", res_ok.data)

        # Failure: 416
        client_416 = FakeRingClient("416")
        res_416 = pull_snapshot(
            self.conn,
            now,
            {"device_id": "cam_front"},
            ring_client=client_416,
            snapshots_dir=self.temp_snapshots.name,
        )
        self.assertFalse(res_416.ok)
        self.assertEqual(res_416.error, "media_unavailable")
        self.assertIn("416", res_416.text)

        # Bad args: extra parameters
        res_bad = pull_snapshot(
            self.conn,
            now,
            {"device_id": "cam_front", "invalid": 123},
            ring_client=client_ok,
        )
        self.assertFalse(res_bad.ok)
        self.assertEqual(res_bad.error, "invalid_args")

    def test_tool_check_routine_status_success_and_bad_args(self):
        """check_routine_status derives routine status cleanly."""
        now = datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)
        save_routine(
            self.conn,
            routine_id="rt_1",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="UTC",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:30:00Z", "webhook")

        # Success by routine_id
        res = check_routine_status(self.conn, now, {"routine_id": "rt_1"})
        self.assertTrue(res.ok)
        self.assertEqual(res.data[0]["status"], "watching")

        # Success by device_id
        res_dev = check_routine_status(self.conn, now, {"device_id": "cam_front"})
        self.assertTrue(res_dev.ok)
        self.assertEqual(res_dev.data[0]["routine_id"], "rt_1")

        # Bad args: no routine_id and no device_id
        res_bad = check_routine_status(self.conn, now, {})
        self.assertFalse(res_bad.ok)
        self.assertEqual(res_bad.error, "invalid_args")

    def test_tool_get_threshold_history_success_and_bad_args(self):
        """get_threshold_history queries past escalations with validation."""
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        save_routine(
            self.conn,
            routine_id="rt_1",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="UTC",
            days=["mon"],
        )
        record_escalation(self.conn, "rt_1", "2026-10-10", "2026-10-10T14:31:00Z")

        # Success
        res = get_threshold_history(self.conn, now, {"routine_id": "rt_1", "days": 30})
        self.assertTrue(res.ok)
        self.assertEqual(len(res.data), 1)

        # Bad args: days > 30
        res_bad = get_threshold_history(self.conn, now, {"routine_id": "rt_1", "days": 31})
        self.assertFalse(res_bad.ok)
        self.assertEqual(res_bad.error, "invalid_args")

    def test_tool_check_household_memory_success_and_bad_args(self):
        """check_household_memory counts false alarms accurately."""
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        record_correction(
            self.conn,
            id="corr_1",
            device_id="cam_front",
            event_type="motion_detected",
            time_bucket="2026-10-10",
            label="false_alarm",
            created_at="2026-10-10T15:00:00Z",
        )

        # Success
        res = check_household_memory(self.conn, now, {"device_id": "cam_front", "event_type": "motion_detected"})
        self.assertTrue(res.ok)
        self.assertEqual(res.data["false_alarm_count"], 1)

        # Bad args: missing event_type
        res_bad = check_household_memory(self.conn, now, {"device_id": "cam_front"})
        self.assertFalse(res_bad.ok)
        self.assertEqual(res_bad.error, "invalid_args")

    # --- 2. Provider Tests ---

    def test_mock_provider_deterministic_replay(self):
        """MockProvider replays trace steps deterministically."""
        p1 = MockProvider("trace_escalate_two_tools.json")
        p2 = MockProvider("trace_escalate_two_tools.json")

        self.assertEqual(p1.scenario, p2.scenario)
        self.assertEqual(p1.provenance, "scripted")
        self.assertEqual(p1.mode, "replay (scripted)")

        state: Dict[str, Any] = {"step_history": []}
        step1_p1 = p1.next(state, [], time.monotonic() + 5.0)
        step1_p2 = p2.next(state, [], time.monotonic() + 5.0)

        self.assertEqual(step1_p1.name, step1_p2.name)
        self.assertEqual(step1_p1.args, step1_p2.args)

    # --- 3. Full Investigation Traces ---

    def test_full_investigation_trace_1_two_tools_escalates(self):
        """Trace 1: Escalate tier with 2 successful tool results cleared by Gate."""
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        now = datetime(2026, 10, 12, 7, 10, tzinfo=timezone.utc)
        event = {
            "event_key": "evt_trace_1",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "sub_type": "human",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        provider = MockProvider("trace_escalate_two_tools.json")
        inv, dec = run_investigation_and_gate(
            conn=self.conn,
            event=event,
            provider=provider,
            tools={"ring_client": FakeRingClient("success"), "snapshots_dir": self.temp_snapshots.name},
            now=now,
        )

        self.assertEqual(inv.tier, "escalate")
        self.assertFalse(inv.fallback)
        self.assertEqual(len(inv.evidence), 2)
        self.assertEqual(dec.decision, "escalate")
        self.assertEqual(dec.reason_code, "cleared")

    def test_full_investigation_trace_2_one_tool_check_needed(self):
        """Trace 2: Escalate tier with only 1 successful tool result -> Gate check_needed."""
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        now = datetime(2026, 10, 12, 7, 10, tzinfo=timezone.utc)
        event = {
            "event_key": "evt_trace_2",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "sub_type": "human",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        provider = MockProvider("trace_escalate_one_tool.json")
        inv, dec = run_investigation_and_gate(
            conn=self.conn,
            event=event,
            provider=provider,
            now=now,
        )

        self.assertEqual(inv.tier, "escalate")
        self.assertEqual(len(inv.evidence), 1)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "insufficient_evidence")

    def test_full_investigation_trace_3_media_unavailable_check_needed(self):
        """Trace 3: Snapshot returns media unavailable and tier is check_needed."""
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        now = datetime(2026, 10, 12, 7, 10, tzinfo=timezone.utc)
        event = {
            "event_key": "evt_trace_3",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "sub_type": "human",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        provider = MockProvider("trace_media_unavailable.json")
        inv, dec = run_investigation_and_gate(
            conn=self.conn,
            event=event,
            provider=provider,
            tools={"ring_client": FakeRingClient("416"), "snapshots_dir": self.temp_snapshots.name},
            now=now,
        )

        self.assertEqual(inv.tier, "check_needed")
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "check_needed")

    # --- 4. Gate Table-Driven Tests ---

    def test_gate_table_driven_all_conditions(self):
        """Table-driven verification of Gate decisions and reason codes across all rules."""
        base_facts = GateFacts(
            device_online_fresh=True,
            routine_snoozed=False,
            minutes_since_last_notification=45.0,
            agent_escalations_last_6h=0,
        )

        # 1. Cleared case
        dec = decide(tier="escalate", ok_tool_results=2, facts=base_facts)
        self.assertEqual(dec.decision, "escalate")
        self.assertEqual(dec.reason_code, "cleared")

        # 2. Rule 1: Invalid tier
        dec = decide(tier="invalid_unknown", ok_tool_results=2, facts=base_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "invalid_tier")

        # 3. Rule 2: Routine tier
        dec = decide(tier="routine", ok_tool_results=0, facts=base_facts)
        self.assertEqual(dec.decision, "routine")
        self.assertEqual(dec.reason_code, "routine")

        # 4. Rule 3: Silent / stale device never escalates
        stale_facts = GateFacts(
            device_online_fresh=False,
            routine_snoozed=False,
            minutes_since_last_notification=None,
            agent_escalations_last_6h=0,
        )
        dec = decide(tier="escalate", ok_tool_results=5, facts=stale_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "device_not_fresh")

        # 5. Rule 4: Snoozed routine blocks escalation
        snoozed_facts = GateFacts(
            device_online_fresh=True,
            routine_snoozed=True,
            minutes_since_last_notification=None,
            agent_escalations_last_6h=0,
        )
        dec = decide(tier="escalate", ok_tool_results=5, facts=snoozed_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "snoozed")

        # 6. Rule 5: Explicit check_needed tier
        dec = decide(tier="check_needed", ok_tool_results=5, facts=base_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "check_needed")

        # 7. Rule 6a: Insufficient evidence
        dec = decide(tier="escalate", ok_tool_results=1, facts=base_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "insufficient_evidence")

        # 8. Rule 6b: Notification cooldown (< 30 min)
        cooldown_facts = GateFacts(
            device_online_fresh=True,
            routine_snoozed=False,
            minutes_since_last_notification=20.0,
            agent_escalations_last_6h=0,
        )
        dec = decide(tier="escalate", ok_tool_results=2, facts=cooldown_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "notification_cooldown")

        # 9. Rule 6c: Rate limit (>= 2 escalations in last 6h)
        rate_limited_facts = GateFacts(
            device_online_fresh=True,
            routine_snoozed=False,
            minutes_since_last_notification=60.0,
            agent_escalations_last_6h=2,
        )
        dec = decide(tier="escalate", ok_tool_results=2, facts=rate_limited_facts)
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "escalation_rate_limit")

    def test_gate_never_escalates_unless_tier_escalate(self):
        """Invariant: Gate never returns 'escalate' unless input tier is exactly 'escalate'."""
        perfect_facts = GateFacts(
            device_online_fresh=True,
            routine_snoozed=False,
            minutes_since_last_notification=100.0,
            agent_escalations_last_6h=0,
        )
        for t in ["routine", "check_needed", "random", "", "ALERT", None]:
            dec = decide(tier=t, ok_tool_results=10, facts=perfect_facts)
            self.assertNotEqual(dec.decision, "escalate")

    # --- 5. Robustness & Fallback Tests ---

    def test_forced_provider_timeout_fallback(self):
        """Forced provider timeout triggers fallback check_needed."""
        class TimeoutProvider:
            name = "timeout_provider"
            mode = "agent"
            def next(self, state, tool_specs, deadline):
                raise TimeoutError("Forced timeout")

        event = {"event_key": "evt_to", "device_id": "cam_front", "event_type": "motion", "sub_type": None, "occurred_at": "2026-10-12T01:00:00Z"}
        inv = investigate(self.conn, event, TimeoutProvider())
        self.assertTrue(inv.fallback)
        self.assertEqual(inv.tier, "check_needed")
        self.assertIn("could not determine", inv.recommendation)

    def test_step_limit_fallback(self):
        """Exceeding max_steps triggers fallback check_needed."""
        class InfiniteLoopProvider:
            name = "loop_provider"
            mode = "agent"
            def next(self, state, tool_specs, deadline):
                return ToolCall(name="check_household_memory", args={"device_id": "cam_front", "event_type": "motion"}, call_id=f"c_{len(state['step_history'])}")

        event = {"event_key": "evt_loop", "device_id": "cam_front", "event_type": "motion", "sub_type": None, "occurred_at": "2026-10-12T01:00:00Z"}
        inv = investigate(self.conn, event, InfiniteLoopProvider(), limits={"max_steps": 2})
        self.assertTrue(inv.fallback)
        self.assertEqual(inv.tier, "check_needed")
        self.assertEqual(inv.steps, 2)

    def test_unknown_tool_step_consumed_call_recorded_failed(self):
        """An unknown tool call consumes a step and records an error result."""
        trace = {
            "provenance": "scripted",
            "scenario": "Unknown tool test",
            "steps": [
                {"type": "call", "tool": "unregistered_magic_tool", "args": {}, "call_id": "call_bad"},
                {"type": "final", "tier": "check_needed", "recommendation": "Done", "cited_call_ids": []},
            ],
        }
        provider = MockProvider(trace)
        event = {"event_key": "evt_unk", "device_id": "cam_front", "event_type": "motion", "sub_type": None, "occurred_at": "2026-10-12T01:00:00Z"}
        inv = investigate(self.conn, event, provider)
        self.assertFalse(inv.fallback)
        self.assertEqual(inv.steps, 3)
        # Check that result for unknown tool was recorded with error
        bad_res = [s for s in inv.trace if s.get("type") == "result" and s.get("call_id") == "call_bad"]
        self.assertEqual(len(bad_res), 1)
        self.assertEqual(bad_res[0]["error"], "unknown_tool")

    def test_invalid_tier_from_provider_coerced_to_check_needed(self):
        """An unrecognized tier returned in Final is coerced to check_needed."""
        trace = {
            "provenance": "scripted",
            "scenario": "Invalid tier test",
            "steps": [
                {"type": "final", "tier": "high_danger", "recommendation": "Run", "cited_call_ids": []},
            ],
        }
        provider = MockProvider(trace)
        event = {"event_key": "evt_tier", "device_id": "cam_front", "event_type": "motion", "sub_type": None, "occurred_at": "2026-10-12T01:00:00Z"}
        inv = investigate(self.conn, event, provider)
        self.assertEqual(inv.tier, "check_needed")

    def test_final_citing_non_existent_call_id_keeps_no_phantom_evidence(self):
        """A Final citing a non-existent call_id drops it without creating phantom evidence."""
        trace = {
            "provenance": "scripted",
            "scenario": "Phantom call id test",
            "steps": [
                {"type": "call", "tool": "check_household_memory", "args": {"device_id": "cam_front", "event_type": "motion"}, "call_id": "call_real"},
                {"type": "final", "tier": "check_needed", "recommendation": "Check", "cited_call_ids": ["call_ghost_123"]},
            ],
        }
        provider = MockProvider(trace)
        event = {"event_key": "evt_phantom", "device_id": "cam_front", "event_type": "motion", "sub_type": None, "occurred_at": "2026-10-12T01:00:00Z"}
        inv = investigate(self.conn, event, provider)
        self.assertEqual(len(inv.evidence), 0)

    # --- 6. Store Operations ---

    def test_investigations_table_round_trip(self):
        """investigations table saves, retrieves, and updates cleanly."""
        save_investigation(
            conn=self.conn,
            event_key="evt_db_1",
            device_id="cam_front",
            created_at="2026-10-12T05:00:00Z",
            mode="replay (scripted)",
            provider_name="mock_provider",
            tier="escalate",
            decision="escalate",
            reason_code="cleared",
            recommendation="Test recommendation",
            evidence_json=[{"tool": "check_routine_status", "ok": True}],
            trace_json=[{"step": 1}],
            notified_at=None,
        )

        inv = get_investigation(self.conn, "evt_db_1")
        self.assertIsNotNone(inv)
        self.assertEqual(inv["device_id"], "cam_front")
        self.assertEqual(inv["decision"], "escalate")
        self.assertEqual(inv["reason_code"], "cleared")

        inv_list = list_investigations(self.conn, device_id="cam_front")
        self.assertEqual(len(inv_list), 1)

    # --- 7. A8a Tests (Distinct Tools, Traversal, Trace 4, Triggers, Runner, Daemon) ---

    def test_distinct_tools_counted_for_ok_tool_results(self):
        """Calling the same tool twice only counts as 1 distinct tool result."""
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # Trace where same tool check_routine_status is called twice with different args
        trace = {
            "provenance": "scripted",
            "scenario": "Duplicate tool call test",
            "steps": [
                {"type": "call", "tool": "check_routine_status", "args": {"routine_id": "rt_morning"}, "call_id": "call_1"},
                {"type": "call", "tool": "check_routine_status", "args": {"device_id": "cam_front"}, "call_id": "call_2"},
                {"type": "final", "tier": "escalate", "recommendation": "Escalate please", "cited_call_ids": ["call_1", "call_2"]},
            ],
        }
        provider = MockProvider(trace)
        now = datetime(2026, 10, 12, 7, 10, tzinfo=timezone.utc)
        event = {
            "event_key": "evt_dup_tool",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        inv, dec = run_investigation_and_gate(
            conn=self.conn,
            event=event,
            provider=provider,
            now=now,
        )
        self.assertEqual(inv.tier, "escalate")
        self.assertEqual(len(inv.evidence), 2)
        # Even though 2 evidence items, they come from the SAME tool ("check_routine_status")
        # Gate must reject as insufficient_evidence
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "insufficient_evidence")

    def test_path_traversal_and_device_validation_in_tools(self):
        """Tools reject path traversal attacks and unknown device IDs."""
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        client = FakeRingClient("success")

        # Path traversal in pull_snapshot device_id
        res_traversal = pull_snapshot(
            self.conn,
            now,
            {"device_id": "../../etc/passwd"},
            ring_client=client,
            snapshots_dir=self.temp_snapshots.name,
        )
        self.assertFalse(res_traversal.ok)
        self.assertEqual(res_traversal.error, "invalid_args")

        # Unknown device not in DB
        res_unknown = pull_snapshot(
            self.conn,
            now,
            {"device_id": "unknown_sensor_123"},
            ring_client=client,
            snapshots_dir=self.temp_snapshots.name,
        )
        self.assertFalse(res_unknown.ok)
        self.assertEqual(res_unknown.error, "invalid_args")

        # Path traversal in get_recent_ring_events
        res_events = get_recent_ring_events(
            self.conn,
            now,
            {"device_id": "../sensitive_file"},
        )
        self.assertFalse(res_events.ok)
        self.assertEqual(res_events.error, "invalid_args")

        # Verify nothing was written outside temp_snapshots
        self.assertEqual(len(os.listdir(self.temp_snapshots.name)), 0)

    def test_full_investigation_trace_4_household_memory_false_alarms(self):
        """Trace 4: 3 past false alarms in household memory result in check_needed."""
        save_routine(
            self.conn,
            routine_id="rt_morning",
            property_id="prop_1",
            name="Morning Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # Record 3 false alarms in corrections table
        for i in range(3):
            record_correction(
                self.conn,
                id=f"corr_{i+1}",
                device_id="cam_front",
                event_type="motion_detected",
                time_bucket="2026-10-10",
                label="false_alarm",
                created_at=f"2026-10-10T1{i}:00:00Z",
            )

        now = datetime(2026, 10, 12, 7, 10, tzinfo=timezone.utc)
        event = {
            "event_key": "evt_trace_4",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "sub_type": "human",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        provider = MockProvider("trace_household_memory_false_alarms.json")
        inv, dec = run_investigation_and_gate(
            conn=self.conn,
            event=event,
            provider=provider,
            now=now,
        )
        self.assertEqual(inv.tier, "check_needed")
        self.assertEqual(dec.decision, "check_needed")
        self.assertEqual(dec.reason_code, "check_needed")
        self.assertIn("repeated past false alarms", inv.recommendation)

    def test_triggers_unexpected_activity(self):
        """is_ambiguous_event correctly identifies quiet hours unexpected activity and dedupes."""
        save_routine(
            self.conn,
            routine_id="rt_day",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )

        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        # 1. Inside routine window (07:30 CDT = 12:30 UTC):
        event_in_window = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T12:30:00Z",
        }
        self.assertIsNone(is_ambiguous_event(self.conn, event_in_window, now))

        # 2. Daytime outside routine window (14:00 CDT = 19:00 UTC):
        event_daytime = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T19:00:00Z",
        }
        self.assertIsNone(is_ambiguous_event(self.conn, event_daytime, now))

        # 3. Device with no routines:
        event_unknown = {
            "event_type": "motion_detected",
            "device_id": "sensor_unknown",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        self.assertIsNone(is_ambiguous_event(self.conn, event_unknown, now))

        # 4. Quiet hours unexpected activity (02:10 CDT = 07:10 UTC):
        event_quiet = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        reason = is_ambiguous_event(self.conn, event_quiet, now)
        self.assertEqual(reason, "quiet_hours_unexpected_activity")

        # 5. Deduplication: save an investigation row for cam_front within 30 min
        save_investigation(
            self.conn,
            event_key="evt_prev",
            device_id="cam_front",
            created_at=now,
            mode="agent",
            provider_name="mock",
            tier="check_needed",
            decision="check_needed",
            reason_code="quiet_hours_unexpected_activity",
        )
        self.assertIsNone(is_ambiguous_event(self.conn, event_quiet, now))

    def test_runner_process_new_events_and_cursor(self):
        """process_new_events advances cursor, investigates ambiguous events, and notifies."""
        save_routine(
            self.conn,
            routine_id="rt_day",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # Insert 3 events
        # Event 1: normal daytime routine window (07:30 CDT = 12:30 UTC) -> not ambiguous
        save_event(self.conn, "webhook", "evt_1", "cam_front", "motion_detected", "2026-10-12T12:30:00Z", "2026-10-12T12:30:00Z", {})
        # Event 2: quiet hours unexpected activity (02:10 CDT = 07:10 UTC) -> ambiguous
        save_event(self.conn, "webhook", "evt_2", "cam_front", "motion_detected", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        # Event 3: daytime outside window (14:00 CDT = 19:00 UTC) -> not ambiguous
        save_event(self.conn, "webhook", "evt_3", "cam_front", "motion_detected", "2026-10-12T19:00:00Z", "2026-10-12T19:00:00Z", {})

        agent_cfg = AgentConfig(
            enabled=True,
            provider="mock",
            trace_file="trace_escalate_two_tools.json",
            notify_enabled=True,
        )
        notifier = RecordingNotifier()
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        fake_client = FakeRingClient("success")
        processed = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
            notifier=notifier,
            ring_client=fake_client,
        )

        self.assertEqual(len(processed), 3)
        self.assertFalse(processed[0]["ambiguous"])
        self.assertTrue(processed[1]["ambiguous"])
        self.assertEqual(processed[1]["decision"], "escalate")
        self.assertTrue(processed[1]["notified"])
        self.assertFalse(processed[2]["ambiguous"])

        # Check that cursor advanced to 3 in app_state
        cursor_val = get_app_state(self.conn, "agent_event_cursor")
        self.assertEqual(cursor_val, "3")

        # Check investigation was saved
        inv = get_investigation(self.conn, "evt_2")
        self.assertIsNotNone(inv)
        self.assertEqual(inv["decision"], "escalate")
        self.assertIsNotNone(inv["notified_at"])

        # Check notifier received message
        self.assertEqual(len(notifier.sent), 1)
        sent_msg = notifier.sent[0]
        self.assertIn("CHECK NEEDED", sent_msg["text"])

    def test_runner_error_isolation_and_fallback(self):
        """Runner catches per-event exceptions, saves fallback record, and advances cursor."""
        save_routine(
            self.conn,
            routine_id="rt_day",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_err", "cam_front", "motion_detected", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})

        agent_cfg = AgentConfig(enabled=True)
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        # Provider factory that raises an unhandled exception
        def bad_factory():
            raise RuntimeError("Catastrophic provider explosion")

        processed = process_new_events(
            conn=self.conn,
            provider_factory=bad_factory,
            now=now,
            cfg=agent_cfg,
        )
        self.assertEqual(len(processed), 1)
        self.assertIn("error", processed[0])

        # Verify cursor still advanced so queue does not stall
        cursor_val = get_app_state(self.conn, "agent_event_cursor")
        self.assertEqual(cursor_val, "1")

        # Verify fallback investigation saved
        inv = get_investigation(self.conn, "evt_err")
        self.assertIsNotNone(inv)
        self.assertEqual(inv["mode"], "fallback")
        self.assertEqual(inv["tier"], "check_needed")
        self.assertEqual(inv["reason_code"], "error")

    def test_daemon_tick_with_agent_enabled_and_disabled(self):
        """ThresholdDaemon executes agent processing on ticks when enabled, ignores when disabled."""
        save_routine(
            self.conn,
            routine_id="rt_day",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # Add an event
        save_event(self.conn, "webhook", "evt_d1", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})

        # Disabled case
        cfg_disabled = DaemonConfig(agent=AgentConfig(enabled=False))
        daemon_dis = ThresholdDaemon(config=cfg_disabled, conn=self.conn, clock=FakeClock(datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)))
        res_dis = daemon_dis.tick_once()
        self.assertEqual(len(res_dis.get("agent_processed", [])), 0)

        # Enabled case
        cfg_enabled = DaemonConfig(agent=AgentConfig(enabled=True, trace_file="trace_escalate_two_tools.json"))
        daemon_en = ThresholdDaemon(config=cfg_enabled, conn=self.conn, clock=FakeClock(datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)))
        res_en = daemon_en.tick_once()
        self.assertEqual(len(res_en.get("agent_processed", [])), 1)
        self.assertTrue(res_en["agent_processed"][0]["ambiguous"])

    # --- 8. Import Guard (AST-Based) ---

    def test_ast_import_guard(self):
        """
        Verify status.py, triage.py, scheduler.py, notifier.py, and ingest.py
        do NOT import threshold.agent.
        """
        base_dir = os.path.dirname(os.path.dirname(__file__))
        core_files = [
            os.path.join(base_dir, "src", "threshold", "status.py"),
            os.path.join(base_dir, "src", "threshold", "triage.py"),
            os.path.join(base_dir, "src", "threshold", "scheduler.py"),
            os.path.join(base_dir, "src", "threshold", "notifier.py"),
            os.path.join(base_dir, "src", "threshold", "ingest.py"),
        ]

        for file_path in core_files:
            self.assertTrue(os.path.exists(file_path), f"File missing: {file_path}")
            with open(file_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=file_path)

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(
                            "agent",
                            alias.name,
                            f"{os.path.basename(file_path)} violates import guard by importing {alias.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    module_name = node.module or ""
                    self.assertNotIn(
                        "agent",
                        module_name,
                        f"{os.path.basename(file_path)} violates import guard by importing from {module_name}",
                    )


if __name__ == "__main__":
    unittest.main()

"""
Comprehensive unit tests for Bounded-Authority Agent Library (src/threshold/agent).
Standard library only; offline, deterministic.
"""

import ast
from datetime import datetime, timedelta, timezone
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
from src.threshold.agent.evidence_format import (
    extract_snapshot_filename,
    format_evidence_text_for_caregiver,
    format_evidence_text_for_dashboard,
)
from src.threshold.agent.runner import (
    deliver_agent_notification,
    format_agent_notification_text,
    process_new_events,
)
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
    check_household_memory,
    check_routine_status,
    execute_tool,
    get_recent_ring_events,
    get_threshold_history,
    pull_snapshot,
)
from src.threshold.agent.triggers import is_ambiguous_event
from src.threshold.config import AgentConfig
from src.threshold.daemon import DaemonConfig, ThresholdDaemon
from src.threshold.notifier import RecordingNotifier, format_snapshot_caption_prefix
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
    to_iso_timestamp,
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

        # Evidence is derived strictly from real check_household_memory tool result (not trace text)
        self.assertEqual(len(inv.evidence), 1)
        self.assertEqual(inv.evidence[0].tool, "check_household_memory")
        self.assertTrue(inv.evidence[0].ok)
        self.assertIn("3 false alarm(s) out of 3 correction(s)", inv.evidence[0].text)
        self.assertIn("cam_front", inv.evidence[0].text)

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

        set_app_state(self.conn, "agent_event_cursor", "0")
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
        set_app_state(self.conn, "agent_event_cursor", "0")
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
        set_app_state(self.conn, "agent_event_cursor", "0")
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

    def test_first_run_guard_initializes_cursor_without_backfill(self):
        """With agent_event_cursor unset and 500 old events, initializes cursor to max rowid without backfill."""
        # Ensure cursor is unset
        self.assertIsNone(get_app_state(self.conn, "agent_event_cursor"))

        # Insert 500 events
        for i in range(1, 501):
            save_event(
                self.conn,
                "webhook",
                f"evt_bulk_{i}",
                "cam_front",
                "motion_detected",
                "2026-10-12T02:10:00Z",
                "2026-10-12T02:10:00Z",
                {},
            )

        agent_cfg = AgentConfig(enabled=True)
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        notifier = RecordingNotifier()

        # First run: should initialize cursor to 500 and process 0 events
        processed = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
            notifier=notifier,
        )
        self.assertEqual(len(processed), 0)
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "500")
        self.assertEqual(len(notifier.sent), 0)
        self.assertEqual(len(list_investigations(self.conn)), 0)

        # Subsequent event arrival: 501st event should now be processed
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
        save_event(
            self.conn,
            "webhook",
            "evt_bulk_501",
            "cam_front",
            "motion_detected",
            "2026-10-12T07:10:00Z",
            "2026-10-12T07:10:00Z",
            {},
        )
        processed2 = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
            notifier=notifier,
        )
        self.assertEqual(len(processed2), 1)
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "501")

    def test_stale_event_guard_skips_investigation_and_advances_cursor(self):
        """Events older than max_event_age_minutes (120m) are skipped and advance cursor."""
        set_app_state(self.conn, "agent_event_cursor", "0")
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

        # Event occurred at 05:00 UTC (180 min before now 08:00 UTC > 120 min max_age)
        save_event(
            self.conn,
            "webhook",
            "evt_stale_1",
            "cam_front",
            "motion_detected",
            "2026-10-12T05:00:00Z",
            "2026-10-12T05:00:00Z",
            {},
        )

        agent_cfg = AgentConfig(enabled=True, max_event_age_minutes=120)
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        notifier = RecordingNotifier()

        processed = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
            notifier=notifier,
        )
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["skipped_reason"], "stale_event")
        self.assertFalse(processed[0]["ambiguous"])
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "1")
        self.assertIsNone(get_investigation(self.conn, "evt_stale_1"))
        self.assertEqual(len(notifier.sent), 0)

    def test_scripted_recommendation_placeholder_with_0340(self):
        """Event at 03:40 local produces text containing 03:40 and never 02:10."""
        # 08:40 UTC in America/Chicago (CDT, UTC-5) is 03:40 CDT
        event = {
            "event_key": "evt_test_0340",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-12T08:40:00Z",
            "timezone": "America/Chicago",
        }
        provider = MockProvider("trace_escalate_two_tools.json")
        inv = investigate(self.conn, provider=provider, event=event)
        self.assertIn("03:40", inv.recommendation)
        self.assertNotIn("02:10", inv.recommendation)

    def test_mock_provider_recommendation_always_prefixed_replay_scripted(self):
        """Mock provider recommendations are always prefixed with (replay, scripted)."""
        event = {
            "event_key": "evt_test_prefix",
            "device_id": "cam_front",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-12T07:10:00Z",
            "timezone": "America/Chicago",
        }
        provider = MockProvider("trace_escalate_two_tools.json")
        inv = investigate(self.conn, provider=provider, event=event)
        self.assertTrue(inv.recommendation.startswith("(replay, scripted)"))

        # Notification text also maintains (replay, scripted) without duplication
        notif_text = format_agent_notification_text(event, inv)
        self.assertIn("(replay, scripted)", notif_text)
        self.assertNotIn("(replay, scripted) (replay, scripted)", notif_text)

    def test_notify_off_does_not_send_notification(self):
        """When notify_enabled=False, no notification is sent even on escalate."""
        set_app_state(self.conn, "agent_event_cursor", "0")
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
        save_event(self.conn, "webhook", "evt_off", "cam_front", "motion_detected", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})

        agent_cfg = AgentConfig(enabled=True, notify_enabled=False)
        notifier = RecordingNotifier()
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        processed = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
            notifier=notifier,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["decision"], "escalate")
        self.assertFalse(processed[0]["notified"])
        self.assertEqual(len(notifier.sent), 0)

    def test_notify_check_needed_does_not_send_notification(self):
        """When gate decision is check_needed, no notification is sent even with notify_enabled=True."""
        set_app_state(self.conn, "agent_event_cursor", "0")
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
        save_event(self.conn, "webhook", "evt_cn", "cam_front", "motion_detected", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})

        agent_cfg = AgentConfig(enabled=True, notify_enabled=True)
        notifier = RecordingNotifier()
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        # trace_escalate_one_tool produces check_needed decision
        processed = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_one_tool.json"),
            now=now,
            cfg=agent_cfg,
            notifier=notifier,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0]["decision"], "check_needed")
        self.assertFalse(processed[0]["notified"])
        self.assertEqual(len(notifier.sent), 0)

    def test_stalled_provider_does_not_block_absence_tick(self):
        """A stalled or failing agent provider does not block the absence scheduler tick."""
        set_app_state(self.conn, "agent_event_cursor", "0")
        # Missed routine: window 07:00-09:00 + 30m grace = 09:30 deadline. Current time is 09:35.
        save_routine(
            self.conn,
            routine_id="rt_missed",
            property_id="prop_1",
            name="Morning Meds",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            escalation_delay_minutes=30,
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T14:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_stall", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})

        class ExplodingProvider:
            name = "exploding"
            mode = "agent"
            def next_step(self, *args, **kwargs):
                raise RuntimeError("Stalled provider failure")

        cfg = DaemonConfig(
            agent=AgentConfig(enabled=True, max_seconds_per_tick=1.0),
        )
        now_dt = datetime(2026, 10, 12, 14, 35, tzinfo=timezone.utc)  # 09:35 CDT
        notifier = RecordingNotifier()
        daemon = ThresholdDaemon(
            config=cfg,
            conn=self.conn,
            clock=FakeClock(now_dt),
            notifier=notifier,
            provider_factory=lambda: ExplodingProvider(),
        )

        res = daemon.tick_once()
        # Absence routine was still evaluated and escalated!
        self.assertEqual(res.get("new_escalations", 0), 1)
        self.assertEqual(res["statuses"].get("rt_missed"), "missed")

    def test_cursor_advance_on_non_ambiguous_events(self):
        """Cursor advances across non-ambiguous events without calling provider."""
        set_app_state(self.conn, "agent_event_cursor", "0")
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
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # 3 daytime events inside routine window (12:30 UTC = 07:30 CDT)
        for i in range(1, 4):
            save_event(self.conn, "webhook", f"evt_norm_{i}", "cam_front", "motion", "2026-10-12T12:30:00Z", "2026-10-12T12:30:00Z", {})

        agent_cfg = AgentConfig(enabled=True)
        now = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)
        processed = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
        )
        self.assertEqual(len(processed), 3)
        self.assertFalse(any(p["ambiguous"] for p in processed))
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "3")

    # --- 7. A8b Gap Closure Tests (Deterministic Trigger, Dedupe, Notifications, Cursor, Isolation) ---

    def test_is_ambiguous_false_for_non_motion_event(self):
        """is_ambiguous_event is False for non-motion events (e.g. door_opened, battery_low)."""
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
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        event_door = {
            "event_type": "door_opened",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T07:10:00Z",  # 02:10 CDT (quiet hours)
        }
        self.assertIsNone(is_ambiguous_event(self.conn, event_door, now))

    def test_is_ambiguous_false_for_device_in_no_routine(self):
        """is_ambiguous_event is False for motion on a device not part of any routine."""
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        event_untracked = {
            "event_type": "motion_detected",
            "device_id": "sensor_untracked_yard",
            "occurred_at": "2026-10-12T07:10:00Z",  # 02:10 CDT (quiet hours)
        }
        self.assertIsNone(is_ambiguous_event(self.conn, event_untracked, now))

    def test_is_ambiguous_false_for_motion_satisfying_routine_in_window_grace(self):
        """is_ambiguous_event is False for motion that satisfies a routine within window + grace."""
        save_routine(
            self.conn,
            routine_id="rt_med",
            property_id="prop_1",
            name="Morning Med",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        now = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        # Event at 07:30 CDT (12:30 UTC) satisfies the routine
        event_in_window = {
            "event_type": "motion_detected",
            "device_id": "sensor_cabinet",
            "occurred_at": "2026-10-12T12:30:00Z",
        }
        with self.subTest(case="motion_in_routine_window"):
            self.assertIsNone(is_ambiguous_event(self.conn, event_in_window, now))

        # Event at 09:15 CDT (14:15 UTC, inside 30m grace buffer) satisfies routine
        event_in_grace = {
            "event_type": "motion_detected",
            "device_id": "sensor_cabinet",
            "occurred_at": "2026-10-12T14:15:00Z",
        }
        with self.subTest(case="motion_in_grace_buffer"):
            self.assertIsNone(is_ambiguous_event(self.conn, event_in_grace, now))

    def test_is_ambiguous_negative_cases(self):
        """Composite test verifying all four negative cases using subTest."""
        save_routine(
            self.conn,
            routine_id="rt_composite_door",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
            escalation_delay_minutes=30,
        )
        now_day = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)

        # Case 1: non-motion event
        with self.subTest(case="non_motion_event"):
            evt = {"event_type": "door_opened", "device_id": "cam_front", "occurred_at": "2026-10-12T07:10:00Z"}
            self.assertIsNone(is_ambiguous_event(self.conn, evt, now_day))

        # Case 2: device not in any routine
        with self.subTest(case="untracked_device_no_routine"):
            evt = {"event_type": "motion_detected", "device_id": "untracked_yard_cam", "occurred_at": "2026-10-12T07:10:00Z"}
            self.assertIsNone(is_ambiguous_event(self.conn, evt, now_day))

        # Case 3: motion satisfies routine window / grace
        with self.subTest(case="satisfies_routine_window"):
            evt = {"event_type": "motion_detected", "device_id": "cam_front", "occurred_at": "2026-10-12T12:30:00Z"}
            self.assertIsNone(is_ambiguous_event(self.conn, evt, now_day))

        # Case 4: motion outside quiet hours
        with self.subTest(case="daytime_outside_quiet_hours"):
            evt = {"event_type": "motion_detected", "device_id": "cam_front", "occurred_at": "2026-10-12T19:00:00Z"}
            self.assertIsNone(is_ambiguous_event(self.conn, evt, now_day))

    def test_is_ambiguous_false_for_motion_outside_quiet_hours(self):
        """is_ambiguous_event is False for motion outside quiet hours (daytime)."""
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
        now = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
        # 14:00 CDT = 19:00 UTC (daytime, not in 23:00-05:00 CDT quiet hours)
        event_daytime = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T19:00:00Z",
        }
        self.assertIsNone(is_ambiguous_event(self.conn, event_daytime, now))

    def test_is_ambiguous_true_for_quiet_hours_unexpected_motion(self):
        """is_ambiguous_event is True for unexpected motion during quiet hours."""
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
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        # 02:10 CDT = 07:10 UTC (inside quiet hours 23:00-05:00)
        event_quiet = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T07:10:00Z",
        }
        reason = is_ambiguous_event(self.conn, event_quiet, now)
        self.assertEqual(reason, "quiet_hours_unexpected_activity")

    def test_device_level_dedupe_within_30m_after_30m_and_different_device(self):
        """
        Dedupe: 2nd event on SAME device <30m later is NOT investigated;
        event >30m later IS investigated; different device is NOT deduped.
        """
        save_routine(
            self.conn,
            routine_id="rt_cam1",
            property_id="prop_1",
            name="Front Cam",
            device_id="cam_front",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        save_routine(
            self.conn,
            routine_id="rt_cam2",
            property_id="prop_1",
            name="Back Cam",
            device_id="cam_back",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )

        # Save an initial investigation for cam_front at 02:10 CDT (07:10 UTC)
        save_investigation(
            self.conn,
            event_key="evt_init",
            device_id="cam_front",
            created_at=datetime(2026, 10, 12, 7, 10, tzinfo=timezone.utc),
            mode="agent",
            provider_name="mock",
            tier="check_needed",
            decision="check_needed",
            reason_code="quiet_hours_unexpected_activity",
        )

        # 1. Same device, 15m later (02:25 CDT = 07:25 UTC) -> < 30m -> DEDUPED (None)
        event_same_15m = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T07:25:00Z",
        }
        eval_time_15m = datetime(2026, 10, 12, 7, 25, tzinfo=timezone.utc)
        self.assertIsNone(is_ambiguous_event(self.conn, event_same_15m, eval_time_15m))

        # 2. Same device, 35m later (02:45 CDT = 07:45 UTC) -> > 30m -> INVESTIGATED (Triggered)
        event_same_35m = {
            "event_type": "motion_detected",
            "device_id": "cam_front",
            "occurred_at": "2026-10-12T07:45:00Z",
        }
        eval_time_35m = datetime(2026, 10, 12, 7, 45, tzinfo=timezone.utc)
        self.assertEqual(
            is_ambiguous_event(self.conn, event_same_35m, eval_time_35m),
            "quiet_hours_unexpected_activity",
        )

        # 3. Different device (cam_back) at 02:25 CDT -> NOT DEDUPED (Triggered)
        event_diff_dev = {
            "event_type": "motion_detected",
            "device_id": "cam_back",
            "occurred_at": "2026-10-12T07:25:00Z",
        }
        self.assertEqual(
            is_ambiguous_event(self.conn, event_diff_dev, eval_time_15m),
            "quiet_hours_unexpected_activity",
        )

    def test_agent_notifications_exhaustive_modes_and_tiers(self):
        """
        Verify notification matrix:
        - notify_enabled=True + escalate -> 1 CHECK NEEDED message and notified_at set
        - notify_enabled=False + escalate -> 0 messages, notified_at is None
        - notify_enabled=True + check_needed -> 0 messages, notified_at is None
        - notify_enabled=True + routine -> 0 messages, notified_at is None
        """
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
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        # Case 1: notify_enabled=True + escalate
        save_event(self.conn, "webhook", "evt_esc_notif", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        set_app_state(self.conn, "agent_event_cursor", "0")
        notifier_1 = RecordingNotifier()
        cfg_1 = AgentConfig(enabled=True, notify_enabled=True, trace_file="trace_escalate_two_tools.json")
        res_1 = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=cfg_1,
            notifier=notifier_1,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(res_1), 1)
        self.assertEqual(res_1[0]["decision"], "escalate")
        self.assertTrue(res_1[0]["notified"])
        self.assertEqual(len(notifier_1.sent), 1)
        self.assertIn("CHECK NEEDED", notifier_1.sent[0]["text"])
        inv_1 = get_investigation(self.conn, "evt_esc_notif")
        self.assertIsNotNone(inv_1["notified_at"])

        # Case 2: notify_enabled=False + escalate
        save_event(self.conn, "webhook", "evt_esc_nonotif", "cam_front", "motion", "2026-10-12T07:45:00Z", "2026-10-12T07:45:00Z", {})
        notifier_2 = RecordingNotifier()
        cfg_2 = AgentConfig(enabled=True, notify_enabled=False, trace_file="trace_escalate_two_tools.json")
        res_2 = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now + timedelta(minutes=45),
            cfg=cfg_2,
            notifier=notifier_2,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(res_2), 1)
        self.assertEqual(res_2[0]["decision"], "escalate")
        self.assertFalse(res_2[0]["notified"])
        self.assertEqual(len(notifier_2.sent), 0)
        inv_2 = get_investigation(self.conn, "evt_esc_nonotif")
        self.assertIsNone(inv_2["notified_at"])

        # Case 3: notify_enabled=True + check_needed (1 tool only -> downgrade)
        save_event(self.conn, "webhook", "evt_cn_notif", "cam_front", "motion", "2026-10-12T08:30:00Z", "2026-10-12T08:30:00Z", {})
        notifier_3 = RecordingNotifier()
        cfg_3 = AgentConfig(enabled=True, notify_enabled=True, trace_file="trace_escalate_one_tool.json")
        res_3 = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_one_tool.json"),
            now=now + timedelta(hours=2),
            cfg=cfg_3,
            notifier=notifier_3,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(res_3), 1)
        self.assertEqual(res_3[0]["decision"], "check_needed")
        self.assertFalse(res_3[0]["notified"])
        self.assertEqual(len(notifier_3.sent), 0)
        inv_3 = get_investigation(self.conn, "evt_cn_notif")
        self.assertIsNone(inv_3["notified_at"])

        # Case 4: notify_enabled=True + routine tier
        save_event(self.conn, "webhook", "evt_rt_notif", "cam_front", "motion", "2026-10-12T09:15:00Z", "2026-10-12T09:15:00Z", {})
        notifier_4 = RecordingNotifier()
        cfg_4 = AgentConfig(enabled=True, notify_enabled=True)
        class RoutineProvider:
            name = "mock_routine"
            mode = "agent"
            def next(self, state, tool_specs, deadline=None):
                return Final(tier="routine", recommendation="Routine activity confirmed", cited_call_ids=[])
        res_4 = process_new_events(
            conn=self.conn,
            provider_factory=RoutineProvider,
            now=now + timedelta(hours=3),
            cfg=cfg_4,
            notifier=notifier_4,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(res_4), 1)
        self.assertEqual(res_4[0]["decision"], "routine")
        self.assertFalse(res_4[0]["notified"])
        self.assertEqual(len(notifier_4.sent), 0)
        inv_4 = get_investigation(self.conn, "evt_rt_notif")
        self.assertIsNone(inv_4["notified_at"])

    def test_cursor_advances_and_second_tick_reprocesses_nothing(self):
        """Cursor advances on tick 1; second tick re-processes nothing."""
        set_app_state(self.conn, "agent_event_cursor", "0")
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
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        # 3 events
        save_event(self.conn, "webhook", "evt_c1", "cam_front", "motion", "2026-10-12T12:30:00Z", "2026-10-12T12:30:00Z", {})
        save_event(self.conn, "webhook", "evt_c2", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        save_event(self.conn, "webhook", "evt_c3", "cam_front", "motion", "2026-10-12T19:00:00Z", "2026-10-12T19:00:00Z", {})

        agent_cfg = AgentConfig(enabled=True, trace_file="trace_escalate_two_tools.json")
        now = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)

        # Tick 1: processes all 3 events
        res_tick1 = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=agent_cfg,
            ring_client=FakeRingClient("success"),
        )
        self.assertEqual(len(res_tick1), 3)
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "3")

        # Tick 2: no new events in DB -> re-processes nothing
        call_count = 0
        def counting_factory():
            nonlocal call_count
            call_count += 1
            return MockProvider("trace_escalate_two_tools.json")

        res_tick2 = process_new_events(
            conn=self.conn,
            provider_factory=counting_factory,
            now=now + timedelta(minutes=5),
            cfg=agent_cfg,
        )
        self.assertEqual(len(res_tick2), 0)
        self.assertEqual(call_count, 0)
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "3")

    def test_runner_per_event_exception_isolation_multi_event(self):
        """An unhandled exception on event N does not stop event N+1 from being processed."""
        set_app_state(self.conn, "agent_event_cursor", "0")
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
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")

        save_routine(
            self.conn,
            routine_id="rt_back",
            property_id="prop_1",
            name="Back Door",
            device_id="cam_back",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_back", 1, "2026-10-12T07:00:00Z", "webhook")

        save_event(self.conn, "webhook", "evt_fail_1", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        save_event(self.conn, "webhook", "evt_pass_2", "cam_back", "motion", "2026-10-12T07:15:00Z", "2026-10-12T07:15:00Z", {})

        agent_cfg = AgentConfig(enabled=True, trace_file="trace_escalate_two_tools.json")
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

        invocations = 0
        def alternating_factory():
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                raise RuntimeError("Explosion on event 1")
            return MockProvider("trace_escalate_two_tools.json")

        processed = process_new_events(
            conn=self.conn,
            provider_factory=alternating_factory,
            now=now,
            cfg=agent_cfg,
            ring_client=FakeRingClient("success"),
        )

        self.assertEqual(len(processed), 2)
        # Event 1 failed cleanly with error isolation
        self.assertIn("error", processed[0])
        inv_1 = get_investigation(self.conn, "evt_fail_1")
        self.assertIsNotNone(inv_1)
        self.assertEqual(inv_1["mode"], "fallback")
        self.assertEqual(inv_1["tier"], "check_needed")

        # Event 2 succeeded despite event 1's exception
        self.assertTrue(processed[1]["ambiguous"])
        self.assertEqual(processed[1]["decision"], "escalate")
        inv_2 = get_investigation(self.conn, "evt_pass_2")
        self.assertIsNotNone(inv_2)
        self.assertEqual(inv_2["decision"], "escalate")

        # Cursor advanced past both events to 2
        self.assertEqual(get_app_state(self.conn, "agent_event_cursor"), "2")

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

    def test_sample_investigation_sync(self):
        """
        Verify docs/sample_investigation.json stays strictly synchronized
        with trace_escalate_two_tools.json.
        """
        base_dir = os.path.dirname(os.path.dirname(__file__))
        sample_path = os.path.join(base_dir, "docs", "sample_investigation.json")
        trace_path = os.path.join(base_dir, "src", "threshold", "agent", "traces", "trace_escalate_two_tools.json")

        self.assertTrue(os.path.exists(sample_path), f"Missing {sample_path}")
        self.assertTrue(os.path.exists(trace_path), f"Missing {trace_path}")

        with open(sample_path, "r", encoding="utf-8") as f:
            sample_data = json.load(f)
        with open(trace_path, "r", encoding="utf-8") as f:
            trace_data = json.load(f)

        # Find final step recommendation template in trace
        trace_final = next(s for s in trace_data["steps"] if s["type"] == "final")
        sample_final = next(s for s in sample_data["steps"] if s["type"] == "final")

        self.assertEqual(
            sample_final["recommendation"],
            trace_final["recommendation"],
            "docs/sample_investigation.json final step recommendation has drifted from trace_escalate_two_tools.json",
        )
        self.assertEqual(sample_final["tier"], trace_final["tier"])
        self.assertIn(
            "Snapshot requested for caregiver review.",
            sample_data["investigation"]["recommendation"],
            "docs/sample_investigation.json investigation recommendation has drifted",
        )

    # --- 9. Photo Attachment Plumbing Tests (Task A9d) ---

    def test_runner_photo_attachment_success(self):
        """
        (a) escalate + notify_enabled + snapshot ok -> send_photo called exactly once,
        bytes length > 0, caption starts with format_snapshot_caption_prefix text.
        State and verify: NO text message is sent when photo delivery succeeds.
        """
        save_routine(
            self.conn,
            routine_id="rt_day_door_p1",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_photo_ok", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        set_app_state(self.conn, "agent_event_cursor", "0")

        class MockPhotoNotifier:
            def __init__(self):
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                self.sent_photos.append({"bytes": photo_bytes, "caption": caption})

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = MockPhotoNotifier()
        cfg = AgentConfig(
            enabled=True,
            notify_enabled=True,
            trace_file="trace_escalate_two_tools.json",
            snapshots_dir=self.temp_snapshots.name,
        )
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        ring_client = FakeRingClient("success")

        res = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=cfg,
            notifier=notifier,
            ring_client=ring_client,
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["decision"], "escalate")
        self.assertTrue(res[0]["notified"])

        # Photo delivered exactly once
        self.assertEqual(len(notifier.sent_photos), 1)
        self.assertGreater(len(notifier.sent_photos[0]["bytes"]), 0)
        # Caption starts with format_snapshot_caption_prefix
        expected_prefix = format_snapshot_caption_prefix(1791800000000, "America/Chicago", now=now)
        self.assertTrue(
            notifier.sent_photos[0]["caption"].startswith(expected_prefix),
            f"Caption '{notifier.sent_photos[0]['caption']}' did not start with '{expected_prefix}'",
        )
        # Verify: NO text message is sent when photo delivery succeeds
        self.assertEqual(len(notifier.sent_messages), 0)

    def test_runner_photo_attachment_media_unavailable(self):
        """
        (b) snapshot failed (MediaUnavailable) -> text only, no send_photo.
        """
        save_routine(
            self.conn,
            routine_id="rt_day_door_p2",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_photo_fail", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        set_app_state(self.conn, "agent_event_cursor", "0")

        class MockPhotoNotifier:
            def __init__(self):
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                self.sent_photos.append({"bytes": photo_bytes, "caption": caption})

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = MockPhotoNotifier()
        cfg = AgentConfig(
            enabled=True,
            notify_enabled=True,
            trace_file="trace_escalate_two_tools.json",
            snapshots_dir=self.temp_snapshots.name,
        )
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        ring_client = FakeRingClient("416")

        res = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=cfg,
            notifier=notifier,
            ring_client=ring_client,
        )
        self.assertEqual(len(res), 1)
        # Snapshot failed -> no photo sent
        self.assertEqual(len(notifier.sent_photos), 0)

    def test_runner_photo_attachment_exception_fallback(self):
        """
        (c) send_photo raises -> runner catches the exception and falls back to text delivery.
        """
        save_routine(
            self.conn,
            routine_id="rt_day_door_p3",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_photo_raise", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        set_app_state(self.conn, "agent_event_cursor", "0")

        class RaisingPhotoNotifier:
            def __init__(self):
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                self.sent_photos.append(photo_bytes)
                raise RuntimeError("Simulated Telegram sendPhoto HTTP 500 error")

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = RaisingPhotoNotifier()
        cfg = AgentConfig(
            enabled=True,
            notify_enabled=True,
            trace_file="trace_escalate_two_tools.json",
            snapshots_dir=self.temp_snapshots.name,
        )
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        ring_client = FakeRingClient("success")

        res = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=cfg,
            notifier=notifier,
            ring_client=ring_client,
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["decision"], "escalate")
        self.assertTrue(res[0]["notified"])
        # send_photo was attempted and raised
        self.assertEqual(len(notifier.sent_photos), 1)
        # Fallback executed: text message sent
        self.assertEqual(len(notifier.sent_messages), 1)
        self.assertIn("CHECK NEEDED", notifier.sent_messages[0])

    def test_runner_notifier_without_send_photo(self):
        """
        (d) notifier without send_photo -> text only.
        """
        save_routine(
            self.conn,
            routine_id="rt_day_door_p4",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_no_photo_notifier", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        set_app_state(self.conn, "agent_event_cursor", "0")

        class TextOnlyNotifier:
            def __init__(self):
                self.sent_messages = []

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = TextOnlyNotifier()
        cfg = AgentConfig(
            enabled=True,
            notify_enabled=True,
            trace_file="trace_escalate_two_tools.json",
            snapshots_dir=self.temp_snapshots.name,
        )
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        ring_client = FakeRingClient("success")

        res = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=cfg,
            notifier=notifier,
            ring_client=ring_client,
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["decision"], "escalate")
        self.assertTrue(res[0]["notified"])
        # Only text delivered
        self.assertEqual(len(notifier.sent_messages), 1)
        self.assertIn("CHECK NEEDED", notifier.sent_messages[0])

    def test_investigation_result_data_keys(self):
        """
        (e) Stored data has exactly the keys {path, captured_at}; trace_json stays JSON-serializable.
        """
        save_routine(
            self.conn,
            routine_id="rt_day_door_p5",
            property_id="prop_1",
            name="Day Door",
            device_id="cam_front",
            expected_event_type="motion",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon"],
        )
        upsert_device_health(self.conn, "cam_front", 1, "2026-10-12T07:00:00Z", "webhook")
        save_event(self.conn, "webhook", "evt_data_keys", "cam_front", "motion", "2026-10-12T07:10:00Z", "2026-10-12T07:10:00Z", {})
        set_app_state(self.conn, "agent_event_cursor", "0")

        cfg = AgentConfig(
            enabled=True,
            notify_enabled=False,
            trace_file="trace_escalate_two_tools.json",
            snapshots_dir=self.temp_snapshots.name,
        )
        now = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        ring_client = FakeRingClient("success")

        res = process_new_events(
            conn=self.conn,
            provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
            now=now,
            cfg=cfg,
            notifier=None,
            ring_client=ring_client,
        )
        self.assertEqual(len(res), 1)

        inv_row = get_investigation(self.conn, "evt_data_keys")
        self.assertIsNotNone(inv_row)
        trace = json.loads(inv_row["trace_json"])
        self.assertIsInstance(trace, list)

        # Re-serialization stays valid JSON
        serialized = json.dumps(trace)
        self.assertIsInstance(serialized, str)

        snapshot_steps = [s for s in trace if s.get("type") == "result" and s.get("tool") == "pull_snapshot"]
        self.assertEqual(len(snapshot_steps), 1)
        snap_step = snapshot_steps[0]
        self.assertIn("data", snap_step)
        data_keys = set(snap_step["data"].keys())
        self.assertEqual(data_keys, {"path", "captured_at"})

        # Check other tools do NOT carry data key
        status_steps = [s for s in trace if s.get("type") == "result" and s.get("tool") == "check_routine_status"]
        self.assertEqual(len(status_steps), 1)
        self.assertNotIn("data", status_steps[0])

    def test_caregiver_message_no_filesystem_paths(self):
        """
        Caregiver notification text never contains machine filesystem paths
        (/var/folders, /Users, /tmp, snapshots/), while dashboard and DB retain valid references.
        """
        raw_snapshot_text = (
            "Snapshot retrieved successfully for device demo-cam-living-room saved to "
            "/var/folders/wv/k0r_74955b721867z7t_1l080000gn/T/threshold_demo_xyz/snapshots/"
            "snapshot_demo-cam-living-room_20261013T071000Z.jpg (captured at 2026-10-13T07:10:00Z)."
        )
        inv = Investigation(
            event_key="evt_path_check",
            mode="replay",
            provider_name="mock",
            steps=2,
            elapsed_ms=120,
            tier="check_needed",
            recommendation="Test recommendation for caregiver path privacy.",
            evidence=[
                Evidence(tool="pull_snapshot", call_id="c1", ok=True, text=raw_snapshot_text),
                Evidence(tool="query_device_health", call_id="c2", ok=True, text="Device demo-cam-living-room is fresh."),
            ],
            fallback=False,
            trace=[],
        )
        event = {
            "device_id": "demo-cam-living-room",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-13T07:10:00Z",
        }

        # 1. Caregiver notification message converts UTC timestamp to household timezone
        msg_text = format_agent_notification_text(event, inv, timezone_name="America/Chicago")
        self.assertNotIn("/var/folders", msg_text)
        self.assertNotIn("/Users", msg_text)
        self.assertNotIn("/tmp", msg_text)
        self.assertNotIn("snapshots/", msg_text)
        self.assertIn("✓ Snapshot retrieved successfully for device demo-cam-living-room (captured 02:10 CDT).", msg_text)

        # 1b. Without timezone or invalid timezone, format_evidence_text_for_caregiver falls back safely
        fallback_text = format_evidence_text_for_caregiver(raw_snapshot_text, timezone_name=None)
        self.assertIn("(captured at 2026-10-13T07:10:00Z)", fallback_text)

        # 2. Raw evidence in Investigation object is untouched
        self.assertEqual(inv.evidence[0].text, raw_snapshot_text)

        # 3. Dashboard helpers sanitize to relative snapshot path
        dash_text = format_evidence_text_for_dashboard(raw_snapshot_text)
        self.assertIn("snapshots/snapshot_demo-cam-living-room_20261013T071000Z.jpg", dash_text)
        self.assertNotIn("/var/folders", dash_text)
        self.assertEqual(
            extract_snapshot_filename(raw_snapshot_text),
            "snapshot_demo-cam-living-room_20261013T071000Z.jpg",
        )

    def test_caption_limit_under_1000_chars_single_message(self):
        """
        Captions <= 1000 chars are delivered as a single send_photo with the combined caption.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        snap_file = os.path.join(temp_dir.name, "snap.jpg")
        with open(snap_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

        inv = Investigation(
            event_key="evt_short_caption",
            mode="replay",
            provider_name="mock",
            steps=1,
            elapsed_ms=50,
            tier="check_needed",
            recommendation="Brief recommendation.",
            evidence=[
                Evidence(tool="pull_snapshot", call_id="c1", ok=True, text="Snapshot retrieved successfully for device cam1."),
            ],
            fallback=False,
            trace=[
                {
                    "type": "result",
                    "tool": "pull_snapshot",
                    "ok": True,
                    "data": {"path": snap_file, "captured_at": "2026-10-13T07:10:00Z"},
                }
            ],
        )
        event = {
            "device_id": "cam1",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-13T07:10:00Z",
        }

        class MockDualNotifier:
            def __init__(self):
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                self.sent_photos.append({"bytes": photo_bytes, "caption": caption})

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = MockDualNotifier()
        now = datetime(2026, 10, 13, 7, 10, tzinfo=timezone.utc)
        delivered = deliver_agent_notification(
            notifier=notifier,
            event=event,
            investigation=inv,
            now=now,
        )
        self.assertTrue(delivered)
        self.assertEqual(len(notifier.sent_photos), 1)
        self.assertEqual(len(notifier.sent_messages), 0)
        self.assertIn("CHECK NEEDED", notifier.sent_photos[0]["caption"])

    def test_caption_limit_over_1000_chars_dual_delivery(self):
        """
        Captions > 1000 chars deliver photo with short prefix caption (<= 200 chars)
        plus full message text as a separate message.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        snap_file = os.path.join(temp_dir.name, "snap.jpg")
        with open(snap_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

        long_rec = "A" * 600
        long_evidence = [
            Evidence(tool="pull_snapshot", call_id="c1", ok=True, text="Snapshot retrieved successfully for device cam1."),
            Evidence(tool="query_device_health", call_id="c2", ok=True, text="B" * 300),
            Evidence(tool="get_recent_ring_events", call_id="c3", ok=True, text="C" * 300),
        ]
        inv = Investigation(
            event_key="evt_long_caption",
            mode="replay",
            provider_name="mock",
            steps=3,
            elapsed_ms=90,
            tier="check_needed",
            recommendation=long_rec,
            evidence=long_evidence,
            fallback=False,
            trace=[
                {
                    "type": "result",
                    "tool": "pull_snapshot",
                    "ok": True,
                    "data": {"path": snap_file, "captured_at": "2026-10-13T07:10:00Z"},
                }
            ],
        )
        event = {
            "device_id": "cam1",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-13T07:10:00Z",
        }

        class MockDualNotifier:
            def __init__(self):
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                self.sent_photos.append({"bytes": photo_bytes, "caption": caption})

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = MockDualNotifier()
        now = datetime(2026, 10, 13, 7, 10, tzinfo=timezone.utc)
        delivered = deliver_agent_notification(
            notifier=notifier,
            event=event,
            investigation=inv,
            now=now,
        )
        self.assertTrue(delivered)
        self.assertEqual(len(notifier.sent_photos), 1)
        self.assertEqual(len(notifier.sent_messages), 1)
        self.assertLessEqual(len(notifier.sent_photos[0]["caption"]), 200)
        self.assertIn("CHECK NEEDED", notifier.sent_messages[0])
        self.assertIn(long_rec, notifier.sent_messages[0])

    def test_caption_limit_strict_photo_length_succeeds_via_dual_delivery(self):
        """
        Prove behavior when send_photo raises if len(caption) > 1024 (mimicking Telegram limits).
        Dual delivery protects against the silent text-only fallback.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        snap_file = os.path.join(temp_dir.name, "snap.jpg")
        with open(snap_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

        long_rec = "D" * 600
        inv = Investigation(
            event_key="evt_raise_test",
            mode="replay",
            provider_name="mock",
            steps=2,
            elapsed_ms=80,
            tier="check_needed",
            recommendation=long_rec,
            evidence=[
                Evidence(tool="pull_snapshot", call_id="c1", ok=True, text="Snapshot retrieved successfully for device cam1."),
                Evidence(tool="check_routine_status", call_id="c2", ok=True, text="E" * 400),
            ],
            fallback=False,
            trace=[
                {
                    "type": "result",
                    "tool": "pull_snapshot",
                    "ok": True,
                    "data": {"path": snap_file, "captured_at": "2026-10-13T07:10:00Z"},
                }
            ],
        )
        event = {
            "device_id": "cam1",
            "event_type": "motion_detected",
            "occurred_at": "2026-10-13T07:10:00Z",
        }

        class TelegramStrictPhotoNotifier:
            def __init__(self):
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                if len(caption) > 1024:
                    raise RuntimeError("Telegram API error: message caption is too long (400 Bad Request)")
                self.sent_photos.append({"bytes": photo_bytes, "caption": caption})

            def send_message(self, text: str):
                self.sent_messages.append(text)

        notifier = TelegramStrictPhotoNotifier()
        now = datetime(2026, 10, 13, 7, 10, tzinfo=timezone.utc)
        delivered = deliver_agent_notification(
            notifier=notifier,
            event=event,
            investigation=inv,
            now=now,
        )
        self.assertTrue(delivered)
        # Because caption was > 1000 chars, it sent short prefix caption (<= 200 chars),
        # so send_photo did NOT raise, and photo was successfully delivered!
        self.assertEqual(len(notifier.sent_photos), 1)
        self.assertEqual(len(notifier.sent_messages), 1)

    def test_over_limit_delivery_combos_and_notified_at(self):
        """
        Test all four combinations (photo ok/fail × text ok/fail) on over-limit (>1000 chars) alerts.
        Assert delivered boolean, messages sent, and verify that notified_at is NOT set when delivered is False.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        snap_file = os.path.join(temp_dir.name, "snap.jpg")
        with open(snap_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

        class ComboNotifier:
            def __init__(self, photo_ok: bool, text_ok: bool):
                self.photo_ok = photo_ok
                self.text_ok = text_ok
                self.chat_id = "test_chat_combo"
                self.sent_photos = []
                self.sent_messages = []

            def send_photo(self, photo_bytes: bytes, caption: str):
                if not self.photo_ok:
                    raise RuntimeError("Telegram API request to sendPhoto failed: photo upload error")
                self.sent_photos.append({"bytes": photo_bytes, "caption": caption})

            def _make_request(self, endpoint: str, payload: dict):
                if endpoint == "sendMessage":
                    if not self.text_ok:
                        raise RuntimeError("Telegram API request to sendMessage failed: 400 Bad Request")
                    self.sent_messages.append(payload.get("text", ""))
                    return {"ok": True, "result": {"message_id": 1234}}
                raise NotImplementedError(endpoint)

        now = datetime(2026, 10, 13, 7, 10, tzinfo=timezone.utc)
        long_rec = "C" * 600

        combos = [
            # (photo_ok, text_ok, expected_delivered, exp_photos, exp_texts)
            (True, True, True, 1, 1),
            (True, False, False, 1, 0),
            (False, True, True, 0, 1),
            (False, False, False, 0, 0),
        ]

        for photo_ok, text_ok, expected_deliv, exp_p, exp_t in combos:
            with self.subTest(photo_ok=photo_ok, text_ok=text_ok):
                conn = init_db(":memory:")
                event_key = f"evt_combo_{photo_ok}_{text_ok}"
                inv = Investigation(
                    event_key=event_key,
                    mode="replay",
                    provider_name="mock",
                    steps=2,
                    elapsed_ms=50,
                    tier="escalate",
                    recommendation=long_rec,
                    evidence=[
                        Evidence(tool="pull_snapshot", call_id="c1", ok=True, text="Snapshot retrieved."),
                        Evidence(tool="check_routine_status", call_id="c2", ok=True, text="F" * 450),
                    ],
                    fallback=False,
                    trace=[
                        {
                            "type": "result",
                            "tool": "pull_snapshot",
                            "ok": True,
                            "data": {"path": snap_file, "captured_at": "2026-10-13T07:10:00Z"},
                        }
                    ],
                )
                save_investigation(
                    conn,
                    event_key=event_key,
                    device_id="cam_combo",
                    created_at=now,
                    mode="replay",
                    provider_name="mock",
                    tier="escalate",
                    decision="escalate",
                    reason_code="cleared",
                    recommendation=long_rec,
                )

                event = {
                    "device_id": "cam_combo",
                    "event_type": "motion_detected",
                    "occurred_at": "2026-10-13T07:10:00Z",
                }

                notifier = ComboNotifier(photo_ok=photo_ok, text_ok=text_ok)
                delivered = deliver_agent_notification(
                    notifier=notifier,
                    event=event,
                    investigation=inv,
                    now=now,
                )

                self.assertEqual(delivered, expected_deliv)
                self.assertEqual(len(notifier.sent_photos), exp_p)
                self.assertEqual(len(notifier.sent_messages), exp_t)

                # Simulate runner's conditional update of notified_at
                if delivered:
                    now_iso = to_iso_timestamp(now)
                    conn.execute(
                        "UPDATE investigations SET notified_at = ? WHERE event_key = ?",
                        (now_iso, inv.event_key),
                    )
                    conn.commit()

                cur = conn.execute("SELECT notified_at FROM investigations WHERE event_key = ?", (event_key,))
                notified_at_val = cur.fetchone()[0]

                if expected_deliv:
                    self.assertIsNotNone(notified_at_val)
                else:
                    self.assertIsNone(notified_at_val)


if __name__ == "__main__":
    unittest.main()


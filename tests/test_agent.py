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
from src.threshold.ring_client import MediaUnavailable, RateLimited, TokenExpired
from src.threshold.store import (
    get_investigation,
    init_db,
    list_investigations,
    record_correction,
    record_escalation,
    save_event,
    save_investigation,
    save_routine,
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

    # --- 7. Import Guard (AST-Based) ---

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

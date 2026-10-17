"""
Investigation of Photo claim vs raw output for Scene 5a.
Evaluates CapturingConsoleNotifier.sent_photos and runner.py behavior.
"""
from datetime import datetime, timezone
import json
import os
import shutil
import tempfile

from src.threshold.agent.provider import MockProvider
from src.threshold.agent.runner import process_new_events
from src.threshold.config import AgentConfig
from src.threshold.demo import DemoSnapshotClient, CapturingConsoleNotifier
from src.threshold.ingest import ingest_event
from src.threshold.notifier import ConsoleNotifier
from src.threshold.scheduler import FakeClock
from src.threshold.store import init_db, save_routine, upsert_device_health

temp_dir = tempfile.mkdtemp()
db_path = os.path.join(temp_dir, "demo.db")
snapshot_dir = os.path.join(temp_dir, "snapshots")
os.makedirs(snapshot_dir, exist_ok=True)

conn = init_db(db_path)
save_routine(
    conn=conn,
    routine_id="rt_morning_movement",
    property_id="prop_demo",
    name="Morning movement",
    device_id="demo-cam-living-room",
    expected_event_type="motion_detected",
    window_start="07:00",
    window_end="09:00",
    timezone_name="America/Chicago",
    days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    escalation_delay_minutes=30,
)
upsert_device_health(conn, "demo-cam-living-room", 1, "2026-10-13T07:00:00Z", "webhook")

clock = FakeClock(datetime(2026, 10, 13, 7, 10, tzinfo=timezone.utc))
snapshot_client = DemoSnapshotClient()

from src.threshold.store import set_app_state
set_app_state(conn, "agent_event_cursor", "0")

# Ingest event 5a
ingest_event(conn, "webhook", "evt_5a_req", "demo-cam-living-room", "motion_detected", "human", "2026-10-13T07:10:00Z", {})

agent_cfg_5a = AgentConfig(
    enabled=True,
    provider="mock",
    trace_file="trace_escalate_two_tools.json",
    notify_enabled=True,
    snapshots_dir=snapshot_dir,
)
agent_notif_5a = ConsoleNotifier()
capturing_agent_5a = CapturingConsoleNotifier(agent_notif_5a)

proc_5a = process_new_events(
    conn=conn,
    provider_factory=lambda: MockProvider("trace_escalate_two_tools.json"),
    now=clock.now(),
    cfg=agent_cfg_5a,
    notifier=capturing_agent_5a,
    ring_client=snapshot_client,
)

print("=== SCENE 5a PHOTO INVESTIGATION RESULTS ===")
print("process_new_events returned:", len(proc_5a), "processed event(s)")
dec = proc_5a[0].get("decision")
reason = proc_5a[0].get("reason_code")
notified = proc_5a[0].get("notified")
print("investigation decision:", dec, f"(reason: {reason})")
print("notified flag:", notified)
print("len(capturing_agent_5a.sent_photos):", len(capturing_agent_5a.sent_photos))
for idx, p in enumerate(capturing_agent_5a.sent_photos):
    print(f"  Photo {idx}: {p.get('bytes_len')} bytes")
print("len(capturing_agent_5a.sent_messages):", len(capturing_agent_5a.sent_messages))
if capturing_agent_5a.sent_messages:
    print("\nMessage sent via send_message (first 120 chars):")
    print(repr(capturing_agent_5a.sent_messages[0][:120]))

# Check runner.py and investigate.py trace data
inv_row = conn.execute("SELECT * FROM investigations WHERE device_id = 'demo-cam-living-room' ORDER BY rowid DESC LIMIT 1").fetchone()
trace = json.loads(inv_row["trace_json"])
print("\nTrace steps recorded in DB for Scene 5a:")
for s in trace:
    print(f"  type={s.get('type')}, tool={s.get('tool')}, keys={list(s.keys())}, has_data={'data' in s}")

print("\n--- CODE ANALYSIS & CITATION ---")
print("1. In src/threshold/agent/investigate.py:144-151:")
print("   step_history.append records: type, call_id, tool, ok, text, error.")
print("   Notice that 'data': tr.data is NOT added to step_history.")
print("2. In src/threshold/agent/runner.py:94-102:")
print("   runner checks: if step.get('type') == 'result' and step.get('tool') == 'pull_snapshot':")
print("                    res_data = step.get('data') or {}")
print("                    if isinstance(res_data, dict) and res_data.get('path'):")
print("                        snapshot_path = res_data.get('path')")
print("   Because step does NOT contain 'data', res_data is {}, snapshot_path remains None.")
print("3. In src/threshold/agent/runner.py:104-112:")
print("   photo_bytes remains None.")
print("4. In src/threshold/agent/runner.py:113-130:")
print("   if photo_bytes and hasattr(notifier, 'send_photo'): is NOT entered.")
print("5. In src/threshold/agent/runner.py:140-142:")
print("   runner falls back to: elif hasattr(notifier, 'send_message'): notifier.send_message(message_text)")
print("   Therefore, runner sent a TEXT message, and NO photo attachment was dispatched.")
print("CONCLUSION: NO photo was sent by runner.py. Demo narration updated to honestly report text-only alert without claiming attachment.")

conn.close()
shutil.rmtree(temp_dir, ignore_errors=True)

"""
Verifies synchronization between docs/sample_investigation.json and
src/threshold/agent/traces/trace_escalate_two_tools.json.
Runs the drift test to prove enforcement.
"""
import json
import os
import subprocess
import sys

print("=== 1. GREP -N 'Snapshot' COMPARISON ===")
print("docs/sample_investigation.json:")
p1 = subprocess.run(["grep", "-n", "Snapshot", "docs/sample_investigation.json"], capture_output=True, text=True)
print(p1.stdout.strip())

print("\nsrc/threshold/agent/traces/trace_escalate_two_tools.json:")
p2 = subprocess.run(["grep", "-n", "Snapshot", "src/threshold/agent/traces/trace_escalate_two_tools.json"], capture_output=True, text=True)
print(p2.stdout.strip())

print("\n=== 2. PARSED JSON COMPARISON ===")
with open("docs/sample_investigation.json") as f:
    sample_data = json.load(f)
with open("src/threshold/agent/traces/trace_escalate_two_tools.json") as f:
    trace_data = json.load(f)

trace_final = next(s for s in trace_data["steps"] if s["type"] == "final")
sample_final = next(s for s in sample_data["steps"] if s["type"] == "final")

print("Trace final recommendation: ", trace_final["recommendation"])
print("Sample final recommendation:", sample_final["recommendation"])
print("Sample investigation recommendation:", sample_data["investigation"]["recommendation"])
assert trace_final["recommendation"] == sample_final["recommendation"]
assert "Snapshot requested for caregiver review." in sample_data["investigation"]["recommendation"]
print("✓ Content is 100% synchronized.")

print("\n=== 3. DRIFT TEST EXECUTION ===")
test_cmd = [
    sys.executable,
    "-m",
    "unittest",
    "tests.test_agent.TestAgentLibrary.test_sample_investigation_sync",
]
p3 = subprocess.run(test_cmd, capture_output=True, text=True)
print(p3.stdout)
print(p3.stderr)
print(f"Test exit code: {p3.returncode}")

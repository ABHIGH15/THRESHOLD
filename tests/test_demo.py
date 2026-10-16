"""
tests/test_demo.py — Test suite for Threshold zero-secret simulated demo mode.

Verifies:
  T1 Scene assertions via structured function API (S1 to S6).
  T2 CLI subprocess smoke (exit 0, banner, replay (scripted), REAL vs SIMULATED).
  T3 Sentinel test (isolation against external .env or threshold.json secrets).
  T4 Static guard (src/threshold/demo.py contains no forbidden references).
  T5 Network guard (loopback allowed, 1.1.1.1 blocked).
  T6 Determinism test (two runs produce identical masked outputs).
"""

from io import StringIO
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.threshold.demo import (
    FALLBACK_JPEG_BYTES,
    SAMPLE_JPEG_BYTES,
    install_network_guard,
    run_demo_pipeline,
    uninstall_network_guard,
)


class TestThresholdDemo(unittest.TestCase):
    """Test suite for the zero-secret simulated demonstration runner."""

    def test_t1_scene_assertions_structured_api(self):
        """T1: Function API executes S1-S6 and returns structured assertions."""
        res = run_demo_pipeline(pace=0.0, no_color=True, serve=False)

        self.assertTrue(res.get("success"), "Pipeline should report overall success")
        scenes = res.get("scenes", {})

        # Scene 1: Routine observed, 0 alerts
        self.assertEqual(scenes["s1"]["status"], "observed")
        self.assertEqual(scenes["s1"]["escalations"], 0)

        # Scene 2: Trust boundary (401 bad sig, duplicate request idempotent)
        self.assertEqual(scenes["s2"]["bad_sig_status"], 401)
        self.assertTrue(scenes["s2"]["idempotent"])

        # Scene 3: Missed routine (exactly 1 escalation, subsequent tick 0)
        self.assertEqual(scenes["s3"]["first_tick_escalations"], 1)
        self.assertEqual(scenes["s3"]["second_tick_escalations"], 0)

        # Scene 4: UNKNOWN offline device silenced (0 escalations)
        self.assertEqual(scenes["s4"]["status"], "unknown")
        self.assertEqual(scenes["s4"]["escalations"], 0)

        # Scene 5: Gate evaluate (5a escalate cleared, 5b deduped, 5c downgraded to check_needed)
        self.assertEqual(scenes["s5"]["decision_5a"], "escalate")
        self.assertEqual(scenes["s5"]["reason_5a"], "cleared")
        self.assertTrue(scenes["s5"]["notified_5a"])
        self.assertTrue(scenes["s5"]["deduped_5b"])
        self.assertEqual(scenes["s5"]["decision_5c"], "check_needed")
        self.assertEqual(scenes["s5"]["reason_5c"], "insufficient_evidence")
        self.assertFalse(scenes["s5"]["notified_5c"])

        # Scene 6: Household memory quiets alert with evidence from real tool result
        self.assertTrue(scenes["s6"]["callback_success"])
        self.assertEqual(scenes["s6"]["decision"], "check_needed")
        self.assertEqual(scenes["s6"]["reason"], "check_needed")
        self.assertIn("3 false alarm(s) out of 3 correction(s)", scenes["s6"]["evidence_text"])

    def test_t2_cli_subprocess_smoke(self):
        """T2: CLI subprocess smoke runs cleanly and contains required honesty banners."""
        cmd = [sys.executable, "-m", "src.threshold", "demo", "--no-color", "--pace", "0"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(f"Demo CLI process timed out after 120s: {stderr}")
        finally:
            if proc.poll() is None:
                proc.kill()

        self.assertEqual(proc.returncode, 0, f"Demo process failed with code {proc.returncode}: {stderr}")
        self.assertIn("THRESHOLD — PATTERN-ABSENCE DETECTION FOR ELDERLY CARE", stdout)
        self.assertIn("SIMULATED HOUSEHOLD (fictional)", stdout)
        self.assertIn("replay (scripted)", stdout)
        self.assertIn("REAL vs SIMULATED BREAKDOWN", stdout)
        self.assertIn("Unknown — device offline, not escalated", stdout)

    def test_t3_sentinel_zero_secret_isolation(self):
        """T3: Demo operates in full isolation without reading .env or external files."""
        temp_dir = tempfile.mkdtemp(prefix="threshold_sentinel_")
        try:
            # Create a fake repository layout
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            shutil.copytree(os.path.join(repo_root, "src"), os.path.join(temp_dir, "src"))
            scripts_dir = os.path.join(repo_root, "scripts")
            if os.path.exists(scripts_dir):
                shutil.copytree(scripts_dir, os.path.join(temp_dir, "scripts"))

            # Inject fake .env with sentinel secrets
            fake_env = os.path.join(temp_dir, ".env")
            with open(fake_env, "w", encoding="utf-8") as f:
                f.write("RING_ACCESS_TOKEN=SENTINEL_DO_NOT_USE_RING_SECRET\n")
                f.write("TELEGRAM_BOT_TOKEN=SENTINEL_DO_NOT_USE_TELEGRAM_SECRET\n")
                f.write("ANTHROPIC_API_KEY=SENTINEL_DO_NOT_USE_LLM_KEY\n")

            # Inject fake threshold.json
            fake_json = os.path.join(temp_dir, "threshold.json")
            with open(fake_json, "w", encoding="utf-8") as f:
                f.write('{"sentinel_key": "SENTINEL_DO_NOT_USE_JSON_SECRET"}\n')

            # Clean environment without any RING_/TELEGRAM_/LLM_ keys
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("RING_", "TELEGRAM_", "LLM_"))}
            clean_env["PYTHONPATH"] = temp_dir

            cmd = [sys.executable, "-m", "src.threshold", "demo", "--no-color", "--pace", "0"]
            proc = subprocess.Popen(
                cmd,
                cwd=temp_dir,
                env=clean_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=120)
            finally:
                if proc.poll() is None:
                    proc.kill()

            self.assertEqual(proc.returncode, 0, f"Demo in sentinel sandbox failed: {stderr}")
            self.assertNotIn("SENTINEL", stdout)
            self.assertNotIn("SENTINEL", stderr)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_t4_static_guard_no_forbidden_references(self):
        """T4: demo.py does not reference evidence/, threshold.json, or .env."""
        demo_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "src", "threshold", "demo.py")
        )
        with open(demo_path, "r", encoding="utf-8") as f:
            code = f.read()

        for forbidden in ["evidence/", "threshold.json", ".env"]:
            self.assertNotIn(
                forbidden,
                code,
                f"Forbidden token '{forbidden}' detected in src/threshold/demo.py",
            )

    def test_t5_network_guard(self):
        """T5: Network guard permits loopback connections and blocks external destinations."""
        install_network_guard()
        try:
            # 1. External connection to 1.1.1.1 must raise ConnectionRefusedError with guard message
            s_ext = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.assertRaises(ConnectionRefusedError) as cm:
                s_ext.connect(("1.1.1.1", 80))
            self.assertIn("[NETWORK GUARD]", str(cm.exception))
            s_ext.close()

            # 2. Loopback connection to 127.0.0.1 must NOT raise network guard error
            # Bind an ephemeral loopback socket
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # connect to loopback - network guard must pass it through to the underlying connect
            client.connect(("127.0.0.1", port))
            client.close()
            listener.close()
        finally:
            uninstall_network_guard()

    def test_t6_output_determinism(self):
        """T6: Two consecutive runs produce byte-identical output after masking ports/paths."""
        def run_and_capture():
            cmd = [sys.executable, "-m", "src.threshold", "demo", "--no-color", "--pace", "0"]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, _ = proc.communicate(timeout=120)
            finally:
                if proc.poll() is None:
                    proc.kill()
            return stdout

        out1 = run_and_capture()
        out2 = run_and_capture()

        def mask_output(text: str) -> str:
            # Mask ephemeral ports and temp snapshot filenames/paths
            masked = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", text)
            masked = re.sub(r"threshold_demo_[a-zA-Z0-9_]+", "threshold_demo_TEMPDIR", masked)
            return masked

        masked1 = mask_output(out1)
        masked2 = mask_output(out2)

        self.assertEqual(masked1, masked2, "Demo outputs differ across identical runs!")


if __name__ == "__main__":
    unittest.main()

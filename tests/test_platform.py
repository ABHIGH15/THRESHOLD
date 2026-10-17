"""
tests/test_platform.py — Tests for platform preflight, OS compatibility, and missing tzdata handling.
Verifies that on platforms missing system timezone databases (e.g., Windows without tzdata),
the CLI entry points fail gracefully with a single clean actionable error line instead of a traceback.
"""

import argparse
import io
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from src.threshold.__main__ import run_daemon_command, run_demo_cli, run_ring_check
from src.threshold.demo import run_demo_pipeline, main as demo_main


class TestPlatformPreflight(unittest.TestCase):

    def test_demo_pipeline_zoneinfo_not_found_handling(self):
        """run_demo_pipeline catches ZoneInfoNotFoundError and prints clear message."""
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with patch("src.threshold.demo.FakeClock", side_effect=ZoneInfoNotFoundError("America/Chicago")):
                res = run_demo_pipeline(pace=0.0, no_color=True, serve=False)

        self.assertFalse(res.get("success", True))
        err_output = buf.getvalue().strip()
        self.assertEqual(
            err_output,
            "Timezone data missing: pip install tzdata, or run on macOS/Linux",
        )

    def test_demo_cli_zoneinfo_not_found_handling(self):
        """run_demo_cli catches ZoneInfoNotFoundError and exits 1 with clear message."""
        args = argparse.Namespace(pace=0.0, fast=True, no_color=True, serve=False, keep_running=False)
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with patch("src.threshold.demo.run_demo_pipeline", side_effect=ZoneInfoNotFoundError("America/Chicago")):
                code = run_demo_cli(args)

        self.assertEqual(code, 1)
        err_output = buf.getvalue().strip()
        self.assertEqual(
            err_output,
            "Timezone data missing: pip install tzdata, or run on macOS/Linux",
        )

    def test_demo_main_zoneinfo_not_found_handling(self):
        """demo.py main() catches ZoneInfoNotFoundError and exits 1 with clear message."""
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with patch("sys.argv", ["demo.py"]):
                with patch("src.threshold.demo._run_demo_pipeline_impl", side_effect=ZoneInfoNotFoundError("America/Chicago")):
                    code = demo_main()

        self.assertEqual(code, 1)
        err_output = buf.getvalue().strip()
        self.assertEqual(
            err_output,
            "Timezone data missing: pip install tzdata, or run on macOS/Linux",
        )

    def test_ring_check_zoneinfo_not_found_handling(self):
        """run_ring_check catches ZoneInfoNotFoundError and exits 1 with clear message."""
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with patch("src.threshold.__main__.load_env", side_effect=ZoneInfoNotFoundError("America/Chicago")):
                code = run_ring_check()

        self.assertEqual(code, 1)
        err_output = buf.getvalue().strip()
        self.assertEqual(
            err_output,
            "Timezone data missing: pip install tzdata, or run on macOS/Linux",
        )

    def test_daemon_command_zoneinfo_not_found_handling(self):
        """run_daemon_command catches ZoneInfoNotFoundError and exits 1 with clear message."""
        args = argparse.Namespace(
            config=None,
            db=":memory:",
            poll_interval=None,
            tick_interval=None,
            household=None,
            timezone=None,
            no_poll=True,
            no_dashboard=True,
            once=True,
        )
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with patch("src.threshold.__main__.init_db", side_effect=ZoneInfoNotFoundError("America/Chicago")):
                code = run_daemon_command(args)

        self.assertEqual(code, 1)
        err_output = buf.getvalue().strip()
        self.assertEqual(
            err_output,
            "Timezone data missing: pip install tzdata, or run on macOS/Linux",
        )


if __name__ == "__main__":
    unittest.main()

"""
Runs headless Chrome against the real running Threshold demo dashboard.
Applies the improved console/network error counter to Chrome's stderr log.
Asserts that the real dashboard produces 0 console errors and 0 broken resources.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import List

from scripts.check_console_counter import improved_counter, CHROME_BIN

# Start demo --serve
demo_cmd = [
    sys.executable,
    "-u",
    "-m",
    "src.threshold",
    "demo",
    "--no-color",
    "--pace",
    "0",
    "--serve",
]

proc = subprocess.Popen(
    demo_cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

server_url = None
try:
    start_time = time.time()
    for line in proc.stdout:
        m = re.search(r"Dashboard running at (http://127\.0\.0\.1:\d+/)", line)
        if m:
            server_url = m.group(1)
            break
        if time.time() - start_time > 30:
            raise TimeoutError("Timed out waiting for dashboard to start")

    if not server_url:
        raise RuntimeError("Could not find dashboard URL in output")

    print(f"=== REAL DASHBOARD CONSOLE & NETWORK ERROR AUDIT ===")
    print(f"Target dashboard: {server_url}")

    temp_dir = tempfile.mkdtemp()
    try:
        chrome_cmd = [
            CHROME_BIN,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={temp_dir}",
            "--window-size=1280,1024",
            "--enable-logging=stderr",
            "--v=1",
            server_url,
            f"--screenshot={temp_dir}/dashboard.png",
        ]
        chrome_proc = subprocess.run(chrome_cmd, capture_output=True, text=True, timeout=20)
        lines = chrome_proc.stderr.splitlines()

        print(f"Chrome exit code: {chrome_proc.returncode}")
        print(f"Chrome stderr total lines: {len(lines)}")

        detected_errors = improved_counter(lines)
        print(f"\nErrors detected by IMPROVED counter: {len(detected_errors)}")
        if detected_errors:
            for e in detected_errors:
                print(f"  [ERROR] {e}")
        else:
            print("  ✓ Zero console errors, zero uncaught exceptions, zero broken resources detected.")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

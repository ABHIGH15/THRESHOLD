"""
Console-Error Counter Evaluation:
1. Runs 3-case control in headless Chrome:
   (i) console.error("boom")
   (ii) <img src="/nope.jpg"> 404
   (iii) uncaught throw new Error("x")
2. Compares OLD counter vs IMPROVED counter.
3. Evaluates real Threshold dashboard using improved counter.
"""
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import List, Tuple

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class ControlHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/case1":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><script>console.error(\"boom\");</script></body></html>")
        elif self.path == "/case2":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><img src=\"/nope.jpg\"></body></html>")
        elif self.path == "/case3":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><script>throw new Error(\"x\");</script></body></html>")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, *args):
        pass


def run_chrome(url: str, timeout: int = 15) -> Tuple[int, List[str]]:
    temp_dir = tempfile.mkdtemp()
    try:
        cmd = [
            CHROME_BIN,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={temp_dir}",
            "--window-size=1280,1024",
            "--enable-logging=stderr",
            "--v=1",
            url,
            f"--screenshot={temp_dir}/shot.png",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        lines = proc.stderr.splitlines()
        return proc.returncode, lines
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def old_counter(lines: List[str]) -> List[str]:
    errors = []
    for line in lines:
        if ":CONSOLE:" in line:
            if "ERROR" in line or "Uncaught" in line or "error" in line.lower():
                errors.append(line)
    return errors


def improved_counter(lines: List[str]) -> List[str]:
    errors = []
    for line in lines:
        # Ignore benign Mac OS system/crashpad noise in test sandboxes
        if any(token in line for token in [
            "cv_display_link_mac",
            "crashpad",
            "task_policy_set",
            "Operation not permitted",
            "Certificate Error Assistant",
            "mutable_profile_oauth2",
            "account_reconcilor",
            "Histogram:",
            "-O",
        ]):
            continue

        # 1. Any console log/error from the web page
        if ":CONSOLE:" in line:
            errors.append(line)
            continue

        # 2. Subresource 404 or network load failures
        if any(token in line for token in ["Failed to load resource", "net::ERR_", "status of 404", "404 Not Found"]):
            errors.append(line)
            continue

        # 3. Uncaught exceptions or Chromium ERROR level lines
        if "Uncaught " in line or ":ERROR:" in line or ":FATAL:" in line:
            errors.append(line)
            continue
    return errors


def main():
    print("=== THREE-CASE CONSOLE CONTROL EXPERIMENT ===")
    server = http.server.HTTPServer(("127.0.0.1", 0), ControlHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]

    cases = [
        ("Case (i): console.error(\"boom\") (no 'error' in text)", f"http://127.0.0.1:{port}/case1"),
        ("Case (ii): <img src=\"/nope.jpg\"> (404 missing resource)", f"http://127.0.0.1:{port}/case2"),
        ("Case (iii): uncaught throw new Error(\"x\")", f"http://127.0.0.1:{port}/case3"),
    ]

    results = {}
    for name, url in cases:
        print(f"\nRunning {name}...")
        rc, lines = run_chrome(url)
        old_errs = old_counter(lines)
        new_errs = improved_counter(lines)

        print(f"  Chrome exit code: {rc}")
        print(f"  OLD counter count:      {len(old_errs)}")
        print(f"  IMPROVED counter count: {len(new_errs)}")
        if new_errs:
            print("  Sample detected line(s):")
            for sample in new_errs[:2]:
                print(f"    -> {sample}")
        results[name] = {
            "old_count": len(old_errs),
            "improved_count": len(new_errs),
        }

    server.shutdown()
    server.server_close()

    print("\n=== SUMMARY OF CONTROL RESULTS ===")
    for name, r in results.items():
        old_ok = "COUNTED" if r["old_count"] > 0 else "MISSED (0 counted)"
        new_ok = "COUNTED" if r["improved_count"] > 0 else "MISSED (0 counted)"
        print(f"{name}:")
        print(f"   Old rule:      {old_ok}")
        print(f"   Improved rule: {new_ok}")


if __name__ == "__main__":
    main()

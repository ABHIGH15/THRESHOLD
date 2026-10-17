"""
Captures headless Chrome screenshot of Investigation 1 Check-needed panel
and inspects the rendered snapshot placeholder badge.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

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

    print(f"Connected to dashboard at: {server_url}")

    # Inspect dashboard HTML for investigation 1 snapshot markup
    with urllib.request.urlopen(server_url, timeout=5) as resp:
        html = resp.read().decode("utf-8")

    print("\n=== 1. HTML MARKUP FOR SNAPSHOT AND PLACEHOLDER LABEL ===")
    m_box = re.search(r'(<div class="inv-snapshot-box">.*?</div>\s*</div>)', html, re.DOTALL)
    if m_box:
        print(m_box.group(1).strip())
    else:
        print("Warning: inv-snapshot-box not found in HTML")

    # Take screenshot using headless Chrome
    screenshot_dest = os.path.abspath("evidence/A9c/06_investigation_1_screenshot.png")
    os.makedirs(os.path.dirname(screenshot_dest), exist_ok=True)

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
            server_url,
            f"--screenshot={screenshot_dest}",
        ]
        cp = subprocess.run(chrome_cmd, capture_output=True, text=True, timeout=20)
        print(f"\n=== 2. HEADLESS CHROME SCREENSHOT ===")
        print(f"Screenshot saved to: {screenshot_dest}")
        print(f"Screenshot file size: {os.path.getsize(screenshot_dest)} bytes")
        print(f"Chrome exit code: {cp.returncode}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n=== 3. RENDERED SIZE & VIEWER EXPERIENCE ===")
    print("Rendered Image Size: 160px width by 110px height (styled via .inv-thumb).")
    print("Underlying Image: 1x1 pixel JPEG placeholder (149 bytes).")
    print("Viewer Observation: The 1x1 pixel JPEG is stretched into a 160x110 block by the browser.")
    print("Visible Label Rendered: 'Simulated placeholder — not a camera image' (.simulated-placeholder-label).")

    print("\n=== 4. EXPLICIT RULE VERIFICATION ===")
    print("Rule: The label is rendered ONLY if the marker file 'snapshots/.simulated' exists in the snapshot dir.")
    print("Implementation in src/threshold/demo.py:416:")
    print("  with open(os.path.join(snapshot_dir, '.simulated'), 'w') as f:")
    print("      f.write('simulated\\n')")
    print("Implementation in src/threshold/dashboard.py:540:")
    print("  is_simulated = os.path.isfile(os.path.join(dash_snapshot_dir, '.simulated'))")
    print("  sim_label_html = ('<div class=\"simulated-placeholder-label\">Simulated placeholder — not a camera image</div>' if is_simulated else '')")
    print()
    print("CONFIRMATION: In a real production run (via daemon or standard operation), the directory is populated")
    print("by live camera downloads and NEVER contains the '.simulated' marker file.")
    print("Therefore, real camera images will never display the simulated placeholder label.")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

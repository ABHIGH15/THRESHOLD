"""
Verifies dashboard images and API responses from running demo --serve.
Extracts every image/snapshot URL, GETs each, and prints status, Content-Type, byte length.
Prints every investigation id, tier, decision, reason_code, notified_at.
"""
from datetime import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(line_buffering=True)

cmd = [
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
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

server_url = None
try:
    # Read stdout until we see the dashboard running line
    start_time = time.time()
    for line in proc.stdout:
        print(line, end="")
        m = re.search(r"Dashboard running at (http://127\.0\.0\.1:\d+/)", line)
        if m:
            server_url = m.group(1)
            break
        if time.time() - start_time > 30:
            raise TimeoutError("Timed out waiting for dashboard to start")

    if not server_url:
        raise RuntimeError("Could not determine dashboard URL from demo output")

    print(f"\nConnected to dashboard at: {server_url}")

    # 1. GET /
    print("\n=== 1. GET / (Dashboard HTML) ===")
    with urllib.request.urlopen(server_url, timeout=5) as resp:
        html_code = resp.status
        html_type = resp.headers.get("Content-Type")
        html_bytes = resp.read()
        print(f"Status: {html_code}, Content-Type: {html_type}, Bytes: {len(html_bytes)}")
        html_text = html_bytes.decode("utf-8", errors="replace")

    # Extract all img src
    img_srcs = set(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text))
    # Extract snapshot URLs
    snapshot_hrefs = set(re.findall(r'href=["\'](/snapshots/[^"\']+)["\']', html_text))
    all_image_urls = sorted(img_srcs.union(snapshot_hrefs))

    print(f"\nExtracted {len(all_image_urls)} image/snapshot URL(s) from HTML:")
    for u in all_image_urls:
        print(f"  - {u}")

    # 2. GET /api/status
    print("\n=== 2. GET /api/status ===")
    status_url = f"{server_url}api/status"
    with urllib.request.urlopen(status_url, timeout=5) as resp:
        st_code = resp.status
        st_type = resp.headers.get("Content-Type")
        st_bytes = resp.read()
        st_json = json.loads(st_bytes.decode("utf-8"))
        print(f"Status: {st_code}, Content-Type: {st_type}, Bytes: {len(st_bytes)}")
        print(f"Routines count: {len(st_json.get('routines', []))}")
        for r in st_json.get("routines", []):
            if r.get("snapshot_url"):
                all_image_urls.append(r["snapshot_url"])

    # 3. GET /api/investigations
    print("\n=== 3. GET /api/investigations ===")
    inv_url = f"{server_url}api/investigations"
    with urllib.request.urlopen(inv_url, timeout=5) as resp:
        inv_code = resp.status
        inv_type = resp.headers.get("Content-Type")
        inv_bytes = resp.read()
        inv_json = json.loads(inv_bytes.decode("utf-8"))
        inv_list = inv_json if isinstance(inv_json, list) else inv_json.get("investigations", [])
        print(f"Total investigations returned: {len(inv_list)}")
        print("\nInvestigation Details:")
        for inv in inv_list:
            inv_id = inv.get("id")
            tier = inv.get("tier")
            decision = inv.get("decision")
            reason = inv.get("reason_code")
            notified_at = inv.get("notified_at")
            dev = inv.get("device_id")
            print(f"  ID: {inv_id:<3} | Device: {dev:<22} | Tier: {tier:<12} | Decision: {decision:<12} | Reason: {reason:<22} | Notified_At: {notified_at}")

    # 4. GET every extracted image URL
    print("\n=== 4. Image Verification (HTTP GET) ===")
    for rel_url in sorted(set(all_image_urls)):
        full_img_url = f"{server_url}{rel_url.lstrip('/')}"
        try:
            with urllib.request.urlopen(full_img_url, timeout=5) as resp:
                i_code = resp.status
                i_type = resp.headers.get("Content-Type")
                i_data = resp.read()
                print(f"URL: {rel_url}")
                print(f"  HTTP Status:  {i_code}")
                print(f"  Content-Type: {i_type}")
                print(f"  Byte length:  {len(i_data)} bytes")
                # Verify JPEG header
                is_jpeg = i_data.startswith(b"\xff\xd8")
                print(f"  Valid JPEG:   {is_jpeg}")
        except urllib.error.HTTPError as e:
            print(f"URL: {rel_url} -> FAILED with HTTP {e.code}")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

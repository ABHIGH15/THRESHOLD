"""
Proves port collision fallback for demo --serve.
1. Occupies port 8080 with a dummy TCP socket.
2. Starts demo --serve in a subprocess.
3. Verifies that the dashboard detects the busy port, falls back to 8081,
   and prints the new URL.
4. Performs a GET request to the fallback URL to verify service.
"""
import re
import socket
import subprocess
import sys
import time
import urllib.request

print("=== 1. OCCUPYING PORT 8080 ===")
dummy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
dummy_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
dummy_socket.bind(("127.0.0.1", 8080))
dummy_socket.listen(1)
print("Successfully bound dummy socket on 127.0.0.1:8080.")

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
    print("\n=== 2. LAUNCHING demo --serve (WITH 8080 OCCUPIED) ===")
    start_time = time.time()
    for line in proc.stdout:
        if "Starting read-only web dashboard" in line or "Dashboard running at" in line:
            print(line.strip())
        m = re.search(r"Dashboard running at (http://127\.0\.0\.1:(\d+)/)", line)
        if m:
            server_url = m.group(1)
            actual_port = int(m.group(2))
            break
        if time.time() - start_time > 30:
            raise TimeoutError("Timed out waiting for dashboard to start")

    print("\n=== 3. PORT FALLBACK VERIFICATION ===")
    print(f"Fallback dashboard URL detected: {server_url}")
    print(f"Occupied port: 8080")
    print(f"Assigned port: {actual_port}")
    assert actual_port != 8080, f"Expected port != 8080, got {actual_port}"
    print(f"✓ Port fallback succeeded: 8080 was busy, server bound to {actual_port}.")

    print("\n=== 4. HTTP REQUEST TO FALLBACK PORT ===")
    healthz_url = f"{server_url}healthz"
    with urllib.request.urlopen(healthz_url, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        print(f"GET {healthz_url} -> HTTP {resp.status}")
        print(f"Response: {body}")
    print("✓ Fallback dashboard is active and serving traffic.")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    dummy_socket.close()
    print("\nDummy socket closed.")

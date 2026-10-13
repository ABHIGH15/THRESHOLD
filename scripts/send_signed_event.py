#!/usr/bin/env python3
"""
CLI utility to send a signed Ring event to the webhook server.
Computes HMAC-SHA256 signature using the secret loaded from an environment variable.
NEVER accepts the secret as a command-line argument.
"""

import argparse
import os
import sys
import urllib.error
import urllib.request

# Ensure project root is in sys.path when script is executed directly
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.probe_ring import load_env
from src.threshold.webhook_auth import compute_signature


def send_signed_event(json_path: str, url: str, secret_env_name: str) -> int:
    """Read JSON file, sign with secret from environment, and POST to server."""
    # 1. Load local .env if present
    load_env()

    # 2. Read secret from designated environment variable
    secret = os.environ.get(secret_env_name, "").strip()
    if not secret:
        print(f"Error: Environment variable '{secret_env_name}' is missing or empty.", file=sys.stderr)
        return 2

    # 3. Read raw JSON payload from file
    if not os.path.exists(json_path):
        print(f"Error: JSON file '{json_path}' not found.", file=sys.stderr)
        return 1

    with open(json_path, "rb") as f:
        raw_body = f.read()

    # 4. Compute HMAC signature
    signature = compute_signature(raw_body, secret)

    # 5. Send POST request
    req = urllib.request.Request(url, data=raw_body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Signature", signature)
    req.add_header("User-Agent", "Threshold-Simulator/1.0")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            print(f"HTTP {resp.status} - {resp_body.strip()}")
            return 0
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else ""
        print(f"HTTP {e.code} - {err_body.strip()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Network error connecting to {url}: {e.reason}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description="Send an HMAC-signed event to Threshold webhook server")
    parser.add_argument("json_file", help="Path to JSON event payload file")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080/webhooks/ring",
        help="Target webhook URL (default: http://127.0.0.1:8080/webhooks/ring)",
    )
    parser.add_argument(
        "--secret-env",
        default="RING_WEBHOOK_SECRET",
        help="Name of environment variable containing shared secret (default: RING_WEBHOOK_SECRET)",
    )
    args = parser.parse_args()

    exit_code = send_signed_event(args.json_file, args.url, args.secret_env)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

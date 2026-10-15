#!/usr/bin/env python3
"""
Ring Partner API Snapshot Diagnostic & Live Probe Script (scripts/snapshot_probe.py).

Probes Ring Partner API image snapshot endpoints using:
1. 'latest_in_range' over configurable lookback window (default 24h).
2. 'at_timestamp' at current time or explicit target timestamp.

Adheres strictly to Threshold security rules:
- Reads credentials exclusively from environment or .env via load_env.
- Zero secrets or token values printed.
- Pre-signed download URLs sanitized to scheme + host only.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import time
import urllib.parse

from scripts.probe_ring import load_env
from src.threshold.ring_client import MediaUnavailable, RingAPIError, RingClient, TokenExpired


def sanitize_url(url: str) -> str:
    """Sanitize presigned download URL by stripping query string."""
    if not url:
        return ""
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ring Partner API Snapshot Probe")
    parser.add_argument("--device-id", default=None, help="Device ID to query (defaults to first discovered device)")
    parser.add_argument("--lookback-hours", type=int, default=24, help="Lookback window in hours for latest_in_range (default: 24)")
    parser.add_argument("--at-timestamp", type=int, default=None, help="Explicit target epoch ms for at_timestamp probe")
    parser.add_argument("--out", "-o", default=None, help="Optional output path to save downloaded snapshot image")
    parser.add_argument("--timeout", type=float, default=5.0, help="Overall deadline in seconds (default: 5.0)")
    args = parser.parse_args()

    load_env(".env")
    token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
    if not token or not token.strip():
        print("ERROR: RING_ACCESS_TOKEN not found in environment or .env file.", file=sys.stderr)
        return 1

    token = token.strip()
    client = RingClient(access_token=token)

    try:
        devices = client.list_devices()
    except TokenExpired:
        print("ERROR: Ring access token has expired (HTTP 401). Please refresh token.", file=sys.stderr)
        return 1
    except RingAPIError as exc:
        print(f"ERROR: Failed to list devices: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("ERROR: No devices found in Ring account.", file=sys.stderr)
        return 1

    device_id = args.device_id or devices[0].get("id")
    device_desc = devices[0].get("description", "Playground Device")
    short_dev = f"{device_id[:30]}..." if len(device_id) > 30 else device_id

    print("=" * 80)
    print("THRESHOLD RING SNAPSHOT PROBE (scripts/snapshot_probe.py)")
    print("=" * 80)
    print(f"Target Device: {device_desc} ({short_dev})")

    # Mode 1: at_timestamp if explicitly requested
    if args.at_timestamp is not None:
        target_ts = args.at_timestamp
        print(f"\n[Probe] at_timestamp mode: timestamp = {target_ts}")
        try:
            img_bytes = client.get_snapshot(device_id, timestamp_ms=target_ts, timeout=args.timeout)
            magic = " ".join(f"{b:02X}" for b in img_bytes[:4])
            print(f"  Result: Success! HTTP 200, {len(img_bytes)} bytes, magic: {magic}")
            if args.out:
                with open(args.out, "wb") as f:
                    f.write(img_bytes)
                print(f"  Saved image to: {args.out}")
        except MediaUnavailable as exc:
            print(f"  Result: Media unavailable (HTTP {exc.status_code}): {exc.reason}")
        except Exception as exc:
            print(f"  Result: Failed ({exc})")

    # Mode 2: latest_in_range
    lookback = max(1, min(24, args.lookback_hours))
    print(f"\n[Probe] latest_in_range mode: lookback = {lookback}h, deadline = {args.timeout}s")
    try:
        img_bytes, captured_ms = client.get_latest_snapshot(device_id, lookback_hours=lookback, deadline_seconds=args.timeout)
        magic = " ".join(f"{b:02X}" for b in img_bytes[:4])
        cap_str = "(unknown)"
        if captured_ms:
            dt_cap = datetime.fromtimestamp(captured_ms / 1000.0, tz=timezone.utc)
            now_dt = datetime.now(timezone.utc)
            age_sec = max(0, int((now_dt - dt_cap).total_seconds()))
            age_str = f"{age_sec // 60}m ago" if age_sec < 3600 else f"{age_sec // 3600}h ago"
            cap_str = f"{dt_cap.strftime('%Y-%m-%d %H:%M:%S UTC')} ({age_str})"

        print(f"  Result: Success! HTTP 200, {len(img_bytes)} bytes, magic: {magic}")
        print(f"  Capture Timestamp: {cap_str}")
        if args.out:
            with open(args.out, "wb") as f:
                f.write(img_bytes)
            print(f"  Saved image to: {args.out}")
        return 0
    except MediaUnavailable as exc:
        if exc.status_code == 416 or (exc.code and "416" in exc.code) or "416" in str(exc) or "MEDIA_NOT_FOUND" in str(exc):
            print(f"  Result: No recordings found in range (HTTP 416 Media Not Found)")
        else:
            print(f"  Result: Media unavailable (HTTP {exc.status_code}): {exc.reason}")
        return 0
    except Exception as exc:
        print(f"  Result: Failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

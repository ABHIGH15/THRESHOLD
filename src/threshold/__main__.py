"""
Threshold CLI Entry Point (python3 -m src.threshold).

Provides administrative and diagnostic subcommands:
- ring-check: Polls devices, status, and recent history from Ring Partner API with token redaction.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import sys

from scripts.probe_ring import load_env
from src.threshold.ring_client import RingAPIError, RingClient, TokenExpired
from src.threshold.store import ms_to_iso


def run_ring_check() -> int:
    """Execute ring-check diagnostic subcommand against Ring Partner API."""
    load_env(".env")

    token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
    if not token or not token.strip():
        print("ERROR: RING_ACCESS_TOKEN is not set in environment or .env file.", file=sys.stderr)
        print("Please configure RING_ACCESS_TOKEN in .env or run with a valid Playground token.", file=sys.stderr)
        return 1

    token = token.strip()
    client = RingClient(access_token=token)

    print("=" * 80)
    print("THRESHOLD RING PARTNER API DIAGNOSTIC (ring-check)")
    print("=" * 80)

    try:
        devices = client.list_devices()
    except TokenExpired:
        print("ERROR: Ring access token has expired (HTTP 401). Please generate a fresh token.", file=sys.stderr)
        return 1
    except RingAPIError as exc:
        print(f"ERROR: Failed to list devices: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("No devices discovered for this Ring account.")
        return 0

    print(f"Discovered {len(devices)} device(s):\n")

    for idx, dev in enumerate(devices, start=1):
        dev_id = dev.get("id", "N/A")
        attrs = dev.get("attributes", {})
        dev_name = attrs.get("name", "Unnamed Device")
        print(f"[{idx}] Device: {dev_name}")
        print(f"    ID:   {dev_id}")

        # Device status
        try:
            status_obj = client.get_device_status(dev_id)
            s_attrs = status_obj.get("attributes", {})
            online = s_attrs.get("online", False)
            reported_at = s_attrs.get("reported_at", "N/A")
            state_str = "ONLINE (Healthy)" if online else "OFFLINE"
            print(f"    Status:      {state_str}")
            print(f"    Reported At: {reported_at}")
        except Exception as e:
            print(f"    Status:      Failed to fetch ({e})")

        # Recent history
        try:
            history = client.get_event_history(dev_id)
            print(f"    Recent Events ({len(history)} recorded):")
            if not history:
                print("      (No events recorded)")
            else:
                for ev in history[:5]:  # show up to 5 recent events
                    ev_id = ev.get("id", "")
                    short_id = ev_id[-16:] if len(ev_id) > 16 else ev_id
                    ev_attrs = ev.get("attributes", {})
                    etype = ev_attrs.get("event_type", "unknown")
                    start_ms = ev_attrs.get("start")
                    start_str = ms_to_iso(start_ms) if start_ms else "N/A"
                    print(f"      - {start_str} | type: {etype} (id: ...{short_id})")
        except Exception as e:
            print(f"    Events:      Failed to fetch ({e})")

        print("-" * 80)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m src.threshold",
        description="Threshold Command-Line Tools & Diagnostics",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # ring-check
    subparsers.add_parser(
        "ring-check",
        help="Check Ring Partner API devices, status, and recent event history",
    )

    args = parser.parse_args()

    if args.subcommand == "ring-check":
        return run_ring_check()
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Ring API Probe — Stage One verification script.

Probe only: no abstractions, no client classes.
Stdlib only: urllib, json, argparse, sys, os, datetime.

Does not print or leak secrets. Redacts access token in all saved evidence.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "https://api.amazonvision.com"


def parse_env_file(filepath=".env"):
    """
    Parse a KEY=VALUE .env file without overriding existing environment variables.
    Ignores comments and blank lines, strips surrounding quotes. Never prints values.
    """
    if not os.path.exists(filepath):
        return {}
    loaded = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    if key and key not in os.environ and key not in loaded:
                        loaded[key] = val
    except Exception as e:
        print(f"Warning: could not read {filepath}: {e}", file=sys.stderr)
    return loaded


def load_env(filepath=".env"):
    """Load variables from .env into os.environ if not already present."""
    parsed = parse_env_file(filepath)
    for k, v in parsed.items():
        if k not in os.environ:
            os.environ[k] = v


def redact_string(val, token):
    """Redact token string from text."""
    if not val or not token:
        return val
    return val.replace(token, "[REDACTED]")


def redact_data(obj, token):
    """Recursively redact token and Authorization headers from nested objects."""
    if isinstance(obj, str):
        return redact_string(obj, token)
    elif isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if str(k).lower() == "authorization":
                res[k] = "[REDACTED]"
            else:
                res[k] = redact_data(v, token)
        return res
    elif isinstance(obj, list):
        return [redact_data(item, token) for item in obj]
    return obj


def detect_image_format(raw_bytes):
    """Detect image format using magic bytes."""
    if not raw_bytes:
        return None, None
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "JPEG", "jpg"
    elif raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG", "png"
    return None, "bin"


def find_component_values(evidence_dir):
    """
    Search evidence JSON files for any confirmed 'component' values.
    Returns list of discovered component strings. Never invents values.
    """
    components = []
    if not os.path.exists(evidence_dir):
        return components
    for fname in sorted(os.listdir(evidence_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(evidence_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)

            def _find(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if "component" in str(k).lower():
                            if isinstance(v, list):
                                components.extend([str(item) for item in v if item])
                            elif isinstance(v, (str, int)):
                                components.append(str(v))
                        _find(v)
                elif isinstance(obj, list):
                    for item in obj:
                        _find(item)

            _find(data)
        except Exception:
            continue
    # De-duplicate while preserving order
    return list(dict.fromkeys(components))


def perform_call(method, url, token, step_name, evidence_dir, req_body_str=None):
    """
    Execute HTTP request, handle status codes, save redacted evidence, and return parsed summary.
    Does not crash on non-200 responses; records error details and continues.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    req_data = req_body_str.encode("utf-8") if req_body_str is not None else None
    req = urllib.request.Request(url, data=req_data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "Threshold-Probe/1.0")
    if req_body_str is not None:
        req.add_header("Content-Type", "application/json")

    status = None
    resp_headers = {}
    raw_body_bytes = b""
    error_msg = None

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            resp_headers = dict(resp.headers.items())
            raw_body_bytes = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = dict(e.headers.items()) if e.headers else {}
        try:
            raw_body_bytes = e.read()
        except Exception:
            raw_body_bytes = b""

        if status == 401:
            error_msg = "token expired - generate a new Playground token"
        elif status == 403:
            error_msg = "403 Forbidden - check token permissions"
        elif status == 404:
            error_msg = "404 Not Found"
        elif status == 429:
            error_msg = "429 Rate limited - slow down or retry later"
        else:
            error_msg = f"HTTP {status} error"
    except urllib.error.URLError as e:
        status = "NETWORK_ERROR"
        error_msg = f"Network error: {e.reason}"
    except Exception as e:
        status = "ERROR"
        error_msg = f"Unexpected error: {e}"

    if error_msg:
        print(f"[{step_name}] {error_msg}", file=sys.stderr)

    content_type = resp_headers.get("Content-Type", "")
    magic_format, magic_ext = detect_image_format(raw_body_bytes)
    is_image = "image" in content_type.lower() or magic_format is not None
    image_saved = None

    # Handle image payload on successful status
    if is_image and raw_body_bytes and status == 200:
        ext = magic_ext if magic_ext else ("jpg" if "jpeg" in content_type.lower() or "jpg" in content_type.lower() else "png" if "png" in content_type.lower() else "bin")
        img_filename = f"{step_name}_{ts}.{ext}"
        img_path = os.path.join(evidence_dir, img_filename)
        try:
            with open(img_path, "wb") as f_img:
                f_img.write(raw_body_bytes)
            image_saved = img_filename
        except Exception as e:
            print(f"Failed to save image bytes: {e}", file=sys.stderr)

    # Attempt to parse body as text or JSON
    raw_body_text = None
    body_json = None
    if not (is_image and status == 200):
        try:
            raw_body_text = raw_body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw_body_text = f"<binary body {len(raw_body_bytes)} bytes>"

        if raw_body_text and not raw_body_text.startswith("<binary"):
            try:
                body_json = json.loads(raw_body_text)
            except Exception:
                body_json = None

    # Redact records for evidence file
    redacted_url = redact_string(url, token)
    redacted_headers = redact_data(resp_headers, token)
    redacted_body_json = redact_data(body_json, token) if body_json is not None else None
    redacted_body_text = redact_string(raw_body_text, token) if raw_body_text else None
    redacted_req_body = redact_string(req_body_str, token) if req_body_str else None

    evidence_record = {
        "step": step_name,
        "timestamp_utc": ts,
        "method": method,
        "url": redacted_url,
        "request_body": redacted_req_body,
        "status": status,
        "error_message": error_msg,
        "response_headers": redacted_headers,
        "content_type": content_type,
        "byte_size": len(raw_body_bytes),
        "magic_format": magic_format,
        "body_json": redacted_body_json,
        "raw_body": redacted_body_text if not (is_image and status == 200) else None,
        "image_file": image_saved,
    }

    evidence_json_path = os.path.join(evidence_dir, f"{step_name}_{ts}.json")
    try:
        with open(evidence_json_path, "w", encoding="utf-8") as f_ev:
            json.dump(evidence_record, f_ev, indent=2)
    except Exception as e:
        print(f"Failed to write evidence file {evidence_json_path}: {e}", file=sys.stderr)

    # Metrics for summary table
    keys_str = "None"
    dev_count = "-"
    event_types_str = "-"

    if body_json and isinstance(body_json, dict):
        keys_str = ",".join(list(body_json.keys())[:5])
        data_field = body_json.get("data")
        if step_name == "list_devices":
            if isinstance(data_field, list):
                dev_count = str(len(data_field))
            elif isinstance(data_field, dict):
                dev_count = "1"
        elif step_name == "event_history":
            if isinstance(data_field, list):
                types = set()
                for item in data_field:
                    if isinstance(item, dict):
                        attrs = item.get("attributes", {})
                        et = attrs.get("event_type") or item.get("event_type")
                        if et:
                            types.add(str(et))
                event_types_str = ",".join(sorted(list(types))[:4]) if types else "none_found"
        elif step_name == "device_status":
            if isinstance(data_field, dict):
                attrs = data_field.get("attributes", {})
                online = attrs.get("online")
                if online is not None:
                    dev_count = f"online:{online}"
        elif step_name == "device_capabilities":
            if isinstance(data_field, dict):
                attrs = data_field.get("attributes", {})
                caps = attrs.get("capabilities") or list(attrs.keys())
                event_types_str = f"{len(caps)} caps"
        elif step_name == "user_profile":
            if isinstance(data_field, dict):
                dev_count = str(data_field.get("id") or "user")

    if step_name.startswith("snapshot"):
        if magic_format:
            event_types_str = f"{magic_format} ({len(raw_body_bytes) // 1024} KB)"
        elif image_saved:
            event_types_str = f"Image ({len(raw_body_bytes) // 1024} KB)"
        elif status == 200:
            event_types_str = f"200 non-image ({len(raw_body_bytes)} B)"
        elif status:
            event_types_str = f"HTTP {status}"

    return {
        "step": step_name,
        "status": status,
        "top_keys": keys_str,
        "device_count": dev_count,
        "event_types": event_types_str,
        "body_json": body_json,
    }


def main():
    parser = argparse.ArgumentParser(description="Ring API Probe")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Base API URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument(
        "--snapshot-path",
        default=None,
        help="Snapshot API endpoint path. Use {id} or {device_id} to substitute first device ID (e.g. /v1/devices/{id}/media/image/download)"
    )
    parser.add_argument(
        "--snapshot-method",
        default="GET",
        choices=["GET", "POST"],
        help="HTTP method for snapshot endpoint (default: GET)"
    )
    parser.add_argument(
        "--snapshot-body",
        default=None,
        help="Optional JSON body string for snapshot request (e.g. '{}' or '{\"components\": [\"cam\"]}')"
    )
    parser.add_argument(
        "--snapshot-doc",
        action="store_true",
        help="Convenience mode: POST to /v1/devices/{id}/media/image/download with at most 2 attempts: (a) body '{}', (b) only if components found in evidence"
    )
    parser.add_argument("--evidence-dir", default="evidence/ring", help="Output evidence directory")
    args = parser.parse_args()

    # 1. Load .env
    load_env()

    # 2. Check token
    token = os.environ.get("RING_ACCESS_TOKEN", "").strip()
    if not token:
        print("RING_ACCESS_TOKEN missing - generate a Playground token and put it in .env")
        sys.exit(2)

    os.makedirs(args.evidence_dir, exist_ok=True)
    summary_rows = []

    # Step (a): List devices
    list_url = f"{args.base_url.rstrip('/')}/v1/devices"
    print(f"\n[1/6] Probing devices: GET {list_url}")
    dev_summary = perform_call("GET", list_url, token, "list_devices", args.evidence_dir)
    summary_rows.append(dev_summary)

    first_device_id = None
    if dev_summary.get("body_json") and isinstance(dev_summary["body_json"], dict):
        data = dev_summary["body_json"].get("data")
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            first_device_id = data[0].get("id")
        elif isinstance(data, dict):
            first_device_id = data.get("id")

    # Step (b): Event history
    if first_device_id:
        history_url = f"{args.base_url.rstrip('/')}/v1/history/devices/{first_device_id}/events"
        print(f"[2/6] Probing event history for device {first_device_id}: GET {history_url}")
        hist_summary = perform_call("GET", history_url, token, "event_history", args.evidence_dir)
        summary_rows.append(hist_summary)
    else:
        print("[2/6] SKIPPED: No device ID available from list_devices to probe event history", file=sys.stderr)
        summary_rows.append({
            "step": "event_history",
            "status": "SKIPPED_NO_DEVICE",
            "top_keys": "-",
            "device_count": "0",
            "event_types": "-",
            "body_json": None,
        })

    # Step (d): Device status
    if first_device_id:
        status_url = f"{args.base_url.rstrip('/')}/v1/devices/{first_device_id}/status"
        print(f"[3/6] Probing device status for device {first_device_id}: GET {status_url}")
        status_summary = perform_call("GET", status_url, token, "device_status", args.evidence_dir)
        summary_rows.append(status_summary)
    else:
        print("[3/6] SKIPPED: No device ID available from list_devices to probe device status", file=sys.stderr)
        summary_rows.append({
            "step": "device_status",
            "status": "SKIPPED_NO_DEVICE",
            "top_keys": "-",
            "device_count": "-",
            "event_types": "-",
            "body_json": None,
        })

    # Step (e): Device capabilities
    if first_device_id:
        caps_url = f"{args.base_url.rstrip('/')}/v1/devices/{first_device_id}/capabilities"
        print(f"[4/6] Probing device capabilities for device {first_device_id}: GET {caps_url}")
        caps_summary = perform_call("GET", caps_url, token, "device_capabilities", args.evidence_dir)
        summary_rows.append(caps_summary)
    else:
        print("[4/6] SKIPPED: No device ID available from list_devices to probe device capabilities", file=sys.stderr)
        summary_rows.append({
            "step": "device_capabilities",
            "status": "SKIPPED_NO_DEVICE",
            "top_keys": "-",
            "device_count": "-",
            "event_types": "-",
            "body_json": None,
        })

    # Step (f): User profile (does not need device ID)
    user_url = f"{args.base_url.rstrip('/')}/v1/users/me"
    print(f"[5/6] Probing user profile: GET {user_url}")
    user_summary = perform_call("GET", user_url, token, "user_profile", args.evidence_dir)
    summary_rows.append(user_summary)

    # Step (c): Snapshot
    if args.snapshot_doc:
        if first_device_id:
            resolved_doc_url = f"{args.base_url.rstrip('/')}/v1/devices/{first_device_id}/media/image/download"
            print(f"[6/6] Probing snapshot (doc mode) attempt (a) body='{{}}': POST {resolved_doc_url}")
            snap_summary_a = perform_call("POST", resolved_doc_url, token, "snapshot_doc_attempt1", args.evidence_dir, req_body_str="{}")
            summary_rows.append(snap_summary_a)

            # Check if any component values exist in evidence
            comp_values = find_component_values(args.evidence_dir)
            if comp_values:
                comp_body = json.dumps({"components": [comp_values[0]]})
                print(f"      Probing snapshot (doc mode) attempt (b) body='{comp_body}': POST {resolved_doc_url}")
                snap_summary_b = perform_call("POST", resolved_doc_url, token, "snapshot_doc_attempt2", args.evidence_dir, req_body_str=comp_body)
                summary_rows.append(snap_summary_b)
            else:
                print("      [snapshot_doc] No component values found in evidence — skipping attempt (b)")
        else:
            print("[6/6] SKIPPED snapshot: No device ID available from list_devices", file=sys.stderr)
            summary_rows.append({
                "step": "snapshot_doc",
                "status": "SKIPPED_NO_DEVICE",
                "top_keys": "-",
                "device_count": "-",
                "event_types": "-",
                "body_json": None,
            })
    elif args.snapshot_path:
        snap_path = args.snapshot_path
        if first_device_id:
            snap_path = snap_path.replace("{device_id}", str(first_device_id)).replace("{id}", str(first_device_id))
        snap_url = snap_path if snap_path.startswith("http") else f"{args.base_url.rstrip('/')}/{snap_path.lstrip('/')}"
        method = args.snapshot_method.upper()
        body = args.snapshot_body
        print(f"[6/6] Probing snapshot: {method} {snap_url}")
        snap_summary = perform_call(method, snap_url, token, "snapshot", args.evidence_dir, req_body_str=body)
        summary_rows.append(snap_summary)
    else:
        print("[6/6] SKIPPED: snapshot path not provided (TODO-VERIFY)")
        summary_rows.append({
            "step": "snapshot",
            "status": "SKIPPED_TODO_VERIFY",
            "top_keys": "-",
            "device_count": "-",
            "event_types": "-",
            "body_json": None,
        })

    # Print summary table
    print("\n" + "=" * 80)
    print("RING API PROBE SUMMARY")
    print("=" * 80)
    print(f"{'Step':<24} | {'Status':<10} | {'Top-level Keys':<20} | {'Devices':<12} | {'Event Types / Caps / Media'}")
    print("-" * 80)
    for row in summary_rows:
        print(f"{row['step']:<24} | {str(row['status']):<10} | {row['top_keys']:<20} | {row['device_count']:<12} | {row['event_types']}")
    print("=" * 80)
    print(f"Evidence files saved to: {args.evidence_dir}/\n")


if __name__ == "__main__":
    main()

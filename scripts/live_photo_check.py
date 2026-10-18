#!/usr/bin/env python3
"""
scripts/live_photo_check.py — Proves the real Ring snapshot -> real Telegram photo+caption path.

Supported modes:
  --dry-run (DEFAULT): No network calls. Builds caption/text as deliver_agent_notification
                       would using a placeholder JPEG, prints character counts, and validates HTML.
  --send:              Requires RING_ACCESS_TOKEN and TELEGRAM_* in environment/.env.
                       Retrieves latest snapshot from Ring, saves to snapshots/,
                       builds synthetic Investigation, and delivers through real
                       deliver_agent_notification to Telegram.
"""

import argparse
from datetime import datetime, timezone
import html.parser
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from scripts.probe_ring import load_env
except ImportError:
    def load_env(path: str = ".env") -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

from src.threshold.agent.runner import (
    deliver_agent_notification,
    format_agent_notification_text,
    format_snapshot_caption_prefix,
    truncate_caption,
)
from src.threshold.agent.schemas import Evidence, Investigation
from src.threshold.notifier import TelegramNotifier
from src.threshold.ring_client import MediaUnavailable, RateLimited, RingAPIError, RingClient, TokenExpired

# 149-byte valid 1x1 JPEG bytes for offline dry-run testing
PLACEHOLDER_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00"
    b"\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19"
    b"\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9"
    b"=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00"
    b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03"
    b"\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
)

ALLOWED_TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji"
}


class TelegramHTMLValidator(html.parser.HTMLParser):
    """Validates that HTML contains only allowed Telegram tags and is strictly balanced."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t_lower = tag.lower()
        if t_lower not in ALLOWED_TELEGRAM_TAGS:
            self.errors.append(f"Disallowed HTML tag: <{tag}>")
            return
        self.stack.append(t_lower)

    def handle_endtag(self, tag: str) -> None:
        t_lower = tag.lower()
        if t_lower not in ALLOWED_TELEGRAM_TAGS:
            self.errors.append(f"Disallowed HTML end tag: </{tag}>")
            return
        if not self.stack:
            self.errors.append(f"Unmatched closing tag: </{tag}>")
            return
        expected = self.stack.pop()
        if expected != t_lower:
            self.errors.append(f"Mismatched tag: expected </{expected}>, got </{tag}>")

    def validate(self, html_text: str) -> List[str]:
        self.stack.clear()
        self.errors.clear()
        self.feed(html_text)
        self.close()
        if self.stack:
            for unclosed in reversed(self.stack):
                self.errors.append(f"Unclosed HTML tag: <{unclosed}>")
        return list(self.errors)


def validate_telegram_html(html_text: str) -> Tuple[bool, List[str]]:
    """Validate HTML for Telegram Bot API compatibility."""
    validator = TelegramHTMLValidator()
    errs = validator.validate(html_text)
    return len(errs) == 0, errs


def build_synthetic_investigation_and_event(
    device_id: str,
    snapshot_path: str,
    captured_at_iso: str,
    now: datetime,
    timezone_name: str = "America/Chicago",
) -> Tuple[Dict[str, Any], Investigation]:
    """Construct a clearly labeled synthetic Investigation and Event for testing."""
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc

    clean_iso = captured_at_iso.replace("Z", "+00:00")
    try:
        dt_cap = datetime.fromisoformat(clean_iso)
        if dt_cap.tzinfo is None:
            dt_cap = dt_cap.replace(tzinfo=timezone.utc)
        local_cap = dt_cap.astimezone(tz)
        local_time_str = local_cap.strftime("%H:%M %Z")
    except Exception:
        local_time_str = "00:00 UTC"

    event: Dict[str, Any] = {
        "id": "evt_live_photo_check",
        "device_id": device_id,
        "event_type": "motion_detected",
        "occurred_at": captured_at_iso,
        "timezone": timezone_name,
    }

    evidence_text = f"Snapshot retrieved from Ring for device {device_id} (captured {local_time_str})"
    evidence = [
        Evidence(
            tool="pull_snapshot",
            call_id="call_snapshot_live_check",
            ok=True,
            text=evidence_text,
        )
    ]

    trace = [
        {
            "type": "result",
            "tool": "pull_snapshot",
            "ok": True,
            "data": {
                "path": snapshot_path,
                "captured_at": captured_at_iso,
            },
        }
    ]

    inv = Investigation(
        event_key="evt_live_photo_check",
        mode="live path test (synthetic investigation)",
        provider_name="live_photo_check",
        steps=1,
        elapsed_ms=0,
        tier="check_needed",
        recommendation="LIVE PATH TEST — not a real event",
        evidence=evidence,
        fallback=False,
        trace=trace,
    )

    return event, inv


def run_dry_run(
    device_id: str = "demo-cam-front-door",
    timezone_name: str = "America/Chicago",
    snapshots_dir: str = "snapshots",
) -> int:
    """Execute offline dry-run validating HTML and character counts without network calls."""
    os.makedirs(snapshots_dir, exist_ok=True)
    placeholder_path = os.path.join(snapshots_dir, "dry_run_placeholder.jpg")
    with open(placeholder_path, "wb") as f:
        f.write(PLACEHOLDER_JPEG_BYTES)

    now = datetime.now(timezone.utc)
    captured_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    event, inv = build_synthetic_investigation_and_event(
        device_id=device_id,
        snapshot_path=placeholder_path,
        captured_at_iso=captured_iso,
        now=now,
        timezone_name=timezone_name,
    )

    message_text = format_agent_notification_text(event, inv, timezone_name)
    captured_ms = int(now.timestamp() * 1000)
    caption_prefix = format_snapshot_caption_prefix(captured_ms, timezone_name, now=now)
    full_caption_raw = f"{caption_prefix}\n\n{message_text}"

    # Validate HTML
    is_valid_text, text_errs = validate_telegram_html(message_text)
    is_valid_caption, caption_errs = validate_telegram_html(full_caption_raw)

    print("=" * 70)
    print("THRESHOLD LIVE PHOTO CHECK — DRY RUN (NO NETWORK)")
    print("=" * 70)
    print(f"Device ID:          {device_id}")
    print(f"Timezone:           {timezone_name}")
    print(f"Placeholder JPEG:   {len(PLACEHOLDER_JPEG_BYTES)} bytes ({placeholder_path})")
    print(f"Caption Prefix:     {len(caption_prefix)} chars")
    print(f"Full Caption:       {len(full_caption_raw)} chars (limit: 1024)")
    print(f"Separate Text:      {len(message_text)} chars (limit: 4096)")

    if len(full_caption_raw) > 1000:
        branch = "Over-limit branch (sendPhoto with <=200 char prefix + separate sendMessage)"
    else:
        branch = "Standard branch (sendPhoto with full caption <=1000 chars)"
    print(f"Delivery Branch:    {branch}")

    if is_valid_text and is_valid_caption:
        print("HTML Validation:    PASS (all tags allowed by Telegram and balanced)")
    else:
        print("HTML Validation:    FAIL", file=sys.stderr)
        for err in text_errs + caption_errs:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("\n--- FORMATTED MESSAGE PREVIEW ---")
    print(message_text)
    print("---------------------------------")
    print("\nResult: DRY RUN PASSED (exit code 0)")
    return 0


def run_send(
    device_id: Optional[str] = None,
    hours: int = 24,
    timezone_name: str = "America/Chicago",
    env_file: str = ".env",
    snapshots_dir: str = "snapshots",
    ring_client: Optional[RingClient] = None,
    notifier: Optional[Any] = None,
) -> int:
    """Execute live snapshot retrieval and Telegram delivery with strict secret hygiene."""
    load_env(env_file)

    ring_token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
    tg_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    missing = []
    if not ring_token and ring_client is None:
        missing.append("RING_ACCESS_TOKEN")
    if not tg_bot_token and notifier is None:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not tg_chat_id and notifier is None:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        print(f"ERROR: Missing required configuration variables: {', '.join(missing)}", file=sys.stderr)
        print("Please configure them in your environment or .env file.", file=sys.stderr)
        return 1

    client = ring_client or RingClient(access_token=ring_token.strip())

    # 1. Resolve device ID
    target_device = device_id
    if not target_device:
        try:
            devices = client.list_devices()
            if not devices:
                print("ERROR: No Ring devices found on account.", file=sys.stderr)
                return 1
            target_device = devices[0].get("id")
            if not target_device:
                print("ERROR: First device returned has no 'id' attribute.", file=sys.stderr)
                return 1
        except TokenExpired:
            print("ERROR: Ring access token expired (HTTP 401). Please refresh token in .env.", file=sys.stderr)
            return 1
        except RingAPIError as e:
            print(f"ERROR: Failed to list Ring devices: {e}", file=sys.stderr)
            return 1

    print(f"Target Device:      {target_device}")
    print(f"Lookback Window:    {hours} hour(s)")

    # 2. Retrieve snapshot
    os.makedirs(snapshots_dir, exist_ok=True)
    img_bytes: Optional[bytes] = None
    captured_at_ms: Optional[int] = None

    try:
        if hasattr(client, "get_latest_snapshot"):
            img_bytes, captured_at_ms = client.get_latest_snapshot(
                device_id=target_device,
                lookback_hours=hours,
                deadline_seconds=10.0,
            )
        elif hasattr(client, "get_snapshot"):
            now_ms = int(time.time() * 1000)
            img_bytes = client.get_snapshot(device_id=target_device, timestamp_ms=now_ms)
            captured_at_ms = now_ms
    except MediaUnavailable as e:
        err_msg = str(e)
        if "416" in err_msg or getattr(e, "status_code", None) == 416:
            print("simulate live view in the Playground, wait ~2 minutes, retry.", file=sys.stderr)
        else:
            print(f"Media unavailable from Ring camera: {err_msg}", file=sys.stderr)
        return 1
    except TokenExpired:
        print("ERROR: Ring access token expired (HTTP 401). Please refresh token in .env.", file=sys.stderr)
        return 1
    except RateLimited:
        print("ERROR: Ring API rate limit exceeded (HTTP 429).", file=sys.stderr)
        return 1
    except Exception as e:
        err_msg = str(e)
        if "416" in err_msg:
            print("simulate live view in the Playground, wait ~2 minutes, retry.", file=sys.stderr)
        else:
            print(f"ERROR: Failed to retrieve snapshot: {err_msg}", file=sys.stderr)
        return 1

    if not img_bytes:
        print("ERROR: Received 0-byte snapshot response from camera.", file=sys.stderr)
        return 1

    print(f"Snapshot Retrieved: {len(img_bytes)} bytes")

    # 3. Save to disk under snapshots/
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y%m%d_%H%M%S")
    sanitized_id = re.sub(r"[^A-Za-z0-9._-]", "_", target_device)
    snapshot_filename = f"snapshot_{sanitized_id}_{ts_str}.jpg"
    snapshot_path = os.path.join(snapshots_dir, snapshot_filename)

    with open(snapshot_path, "wb") as f:
        f.write(img_bytes)
    print(f"Saved Snapshot:     {snapshot_path}")

    # Determine captured ISO
    if captured_at_ms:
        captured_dt = datetime.fromtimestamp(captured_at_ms / 1000.0, tz=timezone.utc)
        captured_iso = captured_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        captured_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 4. Build synthetic event & investigation
    event, inv = build_synthetic_investigation_and_event(
        device_id=target_device,
        snapshot_path=snapshot_path,
        captured_at_iso=captured_iso,
        now=now,
        timezone_name=timezone_name,
    )

    # 5. Deliver through real deliver_agent_notification
    delivery_notifier = notifier or TelegramNotifier(
        bot_token=tg_bot_token.strip(),
        chat_id=str(tg_chat_id).strip(),
    )

    delivered = deliver_agent_notification(
        notifier=delivery_notifier,
        event=event,
        investigation=inv,
        now=now,
        timezone_name=timezone_name,
    )

    if delivered:
        print("Telegram Dispatch:  SUCCESS (delivered=True)")
        print("\nResult: LIVE SEND PASSED (exit code 0)")
        return 0
    else:
        print("Telegram Dispatch:  FAILED (delivered=False)", file=sys.stderr)
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for scripts/live_photo_check.py."""
    parser = argparse.ArgumentParser(
        description="Verify Ring-to-Telegram photo and caption pipeline by hand."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run offline verification with placeholder JPEG and HTML validation (default mode).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        default=False,
        help="Execute live Ring snapshot retrieval and Telegram delivery.",
    )
    parser.add_argument(
        "--device-id",
        type=str,
        default=None,
        help="Optional target device ID (defaults to first device from Ring account).",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Lookback hours for Ring latest_in_range snapshot query (default: 24).",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="America/Chicago",
        help="Timezone for caption timestamp formatting (default: America/Chicago).",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=".env",
        help="Path to .env file for secrets (default: .env).",
    )
    parser.add_argument(
        "--snapshots-dir",
        type=str,
        default="snapshots",
        help="Directory to save downloaded snapshots (default: snapshots).",
    )

    args = parser.parse_args(argv)

    if args.send:
        return run_send(
            device_id=args.device_id,
            hours=args.hours,
            timezone_name=args.timezone,
            env_file=args.env_file,
            snapshots_dir=args.snapshots_dir,
        )
    else:
        return run_dry_run(
            device_id=args.device_id or "demo-cam-front-door",
            timezone_name=args.timezone,
            snapshots_dir=args.snapshots_dir,
        )


if __name__ == "__main__":
    sys.exit(main())

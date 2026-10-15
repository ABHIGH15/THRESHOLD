"""
Threshold Notifier Module (src/threshold/notifier.py).
Provides notification interfaces, console, recording (test), and Telegram notifiers.
Includes escalation ladders, Telegram inline keyboard buttons, callback polling,
chat isolation, and strict token redaction across logs and error messages.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.error
import urllib.request
import uuid

from src.threshold.models import Routine
from src.threshold.store import (
    ack_escalation,
    get_app_state,
    get_routine,
    record_correction,
    set_app_state,
)

logger = logging.getLogger(__name__)


def redact_telegram_token(text: str, token: Optional[str] = None) -> str:
    """
    Redact Telegram bot token from URL paths, query parameters, or message strings.
    Replaces /bot<TOKEN>/ with /bot[REDACTED]/ and direct token strings with [REDACTED].
    """
    if not text:
        return text
    result = text
    if token:
        result = result.replace(token, "[REDACTED]")
    # Match standard /bot<TOKEN>/ pattern in URLs
    result = re.sub(r"/bot[^/\s]+/", "/bot[REDACTED]/", result)
    return result


def format_expected_by(routine: Routine) -> str:
    """Format expected-by deadline in the routine's local timezone."""
    try:
        parts = routine.window_end.split(":")
        end_hour = int(parts[0])
        end_min = int(parts[1])
        total_mins = end_hour * 60 + end_min + getattr(routine, "escalation_delay_minutes", 30)
        dead_h = (total_mins // 60) % 24
        dead_m = total_mins % 60
        return f"{dead_h:02d}:{dead_m:02d} {routine.timezone} (window {routine.window_start}-{routine.window_end} + {routine.escalation_delay_minutes}m delay)"
    except Exception:
        return f"{routine.window_end} {routine.timezone}"


def format_health_state(routine: Routine, device_health: Optional[Any] = None) -> str:
    """Format device health state string."""
    if device_health is not None:
        if hasattr(device_health, "last_event_at") and device_health.last_event_at is not None:
            return f"Online / Healthy ({routine.device_id})"
        return f"Unknown / Stale ({routine.device_id})"
    return f"Online / Healthy ({routine.device_id})"


def format_fired_at(fired_at: Union[str, datetime], timezone_name: str) -> str:
    """Format fired_at timestamp in routine's local timezone, e.g. '09:31 CDT (2026-10-12)'."""
    try:
        from zoneinfo import ZoneInfo
        if isinstance(fired_at, str):
            clean_str = fired_at.replace("Z", "+00:00").replace(" UTC", "+00:00")
            dt = datetime.fromisoformat(clean_str)
        else:
            dt = fired_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(timezone_name)
        dt_local = dt.astimezone(tz)
        return dt_local.strftime("%H:%M %Z (%Y-%m-%d)")
    except Exception:
        return str(fired_at)


def format_last_activity(routine: Routine, device_health: Optional[Any] = None) -> str:
    """
    Format last recorded activity timestamp in routine's local timezone.
    For a routine with no qualifying event in its window:
    'None recorded in window (07:00-09:00 CDT)'
    """
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(routine.timezone)
        now_local = datetime.now(timezone.utc).astimezone(tz)
        tz_abbr = now_local.strftime("%Z")
    except Exception:
        tz_abbr = routine.timezone

    window_str = f"None recorded in window ({routine.window_start}-{routine.window_end} {tz_abbr})"

    # If routine has an actual satisfied activity, format it in the routine's local timezone
    if routine.last_satisfied_at:
        try:
            tz = ZoneInfo(routine.timezone)
            sat_dt = routine.last_satisfied_at
            if sat_dt.tzinfo is None:
                sat_dt = sat_dt.replace(tzinfo=timezone.utc)
            sat_local = sat_dt.astimezone(tz)
            return sat_local.strftime("%H:%M %Z (%Y-%m-%d)")
        except Exception:
            return routine.last_satisfied_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    return window_str


def format_snapshot_caption_prefix(
    captured_at_ms: Optional[int],
    timezone_name: str,
    now: Optional[datetime] = None,
) -> str:
    """
    Format photo caption prefix according to C2b specification:
    - If captured_at_ms is present:
      'Most recent camera image, taken HH:MM TZ (N min/h ago). Not live.'
    - If captured_at_ms is None or invalid:
      'Most recent camera image (capture time unknown). Not live.'
    Times in the routine's local timezone.
    """
    if captured_at_ms is None:
        return "Most recent camera image (capture time unknown). Not live."

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        dt_utc = datetime.fromtimestamp(captured_at_ms / 1000.0, tz=timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        time_str = dt_local.strftime("%H:%M %Z")

        diff_secs = max(0, int((now - dt_utc).total_seconds()))
        if diff_secs < 60:
            age_str = "just now"
        elif diff_secs < 3600:
            mins = diff_secs // 60
            age_str = f"{mins} min ago" if mins != 1 else "1 min ago"
        elif diff_secs < 86400:
            hrs = diff_secs // 3600
            age_str = f"{hrs} h ago" if hrs != 1 else "1 h ago"
        else:
            days = diff_secs // 86400
            age_str = f"{days} d ago" if days != 1 else "1 d ago"

        return f"Most recent camera image, taken {time_str} ({age_str}). Not live."
    except Exception:
        return "Most recent camera image (capture time unknown). Not live."


def format_escalation_text(
    routine: Routine,
    fired_at: Union[str, datetime],
    device_health: Optional[Any] = None,
) -> str:
    """Format Telegram alert message text for an escalation."""
    expected_by = format_expected_by(routine)
    health_state = format_health_state(routine, device_health)
    last_act = format_last_activity(routine, device_health)
    fired_str = format_fired_at(fired_at, routine.timezone)

    return (
        f"🚨 <b>THRESHOLD ESCALATION</b>\n\n"
        f"Routine: <b>{routine.name}</b>\n"
        f"Expected by: {expected_by}\n"
        f"Device State: {health_state}\n"
        f"Last Activity: {last_act}\n"
        f"Fired at: {fired_str}\n\n"
        f"<i>Please note: this is an automated message notification, not a phone call.</i>"
    )


def resolve_callback_routine_id(
    conn: sqlite3.Connection,
    candidate_id: Optional[str],
) -> Optional[str]:
    """
    Resolve callback routine_id via exact match in routines or 16-character SHA256 prefix hash.
    Strictly returns None if the routine cannot be resolved by exact ID or hash match.
    Zero fallback to active/unacknowledged escalations.
    """
    if not candidate_id:
        return None

    # 1. Direct match in routines table
    if get_routine(conn, candidate_id) is not None:
        return candidate_id

    # 2. Check if candidate_id is a 16-character sha256 hash prefix of any routine
    if len(candidate_id) == 16:
        cur = conn.execute("SELECT routine_id FROM routines")
        for row in cur.fetchall():
            real_id = row["routine_id"]
            if hashlib.sha256(real_id.encode("utf-8")).hexdigest()[:16] == candidate_id:
                return real_id

        # Also check distinct routine_ids in escalations table
        cur = conn.execute("SELECT DISTINCT routine_id FROM escalations")
        for row in cur.fetchall():
            real_id = row["routine_id"]
            if hashlib.sha256(real_id.encode("utf-8")).hexdigest()[:16] == candidate_id:
                return real_id

    return None


def make_callback_data(prefix: str, routine_id: str, local_date: str) -> str:
    """
    Construct inline keyboard callback_data staying strictly at or under 64 bytes.
    Uses compact prefix ('fa' for False alarm, 'ak' for Got it).
    """
    cb = f"{prefix}:{routine_id}:{local_date}"
    if len(cb.encode("utf-8")) <= 64:
        return cb
    # Fallback to deterministic truncated hash if routine_id exceeds length
    hashed = hashlib.sha256(routine_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{hashed}:{local_date}"


class Notifier(ABC):
    """Abstract notification delivery interface."""

    @abstractmethod
    def send_escalation(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> None:
        """Send initial escalation notification. Must raise exception on failure."""
        pass

    @abstractmethod
    def send_reminder(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> None:
        """Send reminder notification for unacknowledged escalation. Must raise exception on failure."""
        pass


class RecordingNotifier(Notifier):
    """In-memory recording notifier for tests, verifying delivery, and simulating failures."""

    def __init__(self, fail_first_n: int = 0):
        self.sent: List[Dict[str, Any]] = []
        self.reminders: List[Dict[str, Any]] = []
        self.fail_first_n = fail_first_n
        self.attempts = 0

    def send_escalation(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError(f"Simulated notification failure (attempt {self.attempts})")
        self.sent.append({
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
            "device_health": device_health,
        })

    def send_reminder(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError(f"Simulated reminder notification failure (attempt {self.attempts})")
        self.reminders.append({
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
            "device_health": device_health,
        })


class ConsoleNotifier(Notifier):
    """Console / stdout notifier for CLI operations and demonstrations."""

    def __init__(self, ring_client: Optional[Any] = None):
        self.ring_client = ring_client

    def send_escalation(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        expected_by = format_expected_by(routine)
        health_state = format_health_state(routine, device_health)
        last_act = format_last_activity(routine, device_health)
        cam_info = ""
        snap_id = getattr(routine, "snapshot_device_id", None)
        if snap_id:
            cam_info = f"\nSnapshot Camera: {snap_id}"
        print(
            f"[ESCALATION]\n"
            f"Routine: {routine.name} ({routine.routine_id})\n"
            f"Expected by: {expected_by}\n"
            f"Device State: {health_state}\n"
            f"Last Activity: {last_act}"
            f"{cam_info}\n"
            f"Fired at: {fired_at} (Date: {local_date})\n"
            f"Note: This is an automated text message notification, not a phone call."
        )
        return {
            "channel": "console",
            "delivered": True,
            "type": "text",
            "has_photo": False,
            "snapshot_path": None,
        }

    def send_reminder(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        expected_by = format_expected_by(routine)
        health_state = format_health_state(routine, device_health)
        last_act = format_last_activity(routine, device_health)
        cam_info = ""
        snap_id = getattr(routine, "snapshot_device_id", None)
        if snap_id:
            cam_info = f"\nSnapshot Camera: {snap_id}"
        print(
            f"[REMINDER: Escalation Unacknowledged]\n"
            f"Routine: {routine.name} ({routine.routine_id})\n"
            f"Expected by: {expected_by}\n"
            f"Device State: {health_state}\n"
            f"Last Activity: {last_act}"
            f"{cam_info}\n"
            f"Originally fired at: {fired_at} (Date: {local_date})\n"
            f"Note: This is an automated text message notification, not a phone call."
        )
        return {
            "channel": "console",
            "delivered": True,
            "type": "text",
            "has_photo": False,
            "snapshot_path": None,
        }


def detect_image_mime(data: bytes) -> str:
    """
    Detect image MIME type from binary magic bytes (pure Python standard library).
    Returns 'image/jpeg', 'image/png', 'image/webp', or 'application/octet-stream'.
    """
    if not data or len(data) < 4:
        return "application/octet-stream"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def truncate_caption(caption: str, max_chars: int = 1024) -> str:
    """
    Ensure Telegram photo caption strictly stays within Telegram's 1024-character limit.
    Appends ellipsis while maintaining HTML tag closure if truncation is necessary.
    """
    if len(caption) <= max_chars:
        return caption
    cut_point = max_chars - 3
    prefix = caption[:cut_point]
    if prefix.count("<i>") > prefix.count("</i>"):
        cut_point = max(0, max_chars - 7)
        return caption[:cut_point] + "...</i>"
    return prefix + "..."


def encode_multipart_form_data(
    fields: Dict[str, Any],
    files: Dict[str, Tuple[str, bytes, str]],
    boundary: Optional[str] = None,
) -> Tuple[bytes, str]:
    """
    Encode form fields and binary file uploads into standard multipart/form-data.
    Zero external dependencies (pure Python standard library).

    fields: dict of {field_name: value}
    files: dict of {field_name: (filename, binary_bytes, mime_type)}

    Returns:
        (body_bytes, content_type_header)
    """
    if not boundary:
        boundary = f"----ThresholdBoundary{uuid.uuid4().hex}"

    body_parts: List[bytes] = []

    # Encode regular string form fields
    for name, value in fields.items():
        if value is None:
            continue
        clean_name = re.sub(r'["\r\n]', '', str(name))
        part = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{clean_name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
        body_parts.append(part)

    # Encode binary file parts
    for name, (filename, data, mime_type) in files.items():
        clean_name = re.sub(r'["\r\n]', '', str(name))
        clean_fn = re.sub(r'["\r\n]', '', str(filename))
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{clean_name}"; filename="{clean_fn}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        body_parts.append(header)
        body_parts.append(data)
        body_parts.append(b"\r\n")

    # Final closing boundary
    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    content_type_header = f"multipart/form-data; boundary={boundary}"
    return b"".join(body_parts), content_type_header


class TelegramNotifier(Notifier):
    """
    Production Telegram Bot Notifier.
    Delivers messages via sendMessage or sendPhoto with inline keyboard ['False alarm', 'Got it'].
    Supports snapshot enrichment from RingClient when cameras are configured.
    Maintains getUpdates offset tracking and processes caretaker callback queries.
    Enforces chat isolation: ignores callback queries from foreign chat IDs.
    Redacts bot tokens from all network calls, logged messages, and error exceptions.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: Union[str, int],
        base_url: str = "https://api.telegram.org",
        timeout: float = 10.0,
        ring_client: Optional[Any] = None,
        snapshot_timeout: float = 5.0,
        snapshot_lookback_hours: int = 24,
        snapshot_deadline_seconds: float = 5.0,
        snapshot_dir: Optional[str] = "snapshots",
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.last_offset: int = 0
        self.ring_client = ring_client
        self.snapshot_lookback_hours = int(snapshot_lookback_hours)
        if snapshot_timeout != 5.0 and snapshot_deadline_seconds == 5.0:
            snapshot_deadline_seconds = snapshot_timeout
        self.snapshot_deadline_seconds = float(snapshot_deadline_seconds)
        self.snapshot_timeout = float(snapshot_timeout)
        self.snapshot_dir = snapshot_dir

    def _redact(self, text: str) -> str:
        """Helper to redact the instance's bot token."""
        return redact_telegram_token(text, self.bot_token)

    def _archive_snapshot(self, routine_id: str, local_date: str, photo_bytes: bytes) -> Optional[str]:
        """Save a copy of snapshot image to local disk for dashboard and audit review."""
        if not self.snapshot_dir:
            return None
        try:
            os.makedirs(self.snapshot_dir, exist_ok=True)
            safe_rt = re.sub(r"[^a-zA-Z0-9_-]", "_", routine_id)
            safe_dt = re.sub(r"[^a-zA-Z0-9_-]", "_", local_date)
            ext = "jpg" if photo_bytes.startswith(b"\xff\xd8") else "png"
            target_path = os.path.join(self.snapshot_dir, f"{safe_rt}_{safe_dt}.{ext}")
            with open(target_path, "wb") as f:
                f.write(photo_bytes)
            latest_path = os.path.join(self.snapshot_dir, f"{safe_rt}_latest.{ext}")
            with open(latest_path, "wb") as f:
                f.write(photo_bytes)
            return target_path
        except Exception as e:
            logger.debug("Failed to archive snapshot locally to %s: %s", self.snapshot_dir, e)
            return None

    def _make_request(self, endpoint: str, payload: dict) -> dict:
        """
        Execute an HTTP POST to Telegram Bot API with token redaction on failures.
        """
        url = f"{self.base_url}/bot{self.bot_token}/{endpoint}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Threshold/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, Exception) as exc:
            detail = str(exc)
            if hasattr(exc, "url") and exc.url:
                detail = f"{detail} ({exc.url})"
            redacted_msg = self._redact(detail)
            logger.error("Telegram API call to %s failed: %s", endpoint, redacted_msg)
            raise RuntimeError(f"Telegram API request to {endpoint} failed: {redacted_msg}") from None

    def _make_multipart_request(
        self,
        endpoint: str,
        fields: Dict[str, Any],
        files: Dict[str, Tuple[str, bytes, str]],
    ) -> dict:
        """
        Execute an HTTP POST with multipart/form-data to Telegram Bot API with token redaction.
        """
        url = f"{self.base_url}/bot{self.bot_token}/{endpoint}"
        body_bytes, content_type = encode_multipart_form_data(fields, files)

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body_bytes)),
                "User-Agent": "Threshold/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, Exception) as exc:
            detail = str(exc)
            if hasattr(exc, "url") and exc.url:
                detail = f"{detail} ({exc.url})"
            redacted_msg = self._redact(detail)
            logger.error("Telegram API multipart call to %s failed: %s", endpoint, redacted_msg)
            raise RuntimeError(f"Telegram API request to {endpoint} failed: {redacted_msg}") from None

    def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        filename: Optional[str] = None,
    ) -> dict:
        """
        Send photo with caption and inline keyboard to Telegram using multipart/form-data.
        Validates image binary format and enforces 1024-character caption limit.
        """
        if not photo_bytes or len(photo_bytes) < 32:
            raise ValueError(
                f"Invalid image binary payload: length is too small ({len(photo_bytes) if photo_bytes else 0} bytes)"
            )

        detected_mime = detect_image_mime(photo_bytes)
        if detected_mime not in ("image/jpeg", "image/png", "image/webp"):
            raise ValueError(
                "Unrecognized image binary format: magic bytes do not match JPEG, PNG, or WebP"
            )

        if not filename:
            ext = "jpg" if detected_mime == "image/jpeg" else ("png" if detected_mime == "image/png" else "webp")
            filename = f"snapshot.{ext}"

        safe_caption = truncate_caption(caption, max_chars=1024)

        fields: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "caption": safe_caption,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup)

        files = {
            "photo": (filename, photo_bytes, detected_mime),
        }
        return self._make_multipart_request("sendPhoto", fields, files)

    def send_escalation(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Send initial escalation notification with inline keyboard:
        ['False alarm', 'Got it'].
        If snapshot_device_id (or device_id) and ring_client are available:
        - Fetches snapshot honoring self.snapshot_timeout (default 4s).
        - Archives snapshot to disk for local audit/dashboard.
        - Delivers via sendPhoto.
        If snapshot fails or client is unavailable:
        - Falls back gracefully to sendMessage (zero alert drops).
        Returns delivery metadata dictionary.
        """
        text = format_escalation_text(routine, fired_at, device_health)

        fa_data = make_callback_data("fa", routine.routine_id, local_date)
        ak_data = make_callback_data("ak", routine.routine_id, local_date)

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "False alarm",
                        "callback_data": fa_data,
                    },
                    {
                        "text": "Got it",
                        "callback_data": ak_data,
                    },
                ]
            ]
        }

        # Attempt snapshot capture if ring_client is available
        target_cam_id = getattr(routine, "snapshot_device_id", None) or getattr(routine, "device_id", None)
        photo_sent = False
        saved_snapshot_path = None
        delivered_caption = text

        if self.ring_client and target_cam_id:
            start_t = time.time()
            try:
                lookback = getattr(self, "snapshot_lookback_hours", 24)
                deadline = getattr(self, "snapshot_deadline_seconds", getattr(self, "snapshot_timeout", 5.0))

                captured_at_ms: Optional[int] = None
                if hasattr(self.ring_client, "get_latest_snapshot"):
                    photo_bytes, captured_at_ms = self.ring_client.get_latest_snapshot(
                        target_cam_id,
                        lookback_hours=lookback,
                        deadline_seconds=deadline,
                    )
                else:
                    # Legacy fallback for older test mocks
                    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                    photo_bytes = self.ring_client.get_snapshot(
                        target_cam_id,
                        timestamp_ms=now_ms,
                        timeout=deadline,
                    )

                if photo_bytes:
                    saved_snapshot_path = self._archive_snapshot(routine.routine_id, local_date, photo_bytes)
                    cap_prefix = format_snapshot_caption_prefix(captured_at_ms, routine.timezone)
                    delivered_caption = f"{cap_prefix}\n\n{text}"
                    self.send_photo(
                        photo_bytes=photo_bytes,
                        caption=delivered_caption,
                        reply_markup=keyboard,
                    )
                    photo_sent = True
                    elapsed_ms = int((time.time() - start_t) * 1000)
                    kb_size = len(photo_bytes) / 1024.0
                    logger.info(
                        "Delivered snapshot-enriched escalation photo (%.1f KB) for routine '%s' in %dms",
                        kb_size,
                        routine.routine_id,
                        elapsed_ms,
                    )
            except Exception as exc:
                logger.warning(
                    "Snapshot capture/delivery failed for routine '%s' (%s): %s. Falling back to text-only alert.",
                    routine.routine_id,
                    target_cam_id,
                    self._redact(str(exc)),
                )

        if not photo_sent:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            }
            self._make_request("sendMessage", payload)

        return {
            "channel": "telegram",
            "delivered": True,
            "type": "photo" if photo_sent else "text",
            "has_photo": photo_sent,
            "snapshot_path": saved_snapshot_path,
            "caption": delivered_caption,
        }

    def send_reminder(
        self,
        routine: Routine,
        local_date: str,
        fired_at: str,
        device_health: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Send reminder notification for unacknowledged escalation with inline keyboard.
        If ring_client and camera are available, also attempts snapshot photo.
        Falls back gracefully to sendMessage.
        Returns delivery metadata dictionary.
        """
        expected_by = format_expected_by(routine)
        health_state = format_health_state(routine, device_health)
        last_act = format_last_activity(routine, device_health)

        text = (
            f"⚠️ <b>THRESHOLD REMINDER: Escalation Unacknowledged</b>\n\n"
            f"Routine: <b>{routine.name}</b>\n"
            f"Expected by: {expected_by}\n"
            f"Device State: {health_state}\n"
            f"Last Activity: {last_act}\n"
            f"Originally fired at: {fired_at}\n\n"
            f"<i>Please note: this is an automated message notification, not a phone call.</i>"
        )

        fa_data = make_callback_data("fa", routine.routine_id, local_date)
        ak_data = make_callback_data("ak", routine.routine_id, local_date)

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "False alarm",
                        "callback_data": fa_data,
                    },
                    {
                        "text": "Got it",
                        "callback_data": ak_data,
                    },
                ]
            ]
        }

        # Attempt snapshot capture for reminder as well
        target_cam_id = getattr(routine, "snapshot_device_id", None) or getattr(routine, "device_id", None)
        photo_sent = False
        saved_snapshot_path = None

        if self.ring_client and target_cam_id:
            start_t = time.time()
            try:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                photo_bytes = self.ring_client.get_snapshot(
                    target_cam_id,
                    timestamp_ms=now_ms,
                    timeout=self.snapshot_timeout,
                )
                if photo_bytes:
                    saved_snapshot_path = self._archive_snapshot(routine.routine_id, local_date, photo_bytes)
                    self.send_photo(
                        photo_bytes=photo_bytes,
                        caption=text,
                        reply_markup=keyboard,
                    )
                    photo_sent = True
                    elapsed_ms = int((time.time() - start_t) * 1000)
                    kb_size = len(photo_bytes) / 1024.0
                    logger.info(
                        "Delivered snapshot-enriched reminder photo (%.1f KB) for routine '%s' in %dms",
                        kb_size,
                        routine.routine_id,
                        elapsed_ms,
                    )
            except Exception as exc:
                logger.warning(
                    "Snapshot capture/delivery failed for reminder on routine '%s' (%s): %s. Falling back to text-only alert.",
                    routine.routine_id,
                    target_cam_id,
                    self._redact(str(exc)),
                )

        if not photo_sent:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            }
            self._make_request("sendMessage", payload)

        return {
            "channel": "telegram",
            "delivered": True,
            "type": "photo" if photo_sent else "text",
            "has_photo": photo_sent,
            "snapshot_path": saved_snapshot_path,
            "caption": text,
        }

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> List[Dict[str, Any]]:
        """
        Poll updates from Telegram with offset tracking.
        Default timeout=0 ensures non-blocking short poll.
        """
        req_offset = self.last_offset if offset is None else offset
        payload = {
            "offset": req_offset,
            "timeout": timeout,
        }

        resp = self._make_request("getUpdates", payload)
        updates = resp.get("result", [])
        if updates:
            max_id = max(u.get("update_id", 0) for u in updates)
            self.last_offset = max(self.last_offset, max_id + 1)
        return updates

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        """
        Acknowledge callback query to dismiss loading state in Telegram client.
        """
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        resp = self._make_request("answerCallbackQuery", payload)
        return bool(resp.get("ok", False))

    def poll_and_process_callbacks(self, conn: sqlite3.Connection, timeout: int = 0) -> List[Dict[str, Any]]:
        """
        Poll Telegram for updates, filter for callback queries from authorized chat,
        and update SQLite escalation / correction state accordingly:
        - Persists last_offset in app_state table across process restarts.
        - Short poll: uses timeout=0 so scheduler ticks never block.
        - Chat isolation: Ignores queries from chat IDs other than TELEGRAM_CHAT_ID.
        - 'Got it' (or 'ak:...'): sets escalations.acked_at.
        - 'False alarm' (or 'fa:...'): writes a corrections row idempotently and sets escalations.acked_at.
        - Acknowledges each processed query via answerCallbackQuery.
        """
        # Load offset from SQLite if not yet initialized in-memory
        if self.last_offset == 0:
            persisted = get_app_state(conn, "telegram_last_offset")
            if persisted:
                try:
                    self.last_offset = int(persisted)
                except ValueError:
                    pass

        updates = self.get_updates(timeout=timeout)
        # Persist updated offset
        set_app_state(conn, "telegram_last_offset", self.last_offset)

        processed: List[Dict[str, Any]] = []

        for u in updates:
            cb = u.get("callback_query")
            if not cb:
                continue

            cb_id = cb.get("id")
            cb_data = cb.get("data", "")

            # Determine incoming chat ID
            msg = cb.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", "")) if chat.get("id") is not None else ""
            if not chat_id and "from" in cb:
                from_user = cb.get("from") or {}
                chat_id = str(from_user.get("id", "")) if from_user.get("id") is not None else ""

            # Chat isolation enforcement
            if str(chat_id) != str(self.chat_id):
                logger.warning(
                    "Chat isolation: ignored callback from unauthorized chat ID '%s' (expected '%s')",
                    chat_id,
                    self.chat_id,
                )
                continue

            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Route 1: False alarm -> record correction + ack escalation
            is_fa = cb_data.startswith("fa:") or cb_data.startswith("false_alarm:") or cb_data == "False alarm"
            is_ak = cb_data.startswith("ak:") or cb_data.startswith("ack:") or cb_data == "Got it"

            if is_fa:
                cand_routine_id, local_date = None, None
                if ":" in cb_data:
                    parts = cb_data.split(":", 2)
                    if len(parts) >= 3:
                        cand_routine_id, local_date = parts[1], parts[2]
                routine_id = resolve_callback_routine_id(conn, cand_routine_id)
                if not routine_id:
                    logger.warning(
                        "Unresolvable callback routine '%s' (data: '%s'): alert is no longer active",
                        cand_routine_id,
                        cb_data,
                    )
                    if cb_id:
                        self.answer_callback_query(cb_id, text="This alert is no longer active.")
                    continue

                if not local_date and routine_id:
                    cur = conn.execute(
                        "SELECT local_date FROM escalations WHERE routine_id = ? AND acked_at IS NULL ORDER BY fired_at DESC LIMIT 1",
                        (routine_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        local_date = row["local_date"]
                if not local_date:
                    local_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                if routine_id and local_date:
                    cur_esc = conn.execute(
                        "SELECT 1 FROM escalations WHERE routine_id = ? AND local_date = ?",
                        (routine_id, local_date),
                    )
                    if not cur_esc.fetchone():
                        logger.warning(
                            "Callback routine '%s' date '%s' has no escalation row: alert is no longer active",
                            routine_id,
                            local_date,
                        )
                        if cb_id:
                            self.answer_callback_query(cb_id, text="This alert is no longer active.")
                        continue

                    r_row = get_routine(conn, routine_id)
                    device_id = r_row["device_id"] if r_row else "unknown_device"
                    event_type = r_row["expected_event_type"] if r_row else "motion_detected"
                    time_bucket = f"{r_row['window_start']}-{r_row['window_end']}" if r_row else local_date

                    # Deterministic ID ensures idempotent inserts (no duplicate correction rows)
                    corr_id = f"corr_{routine_id}_{local_date}"
                    record_correction(
                        conn=conn,
                        id=corr_id,
                        device_id=device_id,
                        event_type=event_type,
                        time_bucket=time_bucket,
                        label="false_alarm",
                        created_at=now_iso,
                        ignore_conflicts=True,
                    )
                    ack_escalation(conn, routine_id, local_date, now_iso)
                    if cb_id:
                        self.answer_callback_query(cb_id, text="Recorded as false alarm.")
                    processed.append({
                        "type": "false_alarm",
                        "routine_id": routine_id,
                        "local_date": local_date,
                        "correction_id": corr_id,
                    })

            # Route 2: Got it / ack -> ack escalation
            elif is_ak:
                cand_routine_id, local_date = None, None
                if ":" in cb_data:
                    parts = cb_data.split(":", 2)
                    if len(parts) >= 3:
                        cand_routine_id, local_date = parts[1], parts[2]
                routine_id = resolve_callback_routine_id(conn, cand_routine_id)
                if not routine_id:
                    logger.warning(
                        "Unresolvable callback routine '%s' (data: '%s'): alert is no longer active",
                        cand_routine_id,
                        cb_data,
                    )
                    if cb_id:
                        self.answer_callback_query(cb_id, text="This alert is no longer active.")
                    continue

                if not local_date and routine_id:
                    cur = conn.execute(
                        "SELECT local_date FROM escalations WHERE routine_id = ? AND acked_at IS NULL ORDER BY fired_at DESC LIMIT 1",
                        (routine_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        local_date = row["local_date"]
                if routine_id and local_date:
                    cur_esc = conn.execute(
                        "SELECT 1 FROM escalations WHERE routine_id = ? AND local_date = ?",
                        (routine_id, local_date),
                    )
                    if not cur_esc.fetchone():
                        logger.warning(
                            "Callback routine '%s' date '%s' has no escalation row: alert is no longer active",
                            routine_id,
                            local_date,
                        )
                        if cb_id:
                            self.answer_callback_query(cb_id, text="This alert is no longer active.")
                        continue

                    ack_escalation(conn, routine_id, local_date, now_iso)
                    if cb_id:
                        self.answer_callback_query(cb_id, text="Escalation acknowledged.")
                    processed.append({
                        "type": "ack",
                        "routine_id": routine_id,
                        "local_date": local_date,
                    })

        return processed

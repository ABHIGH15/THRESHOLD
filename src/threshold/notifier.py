"""
Threshold Notifier Module (src/threshold/notifier.py).
Provides notification interfaces, console, recording (test), and Telegram notifiers.
Includes escalation ladders, Telegram inline keyboard buttons, callback polling,
chat isolation, and strict token redaction across logs and error messages.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Union
import urllib.error
import urllib.request
import uuid

from src.threshold.models import Routine
from src.threshold.store import ack_escalation, get_routine, record_correction

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


class Notifier(ABC):
    """Abstract notification delivery interface."""

    @abstractmethod
    def send_escalation(self, routine: Routine, local_date: str, fired_at: str) -> None:
        """Send initial escalation notification. Must raise exception on failure."""
        pass

    @abstractmethod
    def send_reminder(self, routine: Routine, local_date: str, fired_at: str) -> None:
        """Send reminder notification for unacknowledged escalation. Must raise exception on failure."""
        pass


class RecordingNotifier(Notifier):
    """In-memory recording notifier for tests, verifying delivery, and simulating failures."""

    def __init__(self, fail_first_n: int = 0):
        self.sent: List[Dict[str, Any]] = []
        self.reminders: List[Dict[str, Any]] = []
        self.fail_first_n = fail_first_n
        self.attempts = 0

    def send_escalation(self, routine: Routine, local_date: str, fired_at: str) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError(f"Simulated notification failure (attempt {self.attempts})")
        self.sent.append({
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
        })

    def send_reminder(self, routine: Routine, local_date: str, fired_at: str) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError(f"Simulated reminder notification failure (attempt {self.attempts})")
        self.reminders.append({
            "routine_id": routine.routine_id,
            "routine_name": routine.name,
            "local_date": local_date,
            "fired_at": fired_at,
        })


class ConsoleNotifier(Notifier):
    """Console / stdout notifier for CLI operations and demonstrations."""

    def send_escalation(self, routine: Routine, local_date: str, fired_at: str) -> None:
        print(f"[ESCALATION] Routine '{routine.name}' ({routine.routine_id}) missed on {local_date} at {fired_at}")

    def send_reminder(self, routine: Routine, local_date: str, fired_at: str) -> None:
        print(f"[REMINDER] Routine '{routine.name}' ({routine.routine_id}) unacknowledged for {local_date} (fired at {fired_at})")


class TelegramNotifier(Notifier):
    """
    Production Telegram Bot Notifier.
    Delivers messages via sendMessage with inline keyboard ['False alarm', 'Got it'].
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
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.last_offset: int = 0

    def _redact(self, text: str) -> str:
        """Helper to redact the instance's bot token."""
        return redact_telegram_token(text, self.bot_token)

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

    def send_escalation(self, routine: Routine, local_date: str, fired_at: str) -> None:
        """
        Send initial escalation notification with inline keyboard:
        ['False alarm', 'Got it']
        """
        text = (
            f"🚨 <b>THRESHOLD ESCALATION</b>\n\n"
            f"Routine: <b>{routine.name}</b>\n"
            f"Expected window: {routine.window_start} - {routine.window_end} ({routine.timezone})\n"
            f"Fired at: {fired_at}\n\n"
            f"Absence detected. No expected activity observed."
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "False alarm",
                        "callback_data": f"false_alarm:{routine.routine_id}:{local_date}",
                    },
                    {
                        "text": "Got it",
                        "callback_data": f"ack:{routine.routine_id}:{local_date}",
                    },
                ]
            ]
        }

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }

        self._make_request("sendMessage", payload)

    def send_reminder(self, routine: Routine, local_date: str, fired_at: str) -> None:
        """
        Send reminder notification for unacknowledged escalation with inline keyboard.
        """
        text = (
            f"⚠️ <b>THRESHOLD REMINDER</b>\n\n"
            f"Routine: <b>{routine.name}</b>\n"
            f"Expected window: {routine.window_start} - {routine.window_end} ({routine.timezone})\n"
            f"Originally fired at: {fired_at}\n\n"
            f"Escalation remains unacknowledged. Please check on resident."
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "False alarm",
                        "callback_data": f"false_alarm:{routine.routine_id}:{local_date}",
                    },
                    {
                        "text": "Got it",
                        "callback_data": f"ack:{routine.routine_id}:{local_date}",
                    },
                ]
            ]
        }

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }

        self._make_request("sendMessage", payload)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> List[Dict[str, Any]]:
        """
        Poll updates from Telegram with offset tracking.
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

    def poll_and_process_callbacks(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        """
        Poll Telegram for updates, filter for callback queries from authorized chat,
        and update SQLite escalation / correction state accordingly:
        - Chat isolation: Ignores queries from chat IDs other than TELEGRAM_CHAT_ID.
        - 'Got it' (or 'ack:...'): sets escalations.acked_at.
        - 'False alarm' (or 'false_alarm:...'): writes a corrections row and sets escalations.acked_at.
        - Acknowledges each processed query via answerCallbackQuery.
        """
        updates = self.get_updates()
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
            if cb_data.startswith("false_alarm:") or cb_data == "False alarm":
                routine_id, local_date = None, None
                if ":" in cb_data:
                    parts = cb_data.split(":", 2)
                    if len(parts) >= 3:
                        routine_id, local_date = parts[1], parts[2]
                if not routine_id:
                    # Fallback to most recent unacknowledged escalation
                    cur = conn.execute(
                        "SELECT routine_id, local_date FROM escalations WHERE acked_at IS NULL ORDER BY fired_at DESC LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row:
                        routine_id, local_date = row["routine_id"], row["local_date"]

                if routine_id and local_date:
                    r_row = get_routine(conn, routine_id)
                    device_id = r_row["device_id"] if r_row else "unknown_device"
                    event_type = r_row["expected_event_type"] if r_row else "motion_detected"
                    time_bucket = f"{r_row['window_start']}-{r_row['window_end']}" if r_row else local_date

                    corr_id = f"corr_{uuid.uuid4().hex[:8]}"
                    record_correction(
                        conn=conn,
                        id=corr_id,
                        device_id=device_id,
                        event_type=event_type,
                        time_bucket=time_bucket,
                        label="false_alarm",
                        created_at=now_iso,
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
            elif cb_data.startswith("ack:") or cb_data == "Got it":
                routine_id, local_date = None, None
                if ":" in cb_data:
                    parts = cb_data.split(":", 2)
                    if len(parts) >= 3:
                        routine_id, local_date = parts[1], parts[2]
                if not routine_id:
                    cur = conn.execute(
                        "SELECT routine_id, local_date FROM escalations WHERE acked_at IS NULL ORDER BY fired_at DESC LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row:
                        routine_id, local_date = row["routine_id"], row["local_date"]

                if routine_id and local_date:
                    ack_escalation(conn, routine_id, local_date, now_iso)
                    if cb_id:
                        self.answer_callback_query(cb_id, text="Escalation acknowledged.")
                    processed.append({
                        "type": "ack",
                        "routine_id": routine_id,
                        "local_date": local_date,
                    })

        return processed

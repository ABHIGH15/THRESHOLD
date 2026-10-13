"""
Threshold Ring Partner API Client & Poller (src/threshold/ring_client.py).

Provides typed API client for Ring Partner API (api.amazonvision.com):
- list_devices: GET /v1/devices
- get_device_status: GET /v1/devices/{id}/status
- get_event_history: GET /v1/history/devices/{id}/events
- get_snapshot: POST /v1/devices/{id}/media/image/download (303 two-step redirect without Authorization header)
- Typed exceptions: TokenExpired, RateLimited, MediaUnavailable, RingAPIError
- poll_once: idempotent polling routine updating device_health and ingesting event history.
"""

from datetime import datetime, timezone
import json
import logging
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import urllib.error
import urllib.parse
import urllib.request

from src.threshold.ingest import ingest_event
from src.threshold.store import ms_to_iso, to_iso_timestamp, upsert_device_health

logger = logging.getLogger(__name__)


# --- 1. Typed Exception Hierarchy ---

class RingAPIError(Exception):
    """Base exception for all Ring Partner API communication errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class TokenExpired(RingAPIError):
    """HTTP 401: OAuth access token has expired or is invalid."""
    pass


class RateLimited(RingAPIError):
    """HTTP 429: Too many requests, retry backoff attempts exhausted."""
    pass


class MediaUnavailable(RingAPIError):
    """
    Media unavailable / processing failure.
    Raised on 416 (MEDIA_NOT_FOUND), 425 (RECORDING_NOT_READY), 422 (CORRUPT_RECORDING),
    or when media retrieval encounters a format / upstream error.
    """

    def __init__(
        self,
        reason: str,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        msg = f"Media unavailable ({code or status_code}): {reason}"
        super().__init__(msg, status_code=status_code, response_body=response_body)
        self.reason = reason
        self.code = code


class Forbidden(RingAPIError):
    """HTTP 403: Forbidden / insufficient permissions."""
    pass


class NotFound(RingAPIError):
    """HTTP 404: Resource not found."""
    pass


class BadRequest(RingAPIError):
    """HTTP 400: Malformed request parameters."""
    pass


class ServerError(RingAPIError):
    """HTTP 5xx: Ring / Amazon Vision upstream server error."""
    pass


# --- 2. Custom Redirect Handler ---

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Custom HTTP redirect handler that halts auto-redirects.
    Allows capturing 303 See Other location URLs to strip credentials
    before making secondary requests to third-party CDNs.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Return None to instruct urllib NOT to follow the redirect
        return None

    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    def http_error_302(self, req, fp, code, msg, headers):
        return fp

    def http_error_303(self, req, fp, code, msg, headers):
        return fp

    def http_error_307(self, req, fp, code, msg, headers):
        return fp

    def http_error_308(self, req, fp, code, msg, headers):
        return fp


# --- 3. Ring Client Implementation ---

class RingClient:
    """
    Client for Ring Partner API endpoints (https://api.amazonvision.com).
    Standard library urllib only. Redacts tokens on error logging.
    """

    def __init__(
        self,
        access_token: str,
        base_url: str = "https://api.amazonvision.com",
        timeout: float = 10.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.sleep_fn = sleep_fn

    def _redact_token(self, text: str) -> str:
        if not text:
            return text
        if self.access_token and self.access_token in text:
            return text.replace(self.access_token, "[REDACTED]")
        return text

    def _request(
        self,
        method: str,
        path_or_url: str,
        body: Optional[Union[dict, str, bytes]] = None,
        headers: Optional[Dict[str, str]] = None,
        include_auth: bool = True,
        follow_redirects: bool = True,
        max_retries: int = 3,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """
        Execute HTTP request with 429 exponential backoff, error mapping, and token redaction.
        """
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        req_headers = {
            "User-Agent": "Threshold/1.0",
            "Accept": "application/json",
        }
        if include_auth and self.access_token:
            req_headers["Authorization"] = f"Bearer {self.access_token}"
        if headers:
            req_headers.update(headers)

        req_data: Optional[bytes] = None
        if body is not None:
            if isinstance(body, (dict, list)):
                req_data = json.dumps(body).encode("utf-8")
                req_headers["Content-Type"] = "application/json"
            elif isinstance(body, str):
                req_data = body.encode("utf-8")
            else:
                req_data = body

        attempt = 0
        backoff_base = 1.0
        req_timeout = self.timeout if timeout is None else float(timeout)

        while True:
            attempt += 1
            req = urllib.request.Request(url, data=req_data, headers=req_headers, method=method)

            # Choose opener based on redirect behavior
            if not follow_redirects:
                opener = urllib.request.build_opener(NoRedirectHandler())
            else:
                opener = urllib.request.build_opener()

            try:
                with opener.open(req, timeout=req_timeout) as resp:
                    status = resp.status if hasattr(resp, "status") else resp.getcode()
                    resp_headers = dict(resp.headers)
                    resp_bytes = resp.read()
                    return status, resp_headers, resp_bytes

            except urllib.error.HTTPError as err:
                try:
                    status = err.code
                    resp_headers = dict(err.headers)
                    resp_bytes = err.read()
                    resp_text = resp_bytes.decode("utf-8", errors="replace")
                finally:
                    err.close()

                if not follow_redirects and status in (301, 302, 303, 307, 308):
                    return status, resp_headers, resp_bytes

                # Handle 429 Rate Limiting with exponential backoff honoring Retry-After
                if status == 429:
                    if attempt < max_retries:
                        retry_after_hdr = resp_headers.get("Retry-After")
                        wait_sec = None
                        if retry_after_hdr:
                            try:
                                wait_sec = float(retry_after_hdr)
                            except ValueError:
                                pass
                        if wait_sec is None:
                            wait_sec = backoff_base * (2 ** (attempt - 1))

                        logger.warning(
                            "Rate limited (429) calling %s. Retrying in %.1fs (attempt %d/%d)...",
                            method,
                            wait_sec,
                            attempt,
                            max_retries,
                        )
                        self.sleep_fn(wait_sec)
                        continue
                    else:
                        raise RateLimited(
                            f"HTTP 429 Rate Limit exceeded after {max_retries} attempts",
                            status_code=429,
                            response_body=resp_text,
                        )

                # 401 Token Expired
                if status == 401:
                    raise TokenExpired(
                        "HTTP 401: Ring access token expired or invalid",
                        status_code=401,
                        response_body=resp_text,
                    )

                # 416, 425, 422 Media Unavailable
                if status in (416, 422, 425):
                    reason_code = "MEDIA_NOT_FOUND" if status == 416 else ("RECORDING_NOT_READY" if status == 425 else "CORRUPT_RECORDING")
                    raise MediaUnavailable(
                        resp_text or reason_code,
                        code=reason_code,
                        status_code=status,
                        response_body=resp_text,
                    )

                # 403 Forbidden
                if status == 403:
                    raise Forbidden(
                        f"HTTP 403 Forbidden: {self._redact_token(resp_text)}",
                        status_code=403,
                        response_body=resp_text,
                    )

                # 404 Not Found
                if status == 404:
                    raise NotFound(
                        f"HTTP 404 Not Found: {self._redact_token(resp_text)}",
                        status_code=404,
                        response_body=resp_text,
                    )

                # 400 Bad Request
                if status == 400:
                    raise BadRequest(
                        f"HTTP 400 Bad Request: {self._redact_token(resp_text)}",
                        status_code=400,
                        response_body=resp_text,
                    )

                # 5xx Server Error
                if 500 <= status <= 599:
                    raise ServerError(
                        f"HTTP {status} Server Error: {self._redact_token(resp_text)}",
                        status_code=status,
                        response_body=resp_text,
                    )

                # Any other unexpected HTTP status
                raise RingAPIError(
                    f"HTTP {status} Error: {self._redact_token(resp_text)}",
                    status_code=status,
                    response_body=resp_text,
                )

            except Exception as exc:
                safe_msg = self._redact_token(str(exc))
                logger.error("Ring client request %s %s failed: %s", method, path_or_url, safe_msg)
                raise RingAPIError(f"Request failed: {safe_msg}") from None

    # --- High-level Ring API Methods ---

    def list_devices(self) -> List[Dict[str, Any]]:
        """
        List all accessible devices.
        GET /v1/devices
        """
        status, headers, body_bytes = self._request("GET", "/v1/devices")
        data = json.loads(body_bytes.decode("utf-8"))
        return data.get("data", [])

    def get_device_status(self, device_id: str) -> Dict[str, Any]:
        """
        Get current online/offline and reported status for a device.
        GET /v1/devices/{device_id}/status
        """
        path = f"/v1/devices/{urllib.parse.quote(device_id)}/status"
        status, headers, body_bytes = self._request("GET", path)
        data = json.loads(body_bytes.decode("utf-8"))
        return data.get("data", {})

    def get_event_history(
        self,
        device_id: str,
        event_types: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get event history for a device.
        GET /v1/history/devices/{device_id}/events
        """
        path = f"/v1/history/devices/{urllib.parse.quote(device_id)}/events"
        if event_types:
            path = f"{path}?event_types={urllib.parse.quote(event_types)}"
        status, headers, body_bytes = self._request("GET", path)
        data = json.loads(body_bytes.decode("utf-8"))
        return data.get("data", [])

    def get_snapshot(
        self,
        device_id: str,
        timestamp_ms: Union[int, float],
        timeout: Optional[float] = None,
    ) -> bytes:
        """
        Download snapshot image for device at timestamp.
        Two-step flow:
        1. POST /v1/devices/{id}/media/image/download with body {"type": "at_timestamp", "timestamp": <ms>}
           without auto-following redirects. Expects 303 See Other with Location header.
        2. GET <Location URL> WITHOUT the Authorization header (credentials never sent to download host).
        Returns raw image bytes.
        """
        path = f"/v1/devices/{urllib.parse.quote(device_id)}/media/image/download"
        body = {
            "type": "at_timestamp",
            "timestamp": int(timestamp_ms),
        }

        # Step 1: POST to snapshot initiation endpoint without following redirect
        status, headers, body_bytes = self._request(
            method="POST",
            path_or_url=path,
            body=body,
            include_auth=True,
            follow_redirects=False,
            timeout=timeout,
        )

        location_url = headers.get("Location") or headers.get("location")
        if not location_url:
            # If 200 returned directly with image binary
            if status == 200 and body_bytes:
                return body_bytes
            raise MediaUnavailable(
                "303 redirect missing Location header",
                code="MISSING_LOCATION",
                status_code=status,
            )

        # Step 2: GET the Location URL WITHOUT Authorization header
        status2, headers2, img_bytes = self._request(
            method="GET",
            path_or_url=location_url,
            body=None,
            include_auth=False,  # CRITICAL: Do NOT send Ring token to download host
            follow_redirects=True,
            timeout=timeout,
        )

        if not img_bytes:
            raise MediaUnavailable(
                "Empty binary payload received from snapshot download host",
                code="EMPTY_MEDIA",
                status_code=status2,
            )

        return img_bytes


# --- 4. Polling Routine ---

def poll_once(
    conn: sqlite3.Connection,
    client: RingClient,
    now: Union[str, datetime],
) -> Dict[str, Any]:
    """
    Poll Ring API for all accessible devices:
    1. Query device list.
    2. For each device:
       - Query device status and update device_health (source='poll').
       - Query event history and ingest into events table (source='history').
    3. Idempotent: repeated polls preserve existing event state without duplicates.
    Returns summary statistics.
    """
    now_iso = to_iso_timestamp(now)
    devices = client.list_devices()
    total_events_ingested = 0
    devices_polled = 0

    for dev in devices:
        dev_id = dev.get("id")
        if not dev_id:
            continue
        devices_polled += 1

        # 1. Update device status
        try:
            status_obj = client.get_device_status(dev_id)
            attrs = status_obj.get("attributes", {})
            online = bool(attrs.get("online", False))
            reported_at = attrs.get("reported_at")

            upsert_device_health(
                conn=conn,
                device_id=dev_id,
                online=online,
                reported_at=reported_at,
                fetched_at=now_iso,
                source="poll",
            )
        except Exception as e:
            logger.warning("Failed to fetch device status for %s: %s", dev_id, e)

        # 2. Ingest event history
        try:
            history_events = client.get_event_history(dev_id)
            for ev in history_events:
                ext_id = ev.get("id")
                if not ext_id:
                    continue

                ev_attrs = ev.get("attributes", {})
                event_type = ev_attrs.get("event_type", "unknown")
                start_ms = ev_attrs.get("start")
                end_ms = ev_attrs.get("end")

                start_iso = ms_to_iso(start_ms) if start_ms is not None else now_iso
                end_iso = ms_to_iso(end_ms) if end_ms is not None else None

                # Source relationship device ID
                src_dev_id = (
                    ev.get("relationships", {})
                    .get("source", {})
                    .get("data", {})
                    .get("id")
                    or dev_id
                )

                # Do NOT map history event types to webhook event types (TODO-VERIFY)
                inserted = ingest_event(
                    conn=conn,
                    source="history",
                    external_id=ext_id,
                    device_id=src_dev_id,
                    event_type=event_type,
                    sub_type=None,
                    occurred_at=start_iso,
                    raw_json=ev,
                    ended_at=end_iso,
                    received_at=now_iso,
                )
                if inserted:
                    total_events_ingested += 1
        except Exception as e:
            logger.warning("Failed to fetch event history for %s: %s", dev_id, e)

    return {
        "timestamp": now_iso,
        "devices_polled": devices_polled,
        "events_ingested": total_events_ingested,
    }

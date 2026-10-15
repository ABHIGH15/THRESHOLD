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
import email.utils
import json
import logging
import socket
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

    def get_latest_snapshot(
        self,
        device_id: str,
        lookback_hours: int = 24,
        deadline_seconds: float = 5.0,
    ) -> Tuple[bytes, Optional[int]]:
        """
        Download the latest snapshot image for device using 'latest_in_range'.

        Semantics:
        - POST /v1/devices/{id}/media/image/download with:
          {"type": "latest_in_range", "start_timestamp": now_ms - lookback_hours * 3600 * 1000}
          (lookback clamped to 1..24 hours). No end_timestamp sent.
        - ONE overall monotonic deadline (default 5.0s) across POST + redirect + GET + body read.
        - Body read in chunks with deadline checks between chunks.
        - 416 on GET, and 416/425/422 on POST -> MediaUnavailable.
        - 429 -> RateLimited immediately (NO sleeping in this path).
        - 401 -> TokenExpired.
        - Authorization header NEVER sent to download host.
        - Returns (image_bytes, captured_at_ms_or_None).
        """
        start_mono = time.monotonic()
        deadline_mono = start_mono + float(deadline_seconds)

        def remaining_timeout() -> float:
            rem = deadline_mono - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Snapshot deadline exceeded")
            return rem

        # 1. Prepare request payload with clamped lookback (1..24 hours)
        # 15s clock skew margin ensures server-side window (now - start_ts) strictly <= 24h
        clamped_lookback = max(1, min(24, int(lookback_hours)))
        now_ms = int(time.time() * 1000)
        start_ts = now_ms - (clamped_lookback * 3600 * 1000) + 15000

        path = f"/v1/devices/{urllib.parse.quote(device_id)}/media/image/download"
        post_url = urllib.parse.urljoin(self.base_url, path)
        body_dict = {
            "type": "latest_in_range",
            "start_timestamp": start_ts,
        }
        body_bytes = json.dumps(body_dict).encode("utf-8")

        post_req = urllib.request.Request(post_url, data=body_bytes, method="POST")
        post_req.add_header("Authorization", f"Bearer {self.access_token}")
        post_req.add_header("Content-Type", "application/json")
        post_req.add_header("User-Agent", "Threshold/1.0")

        post_opener = urllib.request.build_opener(NoRedirectHandler())
        rem_t = remaining_timeout()

        try:
            with post_opener.open(post_req, timeout=rem_t) as resp:
                post_status = resp.status if hasattr(resp, "status") else resp.getcode()
                post_headers = dict(resp.headers)
                post_resp_bytes = resp.read()
        except urllib.error.HTTPError as err:
            post_status = err.code
            post_headers = dict(err.headers) if err.headers else {}
            try:
                post_resp_bytes = err.read()
            except Exception:
                post_resp_bytes = b""
            finally:
                err.close()

            if post_status not in (301, 302, 303, 307, 308):
                err_text = post_resp_bytes.decode("utf-8", errors="replace")
                if post_status in (416, 422, 425):
                    reason = "MEDIA_NOT_FOUND" if post_status == 416 else ("RECORDING_NOT_READY" if post_status == 425 else "CORRUPT_RECORDING")
                    raise MediaUnavailable(err_text or reason, code=reason, status_code=post_status, response_body=err_text)
                if post_status == 429:
                    raise RateLimited("HTTP 429 Rate Limited (snapshot POST)", status_code=429, response_body=err_text)
                if post_status == 401:
                    raise TokenExpired("HTTP 401 Unauthorized", status_code=401, response_body=err_text)
                if post_status == 403:
                    raise Forbidden("HTTP 403 Forbidden", status_code=403, response_body=err_text)
                if post_status == 404:
                    raise NotFound("HTTP 404 Not Found", status_code=404, response_body=err_text)
                if post_status == 400:
                    raise BadRequest("HTTP 400 Bad Request", status_code=400, response_body=err_text)
                if 500 <= post_status <= 599:
                    raise ServerError(f"HTTP {post_status} Server Error", status_code=post_status, response_body=err_text)
                raise RingAPIError(f"HTTP {post_status} Error", status_code=post_status, response_body=err_text)
        except (socket.timeout, urllib.error.URLError) as exc:
            if "timed out" in str(exc).lower():
                raise TimeoutError(f"Snapshot POST timed out: {exc}") from None
            raise RingAPIError(f"Snapshot POST failed: {self._redact_token(str(exc))}") from None

        location_url = post_headers.get("Location") or post_headers.get("location")
        if not location_url:
            if post_status == 200 and post_resp_bytes:
                return post_resp_bytes, None
            raise MediaUnavailable("303 redirect missing Location header", code="MISSING_LOCATION", status_code=post_status)

        # 2. GET image from download host WITHOUT Authorization header
        get_req = urllib.request.Request(location_url, method="GET")
        get_req.add_header("User-Agent", "Threshold/1.0")

        rem_t = remaining_timeout()
        try:
            with urllib.request.urlopen(get_req, timeout=rem_t) as get_resp:
                get_status = get_resp.status if hasattr(get_resp, "status") else get_resp.getcode()
                get_headers = dict(get_resp.headers)

                chunks = []
                while True:
                    remaining_timeout()
                    chunk = get_resp.read(16384)
                    if not chunk:
                        break
                    chunks.append(chunk)
                img_bytes = b"".join(chunks)

        except urllib.error.HTTPError as err:
            get_status = err.code
            get_headers = dict(err.headers) if err.headers else {}
            try:
                err_bytes = err.read()
            except Exception:
                err_bytes = b""
            finally:
                err.close()

            err_text = err_bytes.decode("utf-8", errors="replace")
            if get_status == 416:
                raise MediaUnavailable(err_text or "No media found in range", code="MEDIA_NOT_FOUND", status_code=416, response_body=err_text)
            if get_status == 429:
                raise RateLimited("HTTP 429 Rate Limited on download host", status_code=429, response_body=err_text)
            if get_status == 401:
                raise TokenExpired("HTTP 401 Unauthorized on download host", status_code=401, response_body=err_text)
            raise MediaUnavailable(f"Download host HTTP {get_status}: {err_text}", code=f"HTTP_{get_status}", status_code=get_status, response_body=err_text)
        except (socket.timeout, urllib.error.URLError) as exc:
            if "timed out" in str(exc).lower():
                raise TimeoutError(f"Snapshot GET timed out: {exc}") from None
            raise RingAPIError(f"Snapshot GET failed: {self._redact_token(str(exc))}") from None

        if not img_bytes:
            raise MediaUnavailable("Empty image binary received from download host", code="EMPTY_MEDIA", status_code=get_status)

        # 3. Parse capture timestamp from headers
        captured_at_ms: Optional[int] = None
        x_media_ts = get_headers.get("X-Media-Timestamp") or get_headers.get("x-media-timestamp")
        if x_media_ts:
            try:
                captured_at_ms = int(x_media_ts)
            except (ValueError, TypeError):
                captured_at_ms = None

        if captured_at_ms is None:
            last_mod = get_headers.get("Last-Modified") or get_headers.get("last-modified")
            if last_mod:
                try:
                    parsed_dt = email.utils.parsedate_to_datetime(last_mod)
                    captured_at_ms = int(parsed_dt.timestamp() * 1000)
                except Exception:
                    captured_at_ms = None

        # Sanity check: must be <= now + 5 min and >= now - 180 days
        if captured_at_ms is not None:
            now_ms_check = int(time.time() * 1000)
            max_future = now_ms_check + (5 * 60 * 1000)
            min_past = now_ms_check - (180 * 24 * 3600 * 1000)
            if not (min_past <= captured_at_ms <= max_future):
                captured_at_ms = None

        return img_bytes, captured_at_ms


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

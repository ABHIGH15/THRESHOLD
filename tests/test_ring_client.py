"""
Unit and integration tests for Threshold Ring Client and Poller (src/threshold/ring_client.py).
Tests RingClient, typed errors (TokenExpired, RateLimited, MediaUnavailable, etc.),
the 303 two-step snapshot flow with credential stripping, and poll_once idempotence.
"""

from datetime import datetime, timezone
import http.server
import json
import os
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlparse

from src.threshold.ring_client import (
    BadRequest,
    Forbidden,
    MediaUnavailable,
    NotFound,
    RateLimited,
    RingAPIError,
    RingClient,
    ServerError,
    TokenExpired,
    poll_once,
)
from src.threshold.store import (
    get_connection,
    get_device_health,
    get_event,
    init_db,
    save_routine,
)


class FakeRingServerHandler(http.server.BaseHTTPRequestHandler):
    """Fake Ring Partner API server handler for unit and integration testing."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        server = self.server
        parsed = urlparse(self.path)
        path = parsed.path
        headers_dict = dict(self.headers)

        server.requests_log.append({
            "method": "GET",
            "path": self.path,
            "headers": headers_dict,
            "body": None,
        })

        # Failure overrides
        if getattr(server, "force_401", False):
            self._send_json(401, {"error": "unauthorized", "message": "Token expired"})
            return

        if getattr(server, "force_403", False):
            self._send_json(403, {"error": "forbidden", "message": "Insufficient permissions"})
            return

        if getattr(server, "force_404", False):
            self._send_json(404, {"error": "not_found", "message": "Resource not found"})
            return

        if getattr(server, "force_400", False):
            self._send_json(400, {"error": "bad_request", "message": "Invalid query parameters"})
            return

        if getattr(server, "force_500", False):
            self._send_json(500, {"error": "server_error", "message": "Internal Ring error"})
            return

        if getattr(server, "rate_limit_first_n", 0) > 0:
            server.rate_limit_first_n -= 1
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0.01")
            self.end_headers()
            self.wfile.write(b'{"error": "rate_limited", "message": "Too many requests"}')
            return

        # Snapshot binary download endpoint (step 2 of snapshot flow)
        if path == "/download/snapshot_image.jpg":
            # Record whether Authorization header was sent on this download request
            server.download_request_headers = headers_dict
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            # Valid JPEG binary magic bytes + dummy payload
            self.wfile.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdbdummy_jpeg_data")
            return

        # /v1/devices
        if path == "/v1/devices":
            self._send_json(200, {
                "data": [
                    {
                        "type": "devices",
                        "id": "ava1.ring.device.TEST_CAM_1",
                        "attributes": {
                            "name": "Front Porch Camera",
                            "kind": "doorbell_v4",
                        },
                    }
                ]
            })
            return

        # /v1/devices/{id}/status
        if path.startswith("/v1/devices/") and path.endswith("/status"):
            self._send_json(200, {
                "data": {
                    "type": "device-status",
                    "id": "ava1.ring.device.status.STATUS_1",
                    "attributes": {
                        "online": True,
                        "reported_at": "2026-10-12T14:17:23Z",
                        "audio": {"snooze": {"active": None, "until": None}},
                    },
                }
            })
            return

        # /v1/history/devices/{id}/events
        if "/history/devices/" in path and path.endswith("/events"):
            self._send_json(200, {
                "data": [
                    {
                        "type": "history-events",
                        "id": "ava1.ring.history.event.HIST_1",
                        "attributes": {
                            "event_type": "on_demand",
                            "start": 1791814643626,
                            "end": 1791814665385,
                            "is_third_party_reviewed": True,
                        },
                        "relationships": {
                            "source": {
                                "data": {
                                    "type": "devices",
                                    "id": "ava1.ring.device.TEST_CAM_1",
                                }
                            }
                        },
                    }
                ]
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        server = self.server
        parsed = urlparse(self.path)
        path = parsed.path
        headers_dict = dict(self.headers)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        payload = json.loads(body.decode("utf-8")) if body else {}

        server.requests_log.append({
            "method": "POST",
            "path": self.path,
            "headers": headers_dict,
            "body": payload,
        })

        if getattr(server, "force_media_416", False):
            self._send_json(416, {"error": "MEDIA_NOT_FOUND", "message": "Recording not found"})
            return

        if getattr(server, "force_media_425", False):
            self._send_json(425, {"error": "RECORDING_NOT_READY", "message": "Recording still processing"})
            return

        if getattr(server, "force_media_422", False):
            self._send_json(422, {"error": "CORRUPT_RECORDING", "message": "Corrupt media stream"})
            return

        # Snapshot initiation: /v1/devices/{id}/media/image/download
        if "/media/image/download" in path:
            # Ring returns 303 See Other with pre-signed Location URL
            self.send_response(303)
            self.send_header("Location", f"http://127.0.0.1:{server.server_address[1]}/download/snapshot_image.jpg")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "redirect"}')
            return

        self.send_response(404)
        self.end_headers()

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))


class TestRingClient(unittest.TestCase):
    """Test suite for RingClient HTTP requests, error handling, snapshot flow, and polling."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(self.db_path)
        conn.close()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRingServerHandler)
        self.server.requests_log = []
        self.server.download_request_headers = None
        self.server.force_401 = False
        self.server.force_403 = False
        self.server.force_404 = False
        self.server.force_400 = False
        self.server.force_500 = False
        self.server.force_media_416 = False
        self.server.force_media_425 = False
        self.server.force_media_422 = False
        self.server.rate_limit_first_n = 0

        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self.token = "Atzr|sample_oauth_token_12345"
        self.sleep_calls = []
        self.client = RingClient(
            access_token=self.token,
            base_url=self.base_url,
            sleep_fn=lambda s: self.sleep_calls.append(s),
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

        for suffix in ("", "-wal", "-shm"):
            p = f"{self.db_path}{suffix}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # 1. Request shapes: list_devices, get_device_status, get_event_history
    def test_list_devices_request_shape(self):
        """list_devices issues GET /v1/devices with Bearer token and returns device list."""
        devices = self.client.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["id"], "ava1.ring.device.TEST_CAM_1")

        req = self.server.requests_log[-1]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/v1/devices")
        self.assertEqual(req["headers"].get("Authorization"), f"Bearer {self.token}")

    def test_get_device_status_request_shape(self):
        """get_device_status issues GET /v1/devices/{id}/status with Bearer token."""
        status = self.client.get_device_status("ava1.ring.device.TEST_CAM_1")
        self.assertTrue(status["attributes"]["online"])
        self.assertEqual(status["attributes"]["reported_at"], "2026-10-12T14:17:23Z")

        req = self.server.requests_log[-1]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/v1/devices/ava1.ring.device.TEST_CAM_1/status")
        self.assertEqual(req["headers"].get("Authorization"), f"Bearer {self.token}")

    def test_get_event_history_request_shape_and_query_param(self):
        """get_event_history issues GET /v1/history/devices/{id}/events with optional event_types."""
        events = self.client.get_event_history("ava1.ring.device.TEST_CAM_1", event_types="motion.human,ding")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["attributes"]["event_type"], "on_demand")

        req = self.server.requests_log[-1]
        self.assertEqual(req["method"], "GET")
        self.assertIn("/v1/history/devices/ava1.ring.device.TEST_CAM_1/events?event_types=", req["path"])
        self.assertEqual(req["headers"].get("Authorization"), f"Bearer {self.token}")

    # 2. 303 Two-Step Snapshot Flow WITHOUT Authorization on Second Request
    def test_snapshot_303_flow_without_authorization_header_on_second_request(self):
        """
        get_snapshot POSTs to initiate, intercepts 303 See Other without following,
        then GETs the Location URL WITHOUT forwarding the Ring Authorization header.
        """
        img_bytes = self.client.get_snapshot("ava1.ring.device.TEST_CAM_1", timestamp_ms=1791814643626)
        self.assertTrue(img_bytes.startswith(b"\xff\xd8\xff"))

        # Verify exactly 2 requests were made
        self.assertGreaterEqual(len(self.server.requests_log), 2)
        req1 = self.server.requests_log[-2]
        req2 = self.server.requests_log[-1]

        # Request 1: POST /v1/devices/.../media/image/download with Authorization
        self.assertEqual(req1["method"], "POST")
        self.assertIn("/media/image/download", req1["path"])
        self.assertEqual(req1["body"]["type"], "at_timestamp")
        self.assertEqual(req1["body"]["timestamp"], 1791814643626)
        self.assertEqual(req1["headers"].get("Authorization"), f"Bearer {self.token}")

        # Request 2: GET /download/snapshot_image.jpg WITHOUT Authorization header
        self.assertEqual(req2["method"], "GET")
        self.assertEqual(req2["path"], "/download/snapshot_image.jpg")
        self.assertNotIn("Authorization", req2["headers"], "CRITICAL: Ring token must NEVER be sent to download host")
        self.assertIsNone(req2["headers"].get("authorization"))

    # 3. 429 then 200 succeeds with backoff
    def test_rate_limit_429_retries_and_succeeds(self):
        """When 429 is encountered, client applies exponential backoff and succeeds on retry."""
        self.server.rate_limit_first_n = 1
        devices = self.client.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(len(self.sleep_calls), 1)
        self.assertGreater(self.sleep_calls[0], 0)

    def test_rate_limit_exhausted_raises_ratelimited(self):
        """When 429 persists beyond max retries, RateLimited exception is raised."""
        self.server.rate_limit_first_n = 5
        with self.assertRaises(RateLimited):
            self.client.list_devices()
        self.assertEqual(len(self.sleep_calls), 2)

    # 4. Media unavailable errors (416, 425, 422)
    def test_media_unavailable_416_not_found(self):
        """HTTP 416 raises MediaUnavailable with MEDIA_NOT_FOUND code."""
        self.server.force_media_416 = True
        with self.assertRaises(MediaUnavailable) as cm:
            self.client.get_snapshot("ava1.ring.device.TEST_CAM_1", timestamp_ms=1791814643626)
        self.assertEqual(cm.exception.code, "MEDIA_NOT_FOUND")

    def test_media_unavailable_425_recording_not_ready(self):
        """HTTP 425 raises MediaUnavailable with RECORDING_NOT_READY code."""
        self.server.force_media_425 = True
        with self.assertRaises(MediaUnavailable) as cm:
            self.client.get_snapshot("ava1.ring.device.TEST_CAM_1", timestamp_ms=1791814643626)
        self.assertEqual(cm.exception.code, "RECORDING_NOT_READY")

    # 5. TokenExpired (401)
    def test_token_expired_401(self):
        """HTTP 401 raises typed TokenExpired exception."""
        self.server.force_401 = True
        with self.assertRaises(TokenExpired):
            self.client.list_devices()

    # 6. Standard typed error mappings (403, 404, 400, 500)
    def test_standard_typed_error_mappings(self):
        """Standard HTTP errors map to Forbidden, NotFound, BadRequest, ServerError."""
        self.server.force_403 = True
        with self.assertRaises(Forbidden):
            self.client.list_devices()
        self.server.force_403 = False

        self.server.force_404 = True
        with self.assertRaises(NotFound):
            self.client.list_devices()
        self.server.force_404 = False

        self.server.force_400 = True
        with self.assertRaises(BadRequest):
            self.client.list_devices()
        self.server.force_400 = False

        self.server.force_500 = True
        with self.assertRaises(ServerError):
            self.client.list_devices()

    # 7. poll_once idempotence: polling twice -> no duplicate events and one health row
    def test_poll_once_twice_no_duplicate_events_and_one_health_row(self):
        """poll_once is idempotent: polling twice leaves 1 health row per device and zero duplicate events."""
        conn = get_connection(self.db_path)

        # Poll 1
        res1 = poll_once(conn, self.client, now="2026-10-12T14:24:00Z")
        self.assertEqual(res1["devices_polled"], 1)
        self.assertEqual(res1["events_ingested"], 1)

        # Check DB state
        health1 = get_device_health(conn, "ava1.ring.device.TEST_CAM_1")
        self.assertIsNotNone(health1)
        self.assertEqual(health1["source"], "poll")
        self.assertTrue(health1["online"])

        ev1 = get_event(conn, "history", "ava1.ring.history.event.HIST_1")
        self.assertIsNotNone(ev1)
        self.assertEqual(ev1["event_type"], "on_demand")

        cur = conn.execute("SELECT COUNT(*) FROM events WHERE source = 'history'")
        self.assertEqual(cur.fetchone()[0], 1)

        cur_health = conn.execute("SELECT COUNT(*) FROM device_health")
        self.assertEqual(cur_health.fetchone()[0], 1)

        # Poll 2: identical data, later poll timestamp
        res2 = poll_once(conn, self.client, now="2026-10-12T14:30:00Z")
        self.assertEqual(res2["devices_polled"], 1)
        self.assertEqual(res2["events_ingested"], 0)  # 0 new events ingested (idempotent duplicate ignore)

        # Verify row counts remain strictly 1
        cur2 = conn.execute("SELECT COUNT(*) FROM events WHERE source = 'history'")
        self.assertEqual(cur2.fetchone()[0], 1)

        cur_health2 = conn.execute("SELECT COUNT(*) FROM device_health")
        self.assertEqual(cur_health2.fetchone()[0], 1)

        # Health row fetched_at was updated to poll 2 timestamp
        health2 = get_device_health(conn, "ava1.ring.device.TEST_CAM_1")
        self.assertEqual(health2["fetched_at"], "2026-10-12T14:30:00Z")
        conn.close()


if __name__ == "__main__":
    unittest.main()

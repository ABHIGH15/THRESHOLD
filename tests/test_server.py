"""
Unit and integration tests for Threshold Webhook Server (src/threshold/server.py).
Runs a real ThreadingHTTPServer on 127.0.0.1 with port 0 in a background daemon thread.
"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest

from src.threshold.server import create_server
from src.threshold.store import (
    get_connection,
    get_device_health,
    get_routine,
    init_db,
    save_routine,
)
from src.threshold.webhook_auth import compute_signature


class TestWebhookServer(unittest.TestCase):
    """Integration test suite for WebhookHandler with a live ThreadingHTTPServer on port 0."""

    def setUp(self):
        self.secret = "test_webhook_secret_key_12345"
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = init_db(self.db_path)
        conn.close()

        # Create server on port 0 (OS allocates available port)
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            db_path=self.db_path,
            secret=self.secret,
        )
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

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

    def _send_request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict = None,
    ) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = headers or {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read()
        conn.close()
        return status, data

    def _make_ring_payload(
        self,
        request_id: str = "2ad45ade-1818-4154-813b-afdd8bcd8085",
        device_id: str = "cam_front",
        event_type: str = "motion_detected",
        sub_type: str = "human",
        timestamp_ms: int = 1791814643626,
        meta_time: str = "2026-02-13T13:39:57.200155525Z",
        account_id: str = "ava1.ring.account.XXXYYY",
        use_camel_sub_type: bool = False,
    ) -> bytes:
        attrs = {
            "source": device_id,
            "source_type": "devices",
            "timestamp": timestamp_ms,
            "timestamp_readable": "2026-08-14 08:53:16",
        }
        if sub_type is not None:
            if not use_camel_sub_type:
                attrs["sub_type"] = sub_type
            attrs["component_ids"] = ["0"]

        data_obj = {
            "id": f"{device_id}_{event_type}_{timestamp_ms}",
            "type": event_type,
            "attributes": attrs,
            "relationships": {
                "devices": {
                    "links": {
                        "self": f"/v1/devices/{device_id}"
                    }
                }
            },
        }
        if use_camel_sub_type and sub_type is not None:
            data_obj["subType"] = sub_type

        payload = {
            "meta": {
                "version": "1.1",
                "time": meta_time,
                "request_id": request_id,
                "account_id": account_id,
            },
            "data": data_obj,
        }
        return json.dumps(payload).encode("utf-8")

    # 1. Valid signature -> 200, exactly 1 row in DB.
    def test_valid_signature_200_and_one_row(self):
        body = self._make_ring_payload(request_id="req_valid")
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status, data = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode("utf-8")), {"status": "received"})

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE external_id = 'req_valid'")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    # 2. Corrupted signature -> 401, 0 rows in DB.
    def test_corrupted_signature_401_zero_rows(self):
        body = self._make_ring_payload(request_id="req_corrupted")
        bad_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"
        headers = {"Content-Type": "application/json", "X-Signature": bad_sig}

        status, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 401)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM events")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 3. Missing signature header -> 401.
    def test_missing_signature_header_401(self):
        body = self._make_ring_payload(request_id="req_nosig")
        headers = {"Content-Type": "application/json"}

        status, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 401)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM events")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 4. Replay attack / duplicate delivery: same request_id twice -> second returns 200, still exactly 1 row.
    def test_replay_attack_duplicate_delivery_one_row(self):
        body = self._make_ring_payload(request_id="req_replay")
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status1, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status1, 200)

        # Second delivery of same request_id
        status2, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status2, 200)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE external_id = 'req_replay'")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    # 5. Malformed JSON with valid signature -> 400.
    def test_malformed_json_with_valid_signature_400(self):
        malformed_body = b'{"unclosed_json": '
        sig = compute_signature(malformed_body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status, data = self._send_request("POST", "/webhooks/ring", malformed_body, headers)
        self.assertEqual(status, 400)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM events")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 6. Body > 1 MB -> 413 without reading whole body.
    def test_body_too_large_413(self):
        import socket
        # Connect raw socket and send headers with Content-Length > 1MB (e.g. 2MB)
        # We only send a few bytes of body; server responds 413 and closes connection WITHOUT reading 2MB.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        raw_req = (
            b"POST /webhooks/ring HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2097152\r\n"
            b"X-Signature: sha256=abcdef123456\r\n"
            b"\r\n"
            b"head_only_not_2mb"
        )
        s.sendall(raw_req)
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
        s.close()
        resp = b"".join(chunks).decode("utf-8")

        self.assertIn("413", resp)

        conn = get_connection(self.db_path)
        cur = conn.execute("SELECT COUNT(*) FROM events")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    # 7. Forced storage failure -> 500 (verifying errors aren't swallowed).
    def test_forced_storage_failure_returns_500(self):
        self.server.force_storage_failure = True
        body = self._make_ring_payload(request_id="req_fail")
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status, data = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 500)

    # 8. Measured response time printed (< 5s).
    def test_measured_response_time_under_five_seconds(self):
        body = self._make_ring_payload(request_id="req_speed")
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        start = time.perf_counter()
        status, data = self._send_request("POST", "/webhooks/ring", body, headers)
        duration_sec = time.perf_counter() - start

        print(f"\n[server_response_time] {duration_sec * 1000:.2f} ms (< 5000 ms limit)")
        self.assertEqual(status, 200)
        self.assertLess(duration_sec, 5.0)

    # 9. device_offline event -> device_health shows offline.
    def test_device_offline_event_updates_device_health(self):
        body = self._make_ring_payload(
            request_id="req_offline",
            device_id="cam_backyard",
            event_type="device_offline",
            sub_type=None,
        )
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 200)

        conn = get_connection(self.db_path)
        health = get_device_health(conn, "cam_backyard")
        self.assertIsNotNone(health)
        self.assertFalse(health["online"])
        conn.close()

    # 10. Matching motion event -> advances routine's last_satisfied_at.
    def test_matching_motion_event_advances_last_satisfied_at(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_front_porch",
            property_id="prop_main",
            name="Porch Motion",
            device_id="cam_porch",
            expected_event_type="motion_detected",
            expected_sub_type="human",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        conn.close()

        body = self._make_ring_payload(
            request_id="req_porch_motion",
            device_id="cam_porch",
            event_type="motion_detected",
            sub_type="human",
            timestamp_ms=1791814643626,
        )
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 200)

        conn = get_connection(self.db_path)
        rt = get_routine(conn, "rt_front_porch")
        self.assertEqual(rt["last_satisfied_at"], "2026-10-12T14:17:23Z")
        conn.close()

    # 11. Extra: Fallback support for camelCase data.subType
    def test_camelcase_subtype_fallback(self):
        conn = get_connection(self.db_path)
        save_routine(
            conn,
            routine_id="rt_camel_routine",
            property_id="prop_main",
            name="Camel SubType Routine",
            device_id="cam_camel",
            expected_event_type="motion_detected",
            expected_sub_type="human",
            window_start="07:00",
            window_end="09:00",
            timezone_name="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        )
        conn.close()

        body = self._make_ring_payload(
            request_id="req_camel_sub_type",
            device_id="cam_camel",
            event_type="motion_detected",
            sub_type="human",
            timestamp_ms=1791814643626,
            use_camel_sub_type=True,
        )
        sig = compute_signature(body, self.secret)
        headers = {"Content-Type": "application/json", "X-Signature": sig}

        status, _ = self._send_request("POST", "/webhooks/ring", body, headers)
        self.assertEqual(status, 200)

        conn = get_connection(self.db_path)
        rt = get_routine(conn, "rt_camel_routine")
        self.assertEqual(rt["last_satisfied_at"], "2026-10-12T14:17:23Z")
        conn.close()

    # Extra: GET /healthz returns 200 {"status": "ok"}
    def test_healthz_endpoint(self):
        status, data = self._send_request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode("utf-8")), {"status": "ok"})

    def test_missing_id_different_payloads_two_rows(self):
        """
        Two distinct webhooks without request_id or data.id must not collide on 'missing_id'.
        Each should receive a unique hash-derived external_id and both be stored in events.
        """
        payload1 = json.dumps({
            "data": {
                "type": "motion_detected",
                "attributes": {
                    "source": "cam_front",
                    "timestamp": 1791814643000,
                }
            }
        }).encode("utf-8")
        payload2 = json.dumps({
            "data": {
                "type": "motion_detected",
                "attributes": {
                    "source": "cam_back",
                    "timestamp": 1791814644000,
                }
            }
        }).encode("utf-8")

        sig1 = compute_signature(payload1, self.secret)
        st1, _ = self._send_request("POST", "/webhooks/ring", payload1, {"X-Signature": sig1})
        self.assertEqual(st1, 200)

        sig2 = compute_signature(payload2, self.secret)
        st2, _ = self._send_request("POST", "/webhooks/ring", payload2, {"X-Signature": sig2})
        self.assertEqual(st2, 200)

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT source, external_id, device_id FROM events WHERE source = 'webhook'")
        rows = cur.fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][1], rows[1][1])


if __name__ == "__main__":
    unittest.main()

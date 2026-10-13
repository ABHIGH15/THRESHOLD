"""
Tests for scripts/probe_ring.py:
- .env parser behavior (quotes, comments, blanks, no-override)
- Token and header redaction logic
- Missing token dry run handling (exit code 2)
- Fake HTTP layer tests for new endpoints (device_status, device_capabilities, user_profile)
- Explicit 401 and 404 error handling
- Error isolation across steps
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts.probe_ring import (
    parse_env_file,
    perform_call,
    redact_data,
    redact_string,
)


class FakeResponse:
    """Mock context manager for urllib.request.urlopen."""

    def __init__(self, status=200, headers=None, body=b"{}"):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = io.BytesIO(body)

    def read(self):
        return self._body.read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def make_http_error(url, code, msg, body=b'{"error": "mock_error"}'):
    """Create a urllib.error.HTTPError with readable body."""
    return urllib.error.HTTPError(
        url,
        code,
        msg,
        hdrs={"Content-Type": "application/json"},
        fp=io.BytesIO(body),
    )


class TestEnvParser(unittest.TestCase):
    """Test custom tiny .env parser."""

    def test_parses_quotes_comments_and_blanks(self):
        sample_env = """
        # This is a comment
        
        KEY_PLAIN=simple_val
        KEY_DOUBLE="double quoted value"
        KEY_SINGLE='single quoted value'
        # Another comment
        KEY_WITH_EQUALS=foo=bar=baz
        """
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(sample_env)
            f_path = f.name

        try:
            parsed = parse_env_file(f_path)
            self.assertEqual(parsed.get("KEY_PLAIN"), "simple_val")
            self.assertEqual(parsed.get("KEY_DOUBLE"), "double quoted value")
            self.assertEqual(parsed.get("KEY_SINGLE"), "single quoted value")
            self.assertEqual(parsed.get("KEY_WITH_EQUALS"), "foo=bar=baz")
            self.assertNotIn("# This is a comment", parsed)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_does_not_override_existing_environ(self):
        os.environ["PRE_EXISTING_KEY"] = "original_val"
        sample_env = "PRE_EXISTING_KEY=override_attempt\nNEW_KEY=new_val\n"
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(sample_env)
            f_path = f.name

        try:
            parsed = parse_env_file(f_path)
            self.assertNotIn("PRE_EXISTING_KEY", parsed)
            self.assertEqual(parsed.get("NEW_KEY"), "new_val")
        finally:
            os.environ.pop("PRE_EXISTING_KEY", None)
            if os.path.exists(f_path):
                os.remove(f_path)


class TestRedaction(unittest.TestCase):
    """Test token and Authorization header redaction."""

    def test_redact_string(self):
        token = "secret_token_12345"
        text = f"Calling https://api.amazonvision.com with Bearer {token} now"
        redacted = redact_string(text, token)
        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_nested_structures(self):
        token = "secret_token_12345"
        data = {
            "Authorization": f"Bearer {token}",
            "authorization": f"Bearer {token}",
            "info": {
                "url": f"https://api.amazonvision.com?token={token}",
                "items": [f"token-is-{token}", 123, True],
            },
        }
        redacted = redact_data(data, token)
        self.assertEqual(redacted["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["authorization"], "[REDACTED]")
        self.assertNotIn(token, redacted["info"]["url"])
        self.assertIn("[REDACTED]", redacted["info"]["url"])
        self.assertEqual(redacted["info"]["items"][0], "token-is-[REDACTED]")
        self.assertEqual(redacted["info"]["items"][1], 123)


class TestDryRunNoToken(unittest.TestCase):
    """Test CLI execution when RING_ACCESS_TOKEN is missing."""

    def test_missing_token_exits_code_2(self):
        env = os.environ.copy()
        env.pop("RING_ACCESS_TOKEN", None)

        with tempfile.TemporaryDirectory() as empty_dir:
            cmd = [sys.executable, os.path.abspath("scripts/probe_ring.py")]
            proc = subprocess.run(
                cmd,
                cwd=empty_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn(
                "RING_ACCESS_TOKEN missing - generate a Playground token and put it in .env",
                proc.stdout,
            )


class TestHttpProbeSteps(unittest.TestCase):
    """Test individual HTTP steps and error cases using fake HTTP layer."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token = "fake_test_token_xyz"

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("urllib.request.urlopen")
    def test_device_status_step(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            status=200,
            body=json.dumps({"data": {"id": "cam_1", "attributes": {"online": True}}}).encode("utf-8"),
        )
        summary = perform_call(
            "GET",
            "https://api.amazonvision.com/v1/devices/cam_1/status",
            self.token,
            "device_status",
            self.temp_dir.name,
        )
        self.assertEqual(summary["status"], 200)
        self.assertEqual(summary["device_count"], "online:True")
        # Verify saved evidence file
        files = os.listdir(self.temp_dir.name)
        self.assertTrue(any(f.startswith("device_status_") and f.endswith(".json") for f in files))

    @patch("urllib.request.urlopen")
    def test_device_capabilities_step(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            status=200,
            body=json.dumps({"data": {"id": "cam_1", "attributes": {"capabilities": ["motion", "video"]}}}).encode("utf-8"),
        )
        summary = perform_call(
            "GET",
            "https://api.amazonvision.com/v1/devices/cam_1/capabilities",
            self.token,
            "device_capabilities",
            self.temp_dir.name,
        )
        self.assertEqual(summary["status"], 200)
        self.assertEqual(summary["event_types"], "2 caps")

    @patch("urllib.request.urlopen")
    def test_user_profile_step(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            status=200,
            body=json.dumps({"data": {"id": "usr_99", "attributes": {"email": "user@example.com"}}}).encode("utf-8"),
        )
        summary = perform_call(
            "GET",
            "https://api.amazonvision.com/v1/users/me",
            self.token,
            "user_profile",
            self.temp_dir.name,
        )
        self.assertEqual(summary["status"], 200)
        self.assertEqual(summary["device_count"], "usr_99")

    @patch("urllib.request.urlopen")
    def test_http_401_error_handling(self, mock_urlopen):
        mock_urlopen.side_effect = make_http_error(
            "https://api.amazonvision.com/v1/devices",
            401,
            "Unauthorized",
            body=b'{"error": "token_expired"}',
        )
        summary = perform_call(
            "GET",
            "https://api.amazonvision.com/v1/devices",
            self.token,
            "list_devices",
            self.temp_dir.name,
        )
        self.assertEqual(summary["status"], 401)
        # Check evidence saved with error message
        files = [f for f in os.listdir(self.temp_dir.name) if f.startswith("list_devices_")]
        self.assertTrue(len(files) > 0)
        with open(os.path.join(self.temp_dir.name, files[0]), "r", encoding="utf-8") as f:
            ev = json.load(f)
        self.assertEqual(ev["error_message"], "token expired - generate a new Playground token")

    @patch("urllib.request.urlopen")
    def test_http_404_error_handling(self, mock_urlopen):
        mock_urlopen.side_effect = make_http_error(
            "https://api.amazonvision.com/v1/devices/missing/status",
            404,
            "Not Found",
            body=b'{"error": "not_found"}',
        )
        summary = perform_call(
            "GET",
            "https://api.amazonvision.com/v1/devices/missing/status",
            self.token,
            "device_status",
            self.temp_dir.name,
        )
        self.assertEqual(summary["status"], 404)
        files = [f for f in os.listdir(self.temp_dir.name) if f.startswith("device_status_")]
        self.assertTrue(len(files) > 0)
        with open(os.path.join(self.temp_dir.name, files[0]), "r", encoding="utf-8") as f:
            ev = json.load(f)
        self.assertEqual(ev["error_message"], "404 Not Found")


    @patch("urllib.request.urlopen")
    def test_post_with_body_sends_headers_and_payload(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            status=200,
            body=b'{"status": "ok"}',
        )
        body_payload = '{"components": ["camera_main"]}'
        summary = perform_call(
            "POST",
            "https://api.amazonvision.com/v1/devices/dev1/media/image/download",
            self.token,
            "snapshot_post",
            self.temp_dir.name,
            req_body_str=body_payload,
        )
        self.assertEqual(summary["status"], 200)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.data, body_payload.encode("utf-8"))

    @patch("urllib.request.urlopen")
    def test_binary_jpeg_response_saved_and_magic_detected(self, mock_urlopen):
        jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 512
        mock_urlopen.return_value = FakeResponse(
            status=200,
            headers={"Content-Type": "image/jpeg"},
            body=jpeg_bytes,
        )
        summary = perform_call(
            "POST",
            "https://api.amazonvision.com/v1/devices/dev1/media/image/download",
            self.token,
            "snapshot_jpeg",
            self.temp_dir.name,
            req_body_str="{}",
        )
        self.assertEqual(summary["status"], 200)
        self.assertTrue(summary["event_types"].startswith("JPEG"))
        # Verify saved .jpg file
        jpg_files = [f for f in os.listdir(self.temp_dir.name) if f.endswith(".jpg")]
        self.assertEqual(len(jpg_files), 1)
        with open(os.path.join(self.temp_dir.name, jpg_files[0]), "rb") as f:
            saved_bytes = f.read()
        self.assertEqual(saved_bytes, jpeg_bytes)

    @patch("urllib.request.urlopen")
    def test_binary_png_response_saved_and_magic_detected(self, mock_urlopen):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
        mock_urlopen.return_value = FakeResponse(
            status=200,
            headers={"Content-Type": "application/octet-stream"},
            body=png_bytes,
        )
        summary = perform_call(
            "POST",
            "https://api.amazonvision.com/v1/devices/dev1/media/image/download",
            self.token,
            "snapshot_png",
            self.temp_dir.name,
            req_body_str="{}",
        )
        self.assertEqual(summary["status"], 200)
        self.assertTrue(summary["event_types"].startswith("PNG"))
        # Verify saved .png file
        png_files = [f for f in os.listdir(self.temp_dir.name) if f.endswith(".png")]
        self.assertEqual(len(png_files), 1)

    @patch("urllib.request.urlopen")
    def test_error_response_saved_without_crashing(self, mock_urlopen):
        error_body = b'{"error": "component_required", "message": "Missing camera component"}'
        mock_urlopen.side_effect = make_http_error(
            "https://api.amazonvision.com/v1/devices/dev1/media/image/download",
            400,
            "Bad Request",
            body=error_body,
        )
        summary = perform_call(
            "POST",
            "https://api.amazonvision.com/v1/devices/dev1/media/image/download",
            self.token,
            "snapshot_error",
            self.temp_dir.name,
            req_body_str="{}",
        )
        self.assertEqual(summary["status"], 400)
        # Verify evidence json saved containing error details
        ev_files = [f for f in os.listdir(self.temp_dir.name) if f.startswith("snapshot_error_") and f.endswith(".json")]
        self.assertEqual(len(ev_files), 1)
        with open(os.path.join(self.temp_dir.name, ev_files[0]), "r", encoding="utf-8") as f:
            ev_data = json.load(f)
        self.assertEqual(ev_data["status"], 400)
        self.assertIn("component_required", ev_data["raw_body"])


if __name__ == "__main__":
    unittest.main()

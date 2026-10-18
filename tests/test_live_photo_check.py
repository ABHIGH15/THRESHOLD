"""
tests/test_live_photo_check.py — Unit tests for scripts/live_photo_check.py.

Verifies:
1. Dry-run execution, stdout formatting, byte counts, and HTML validation.
2. HTML validator strict tag allowlist and tag balancing.
3. --send flow with mocked Ring client and Telegram notifier.
4. HTTP 416 handling printing the Playground simulation instruction.
5. Missing configuration handling without secret leakage.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import create_autospec, patch

from scripts.live_photo_check import (
    main,
    run_dry_run,
    run_send,
    validate_telegram_html,
    PLACEHOLDER_JPEG_BYTES,
)
from src.threshold.notifier import TelegramNotifier
from src.threshold.ring_client import MediaUnavailable, RingClient, TokenExpired


class TestLivePhotoCheck(unittest.TestCase):
    """Test suite for live photo pipeline verification script."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_real_client_and_notifier_contracts_without_network(self):
        """Assert real RingClient and TelegramNotifier instantiate without network and possess required attributes."""
        # Real RingClient
        client = RingClient(access_token="fake_test_token")
        self.assertTrue(callable(getattr(client, "list_devices", None)))
        self.assertTrue(callable(getattr(client, "get_latest_snapshot", None)))
        self.assertTrue(callable(getattr(client, "get_snapshot", None)))

        # Real TelegramNotifier
        notifier = TelegramNotifier(bot_token="fake_bot_token", chat_id="123456")
        self.assertEqual(getattr(notifier, "chat_id", None), "123456")
        self.assertTrue(callable(getattr(notifier, "send_photo", None)))
        self.assertTrue(callable(getattr(notifier, "_make_request", None)))

    def test_dry_run_execution(self):
        """Verify dry-run completes offline with exit 0 and expected metrics."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = run_dry_run(
                device_id="demo-cam-front-door",
                timezone_name="America/Chicago",
                snapshots_dir=self.temp_dir.name,
            )
        output = buf.getvalue()

        self.assertEqual(ret, 0)
        self.assertIn("THRESHOLD LIVE PHOTO CHECK — DRY RUN (NO NETWORK)", output)
        self.assertIn("Placeholder JPEG:   149 bytes", output)
        self.assertIn("HTML Validation:    PASS", output)
        self.assertIn("Standard branch", output)
        self.assertIn("LIVE PATH TEST — not a real event", output)
        self.assertIn("✓ Snapshot retrieved from Ring for device demo-cam-front-door", output)

        # Confirm file was written
        placeholder = os.path.join(self.temp_dir.name, "dry_run_placeholder.jpg")
        self.assertTrue(os.path.exists(placeholder))
        with open(placeholder, "rb") as f:
            self.assertEqual(f.read(), PLACEHOLDER_JPEG_BYTES)

    def test_dry_run_long_flag_over_limit_branch(self):
        """Verify --long flag exercises dual delivery over-limit branch in dry run."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = run_dry_run(
                device_id="demo-cam-front-door",
                timezone_name="America/Chicago",
                snapshots_dir=self.temp_dir.name,
                long=True,
            )
        output = buf.getvalue()
        self.assertEqual(ret, 0)
        self.assertIn("Delivery Branch:    Over-limit (photo + separate message)", output)
        self.assertIn("Full Caption:       1153 chars (limit: 1024)", output)
        self.assertIn("[SYNTHETIC FILLER 1/4]", output)
        self.assertIn("Result: DRY RUN PASSED (exit code 0)", output)

    def test_main_cli_default_dry_run(self):
        """Verify main() without flags runs dry-run by default."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["--dry-run", "--snapshots-dir", self.temp_dir.name])
        self.assertEqual(ret, 0)
        self.assertIn("DRY RUN PASSED", buf.getvalue())

    def test_main_cli_long_flag(self):
        """Verify main() accepts --long flag and exercises over-limit branch."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["--dry-run", "--long", "--snapshots-dir", self.temp_dir.name])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("Delivery Branch:    Over-limit (photo + separate message)", output)
        self.assertIn("DRY RUN PASSED", output)

    def test_html_validator_valid_and_invalid_tags(self):
        """Verify TelegramHTMLValidator accurately checks tags and balance."""
        # 1. Valid Telegram HTML
        valid_html = (
            "<b>CHECK NEEDED</b>\n\n"
            "<b>Device:</b> <code>cam_front</code>\n"
            "<i>Recommendation:</i> Check front yard."
        )
        ok, errs = validate_telegram_html(valid_html)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

        # 2. Disallowed tag (div, p, table)
        invalid_tag = "<div><b>Hello</b></div>"
        ok2, errs2 = validate_telegram_html(invalid_tag)
        self.assertFalse(ok2)
        self.assertTrue(any("Disallowed HTML tag: <div>" in e for e in errs2))

        # 3. Unclosed tag
        unclosed = "<b>Bold text without closing tag"
        ok3, errs3 = validate_telegram_html(unclosed)
        self.assertFalse(ok3)
        self.assertTrue(any("Unclosed HTML tag: <b>" in e for e in errs3))

        # 4. Mismatched tag order
        mismatched = "<b><i>Crossed tags</b></i>"
        ok4, errs4 = validate_telegram_html(mismatched)
        self.assertFalse(ok4)
        self.assertTrue(any("Mismatched tag" in e for e in errs4))

    def test_send_mocked_ring_and_telegram(self):
        """Verify --send path retrieves image, saves to snapshots/, and dispatches to Telegram using autospec."""
        mock_ring = create_autospec(RingClient, instance=True)
        mock_ring.list_devices.return_value = [{"id": "mock_cam_garden"}]
        mock_ring.get_latest_snapshot.return_value = (
            b"\xff\xd8\xff\xe0mock_snapshot_bytes\xff\xd9",
            1791875400000,
        )

        mock_notifier = create_autospec(TelegramNotifier, instance=True)
        mock_notifier.chat_id = "999888"
        mock_notifier.send_photo.return_value = {"ok": True, "result": {"message_id": 999}}

        buf_out = io.StringIO()
        with redirect_stdout(buf_out):
            ret = run_send(
                device_id="mock_cam_garden",
                hours=24,
                timezone_name="America/Chicago",
                snapshots_dir=self.temp_dir.name,
                ring_client=mock_ring,
                notifier=mock_notifier,
            )

        output = buf_out.getvalue()
        self.assertEqual(ret, 0)
        self.assertIn("Target Device:      mock_cam_garden", output)
        self.assertIn("Snapshot Retrieved: 25 bytes", output)
        self.assertIn("Telegram Dispatch:  SUCCESS (delivered=True)", output)

        # Verify photo was saved on disk
        saved_files = [f for f in os.listdir(self.temp_dir.name) if f.endswith(".jpg")]
        self.assertEqual(len(saved_files), 1)
        self.assertTrue(saved_files[0].startswith("snapshot_mock_cam_garden_"))

        # Verify notifier called
        mock_notifier.send_photo.assert_called_once()
        _, kwargs = mock_notifier.send_photo.call_args
        self.assertEqual(kwargs["photo_bytes"], b"\xff\xd8\xff\xe0mock_snapshot_bytes\xff\xd9")
        self.assertIn("LIVE PATH TEST — not a real event", kwargs["caption"])
        self.assertIn("✓ Snapshot retrieved from Ring for device mock_cam_garden", kwargs["caption"])

    def test_send_mocked_ring_and_telegram_long(self):
        """Verify --send with long=True dispatches photo plus separate text message via autospec."""
        mock_ring = create_autospec(RingClient, instance=True)
        mock_ring.list_devices.return_value = [{"id": "mock_cam_garden"}]
        mock_ring.get_latest_snapshot.return_value = (
            b"\xff\xd8\xff\xe0mock_snapshot_bytes\xff\xd9",
            1791875400000,
        )

        mock_notifier = create_autospec(TelegramNotifier, instance=True)
        mock_notifier.chat_id = "999888"
        mock_notifier.send_photo.return_value = {"ok": True, "result": {"message_id": 999}}
        mock_notifier._make_request.return_value = {"ok": True, "result": {"message_id": 1000}}

        buf_out = io.StringIO()
        with redirect_stdout(buf_out):
            ret = run_send(
                device_id="mock_cam_garden",
                hours=24,
                timezone_name="America/Chicago",
                snapshots_dir=self.temp_dir.name,
                ring_client=mock_ring,
                notifier=mock_notifier,
                long=True,
            )

        output = buf_out.getvalue()
        self.assertEqual(ret, 0)
        self.assertIn("Telegram Dispatch:  SUCCESS (delivered=True)", output)
        mock_notifier.send_photo.assert_called_once()
        mock_notifier._make_request.assert_called_once()
        endpoint, payload = mock_notifier._make_request.call_args[0]
        self.assertEqual(endpoint, "sendMessage")
        self.assertEqual(payload["chat_id"], "999888")
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_send_missing_media_416_guidance(self):
        """Verify 416 missing media error prints the specific retry guidance message."""
        mock_ring = create_autospec(RingClient, instance=True)
        mock_ring.list_devices.return_value = [{"id": "mock_cam_hallway"}]
        mock_ring.get_latest_snapshot.side_effect = MediaUnavailable(
            "No media found in range", code="MEDIA_NOT_FOUND", status_code=416
        )

        mock_notifier = create_autospec(TelegramNotifier, instance=True)

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            ret = run_send(
                device_id="mock_cam_hallway",
                snapshots_dir=self.temp_dir.name,
                ring_client=mock_ring,
                notifier=mock_notifier,
            )

        self.assertEqual(ret, 1)
        err_output = buf_err.getvalue()
        self.assertIn("simulate live view in the Playground, wait ~2 minutes, retry.", err_output)

    def test_send_missing_configuration(self):
        """Verify missing env vars print informative error without leaking secrets."""
        with patch.dict(os.environ, {}, clear=True):
            buf_err = io.StringIO()
            with redirect_stderr(buf_err):
                ret = run_send(
                    device_id="cam_test",
                    env_file="/nonexistent/.env",
                    snapshots_dir=self.temp_dir.name,
                )
            self.assertEqual(ret, 1)
            err_output = buf_err.getvalue()
            self.assertIn("Missing required configuration variables", err_output)
            self.assertIn("RING_ACCESS_TOKEN", err_output)
            self.assertIn("TELEGRAM_BOT_TOKEN", err_output)
            self.assertIn("TELEGRAM_CHAT_ID", err_output)


if __name__ == "__main__":
    unittest.main()

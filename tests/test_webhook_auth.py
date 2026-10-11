"""
Tests for HMAC-SHA256 webhook signature verification.

Phase 3 DoD requires:
  - HMAC-SHA256 verification tested against both a valid and a
    deliberately corrupted signature
  - The corrupted one must be rejected
"""

import unittest
import json

from src.threshold.webhook_auth import (
    verify_signature,
    compute_signature,
    SignatureVerificationError,
)


class TestSignatureVerification(unittest.TestCase):
    """Test HMAC-SHA256 signature verification."""

    SHARED_SECRET = "test-ring-webhook-secret-do-not-use-in-prod"

    SAMPLE_PAYLOAD = json.dumps({
        "data": {
            "event_type": "motion_detected",
            "device_id": "front_door_cam",
            "subType": "human",
        }
    }).encode("utf-8")

    def test_valid_signature_passes(self):
        """A correctly signed request passes verification."""
        sig = compute_signature(self.SAMPLE_PAYLOAD, self.SHARED_SECRET)
        result = verify_signature(self.SAMPLE_PAYLOAD, sig, self.SHARED_SECRET)
        self.assertTrue(result)

    def test_corrupted_signature_rejected(self):
        """A deliberately corrupted signature is rejected."""
        sig = compute_signature(self.SAMPLE_PAYLOAD, self.SHARED_SECRET)
        # Corrupt one character
        corrupted = sig[:-1] + ("a" if sig[-1] != "a" else "b")
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_signature(self.SAMPLE_PAYLOAD, corrupted, self.SHARED_SECRET)
        self.assertIn("mismatch", str(ctx.exception))

    def test_wrong_secret_rejected(self):
        """A signature computed with a different secret is rejected."""
        sig = compute_signature(self.SAMPLE_PAYLOAD, "wrong-secret")
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_signature(self.SAMPLE_PAYLOAD, sig, self.SHARED_SECRET)
        self.assertIn("mismatch", str(ctx.exception))

    def test_tampered_body_rejected(self):
        """If the body is modified after signing, verification fails."""
        sig = compute_signature(self.SAMPLE_PAYLOAD, self.SHARED_SECRET)
        tampered = self.SAMPLE_PAYLOAD + b"extra"
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_signature(tampered, sig, self.SHARED_SECRET)
        self.assertIn("mismatch", str(ctx.exception))

    def test_empty_signature_rejected(self):
        """An empty signature header is rejected."""
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_signature(self.SAMPLE_PAYLOAD, "", self.SHARED_SECRET)
        self.assertIn("Missing", str(ctx.exception))

    def test_empty_body_rejected(self):
        """An empty request body is rejected."""
        sig = compute_signature(b"anything", self.SHARED_SECRET)
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_signature(b"", sig, self.SHARED_SECRET)
        self.assertIn("Empty", str(ctx.exception))

    def test_no_secret_rejected(self):
        """Missing shared secret raises an error (misconfiguration catch)."""
        sig = compute_signature(self.SAMPLE_PAYLOAD, "anything")
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_signature(self.SAMPLE_PAYLOAD, sig, "")
        self.assertIn("No shared secret", str(ctx.exception))

    def test_compute_signature_deterministic(self):
        """Same input always produces same signature."""
        sig1 = compute_signature(self.SAMPLE_PAYLOAD, self.SHARED_SECRET)
        sig2 = compute_signature(self.SAMPLE_PAYLOAD, self.SHARED_SECRET)
        self.assertEqual(sig1, sig2)

    def test_different_payloads_different_signatures(self):
        """Different payloads produce different signatures."""
        sig1 = compute_signature(b'{"a": 1}', self.SHARED_SECRET)
        sig2 = compute_signature(b'{"a": 2}', self.SHARED_SECRET)
        self.assertNotEqual(sig1, sig2)


if __name__ == "__main__":
    unittest.main()

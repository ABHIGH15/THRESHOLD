"""
HMAC-SHA256 webhook signature verification for Ring Partner API.

From threshold_spec.md ground-truth table:
  "Webhooks: HMAC-SHA256 X-Signature, 5s response, JSON:API"

This module verifies that incoming webhook requests are genuinely from Ring
by checking the HMAC-SHA256 signature in the X-Signature header against
the raw request body using the shared secret from Ring's Partner App config.

Phase 3 will wire this into the webhook Lambda handler.
Phase 2's stub handler accepts all requests without verification.
"""

import hashlib
import hmac
import time
from typing import Optional


class SignatureVerificationError(Exception):
    """Raised when webhook signature verification fails."""
    pass


def verify_signature(
    raw_body: bytes,
    signature_header: str,
    shared_secret: str,
    max_age_seconds: Optional[int] = None,
) -> bool:
    """
    Verify the HMAC-SHA256 signature of a Ring webhook request.

    Args:
        raw_body: The raw request body as bytes (NOT parsed JSON —
                  the signature is computed over the exact bytes Ring sent).
        signature_header: The value of the X-Signature header from the request.
        shared_secret: The webhook signing secret from Ring Partner App config.
        max_age_seconds: Optional. If set, reject signatures older than this
                        (requires a timestamp in the signature scheme — Ring's
                        exact scheme is TODO-VERIFY in Phase 0 portal access).

    Returns:
        True if the signature is valid.

    Raises:
        SignatureVerificationError: If the signature is invalid or missing.
    """
    if not signature_header:
        raise SignatureVerificationError("Missing X-Signature header")

    if not raw_body:
        raise SignatureVerificationError("Empty request body")

    if not shared_secret:
        raise SignatureVerificationError("No shared secret configured")

    # Compute expected signature
    expected = hmac.new(
        key=shared_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(expected, signature_header):
        raise SignatureVerificationError(
            "Signature mismatch — request may not be from Ring"
        )

    return True


def compute_signature(raw_body: bytes, shared_secret: str) -> str:
    """
    Compute the HMAC-SHA256 signature for a request body.

    Useful for:
    - Testing: generate valid signatures for synthetic payloads
    - Debugging: compare against received X-Signature values

    Args:
        raw_body: The raw request body as bytes.
        shared_secret: The webhook signing secret.

    Returns:
        The hex-encoded HMAC-SHA256 signature.
    """
    return hmac.new(
        key=shared_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

"""
Webhook server for Ring events.
Stdlib only: http.server, threading, json, os, sys.

Runs ThreadingHTTPServer on 127.0.0.1 (configurable port).
Verifies HMAC-SHA256 signature BEFORE parsing JSON.
Rejects > 1 MB with 413.
Endpoints:
- POST /webhooks/ring
- GET /healthz
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
import os
import sys
import threading
from typing import Optional, Tuple
import urllib.parse

from src.threshold.ingest import ingest_event
from src.threshold.models import CONFIRMED_EVENT_TYPES
from src.threshold.store import get_connection, init_db
from src.threshold.webhook_auth import (
    SignatureVerificationError,
    verify_signature,
)

logger = logging.getLogger("threshold.server")
MAX_BODY_BYTES = 1024 * 1024  # 1 MB


def get_webhook_secret() -> str:
    """Read RING_WEBHOOK_SECRET from environment."""
    return os.environ.get("RING_WEBHOOK_SECRET", "").strip()


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Threshold webhook and healthz endpoints."""

    # Silence default stderr logging during tests
    def log_message(self, format, *args):
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def do_GET(self):
        """Handle GET requests: /healthz only."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            resp = b'{"status": "ok"}\n'
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        """Handle POST requests: /webhooks/ring only."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/webhooks/ring":
            self.send_error(404, "Not Found")
            return

        # 1. Check Content-Length and body size limit (reject > 1MB with 413)
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid Content-Length")
            return

        if content_length > MAX_BODY_BYTES:
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            resp = b'{"error": "Payload Too Large - body exceeds 1 MB"}\n'
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            self.close_connection = True
            return

        raw_body = self.rfile.read(content_length)

        # 2. Check and verify signature BEFORE parsing JSON
        sig_header = self.headers.get("X-Signature", "")
        secret = getattr(self.server, "webhook_secret", None) or get_webhook_secret()

        if not sig_header or not secret:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            resp = b'{"error": "Unauthorized: missing or empty signature/secret"}\n'
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        try:
            verify_signature(raw_body, sig_header, secret)
        except SignatureVerificationError as e:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            resp = json.dumps({"error": f"Unauthorized: {e}"}).encode("utf-8") + b"\n"
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 3. Parse JSON body (400 if malformed)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            resp = json.dumps({"error": f"Malformed JSON payload: {e}"}).encode("utf-8") + b"\n"
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 4. Extract fields according to documented Ring v1.1 payload format
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        attrs = data.get("attributes", {}) if isinstance(data, dict) else {}

        # External ID / Idempotency Key
        body_hash = hashlib.sha256(raw_body).hexdigest()[:32]
        external_id = meta.get("request_id") or data.get("id") or body_hash

        # Event type
        raw_event_type = data.get("type", "unrecognized")
        if raw_event_type not in CONFIRMED_EVENT_TYPES:
            logger.warning("Unrecognized event type: %s", raw_event_type)
            event_type = "unrecognized"
        else:
            event_type = raw_event_type

        # Device ID
        device_id = attrs.get("source") or "unknown_device"

        # Sub type (support attributes.sub_type or camelCase data.subType)
        sub_type = attrs.get("sub_type") or data.get("subType")

        # Occurred at timestamp
        occurred_at = attrs.get("timestamp") or meta.get("time")

        # 5. Ingest into storage layer (do NOT swallow storage errors)
        db_path = getattr(self.server, "db_path", None)
        try:
            conn = get_connection(db_path)
            try:
                # Check for forced storage failure injection (used in tests)
                if getattr(self.server, "force_storage_failure", False):
                    raise RuntimeError("Forced storage failure for testing 5xx response")

                ingest_event(
                    conn=conn,
                    source="webhook",
                    external_id=external_id,
                    device_id=device_id,
                    event_type=event_type,
                    sub_type=sub_type,
                    occurred_at=occurred_at,
                    raw_json=payload,
                )
            finally:
                conn.close()
        except Exception as e:
            logger.error("Storage error during webhook ingestion: %s", e)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            resp = json.dumps({"error": f"Internal Server Error: {e}"}).encode("utf-8") + b"\n"
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # 6. Success response: 200 OK (fast response < 5s)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        resp = b'{"status": "received"}\n'
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    db_path: Optional[str] = None,
    secret: Optional[str] = None,
) -> ThreadingHTTPServer:
    """Create and configure ThreadingHTTPServer instance."""
    server = ThreadingHTTPServer((host, port), WebhookHandler)
    server.db_path = db_path
    server.webhook_secret = secret
    server.force_storage_failure = False
    return server


def run_server(port: Optional[int] = None, db_path: Optional[str] = None) -> None:
    """Run the server synchronously."""
    from scripts.probe_ring import load_env

    load_env()
    server_port = port or int(os.environ.get("THRESHOLD_PORT", 8080))
    secret = get_webhook_secret()
    init_db(db_path)

    server = create_server(port=server_port, db_path=db_path, secret=secret)
    print(f"Starting Threshold Webhook Server on 127.0.0.1:{server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()

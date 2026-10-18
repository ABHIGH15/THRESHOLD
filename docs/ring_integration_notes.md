# Ring Partner API Integration Notes & Developer Friction Log

This document compiles integration observations, friction points, and product feedback based on hands-on integration with the Ring Partner API (`api.amazonvision.com`) and the Ring Developer Portal Playground during the October 2026 Hackathon.

Every observation is cited directly from verified response artifacts under `docs/ring_samples/` or `docs/dev/BUILD_LOG.md`.

---

## 1. Summary of Partner API Observations

| # | Topic | Finding / Observation | Evidence File & Date | Status |
|---|---|---|---|---|
| 1 | **Snapshot HTTP 303 Redirect** | `POST /v1/devices/{id}/media/image/download` returns HTTP 303 even when no media exists; HTTP 416 occurs only on subsequent GET to download host. | `docs/ring_samples/c2_case_a_latest_in_range.json` (2026-10-15) | VERIFIED LIVE |
| 2 | **Snapshot at Current Timestamp** | Requesting `at_timestamp=now` returns HTTP 416 `MEDIA_NOT_FOUND`; snapshots are post-recording extracts, not live camera shutters. | `docs/ring_samples/c2_case_b_at_timestamp_now.json` (2026-10-15) | VERIFIED LIVE |
| 3 | **Undocumented Download Headers** | Snapshot download responses carry undocumented media metadata headers (`X-Media-Timestamp`, `Last-Modified`, `X-Media-Origin`). | `docs/ring_samples/c2_case_a2_latest_in_range_recording.json` (2026-10-15) | VERIFIED LIVE |
| 4 | **Playground Token Expiry** | Playground OAuth tokens expire after exactly 30 minutes, returning HTTP 401 `Unauthorized` on expiry. | `docs/ring_samples/FINDINGS.md` (2026-10-12) | VERIFIED LIVE |
| 5 | **Playground Webhook Delivery** | The Developer Playground does not deliver simulated events to external or local webhook URLs; events appear solely in the browser UI. | `docs/ring_samples/FINDINGS.md` (2026-10-12) | VERIFIED LIVE |
| 6 | **Sandbox Event History Types** | Event history in the sandbox contains only `event_type: "on_demand"`; simulated Playground motion does not appear in history queries. | `docs/ring_samples/event_history.json` (2026-10-12) | VERIFIED LIVE |
| 7 | **Webhook Schema Attribute** | Documented webhook payloads use `data.attributes.sub_type` (e.g. `"human"`), distinct from event history enum naming. | Starter repo documentation (`ring-api-helloworld`) | DOC-VERIFIED (UNVERIFIED on live wire) |
| 8 | **Non-Camera Devices Untested** | The sandbox account used lists 1 device, doorbell_v4; contact sensors and smart locks are absent from this account. | `docs/ring_samples/list_devices.json` (2026-10-13) | VERIFIED LIVE |
| 9 | **Device Online Status Semantics** | In the sandbox environment when a camera is idle, `attributes.online: false` reflects an idle state; system safety logic correctly downgrades to `STATUS_UNKNOWN` and suppresses escalations. | `docs/ring_samples/device_status.json` (2026-10-12) | VERIFIED LIVE |

---

## 2. Detailed Findings & Developer Feedback

### Finding 1: Two-Step 303 Snapshot Flow & 416 Error Signaling
- **Observation:** Calling `POST https://api.amazonvision.com/v1/devices/{device_id}/media/image/download` returns `HTTP 303 See Other` with a pre-signed `Location` URL (`https://download-ap-northeast-1.prod.phoenix.devices.amazon.dev/v1/download?...`). When no snapshot exists within the requested window, the `POST` still succeeds with `303`, but the subsequent `GET` to the download host fails with `HTTP 416 Requested Range Not Satisfiable` / `MEDIA_NOT_FOUND`.
- **How Verified:** `docs/ring_samples/c2_case_a_latest_in_range.json` and `docs/ring_samples/c2_case_b_at_timestamp_now.json` (2026-10-15).
- **Authentication Detail:** The subsequent `GET` to the presigned download URL must be executed **without** the `Authorization: Bearer <token>` header, or the download host rejects the request.
- **Developer Impact:** Standard HTTP clients following redirects automatically either forward headers or raise errors late. Code must intercept the 303 redirect, strip the auth header, and handle 416 as expected missing media rather than fatal infrastructure failure.
- **Product Suggestion:** Return HTTP 404 or 416 directly on the `POST` if no media is available in range, avoiding an unnecessary round-trip to the download CDN.

### Finding 2: Snapshots Are Historical Recording Stills, Not Instant Captures
- **Observation:** Calling `get_snapshot` with `type: "at_timestamp"` for current time (`now`) consistently fails with HTTP 416. Stills can only be extracted from completed video recordings.
- **How Verified:** Tested in `docs/ring_samples/c2_case_b_at_timestamp_now.json` (timestamp `1792036749834` returned 416). Successfully extracted an 88,903 byte JPEG only when targeting an interval covering a past recorded session (`docs/ring_samples/c2_case_a2_latest_in_range_recording.json`).
- **Developer Impact:** Applications cannot use the snapshot endpoint as an on-demand real-time camera trigger. The integration must query a retrospective window (`latest_in_range`).
- **Product Suggestion:** Document clearly in Partner API reference that snapshot generation is tied to completed cloud recordings.

### Finding 3: High-Value Undocumented Download Headers
- **Observation:** Successful snapshot image downloads include headers that are not documented in the public Partner API specification:
  - `X-Media-Timestamp`: Millisecond epoch timestamp of the exact video frame captured.
  - `X-Media-Origin`: Origin classification (e.g. `"recording"`).
  - `Last-Modified`: HTTP date string of the recording artifact.
  - `Content-Encryption`: `"none"` (verified response header in `docs/ring_samples/c2_case_a2_latest_in_range_recording.json`, line 20).
- **How Verified:** `docs/ring_samples/c2_case_a2_latest_in_range_recording.json` (lines 14–25).
- **Developer Impact:** These headers allow Threshold to display the exact capture time to caregivers (e.g. `taken 02:10 CDT (2 min ago)`) without guessing or assuming receipt time equals capture time.
- **Product Suggestion:** Formalize and document `X-Media-Timestamp` and `X-Media-Origin` in the official Ring Partner API documentation.

### Finding 4: Playground Webhook Ingestion Gap
- **Observation:** The Ring Developer Portal Playground allows developers to click "Simulate motion event", "Simulate package event", and "Simulate vehicle event", but events are rendered only within the browser console. There is no configuration option to deliver webhook payloads to an external HTTPS webhook URL.
- **How Verified:** `docs/ring_samples/FINDINGS.md` (Section 5).
- **Developer Impact:** Developers cannot test real webhook handlers (signature validation, HMAC computation, retry handling) against the Playground sandbox. Developers must either build a polling fallback against `GET /v1/history/...` or construct synthetic webhook simulators.
- **Product Suggestion:** Add a "Webhook Destination URL" input in the Playground UI with an HMAC signing secret for webhook push testing.

### Finding 5: Discrepancy Between Event History and Webhook Enums
- **Observation:** Historical events queried via `GET /v1/history/devices/{device_id}/events` use `attributes.event_type: "on_demand"` or `"motion.human"`. Webhook payloads documented for the Partner API use `data.type: "motion_detected"` with `data.attributes.sub_type: "human"`.
- **How Verified:** `docs/ring_samples/event_history.json` vs Ring starter repo webhook schemas.
- **Developer Impact:** An ingest pipeline processing both webhooks and event history must maintain bidirectional enum normalization tables.
- **Product Suggestion:** Harmonize event type taxonomy between push webhooks and the Event History API.

### Finding 6: Non-Camera Devices in Sandbox
- **Observation:** The sandbox account used lists 1 device, doorbell_v4 (`docs/ring_samples/list_devices.json`); contact sensors, motion detectors, or alarm keypads are absent from this sandbox account.
- **How Verified:** `docs/ring_samples/list_devices.json` (lists 1 doorbell camera with `attributes.name: "Playground Device"`).
- **Developer Impact:** Routine monitoring for door sensors or cabinet contact sensors can only be tested using simulated payloads.
- **Product Suggestion:** Allow developers to instantiate simulated Ring Alarm contact sensors in the Developer Playground.

### Finding 7: Sandbox Device Online Semantics and Pipeline Fail-Safe Behavior
- **Observation:** `GET /v1/devices/{id}/status` returns `attributes.online: false` when a device is idle (`docs/ring_samples/device_status.json`). `docs/ring_samples/FINDINGS.md:35` records: `attributes.online: boolean (false when camera idle, true when live stream/session active)`.
- **Code Trace & Behavioral Proof:**
  1. `src/threshold/ring_client.py::poll_once`: Reads `attrs.get("online", False)` and records `online=False` in `device_health`.
  2. `src/threshold/scheduler.py::build_device_health_record`: If `not online`, returns `DeviceHealthRecord(last_event_at=None)`.
  3. `src/threshold/models.py::DeviceHealthRecord.is_stale`: Evaluates `last_event_at is None` and returns `True`.
  4. `src/threshold/status.py::derive_status`: When past the escalation deadline and `device_health.is_stale(now)`, `derive_status` returns `STATUS_UNKNOWN` instead of `STATUS_MISSED`.
  5. `src/threshold/scheduler.py::Scheduler.tick`: Routine evaluation only escalates `STATUS_MISSED`. Because the status is `STATUS_UNKNOWN`, exactly **0 absence escalations** are triggered.
  6. `src/threshold/agent/facts.py::build_gate_facts`: Derives `device_online_fresh = False` because `is_online` is `False`.
  7. `src/threshold/agent/gate.py::decide`: Rule 3 enforces `if not facts.device_online_fresh: return GateDecision(decision="check_needed", reason_code="device_not_fresh")`. Any agent-proposed escalation is downgraded to `check_needed`.
- **Verification Test:** `tests.test_scheduler.TestSchedulerAbsenceEngine.test_offline_fresh_device_returns_unknown_zero_escalations_and_gate_checks_needed` proves that an idle device with `online=False` and fresh timestamps results in `STATUS_UNKNOWN`, 0 absence escalations, and gate `check_needed` (`device_not_fresh`).
- **Architectural Options for Handling Idle Online State:**
  1. *Option 1 (Current Safe Baseline):* Treat `attributes.online: false` strictly as unconfirmed health. System fails safe by marking the routine `STATUS_UNKNOWN` and blocking automated absence escalations while keeping events visible on the dashboard.
  2. *Option 2 (Timestamp & Event Correlation):* Differentiate deep disconnection from an idle state by examining `reported_at` or recent event history timestamps (`docs/ring_samples/event_history.json`). If `reported_at` is within the staleness window (e.g. < 2 hours), treat device as reachable for event reception.
  3. *Option 3 (Active Health Probe):* Provide an active probe fallback (`RingClient.get_snapshot` via `media/image/download`) to verify camera responsiveness during evaluation.
- **Product Suggestion:** Document the semantics of `attributes.online` during idle periods, and provide a distinct status enum (e.g., distinguishing idle reachability from offline disconnection).

---

## 3. Redacted Sample Response Artifacts

All tokens, client IDs, and device identifiers below have been redacted for security.

### Sample 1: Device Discovery (`GET /v1/devices`)
```json
{
  "data": [
    {
      "type": "devices",
      "id": "ava1.ring.device.[REDACTED_DEVICE_ID]",
      "attributes": {
        "name": "Front Door",
        "kind": "doorbell_v4",
        "owner_id": "ava1.ring.account.[REDACTED_ACCOUNT_ID]"
      }
    }
  ]
}
```

### Sample 2: Device Status (`GET /v1/devices/{id}/status`)
```json
{
  "data": {
    "type": "device-status",
    "id": "ava1.ring.device.[REDACTED_DEVICE_ID]",
    "attributes": {
      "online": true,
      "reported_at": "2026-10-12T14:24:03Z"
    }
  }
}
```

### Sample 3: Snapshot Download Metadata (HTTP 303 Redirect Headers)
```text
HTTP/1.1 303 See Other
Date: Thu, 15 Oct 2026 04:01:06 GMT
Location: https://download-ap-northeast-1.prod.phoenix.devices.amazon.dev/v1/download?[REDACTED_PRESIGNED_QUERY]
x-request-id: 0a3768cb-2f5f-4d39-bdf8-8cb3412d132e
server: envoy
```

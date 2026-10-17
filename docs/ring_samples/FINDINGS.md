# Ring Partner API & Playground Findings (Redacted Sample)

*Note: This file records observations from Ring Partner API responses captured via `scripts/probe_ring.py` and Playground live session. Sensitive credentials and hardware identifiers have been redacted for public disclosure.*

---

## 1. Base URL and Auth Header
- **Base URL:** `https://api.amazonvision.com` (confirmed across all calls).
- **Auth Header:** `Authorization: Bearer <token>`
- **Token Format:** Playground OAuth token starts with `Atzr|[REDACTED]` (URL-encoded access token, 30-minute expiry strictly enforced; returned HTTP 401 when expired).
- **Server:** `envoy` upstream proxy, with `x-request-id` and `x-envoy-upstream-service-time`.

## 2. Device Fields
- **Data Envelope:** Standard JSON:API format:
  ```json
  {
    "data": [
      {
        "type": "devices",
        "id": "ava1.ring.device.EXAMPLE",
        "attributes": {
          "name": "Playground Camera",
          "kind": "doorbell_v4"
        }
      }
    ],
    "meta": { }
  }
  ```
- **Device ID Prefix:** Prefixed with `ava1.ring.device.`

## 3. Device Status Fields and Online/Offline Representation
- **Endpoint:** `GET /v1/devices/{id}/status`
- **Response Attributes:**
  - `attributes.online`: boolean (`false` when camera idle, `true` when live stream/session active).
  - `attributes.reported_at`: ISO 8601 timestamp (e.g. `"2026-10-12T08:37:05Z"`).
- **Architecture Validation:** Directly feeds device health checks (`is_stale()` logic) and provides primary-source proof of device reachability.

## 4. Capabilities (Snapshot, Video, Motion)
- **Endpoint:** `GET /v1/devices/{id}/capabilities`
- **Attributes confirmed:**
  - `image_enhancements.configurations`: includes `["color_night_vision", "privacy_zones", "snapshot"]`. Confirms device explicitly declares `snapshot` capability.
  - `video`: codecs `["AVC"]`, ratio `"16:9"`, max resolution `1080`.
  - `motion_detection.configurations`: `["enabled", "motion_zones"]`.

## 5. Event-History Fields and Pagination
- **Endpoint:** `GET /v1/history/devices/{id}/events`
- **Confirmed Structure (JSON:API):**
  - `type`: `"history-events"`
  - `id`: `"ava1.ring.history.event.EXAMPLE"`
  - `attributes.event_type`: `"on_demand"` (created during live view session)
  - `attributes.start`: millisecond unix epoch (e.g. `1791814643626`)
  - `attributes.end`: millisecond unix epoch (e.g. `1791814665385`)
  - `attributes.is_third_party_reviewed`: boolean
  - `relationships.source.data.id`: device ID
  - `relationships.cv_detections.data`: list of computer vision detections

## 6. Event Types Naming Seen in History
- Observed in real response: `event_type: "on_demand"`.
- Starter repo filter uses: `?event_types=motion.human,ding`.
- Webhooks spec uses: `motion_detected` with `subType: "human"`.
- *Key Takeaway:* History event types (`on_demand`, `motion.human`) use distinct enum values from raw webhook notifications (`motion_detected`).

## 7. Snapshot & Media Streaming Behavior
- **Live Stream WHEP (Confirmed from Playground UI):**
  - Endpoint: `POST https://api.amazonvision.com/v1/devices/{device_id}/media/streaming/whep/sessions`
  - Request: `Content-Type: application/sdp`, SDP Offer body.
  - Response: `HTTP/1.1 201 Created`, returns stream URL with `Location` header.
- **Watermarking (Confirmed from Video Stream Screenshot):**
  - Top-left: `ring` logo watermark.
  - Top-right: `Device ID: [REDACTED] Partner: Ring Developer Portal Playground`.
  - Bottom: timestamps (`CDT` and `UTC`).
  - *Conclusion: Non-removable server-side watermarking is actively applied by Ring on media.*
- **Snapshot Image Download Endpoint:**
  - Endpoint: `POST /v1/devices/{device_id}/media/image/download`
  - Calling with `{}` returned `HTTP 403`:
    ```json
    "detail": "Cannot authorize: missing required timestamp fields in request body"
    ```
  - Confirmed: Ring's snapshot endpoint requires specific timestamp fields in the request body (e.g. correlating to a recorded event or specific capture moment).

## 8. User Profile Fields
- **Endpoint:** `GET /v1/users/me`
- **Response Structure:** `data.id` with `ava1.ring.account.EXAMPLE` prefix.

## 9. Contradictions with Earlier Assumptions
- **Status Endpoint:** Originally assumed status came solely from `device_online` / `device_offline` webhooks. Confirmed `GET /v1/devices/{id}/status` provides real-time `online: boolean` and `reported_at: timestamp`.
- **Event Timestamps:** Event history uses millisecond integer timestamps (`start: 1791814643626`), not ISO strings.
- **Snapshot Parameters:** Snapshot download requires timestamp fields in the JSON body, rather than component query parameters.

# Threshold

Threshold is a state-aware pattern-absence detection system for remote caregivers monitoring elderly family members living alone. While Ring alerts you when motion occurs, Threshold monitors daily living routines and deterministically escalates when expected activity silently does not happen.

```bash
# Try it in 60 seconds (zero secrets, zero network calls, no Ring account needed):
python3 -m src.threshold demo
```

**Ring Partner APIs Used:**
- **Devices & Health (`GET /v1/devices`, `GET /v1/devices/{id}/status`):** Discovers hardware and tracks online freshness.
- **Event History (`GET /v1/history/devices/{id}/events`):** Polls recent motion events to verify routine satisfaction.
- **Image Snapshots (`POST /v1/devices/{id}/media/image/download`):** Fetches JPEG captures via two-step HTTP 303 redirect.
- **Webhook Ingestion (`POST /webhook`):** Receives Ring v1.1 push events with HMAC-SHA256 signature verification.

> **Scope Fence:** Threshold is a caregiver-support notification tool, not a medical or life-safety emergency device, and does not contact 911 or emergency services.
> **Disclaimer:** Independent hackathon project. Not affiliated with, endorsed by, or sponsored by Ring or Amazon.

---

## 1. What Threshold Is

Ring's Shared Users feature lets a remote caregiver see camera motion feeds, but a continuous stream of events is the wrong interface for someone who isn't there. Caregivers need to know property **state**:
1. Is Mom's morning routine proceeding normally?
2. Did she open the medicine cabinet between 07:00 and 09:00?
3. Did the expected evening movement fail to happen?

Most critically, the absence of activity produces **zero events**, making absence detection structurally impossible for reactive, event-driven webhooks.

Threshold bridges this gap:
- **Absence Detection:** Evaluates time windows with grace buffers against local state. If expected routine activity is missing and device health is fresh, it fires a deterministic escalation alert.
- **Unknown, Not Alarm:** If a camera is offline or its health check is stale, Threshold derives `UNKNOWN` and silences the alert ("no event ≠ no data").
- **Caregiver Triage:** Routine events are logged silently. Ambiguous quiet-hours motion initiates a bounded investigation displaying structured evidence and snapshot attachments.

---

## 2. Try It in 60 Seconds

The demo runs completely offline with zero configuration, zero credentials, and zero network calls using only Python's standard library and SQLite.

```bash
python3 -m src.threshold demo
```
*Tested on macOS and Linux with Python 3.14.7 (`Python 3.14+` recommended).*

### Real Demo Terminal Output (First 25 Lines)
```text
======================================================================================
  THRESHOLD — PATTERN-ABSENCE DETECTION FOR ELDERLY CARE
  'Ring tells you when something happens. Threshold tells you when something doesn't.'
======================================================================================
  Caregiver-support notification tool. Not an emergency or medical device.
  Does not contact emergency services.
  SIMULATED HOUSEHOLD (fictional) — Dallas, TX (America/Chicago)
  All devices, events, routines, and clock times in this demo are simulated.
  Device health polls:    SIMULATED (written with upsert_device_health, the function the real poller uses)
  Stack: Python standard library only (no pip dependencies) + SQLite.
  Zero network calls: all webhooks are signed and posted locally to 127.0.0.1.
--------------------------------------------------------------------------------------
  Pre-roll:  Replayed Sat/Sun missed routines through scheduler.tick() + callback. Historical false alarms in DB: 2.

[SCENE 1: CALM MORNING] Routine Observed — Peace of Mind
  Context:   Routine 'Morning movement' (07:00–09:00 CDT, +30m grace) on living room camera.
  Action:    Resident steps into living room at 07:42 CDT. Signed motion webhook posted.
  Result:    Event accepted (HTTP 200). Status derived: OBSERVED.
  Tick:      Clock advances past deadline to 09:31 CDT. Scheduler tick: 0 escalations.

[SCENE 2: TRUST BOUNDARY] Signature Check & Idempotent Ingestion
  Test 2a:   POST event with invalid HMAC signature.
  Result 2a: Rejected by server with HTTP 401 Unauthorized. Stored rows: 3 (unchanged).
  Test 2b:   Send valid signed event twice with identical meta.request_id.
  Result 2b: 1st POST HTTP 200 (events: 4). 2nd duplicate POST HTTP 200 (events: 4).
```

To run with an interactive web dashboard on loopback:
```bash
python3 -m src.threshold demo --serve
```

---

## 3. Ring Partner API Usage

All Ring Partner API interactions are implemented in `src/threshold/ring_client.py` and validated against the live Ring Developer Portal Playground (`api.amazonvision.com`).

| Endpoint | Used For | Code Reference | Live Status |
|---|---|---|---|
| `GET /v1/users/me` | Account token verification & account identity check | `scripts/probe_ring.py:75` | VERIFIED LIVE 2026-10-12 (`evidence/ring/user_profile_20261012T084113Z.json`) |
| `GET /v1/devices` | Discovers hardware devices (cameras/doorbells) for household | `src/threshold/ring_client.py:317` | VERIFIED LIVE 2026-10-12 (`evidence/ring/list_devices_20261012T084122Z.json`) |
| `GET /v1/devices/{id}/status` | Device health check (`online` boolean, `reported_at` timestamp) | `src/threshold/ring_client.py:326` | VERIFIED LIVE 2026-10-12 (`evidence/ring/device_status_20261012T084126Z.json`) |
| `GET /v1/devices/{id}/capabilities` | Probing device feature configurations (snapshot, motion zones) | `scripts/probe_ring.py:127` | VERIFIED LIVE 2026-10-12 (`evidence/ring/device_capabilities_20261012T084129Z.json`) |
| `GET /v1/history/devices/{id}/events` | Ingesting motion event history in poller mode | `src/threshold/ring_client.py:336` | VERIFIED LIVE 2026-10-12 (`evidence/ring/event_history_20261012T084125Z.json`) |
| `POST /v1/devices/{id}/media/image/download` | Initiating snapshot download via `latest_in_range` (HTTP 303 redirect) | `src/threshold/ring_client.py:352` | VERIFIED LIVE 2026-10-15 (`evidence/ring/c2_case_a2_latest_in_range_recording.json`) |
| `GET <download_url>` (from 303 `Location`) | Fetching snapshot JPEG bytes (executed **without** Authorization header) | `src/threshold/ring_client.py:377` | VERIFIED LIVE 2026-10-15 (`evidence/ring/c2_case_a2_latest_in_range_recording.jpg`) |
| `POST /webhook` | Receiving Ring v1.1 push webhook notifications | `src/threshold/server.py:59` | IMPLEMENTED, NOT LIVE-VERIFIED *(Playground cannot push webhooks; poll mode used instead)* |
| `POST /v1/devices/{id}/media/streaming/whep/sessions` | Live WebRTC/WHEP media streaming session | N/A (Observed in portal UI) | NOT USED *(Threshold monitors routine state, not live surveillance)* |

*Note: Live verification was performed exclusively against camera devices (`doorbell_v4`). Non-camera Ring device types (e.g., contact sensors, smart locks) are untested in the sandbox.*

---

## 4. REAL vs SIMULATED

Threshold explicitly separates production logic from simulated test scenarios:

| Component | Status | Implementation Notes | Verified Against Real Ring Sandbox |
|---|---|---|---|
| Signature verification | REAL | HMAC-SHA256 verified before JSON parse (401 on bad signature) | Implemented per Ring v1.1 spec; sandbox cannot push webhooks |
| Idempotency | REAL | SQLite UNIQUE constraint on `(source, external_id)` | Verified locally with signed duplicate requests |
| Webhook ingestion | REAL | `ingest_event` parses schema and normalizes types | Webhook payload schema verified against sandbox documentation |
| Status derivation | REAL | `derive_status` evaluates window + grace period | Deterministic offline logic; verified across unit tests |
| Scheduler loop | REAL | `Scheduler.tick` checks deadlines and transitions | Deterministic offline logic; verified across unit tests |
| Fire-once escalation | REAL | One escalation per missed routine per day (`scheduler.py:277`); reminders while unacknowledged (`scheduler.py:318`) | Deterministic offline logic; verified across unit tests |
| Alert formatting | REAL | Rendered through `format_escalation_text` (`notifier.py:167`) matching Telegram alerts | Telegram HTML parsing & delivery verified with test harness |
| Agent tool execution | REAL | `check_routine_status`, `check_household_memory` real; demo Ring client returns placeholder JPEG | Tool definitions execute real `RingClient` methods live-verified against sandbox |
| Evidence derivation | REAL | Derives evidence strictly from tool execution results | Offline logic; verified with 53 agent unit tests |
| Deterministic gate | REAL | Pure function gate enforces evidence & cooldown bars | Offline logic; verified with unit tests (frozen `gate.py`) |
| Web Dashboard | REAL | Read-only HTTP server with dynamic HTML escaping | Verified in browser; loopback socket binding |
| Ring API Client (`ring_client.py`) | REAL | Polls devices, status, event history, snapshots via 303 redirect | YES — verified live against Playground (`evidence/ring/`) |
| Household & Resident | SIMULATED | Fictional household in Dallas, TX | N/A (simulated scenario) |
| Hardware devices | SIMULATED | Fictional device identifiers (e.g. `demo-cam-living-room`) | Live `ava1.ring.device.*` devices verified in `evidence/ring/` |
| Device health polls | SIMULATED | Written with `upsert_device_health`, the function the real poller uses | Real poller writes same schema from `GET /v1/devices/{id}/status` |
| Device events | SIMULATED | Synthetic motion events generated for narrative | Real events verified from `GET /v1/history/devices/{id}/events` |
| System clock | SIMULATED | `FakeClock` drives time progression across scenes | Real daemon uses system UTC clock |
| Agent reasoning | SIMULATED | Replay mode: committed scripted traces (not a live LLM) | Default shipped mode; no live model calls |

---

## 5. How It Works

```mermaid
flowchart TD
    subgraph Ingestion["1. Ingestion Layer"]
        WH["Ring Webhooks<br/>(HMAC-SHA256)"] -->|POST /webhook| INGEST["ingest_event()"]
        POLL["Ring API Poller<br/>(devices/status/history)"] -->|poll_once()| INGEST
    end

    subgraph StateStore["2. Persistent Store"]
        INGEST -->|Idempotent Write| DB[("SQLite Database<br/>events, device_health, routines")]
    end

    subgraph AbsencePath["3. Absence Engine (Deterministic)"]
        SCHED["Scheduler Loop<br/>(every 60s)"] -->|Read State| DERIVE["derive_status()<br/>watching / observed / missed / unknown"]
        DERIVE -->|watching → missed| ESCALATE{"Device Healthy?"}
        ESCALATE -->|Yes| FIRE["Fire-Once Escalation<br/>(Telegram / Console)"]
        ESCALATE -->|No: Stale/Offline| UNK["Derive UNKNOWN<br/>(Silenced, 0 Alerts)"]
    end

    subgraph AgentBranch["4. Bounded Agent Branch (Off Absence Path)"]
        AMBIG["Ambiguous Event?<br/>(Quiet Hours / Unlinked)"] -->|Trigger| AGENT["Bounded Agent Loop<br/>(5 Whitelisted Tools)"]
        AGENT -->|Tool Results| FACTS["Structured Facts<br/>(Evidence List)"]
        FACTS --> GATE{"Deterministic Gate<br/>(Pure Function)"}
        GATE -->|Cleared (≥2 tools + cooldown)| ALERT["Caregiver Alert<br/>(CHECK NEEDED + Snapshot)"]
        GATE -->|Not Cleared| SUPPRESS["Downgrade / Silent"]
    end

    subgraph Presentation["5. Presentation Layer"]
        DB --> DASH["Read-Only Dashboard<br/>(http://127.0.0.1:8080)"]
        FIRE --> CAREGIVER["Caregiver Telegram"]
        ALERT --> CAREGIVER
        CAREGIVER -->|Tap [False Alarm]| CB["POST /callback<br/>(Household Memory)"]
        CB --> DB
    end
```

### Core Architecture Principles
1. **Derived-Not-Stored Status:** Status is derived on every evaluation tick (`derive_status()`), never stored as mutable status columns. This prevents stuck state and forgotten resets.
2. **Grace Semantics:** A routine with window `07:00–09:00` and delay `30m` evaluates as `watching` from `07:00` through `09:30`.
3. **Deterministic Fire-Once:** The scheduler monitors status transitions and fires exactly once on the `watching → missed` boundary per routine per day. Unacknowledged escalations trigger polite reminders on subsequent ticks.
4. **Safety Isolation (`UNKNOWN` Never Escalates):** When a device health record is stale or offline, the absence engine derives `UNKNOWN`. It never assumes absence when sensor reachability is unverified.
5. **Agent Never Decides Absence:** The bounded agent runs strictly on anomalous motion events; the absence escalation path has zero model involvement.

---

## 6. The Bounded-Authority Agent

When unexpected motion occurs outside routine windows (e.g. 02:10 AM quiet hours), Threshold initiates an investigation.

### What the Agent Can and Cannot Do
- **CANNOT** create, suppress, or delay routine absence alerts.
- **CANNOT** directly trigger phone calls or dispatch emergency services.
- **CANNOT** inspect raw camera images (vision models are out of scope).
- **CAN** invoke up to 5 strictly whitelisted tools within a 30-second budget:
  1. `check_routine_status`: Queries routine windows and current state.
  2. `query_device_health`: Checks online status and reported freshness.
  3. `query_recent_events`: Retrieves event timestamps for context.
  4. `check_household_memory`: Checks past false alarms and caretaker corrections.
  5. `pull_snapshot`: Downloads latest image capture via Ring Client.

### The Deterministic Escalation Gate (`src/threshold/agent/gate.py`)
Model recommendations are never trusted blindly. The final verdict is determined by a frozen, pure mathematical gate:
- **Evidence Bar:** Requires $\ge 2$ distinct successful tool executions.
- **Cooldown Bar:** Minimum 30 minutes between alerts for the same device.
- **Velocity Limit:** Maximum 2 agent escalations per 6 hours.
- If any check fails, the verdict is downgraded to `check_needed` without push alert.

### Shipped Provider Modes
- **Default Shipped Mode:** `mock` (`MockProvider`). Replays deterministic, committed scripted traces (`traces/*.json`) labeled `"replay (scripted)"` everywhere.
- **Live LLM Providers:** No live cloud LLM is shipped by default. Provider stubs (`anthropic`, `openai`) exist in configuration validation but raise `ValueError("not implemented yet")`.
- **Data Privacy:** In the default `mock` mode, **zero data leaves your machine**. All tool executions and traces operate locally.

---

## 7. Running for Real

### 1. Environment Configuration (`.env`)
Copy `.env.example` to `.env` and supply your credentials:
```bash
cp .env.example .env
```
Variables:
- `RING_ACCESS_TOKEN`: OAuth bearer token (valid 30 minutes from Ring Playground).
- `RING_CLIENT_ID`: Ring Partner application client ID.
- `RING_CLIENT_SECRET`: Ring Partner application client secret.
- `RING_WEBHOOK_SECRET`: HMAC-SHA256 secret key for webhook verification.
- `TELEGRAM_BOT_TOKEN`: Bot token from Telegram `@BotFather`.
- `TELEGRAM_CHAT_ID`: Caregiver Telegram chat ID (found via `scripts/telegram_whoami.py`).
- `LLM_API_KEY`: Reserved for optional live provider.

### 2. Live API Health Check
Test Ring API connectivity and token validity:
```bash
python3 -m src.threshold ring-check
```

### 3. CLI Subcommands
Copied verbatim from `python3 -m src.threshold --help`:
```text
usage: python3 -m src.threshold [-h]
                                {init,ring-check,seed,test-alert,dashboard,demo,run} ...

Threshold Command-Line Tools & Diagnostics

positional arguments:
  {init,ring-check,seed,test-alert,dashboard,demo,run}
                        Available subcommands
    init                Initialize a new threshold.json configuration file
                        from template
    ring-check          Check Ring Partner API devices, status, and recent
                        event history
    seed                Seed routines from declarative JSON configuration into
                        SQLite database
    test-alert          Dispatch a test escalation alert (with live snapshot
                        if available) to Telegram or Console
    dashboard           Launch the Threshold local web status dashboard
    demo                Run the zero-secret simulated household hackathon demo
    run                 Run the Threshold background service daemon

options:
  -h, --help            show this help message and exit
```

### 4. Running the Background Daemon
Run Threshold in poller mode with embedded dashboard:
```bash
python3 -m src.threshold run --config threshold.example.json --db threshold.db
```

---

## 8. Test Suite

Threshold includes 275 unit and integration tests requiring no external network connectivity:
```bash
python3 -m unittest discover -s tests -v
```
- **Test Count:** 275 tests.
- **Runtime:** ~50 seconds.
- **Network Requirements:** Loopback sockets only (`127.0.0.1`); zero external requests.

---

## 9. Known Limitations

In the interest of complete engineering honesty:
1. **Sandbox-Only Live Verification:** Live testing was conducted exclusively in the official Ring Developer Portal Playground. Behavior on production Ring hardware in physical residences remains unverified.
2. **Poller Mode Default:** The Ring Developer Playground cannot push webhooks to external URLs. Threshold defaults to polling `GET /v1/history/devices/{id}/events`.
3. **Snapshots Are Historical Recordings:** Ring's `media/image/download` endpoint extracts stills from completed cloud video recordings; it does not trigger an instantaneous camera shutter.
4. **Household Memory Heuristic:** Caretaker false-alarm feedback is used as a loose proxy for noisy devices and lowers push alert velocity; events remain visible on the dashboard.
5. **Replay Agent:** The shipped agent replays scripted step traces and does not perform dynamic natural-language reasoning over event context.
6. **Single Household / Single Chat:** Current implementation supports a single household database and a single caregiver Telegram chat destination.
7. **Unauthenticated Loopback Dashboard:** The web status dashboard is designed for local observation (`127.0.0.1`) and does not provide session authentication.
8. **Timezone & DST Scope:** Unit tests verify standard transitions (e.g. America/Chicago CDT/CST and midnight-crossing windows); arbitrary international boundary changes are untested.
9. **macOS / Linux Tested:** Tested on macOS and Linux. Windows requires `pip install tzdata` for Python `zoneinfo` support.
10. **Simulated Household Data:** The Dallas household, devices, and motion timestamps in the demo are fictional scenarios for presentation purposes.

---

## 10. Documentation & Links

- **Integration Notes & Friction Log:** [docs/ring_integration_notes.md](docs/ring_integration_notes.md)
- **Sample Agent Investigation JSON:** [docs/sample_investigation.json](docs/sample_investigation.json)
- **Demo Video Script:** [docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md)
- **License:** [MIT License](LICENSE) (Copyright 2026 Amit Kumar Gupta)

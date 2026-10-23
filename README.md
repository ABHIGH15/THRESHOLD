# Threshold

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.14](https://img.shields.io/badge/Python-3.14-blue.svg)](https://www.python.org/)
[![Dependencies: stdlib only](https://img.shields.io/badge/Dependencies-stdlib%20only-green.svg)](https://docs.python.org/3/)

> **Ring tells you when something happens. Threshold tells you when something doesn't.**

Threshold is a state-aware pattern-absence detection system for remote caregivers monitoring elderly family members living alone. While Ring alerts you when motion occurs, Threshold monitors daily living routines and deterministically escalates when expected activity silently does not happen.

```bash
# Try it in 30 seconds (zero secrets, zero network calls, no Ring account needed):
python3 -m src.threshold demo
```

**Ring Partner APIs Used:**
- **Devices & Health (`GET /v1/devices`, `GET /v1/devices/{id}/status`):** Discovers hardware and tracks online freshness (verified live in sandbox).
- **Event History (`GET /v1/history/devices/{id}/events`):** Polls recent motion events to verify routine satisfaction (verified live in sandbox).
- **Image Snapshots (`POST /v1/devices/{id}/media/image/download`):** Fetches JPEG captures via two-step HTTP 303 redirect (verified live in sandbox).
- **Webhook Ingestion (`POST /webhook`):** receiver implemented; verified only with locally signed POSTs, not Ring-delivered (the Playground cannot push webhooks).

> **Scope Fence:** Threshold is a caregiver-support notification tool, not a medical or life-safety emergency device, and does not contact 911 or emergency services.  
> **Disclaimer:** Independent hackathon project. Not affiliated with, endorsed by, or sponsored by Ring or Amazon.

---

## Table of Contents
- [1. Why Threshold (The Silence Problem)](#1-why-threshold-the-silence-problem)
- [2. How It Works](#2-how-it-works)
- [3. Demo Walkthrough: Six Deterministic Scenes](#3-demo-walkthrough-six-deterministic-scenes)
- [4. Safety by Design](#4-safety-by-design)
- [5. Ring Partner API Usage](#5-ring-partner-api-usage)
- [6. REAL vs SIMULATED](#6-real-vs-simulated)
- [7. Running for Real](#7-running-for-real)
- [8. Tests & Verification](#8-tests--verification)
- [9. Known Limitations](#9-known-limitations)
- [10. Repository Map & Documentation](#10-repository-map--documentation)

---

## 1. Why Threshold (The Silence Problem)

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

## 2. How It Works

### Architecture Flowchart
Every node below maps directly to an implementation module inside `src/threshold/`:

```mermaid
flowchart TD
    subgraph Ingestion["1. Ingestion Layer"]
        WH["src/threshold/server.py<br/>(Webhook Receiver & HMAC)"] -->|POST /webhook| INGEST["src/threshold/ingest.py<br/>(ingest_event)"]
        POLL["src/threshold/ring_client.py<br/>(Ring API Poller)"] -->|"poll_once()"| INGEST
    end

    subgraph StateStore["2. Persistent Store"]
        INGEST -->|Idempotent Write| DB[("src/threshold/store.py<br/>(SQLite Schema in WAL Mode)")]
    end

    subgraph AbsencePath["3. Absence Engine (Deterministic)"]
        SCHED["src/threshold/scheduler.py<br/>(Scheduler.tick Loop)"] -->|Read Health & Events| DERIVE["src/threshold/status.py<br/>(derive_status)"]
        DERIVE -->|watching to missed| ESCALATE{"Device Healthy & Fresh?"}
        ESCALATE -->|Yes| FIRE["src/threshold/notifier.py<br/>(Fire-Once Escalation)"]
        ESCALATE -->|No: Stale/Offline| UNK["Derive UNKNOWN<br/>(Silenced, 0 Alerts)"]
    end

    subgraph AgentBranch["4. Bounded Agent Branch (Off Absence Path)"]
        AMBIG["src/threshold/agent/triggers.py<br/>(is_ambiguous_event)"] -->|Quiet Hours Trigger| AGENT["src/threshold/agent/investigate.py<br/>(run_investigation_and_gate)"]
        TOOLS["src/threshold/agent/tools.py<br/>(5 Whitelisted Tools)"] -->|Diagnostic Results| AGENT
        AGENT --> FACTS["src/threshold/agent/facts.py<br/>(build_gate_facts)"]
        FACTS --> GATE{"src/threshold/agent/gate.py<br/>(Deterministic decide Gate)"}
        GATE -->|"Cleared: ≥2 tools + cooldown"| ALERT["Caregiver Notification<br/>(CHECK NEEDED + Snapshot)"]
        GATE -->|Not Cleared| SUPPRESS["Downgrade to check_needed<br/>(0 Alerts Sent)"]
    end

    subgraph Presentation["5. Presentation Layer"]
        DB --> DASH["src/threshold/dashboard.py<br/>(Local Web Dashboard: http://127.0.0.1:8080)"]
        FIRE --> CAREGIVER["Caregiver Telegram"]
        ALERT --> CAREGIVER
        CAREGIVER -->|"Tap [False alarm]"| CB["Callback Processing<br/>(src/threshold/notifier.py)"]
        CB --> DB
    end
```

### The Five Derived Routine Statuses
Threshold evaluates routine state on every tick via `src/threshold/status.py::derive_status`:

| Status | Meaning | Absence Escalation? |
|---|---|---|
| `watching` | Active routine window or grace period; waiting for expected activity. | No (monitoring) |
| `observed` | Expected motion detected during the window. Silently satisfied. | No (satisfied) |
| `missed` | Window + grace passed with zero motion; device verified online and fresh. | **YES** (fires single escalation) |
| `unknown` | Device offline, unreachable, or health poll stale (> 120m). Untrusted silence is not evidence. | No (silenced, 0 alerts) |
| `snoozed` | Caregiver temporarily snoozed routine. | No (paused) |

---

## 3. Demo Walkthrough: Six Deterministic Scenes

The offline demo runs through six complete operational scenarios using only the standard library and SQLite (`python3 -m src.threshold demo`).

*Tested on macOS with Python 3.14.7. Linux and Windows: not tested. Windows additionally needs `pip install tzdata`.*

| Scene | Printed Title | Operational Verification |
|---|---|---|
| **Scene 1** | `[SCENE 1: CALM MORNING] Routine Observed — Peace of Mind` | Morning movement detected at 07:42 CDT; status derives as `observed`; 09:31 deadline passes with 0 escalations. |
| **Scene 2** | `[SCENE 2: TRUST BOUNDARY] Signature Check & Idempotent Ingestion` | Invalid HMAC rejected with HTTP 401 Unauthorized; duplicate POST ignored via database idempotency. |
| **Scene 3** | `[SCENE 3: MISSED ROUTINE] The Silence — Deterministic Absence Escalation` | Evening check-in window and 30m grace expire in silence; status transitions to `missed`; exactly 1 escalation alert dispatched. |
| **Scene 4** | `[SCENE 4: UNKNOWN NOT ALARM] Stale / Offline Device Silenced` | Device offline > 120m derives `UNKNOWN`; absence alert is silenced to prevent false panic. |
| **Scene 5** | `[SCENE 5: CHECK NEEDED] Bounded Investigation & Gate Evaluation` | Quiet-hours motion at 02:10 CDT triggers bounded investigation; gate clears when $\ge 2$ tools succeed; 1 Check Needed message dispatched with evidence lines. |
| **Scene 6** | `[SCENE 6: HOUSEHOLD MEMORY] Caretaker Resolution & Learned Safety` | Caregiver false-alarm callback recorded in SQLite; recurring quiet-hours motion queries household memory (3 past false alarms) and safely suppresses duplicate alert. |

To run the demo with an interactive web dashboard on loopback:
```bash
python3 -m src.threshold demo --serve
```

---

## 4. Safety by Design

- **Unknown Not Alarm:** When a device health record is stale (> 120m) or offline, the absence engine derives `UNKNOWN`. It never assumes absence when sensor reachability is unverified (`tests.test_scheduler.TestSchedulerAbsenceEngine.test_offline_fresh_device_returns_unknown_zero_escalations_and_gate_checks_needed`).
- **Deterministic Fire-Once with Reminders:** The scheduler monitors status transitions and fires exactly once on the `watching → missed` boundary per routine per day (`src/threshold/scheduler.py::Scheduler.tick`). Unacknowledged escalations trigger polite reminders on subsequent ticks (`tests.test_scheduler.TestSchedulerAbsenceEngine.test_five_consecutive_ticks_past_miss_exactly_one_escalation_row`).
- **Safety Isolation (AST Import Guard):** The bounded agent runs strictly on anomalous motion events; the absence escalation path has zero model involvement. This architectural boundary is enforced by an automated AST import guard test (`tests.test_agent.TestAgentLibrary.test_ast_import_guard`).
- **Five Whitelisted Diagnostic Tools (`src/threshold/agent/tools.py`):**
  1. `get_recent_ring_events`: Retrieves event timestamps for context.
  2. `pull_snapshot`: Downloads latest image capture via Ring Client.
  3. `check_routine_status`: Queries routine windows and current state.
  4. `get_threshold_history`: Retrieves historical threshold activity.
  5. `check_household_memory`: Checks past false alarms and caretaker corrections.
- **Deterministic Escalation Gate (`src/threshold/agent/gate.py::decide`):**
  - **Evidence Bar:** Requires $\ge 2$ distinct successful tool executions (`tests.test_agent.TestAgentLibrary.test_gate_table_driven_all_conditions`).
  - **Cooldown Bar:** The 30-minute notification cooldown (`src/threshold/agent/gate.py::decide`) is **household-wide** (not per-device). Per `src/threshold/agent/facts.py::build_gate_facts`, **ANY prior notification** counts—whether an absence escalation or an agent alert—by querying `MAX(notified_at)` across both tables (`tests.test_agent.TestAgentLibrary.test_build_gate_facts_cooldown_evaluates_latest_across_both_tables`).
  - **Velocity Limit:** Maximum 2 agent escalations per 6 hours (`tests.test_agent.TestAgentLibrary.test_gate_table_driven_all_conditions`).
  - **Delivery Hardening:** Over-limit (>1000 char) alerts send a photo with a short prefix caption followed by a full text message; `notified_at` is stamped only on confirmed delivery (`tests.test_agent.TestAgentLibrary.test_over_limit_delivery_combos_and_notified_at`).

---

## 5. Ring Partner API Usage

All Ring Partner API interactions are implemented in `src/threshold/ring_client.py` and validated against the live Ring Developer Portal Playground (`api.amazonvision.com`).

| Endpoint | Used For | Code Reference | Live Status |
|---|---|---|---|
| `GET /v1/users/me` | Account token verification & account identity check | `scripts/probe_ring.py::perform_call` | VERIFIED LIVE 2026-10-12 (`docs/ring_samples/user_profile.json`) |
| `GET /v1/devices` | Discovers hardware devices (cameras/doorbells) for household | `src/threshold/ring_client.py::RingClient.list_devices` | VERIFIED LIVE 2026-10-12 (`docs/ring_samples/list_devices.json`) |
| `GET /v1/devices/{id}/status` | Device health check (`online` boolean, `reported_at` timestamp) | `src/threshold/ring_client.py::RingClient.get_device_status` | VERIFIED LIVE 2026-10-12 (`docs/ring_samples/device_status.json`) |
| `GET /v1/devices/{id}/capabilities` | Probing device feature configurations (snapshot, motion zones) | `scripts/probe_ring.py::perform_call` | VERIFIED LIVE 2026-10-12 (`docs/ring_samples/device_capabilities.json`) |
| `GET /v1/history/devices/{id}/events` | Ingesting motion event history in poller mode | `src/threshold/ring_client.py::RingClient.get_event_history` | VERIFIED LIVE 2026-10-12 (`docs/ring_samples/event_history.json`) |
| `POST /v1/devices/{id}/media/image/download` | Initiating snapshot download via `latest_in_range` (HTTP 303 redirect) | `src/threshold/ring_client.py::RingClient.get_snapshot` | VERIFIED LIVE 2026-10-15 (`docs/ring_samples/c2_case_a2_latest_in_range_recording.json`) |
| `GET <download_url>` (from 303 `Location`) | Fetching snapshot JPEG bytes (executed **without** Authorization header) | `src/threshold/ring_client.py::RingClient.get_snapshot` | VERIFIED LIVE 2026-10-15 (`docs/ring_samples/c2_case_a2_latest_in_range_recording.json`) |
| `POST /webhook` | Receiving Ring v1.1 push webhook notifications | `src/threshold/server.py::WebhookHandler.do_POST` | IMPLEMENTED, NOT LIVE-VERIFIED *(receiver implemented; verified only with locally signed POSTs; Playground cannot push webhooks)* |
| `POST /v1/devices/{id}/media/streaming/whep/sessions` | Live WebRTC/WHEP media streaming session | N/A (Observed in portal UI) | NOT USED *(Threshold monitors routine state, not live surveillance)* |

*Note: The sandbox account used lists 1 device, doorbell_v4 (`docs/ring_samples/list_devices.json`). Non-camera Ring device types (e.g., contact sensors, smart locks) are untested in the sandbox.*

---

## 6. REAL vs SIMULATED

Threshold explicitly separates production logic from simulated test scenarios:

| Component | Status | Implementation Notes | Verified Against Real Ring Sandbox / Tests |
|---|---|---|---|
| Signature verification | REAL | HMAC-SHA256 verified before JSON parse (401 on bad signature) in `src/threshold/server.py::WebhookHandler.do_POST` | Verified locally with signed POSTs (`tests.test_webhook_auth.TestSignatureVerification.test_valid_signature_passes`) and corrupted signatures rejected (`tests.test_webhook_auth.TestSignatureVerification.test_corrupted_signature_rejected`) |
| Idempotency | REAL | SQLite schema constraint `PRIMARY KEY (source, external_id)` in `src/threshold/store.py`; external ID mapped via `external_id = meta.get("request_id") or data.get("id") or body_hash` in `src/threshold/server.py::WebhookHandler.do_POST` | Verified locally with duplicate requests (`tests.test_server.TestWebhookServer.test_replay_attack_duplicate_delivery_one_row`) |
| Webhook ingestion | REAL | `ingest_event` parses schema and normalizes types (`src/threshold/ingest.py::ingest_event`) | Verified against Ring v1.1 documentation with signed test requests (`tests.test_ingest.TestIngestPipeline.test_matching_event_advances_last_satisfied_at`) |
| Status derivation | REAL | `derive_status` evaluates window + grace period (`src/threshold/status.py::derive_status`) | Deterministic offline logic across all five statuses: watching (`tests.test_triage.TestDerivedStatus.test_watching_within_window`), observed (`tests.test_triage.TestDerivedStatus.test_observed`), missed (`tests.test_triage.TestDerivedStatus.test_missed_healthy_device`), unknown (`tests.test_triage.TestDerivedStatus.test_unknown_stale_device`), snoozed (`tests.test_triage.TestDerivedStatus.test_snoozed`) |
| Scheduler loop | REAL | `Scheduler.tick` checks deadlines and transitions (`src/threshold/scheduler.py::Scheduler.tick`) | Deterministic offline logic (`tests.test_scheduler.TestSchedulerAbsenceEngine.test_five_consecutive_ticks_past_miss_exactly_one_escalation_row`) |
| Fire-once escalation | REAL | One escalation per missed routine per day (`src/threshold/scheduler.py::Scheduler.tick`); reminders while unacknowledged | Deterministic offline logic (`tests.test_scheduler.TestSchedulerAbsenceEngine.test_five_consecutive_ticks_past_miss_exactly_one_escalation_row`) |
| Alert formatting | REAL | Rendered through `ConsoleNotifier.send_escalation` (`src/threshold/notifier.py::ConsoleNotifier.send_escalation`) and `format_escalation_text` matching Telegram alerts | Verified across unit tests (`tests.test_notifier.TestNotifierSystem.test_console_notifier_message_content_and_phone_call_disclaimer`) |
| Agent tool execution | REAL | `check_routine_status`, `check_household_memory` real; demo Ring client returns placeholder JPEG (`src/threshold/agent/tools.py`) | Tool execution verified (`tests.test_agent.TestAgentLibrary.test_tool_check_routine_status_success_and_bad_args`) |
| Evidence derivation | REAL | Derives evidence strictly from tool execution results (`src/threshold/agent/investigate.py::run_investigation_and_gate`) | Offline logic verified (`tests.test_agent.TestAgentLibrary.test_final_citing_non_existent_call_id_keeps_no_phantom_evidence`) |
| Deterministic gate | REAL | Pure function gate enforces evidence & cooldown bars (`src/threshold/agent/gate.py::decide`) | Offline logic verified (`tests.test_agent.TestAgentLibrary.test_gate_table_driven_all_conditions`) |
| Web Dashboard | REAL | Read-only HTTP server with dynamic HTML escaping (`src/threshold/dashboard.py::render_dashboard_html`) | Verified in browser and unit tests (`tests.test_dashboard.TestDashboardSystem.test_render_dashboard_html_contains_critical_elements`) |
| Ring API Client (`ring_client.py`) | REAL | Polls devices, status, event history, snapshots via 303 redirect (`src/threshold/ring_client.py::RingClient`) | YES — verified live against Playground (`docs/ring_samples/`); tested in `tests.test_ring_client.TestRingClient.test_list_devices_request_shape` |
| Household & Resident | SIMULATED | Fictional household in Dallas, TX | N/A (simulated scenario) |
| Hardware devices | SIMULATED | Fictional device identifiers (e.g. `demo-cam-living-room`) | Live device verified in `docs/ring_samples/list_devices.json` |
| Device health polls | SIMULATED | Written with `upsert_device_health`, the function the real poller uses (`src/threshold/store.py::upsert_device_health`) | Real poller writes same schema from `GET /v1/devices/{id}/status` |
| Device events | SIMULATED | Synthetic motion events generated for narrative | Real events verified from `docs/ring_samples/event_history.json` (`attributes.event_type: "on_demand"`) |
| System clock | SIMULATED | `FakeClock` drives time progression across scenes (`src/threshold/scheduler.py::FakeClock`) | Real daemon uses system UTC clock |
| Agent reasoning | SIMULATED | Replay mode: committed scripted traces (not a live LLM) | Default shipped mode; no live model calls |

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

### 2. Live Diagnostic & Path Checks
- **Ring Partner API Diagnostic:** Validate token, devices, health, and recent event history:
  ```bash
  python3 -m src.threshold ring-check
  ```
- **Optional Live Photo Path Check:** Test real Ring snapshot retrieval and Telegram delivery:
  ```bash
  # Offline dry-run verification:
  python3 scripts/live_photo_check.py --dry-run

  # Live snapshot retrieval and Telegram delivery (path test, not an absence event):
  python3 scripts/live_photo_check.py --send
  ```
  `--long` pads the message with synthetic filler to exercise the over-limit branch; it contains no real evidence.
- **Test Alert Dispatch:** Send an immediate test escalation alert to Telegram or Console:
  ```bash
  python3 -m src.threshold test-alert
  ```
  *Note: Telegram buttons (`False alarm`, `Got it`) are implemented and covered by automated tests (`tests/test_notifier.py`); live callback processing on your phone requires the background daemon to be running.*

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

## 8. Tests & Verification

Threshold includes 292 unit and integration tests requiring no external network connectivity:
```bash
python3 -m unittest discover -s tests -v
```
- **Test Count:** 292 tests.
- **Runtime:** ~50 seconds.
- **Network Requirements:** Loopback sockets only (`127.0.0.1`); zero external requests.

### Mechanical Integrity Checkers
The repository includes three automated verification checkers:
```bash
# Verify relative links, CLI subcommands, top-30 lines, and env vars:
python3 scripts/dev/check_readme.py

# Verify AST code symbols and test citations in documentation:
python3 scripts/dev/check_citations.py

# Verify zero secrets or unredacted tokens across git-tracked files:
python3 scripts/dev/check_redaction.py
```

---

## 9. Known Limitations

In the interest of complete engineering honesty:
1. **Sandbox-Only Live Verification:** Live testing was conducted exclusively in the official Ring Developer Portal Playground (`docs/ring_samples/`). The sandbox account used lists 1 device, doorbell_v4 (`docs/ring_samples/list_devices.json`). Behavior on production Ring hardware in physical residences remains unverified.
2. **Poller Mode Default:** The Ring Developer Playground cannot push webhooks to external URLs (verified in `docs/ring_samples/FINDINGS.md`). Threshold defaults to polling `GET /v1/history/devices/{id}/events` (`src/threshold/ring_client.py::RingClient.get_event_history`). Webhook ingestion is implemented and tested with locally signed POSTs only.
3. **Snapshots Are Historical Recordings:** Ring's `media/image/download` endpoint extracts stills from completed cloud video recordings; it does not trigger an instantaneous camera shutter (`docs/ring_samples/c2_case_b_at_timestamp_now.json`).
4. **Household Memory Heuristic:** Caretaker false-alarm feedback is used as a loose proxy for noisy devices and lowers push alert velocity; events remain visible on the dashboard.
5. **Replay Agent:** The shipped agent replays scripted step traces and does not perform dynamic natural-language reasoning over event context.
6. **Single Household / Single Chat:** Current implementation supports a single household database and a single caregiver Telegram chat destination.
7. **Unauthenticated Loopback Dashboard:** The web status dashboard is designed for local observation (`127.0.0.1`) and does not provide session authentication.
8. **Timezone & DST Scope:** Unit tests verify standard US transitions (e.g. America/New_York EDT/EST DST boundaries in `tests.test_scheduler.TestSchedulerAbsenceEngine.test_us_fallback_dst_boundary`, and America/Chicago CDT/CST routines in `src/threshold/status.py::derive_status`); arbitrary international boundary changes are untested.
9. **Platform Testing:** Tested on macOS with Python 3.14.7 (`tests.test_platform.TestPlatformPreflight.test_demo_pipeline_zoneinfo_not_found_handling`). Linux and Windows: not tested. Windows additionally needs `pip install tzdata` for Python `zoneinfo` support.
10. **Simulated Household Data:** The Dallas household, devices, and motion timestamps in the demo are fictional scenarios for presentation purposes.
11. **Offline/Stale Device Absence Suppression:** A stale or offline (`STATUS_UNKNOWN`) device produces a dashboard state but NO push notification; Threshold refuses to claim absence from unverified data (`tests.test_scheduler.TestSchedulerAbsenceEngine.test_offline_fresh_device_returns_unknown_zero_escalations_and_gate_checks_needed`).
12. **Freshness Bound Uses Poll Time Over Reported Timestamp:** Threshold determines device freshness from the poll time (`fetched_at`), treating an `online: true` device as fresh even if its Ring `reported_at` timestamp is stale. For background and analysis of this behavior in the sandbox, see Finding 8 in [docs/ring_integration_notes.md](docs/ring_integration_notes.md).

---

## 10. Repository Map & Documentation

```text
Threshold/
├── README.md                      # Complete project documentation & quickstart
├── LICENSE                        # MIT License (Copyright 2026 Amit Kumar Gupta)
├── threshold.example.json         # Declarative configuration template
├── .env.example                   # Environment variable template
├── docs/
│   ├── ring_integration_notes.md  # Primary-source friction log & 8 API findings
│   ├── DEMO_VIDEO_SCRIPT.md       # Scene-by-scene video script & talking points
│   ├── sample_investigation.json  # Sanitized agent investigation trace
│   ├── dev/
│   │   ├── BUILD_LOG.md           # Verbatim engineering log (A1 through A12)
│   │   └── threshold_spec.md      # Foundational system architecture spec
│   └── ring_samples/              # Redacted raw JSON API responses from sandbox
├── src/threshold/                 # Production core: ring_client, scheduler, status, etc.
├── scripts/                       # Diagnostics, live photo check, and dev checkers
└── tests/                         # 292 unit, integration, and platform tests
```

- **Integration Notes & Friction Log:** [docs/ring_integration_notes.md](docs/ring_integration_notes.md)
- **Sample Agent Investigation JSON:** [docs/sample_investigation.json](docs/sample_investigation.json)
- **Demo Video Script:** [docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md)
- **License:** [MIT License](LICENSE) (Copyright 2026 Amit Kumar Gupta)

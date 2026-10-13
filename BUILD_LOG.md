# Build Log

### [2026-09-24T06:31:00Z] Phase 0 - Task 7: Secrets handling (.gitignore)
Attempted: Created .gitignore to prevent committing .env files and other credentials.
Command run: `cat .gitignore`
Output: 
```
# Secrets and Credentials
.env
.env.local
.env.*
secrets.json
*.pem
*.key
credentials.json
# AWS SAM local
.aws-sam/
```
Expected: .gitignore file exists with credential-blocking patterns.
Result: PASS — File created and patterns are in place.
Spec cross-check: EXECUTION_PLAN.md - Phase 0, Task 7

> **Note on .env usage:** A git-ignored `.env` file is used *only* for the temporary 30-minute Playground token during Phase 0 verification. Once the real Ring Partner App is registered, actual OAuth credentials will go to AWS Secrets Manager/SSM Parameter Store per the execution plan, not a permanent `.env` file.

### [2026-09-30T07:52:00Z] Sequencing decision: Phase 1 started ahead of Phase 0 completion
**Reason:** Phase 0 tasks 1, 2, 5–6, 9–12 require manual account creation (Ring Developer Portal, AWS Console) and live API access that the local agent cannot perform. Phase 1 was explicitly designed as "no AWS" — pure logic against synthetic fixtures. All confirmed event types (`motion_detected`, `button_press`, `device_online`, `device_offline`) and the locked three-state absence model provide enough schema to build and test the rules engine now.

**What is NOT being built in this batch:**
- Package/vehicle-specific rules (schema unconfirmed — Rule 3)
- Vision/snapshot actual behavior (needs live API — interface can be stubbed, not marked done)
- Anything requiring AWS deploy (Phase 2+)

**Phase 0 items still open:** Ring account, Playground token, Bedrock model ID, TAKE media check, Event History API shape, Package/Vehicle payload verification. TAKE check specifically flagged as time-sensitive (September 2026 rollout).

### [2026-09-30T07:53:00Z] Phase 1 - Unit 1: Data models (models.py)
Attempted: Created Routine, DeviceHealthRecord, RingEvent, Verdict dataclasses matching threshold_spec.md exactly. Only confirmed event types and subType values included.
Command run: `python3 -c "from src.threshold.models import Routine, DeviceHealthRecord, RingEvent, Verdict, CONFIRMED_EVENT_TYPES; ..."`
Output:
```
Models imported successfully
Confirmed event types: ['app_integration_added', 'app_integration_removed', 'button_press', 'device_added', 'device_offline', 'device_online', 'device_removed', 'motion_detected', 'subscription_activated', 'subscription_deactivated']
Routine created: Morning door, window: 07:00-09:00
DeviceHealth stale at +3h? True
DeviceHealth stale at +1h? False
```
Expected: All confirmed event types present. Routine creates with correct fields. Staleness check returns True at 3h (past 2h threshold) and False at 1h (within threshold).
Result: PASS
Spec cross-check: threshold_spec.md — "Routine Schema" section, ground-truth table event list

### [2026-09-30T07:54:00Z] Phase 1 - Unit 2: Derived status function (status.py)
Attempted: Three-state absence model: snoozed → observed → watching → unknown | missed. Status never stored, computed fresh per tick.
Result: Tested as part of Unit 4 below.
Spec cross-check: threshold_spec.md — "Routine Schema (Locked — Derived Status, Three-State Absence Model)"

### [2026-09-30T07:55:00Z] Phase 1 - Unit 3: Triage engine (triage.py)
Attempted: Rules-determined paths (event→routine matching, absence detection with edge detection). Agent path stubbed as Needs a look for Phase 5.
Result: Tested as part of Unit 4 below.
Spec cross-check: threshold_spec.md — "Triage Engine — Bounded-Authority Agent", "Rules-determined paths"

### [2026-09-30T07:56:00Z] Phase 1 - Unit 4: Synthetic fixtures + full test suite
Attempted: 21 tests covering all four scenarios (normal day, missed pickup, 3am ambiguous event, silent morning), edge detection (fire-once), unknown state safety, failure/edge cases.
Command run: `python3 -m unittest tests.test_triage -v`
Output (verbatim):
```
test_missed_produces_urgent ... ok
test_silent_morning_produces_unknown_not_urgent ... ok
test_missed_healthy_device ... ok
test_non_routine_day ... ok
test_observed ... ok
test_snoozed ... ok
test_unknown_never_seen_device ... ok
test_unknown_stale_device ... ok
test_watching_past_window_before_deadline ... ok
test_watching_within_window ... ok
test_fire_once_on_transition ... ok
test_no_fire_when_already_missed ... ok
test_no_fire_when_observed ... ok
test_doorbell_matches_routine ... ok
test_event_matches_routine ... ok
test_event_no_match ... ok
test_bad_event_type_no_match ... ok
test_snoozed_routine_no_escalation ... ok
test_wrong_device_no_match ... ok
test_missed_pickup_one_urgent ... ok
test_normal_day_no_actions ... ok

----------------------------------------------------------------------
Ran 21 tests in 0.001s

OK
```
Expected: All 21 tests pass. Specifically:
  - Scenario 1 (normal day): all routines satisfied → Routine verdict ✓
  - Scenario 2 (missed pickup): watching → missed → Urgent ✓
  - Scenario 3 (3am door-open): no routine match → Needs a look ✓
  - Scenario 4 (silent morning): stale device → unknown (NOT Urgent) ✓
  - Edge detection: 5 ticks past miss, exactly 1 Urgent fires ✓
  - Failure cases: bad event type, wrong device, snoozed routine all handled correctly ✓
Result: PASS — 21/21 tests passing.
Spec cross-check: EXECUTION_PLAN.md Phase 1 Definition of Done — all four bullets satisfied.
No AWS resource touched.

> **Toolchain note:** `pytest` is not installed (`No module named pytest`). All tests run via `python3 -m unittest`, which is stdlib and requires no install. Sufficient for current needs; revisit if test complexity warrants `pytest` later.

### [2026-09-30T08:00:00Z] Post-Phase-1: Three implicit decisions pinned explicitly

**1. Pre-agent ambiguous event default → Needs a look.** Any event that doesn't cleanly match a routine or a clear absence defaults conservatively to Needs a look until Phase 5 adds the bounded-authority agent. This is an architectural choice: no event is silently dropped or incorrectly escalated while the agent layer is being built. Pinned in `threshold_spec.md` under "Agent-investigated path."

**2. `unknown` status → Needs a look verdict.** When a device's health data is stale (past the staleness threshold), the three-state model returns `unknown` instead of `missed`. This maps to Needs a look: don't escalate (data isn't trustworthy enough to conclude absence), don't silently swallow (caretaker should know the device may be offline). This is a deterministic rules path, not an agent decision. Pinned in `threshold_spec.md` under "Rules-determined paths."

**3. Device health staleness threshold: 2 hours (120 minutes).** A device with no events in >2h is treated as stale. Trades off false `unknown`s (too short → healthy devices look stale) against missed escalations going undetected (too long → genuinely offline devices look healthy). Configurable per-device via `staleness_threshold_minutes` on `DeviceHealthRecord`. Logged in EXECUTION_PLAN.md Section 12.

### [2026-09-30T08:25:00Z] Phase 2 - Prep: SAM template + stub handlers (pre-deploy)
Attempted: Wrote all Phase 2 deploy artifacts ahead of AWS CLI availability.
Files created:
  - `template.yaml` — SAM template: 2 Lambdas (webhook + scheduler), DynamoDB table (PK/SK), EventBridge schedule (1min), HttpApi endpoint
  - `handlers/webhook.py` — stub: accepts POST, writes raw body to DynamoDB, returns 200
  - `handlers/scheduler.py` — stub: writes heartbeat record to DynamoDB on each tick
  - `samconfig.toml` — non-interactive deploy config (stack name `threshold`, auto-resolve S3, CAPABILITY_IAM)
Result: PARTIAL — code written but not deployed. Blocked on AWS CLI + SAM CLI installation and AWS credentials.
Spec cross-check: EXECUTION_PLAN.md Phase 2 — template matches the spec's architecture (two Lambdas, one DynamoDB, one EventBridge schedule). DoD requires actual deploy output (resource ARNs), which is pending.

### [2026-09-30T14:29:00Z] Dashboard UI — interactive prototype (created, pending human review)
Attempted: Built interactive HTML dashboard with four demo scenes (All On Track, Watching, Escalation, Correction).
File: `dashboard.html`
Design: Warm dark charcoal palette, custom CSS tokens (not Tailwind defaults). Dashed-border absence gap as visual hero. Evidence-based investigation panel (✓/✗ list, not confidence scores). Deterministic gate visible. Escalation banner with pulse animation. Correction scene shows false-alarm loop closing.
Result: PARTIAL — code written, NOT reviewed by the human. The artifact approval mechanism triggered but the user had not seen the actual content.
Spec cross-check: threshold_spec.md — UI-facing labels (Routine/Check Needed/Escalation), evidence-based agent output, three demo beats, correction loop.

### [2026-09-30T14:30:00Z] Phase 3 prep — HMAC-SHA256 webhook verification (webhook_auth.py)
Attempted: Signature verification module using hmac.compare_digest (constant-time). 9 tests: valid passes, corrupted/wrong-secret/tampered-body/empty-inputs all rejected correctly.
Command run: `python3 -m unittest tests.test_webhook_auth -v`
Output: 9 tests, all OK.
Command run: `python3 -m unittest discover tests -v`
Output: **30 tests in 0.001s — all passing** (21 triage + 9 HMAC).
Result: PASS — verification logic ready for Phase 3 wiring.
Spec cross-check: threshold_spec.md — "Webhooks: HMAC-SHA256 X-Signature, 5s response, JSON:API". Phase 3 DoD requires testing against both valid and corrupted signatures — both covered.

> **Note:** Ring's exact signature scheme details (whether the header contains a prefix like `sha256=`, whether a timestamp is included) are TODO-VERIFY against the actual portal docs. The core HMAC-SHA256 computation and constant-time comparison are correct regardless — only the header parsing may need adjustment in Phase 3.

### [2026-10-01T05:31:00Z] Template review — two real findings from human review
**1. `rate(1 minute)` scheduler cost risk.** The EventBridge schedule runs 24/7 once deployed. Fine for stub heartbeats, but once Phase 5 wires Bedrock into the scheduler Lambda, a forgotten 1-minute trigger calling an LLM around the clock will burn through the $150 AWS credit for no reason. **Action required:** disable the schedule or tear down the stack between active sessions, starting the moment Bedrock enters this Lambda.

**2. Silent DynamoDB write failures.** Both stub handlers swallow write errors and still return 200. Acceptable for Phase 2 stubs, but this is exactly why the Phase 2 DoD requires an independent `aws dynamodb get-item` check — a 200 from the handler proves the Lambda ran, not that the write succeeded. **Action required:** once handlers stop being stubs, propagate write failures properly rather than swallowing them.

### [2026-10-11T14:45:00Z] A0 — Pivot cleanup & repo baseline (COMPLETE)
Commands run and verbatim output:
  1. `git init && git branch -m master main` → Initialized empty Git repository
  2. `git check-ignore -v .env` → `.gitignore:2:.env  .env` — confirmed ignored before any staging
  3. `python3 -m unittest -v 2>&1 | tail -15` → Ran 30 tests in 0.001s — OK
  4. `.gitignore` updated: added `*.db`, `*.db-journal`, `evidence/`, `.venv/`
  5. `.env.example` rewritten: `RING_ACCESS_TOKEN`, `RING_CLIENT_ID`, `RING_CLIENT_SECRET`, `RING_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `LLM_API_KEY`
  6. `evidence/ring/`, `evidence/ring/playground/`, `evidence/dashboard/` created (git-ignored). `docs/ring_samples/` created (committed).
  7. `README.md` stub, `LICENSE` (MIT, copyright Amit Kumar Gupta 2026), `NO_AWS_RUNBOOK.md` added.
  8. `git add -A && git commit -m "baseline before no-AWS pivot"` → c5cc62b, 24 files
  9. `git rm template.yaml samconfig.toml handlers/webhook.py handlers/scheduler.py` → 4 files deleted
  10. `git commit -m "remove AWS SAM scaffolding"` → 08beee1, 4 files deleted
  11. `python3 -m unittest -v 2>&1 | tail -15` → Ran 30 tests in 0.001s — OK (post-pivot)
  12. Secret grep: all matches are documentation text, empty assignments, or fake test fixtures. No real values.
  13. `git ls-files | grep "^\.env$"` → (empty — .env not tracked)
  14. `git ls-files | grep -E "template\.yaml|samconfig"` → (empty — AWS files gone)
Expected: ≥2 commits, 30 tests green, no AWS files, .env not tracked, no real secrets in history.
Actual: matches expected on all counts.
Result: PASS.
Remote origin added: https://github.com/ABHIGH15/THRESHOLD
Status: A0 Complete.

### [2026-10-11T15:15:00Z] A1 — Ring probe & FINDINGS.md template (COMPLETE)
Commands run and verbatim output:
  1. `git clone https://github.com/AmazonAppDev/ring-api-helloworld scratch/ring-helloworld-ref`
     - Confirmed endpoints and headers from starter repo scripts (`list_devices.py`, `event_history.py`, `device_status.py`, etc.):
       - Base URL: `https://api.amazonvision.com`
       - Headers: `Authorization: Bearer <token>`
       - Endpoints: `GET /v1/devices`, `GET /v1/history/devices/{id}/events`, `GET /v1/devices/{id}/status`, `GET /v1/devices/{id}/capabilities`, `GET /v1/devices/{id}/configurations`, `GET /v1/devices/{id}/location`, `GET /v1/users/me`
       - Snapshot: Not present in starter repo scripts (TODO-VERIFY)
  2. Implemented `scripts/probe_ring.py`:
     - stdlib only (`urllib.request`, `urllib.error`, `json`, `argparse`, `sys`, `os`, `datetime`)
     - Custom `.env` parser that ignores comments/blanks, strips quotes, does not override existing env vars, never prints secrets
     - Token gate: exits with code 2 if `RING_ACCESS_TOKEN` is unset/empty
     - 3-step sequence: list devices, event history for first device, snapshot (via `--snapshot-path` or skipped with TODO-VERIFY)
     - Full redaction of token and Authorization headers across saved JSON files and image byte saving
     - Formatted summary table output
  3. Created `evidence/ring/FINDINGS.md` with empty template sections
  4. Created unit tests in `tests/test_probe_ring.py`:
     - `python3 -m unittest tests/test_probe_ring.py -v`:
       - test_missing_token_exits_code_2 ... ok
       - test_does_not_override_existing_environ ... ok
       - test_parses_quotes_comments_and_blanks ... ok
       - test_redact_nested_structures ... ok
       - test_redact_string ... ok
       - Ran 5 tests in 0.042s OK
  5. Dry run without token:
     - `python3 scripts/probe_ring.py 2>&1; echo "EXIT_CODE=$?"`
     - Output:
       RING_ACCESS_TOKEN missing - generate a Playground token and put it in .env
       EXIT_CODE=2
  6. Full test suite: `python3 -m unittest discover tests -v` → 35/35 tests passing (0.042s).
Expected: probe script ready, findings template in place, unit tests passing, dry run exits 2 with missing-token message.
Actual: Matched expected on all counts.
Result: PASS.
Status: A1 Complete.

### [2026-10-11T17:05:00Z] A1b — Extend Ring probe (COMPLETE)
Commands run and verbatim output:
  1. Updated `scripts/probe_ring.py`:
     - Added steps from starter repo:
       (d) GET /v1/devices/{id}/status for first device
       (e) GET /v1/devices/{id}/capabilities for first device
       (f) GET /v1/users/me
     - Failure isolation: failure in one step does not abort later steps; records error and continues.
     - Confirmed `--snapshot-path` accepts `{id}` or `{device_id}` placeholders.
     - Summary table updated to include all 6 steps.
  2. Updated `evidence/ring/FINDINGS.md` template with:
     - "3. Device Status Fields and Online/Offline Representation"
     - "4. Capabilities (Snapshot, Video, Motion)"
     - "6. Event Types Naming Seen in History (dot style vs webhook format)"
     - "8. User Profile Fields"
  3. Added unit tests in `tests/test_probe_ring.py` using fake HTTP layer:
     - `test_device_status_step` (status 200, online attribute verified)
     - `test_device_capabilities_step` (status 200, capabilities verified)
     - `test_user_profile_step` (status 200, user id verified)
     - `test_http_401_error_handling` (401 handled cleanly, error message saved)
     - `test_http_404_error_handling` (404 handled cleanly, error message saved)
  4. Test run: `python3 -m unittest tests/test_probe_ring.py -v`:
     - 10/10 tests passing (0.046s).
  5. Dry run without token:
     - `python3 scripts/probe_ring.py; echo "exit=$?"`
     - Output:
       RING_ACCESS_TOKEN missing - generate a Playground token and put it in .env
       exit=2
  6. Help text: `python3 scripts/probe_ring.py --help` shows `{id}` and `{device_id}` substitution.
  7. Full test suite: `python3 -m unittest discover tests -v` → 40/40 tests passing (0.045s).
Expected: 3 new endpoints supported, failure isolation active, 401/404 handled, 40 tests passing, dry run exits 2.
Actual: Matched expected on all counts.
Result: PASS.
Status: A1b Complete.

### [2026-10-12T13:50:00Z] A1c — Snapshot POST & doc mode support (COMPLETE)
Commands run and verbatim output:
  1. Searched `evidence/ring/*.json` for any key/value containing "component":
     - Python script inspected all JSON files under `evidence/ring/`.
     - Result: None found (0 matches across `list_devices`, `device_status`, `device_capabilities`, `user_profile`, `event_history`).
  2. Extended `scripts/probe_ring.py`:
     - Added `--snapshot-method` (GET or POST) and `--snapshot-body` (JSON string, sets Content-Type: application/json).
     - Added `--snapshot-doc` convenience mode: POST to `/v1/devices/{id}/media/image/download`:
       - Attempt (a): body `{}`.
       - Attempt (b): only if real component values discovered in evidence (skipped since none found; never invents values).
     - Added binary magic bytes detection: JPEG (`\xff\xd8\xff`) and PNG (`\x89PNG\r\n\x1a\n`), auto-saves `.jpg` or `.png`.
     - Recorded `magic_format`, `content_type`, and `byte_size` in evidence JSON and summary table.
     - Error response bodies saved without crashing on non-200.
  3. Added unit tests in `tests/test_probe_ring.py`:
     - `test_post_with_body_sends_headers_and_payload` (POST method, Content-Type: application/json, body verified) ... ok
     - `test_binary_jpeg_response_saved_and_magic_detected` (JPEG magic byte detection, .jpg saved) ... ok
     - `test_binary_png_response_saved_and_magic_detected` (PNG magic byte detection, .png saved) ... ok
     - `test_error_response_saved_without_crashing` (HTTP 400 error body saved) ... ok
     - Run: `python3 -m unittest tests/test_probe_ring.py -v` → 14/14 tests passing (0.057s).
  4. Full test suite: `python3 -m unittest discover tests -v` → 44/44 tests passing (0.045s).
Expected: Snapshot POST supported, magic bytes detected for JPEG/PNG, error bodies captured, 44 tests green.
Actual: Matched expected on all counts.
Result: PASS.
Status: A1c Complete.

### [2026-10-12T14:30:00Z] GATE 1 — Live Ring API Probe & Playground Findings (COMPLETE ✅)
Commands run and verbatim output:
  1. `python3 scripts/probe_ring.py --snapshot-doc`
     - Authenticated live calls against `https://api.amazonvision.com`:
       - `GET /v1/devices` → HTTP 200 (1 device returned)
       - `GET /v1/history/devices/{id}/events` → HTTP 200 (event_type: 'on_demand', millisecond start/end timestamps confirmed)
       - `GET /v1/devices/{id}/status` → HTTP 200 (online: true during stream, reported_at timestamp confirmed)
       - `GET /v1/devices/{id}/capabilities` → HTTP 200 (12 capabilities, snapshot confirmed under image_enhancements)
       - `GET /v1/users/me` → HTTP 200 (account ID returned)
       - `POST /v1/devices/{id}/media/image/download` with `{}` → HTTP 403:
         detail: "Cannot authorize: missing required timestamp fields in request body"
  2. Playground UI findings (User screenshot):
     - WHEP Live Streaming verified: `POST /v1/devices/{id}/media/streaming/whep/sessions` returns HTTP 201 Created.
     - Watermarking verified: Non-removable Ring logo, Device ID, partner portal text, and timestamps visible on feed.
  3. Populated `evidence/ring/FINDINGS.md` with all confirmed field shapes and schemas.
Expected: Real authenticated calls to Ring pass; response shapes captured; GATE 1 passed.
Actual: Matched expected. Ring-at-runtime requirement satisfied.
Result: PASS — GATE 1 CLEARED.
Status: A1 Complete.

### [2026-10-13T06:00:00Z] A2 — SQLite Store & DynamoDB Schema Mapping (COMPLETE)

#### DynamoDB Single-Table to SQLite Relational Mapping:
| DynamoDB Item Type / Key Shape | SQLite Table | Primary Key | Notes |
|---|---|---|---|
| `ROUTINE#<id>` (config + mutable facts) | `routines` | `routine_id` | Stores window, days, timezone, delay, `last_satisfied_at` fact, and `snoozed_until`. Derived status is NEVER stored. |
| `DEVICE#<id>` (recent event stream) | `events` | `(source, external_id)` | Source check: `('webhook', 'history', 'simulator')`. Raw payload stored in `raw_json`. |
| `DEVICE#<id>` (latest health / status) | `device_health` | `device_id` | `online` boolean integer, `reported_at`, `fetched_at`, `source`. |
| `ESCALATION#<routine_id>#<date>` | `escalations` | `(routine_id, local_date)` | `UNIQUE(routine_id, local_date)` enforces fire-once per local day. |
| `CORRECTION#<id>` (household memory) | `corrections` | `id` | Device, event_type, time_bucket, label, created_at. |
| `SNOOZE#<routine_id>` | `routines` | `routine_id` | Stored as `snoozed_until` directly on `routines` (single source of truth matching models.py and status.py). |

**Cross-source deduplication note (TODO-VERIFY):**
Cross-source duplicates (e.g. the exact same physical event arriving via real webhook notification and subsequently ingested via history polling) are NOT resolved yet in this baseline. We do not invent an unproven dedupe rule; both rows will be preserved per source until real dual-path evidence defines the relationship.

#### Commands run and verbatim output:
  1. `git add BUILD_LOG.md scripts/ tests/test_probe_ring.py && git commit -m "A1: Ring probe script, extensions, tests"`
     - Commit hash: `9f2fb93`
  2. Implemented `src/threshold/store.py`:
     - SQLite with WAL mode (`PRAGMA journal_mode = WAL`) and foreign keys (`PRAGMA foreign_keys = ON`).
     - Tables created: `events`, `device_health`, `routines`, `escalations`, `corrections`.
     - Timestamp normalization: `to_iso_timestamp()` and `ms_to_iso()` converting epoch milliseconds (e.g. `1791814643626` -> `2026-10-12T14:17:23Z`).
  3. Created `tests/test_store.py`:
     - 9 unit tests covering:
       - ms epoch conversion (`1791814643626` -> `2026-10-12T14:17:23Z`)
       - insert and read back event
       - same (source, external_id) twice -> exactly one row
       - same external_id from two sources -> two rows
       - upsert device_health twice -> one row with newer values
       - malformed insert (missing NOT NULL) -> `sqlite3.IntegrityError`
       - escalation UNIQUE constraint -> `sqlite3.IntegrityError` on same routine and date
       - routine creation & last_satisfied_at fact update
       - corrections and snoozes table operations
     - Test run: `python3 -m unittest tests/test_store.py -v` → 9/9 tests OK (0.016s).
  4. Full test suite: `python3 -m unittest discover tests -v` → 53/53 tests passing (0.058s).
  5. Schema verification: `sqlite3 <db> .schema` verified all constraints and data types.
Expected: 53 tests passing, SQLite schema matches spec without guessed fields, fire-once UNIQUE constraint enforced.
Actual: Matched expected on all counts.
Result: PASS.

### [2026-10-13T07:15:00Z] A2b — Fix SQLite Store Schema & Dedupe Hole (COMPLETE)
Commands run and verbatim output:
  1. Schema improvements in `src/threshold/store.py`:
     - Added explicit `NOT NULL` to all PRIMARY KEY columns (`events.source`, `device_health.device_id`, `routines.routine_id`, `corrections.id`).
     - Consolidated snooze storage: `status.py` and `models.py` read `routine.snoozed_until` directly. Removed redundant `snoozes` table; `set_snooze()`, `get_snooze()`, and `clear_snooze()` operate on `routines.snoozed_until`.
  2. Added unit tests in `tests/test_store.py`:
     - `test_insert_event_null_source_raises_integrity_error` ... ok
     - `test_insert_device_health_null_device_id_raises_integrity_error` ... ok
     - `test_snooze_set_read_and_clear_single_source_of_truth` ... ok
     - Test run: `python3 -m unittest tests/test_store.py -v` → 12/12 tests OK (0.023s).
  3. Full test suite: `python3 -m unittest discover tests -v` → 56/56 tests passing (0.062s).
     - Breakdown:
       - `test_triage.py`: 21 tests
       - `test_webhook_auth.py`: 9 tests
       - `test_probe_ring.py`: 14 tests (10 from A1/A1b + 4 from A1c)
       - `test_store.py`: 12 tests (9 from A2 + 3 from A2b)
       - Total: 21 + 9 + 14 + 12 = 56 tests.
Expected: PRIMARY KEY columns reject NULL; snooze stored in single place on routines; 56 tests green.
Actual: Matched expected on all counts.
Result: PASS.
Status: A2b Complete.

### [2026-10-13T07:45:00Z] A3 — Shared Ingest & Webhook Server (IN PROGRESS)

#### Official Ring Webhook v1.1 Payload Documentation (Verbatim from Amazon Docs):
Source: `https://developer.amazon.com/docs/ring/api-documentation.html#notifications` & `#motion-detection`

```json
{
  "meta": {
    "version": "1.1",
    "time": "2026-02-13T13:39:57.200155525Z",
    "request_id": "2ad45ade-1818-4154-813b-afdd8bcd8085",
    "account_id": "ava1.ring.account.XXXYYY"
  },
  "data": {
    "id": "<device_id>_motion_<timestamp>",
    "type": "motion_detected",
    "attributes": {
      "source": "<device_id>",
      "source_type": "devices",
      "timestamp": 1786715596787,
      "timestamp_readable": "2026-08-14 08:53:16",
      "sub_type": "motion",
      "component_ids": ["0"]
    },
    "relationships": {
      "devices": {
        "links": {
          "self": "/v1/devices/<device_id>"
        }
      }
    }
  }
}
```

**Field locations confirmed from official documentation:**
- **Event Type:** `data.type` (e.g. `motion_detected`, `button_press`, `device_online`, `device_offline`).
- **Idempotency Key / External ID:** `meta.request_id` (or `data.id`).
- **Device ID:** `data.attributes.source` (or `data.relationships.devices...`).
- **Timestamp:** `data.attributes.timestamp` (epoch milliseconds integer, e.g. `1786715596787`).
- **Sub Type:** `data.attributes.sub_type` (docs note: "sub_type is returned inside attributes — read it as `data.attributes.sub_type`, not `data.subType`"). Values: `human`, `vehicle`, `package_delivery`, `motion`, `other_motion`.

#### Official Snapshot API Request Body (Verbatim from Amazon Docs):
Source: `https://developer.amazon.com/docs/ring/api-documentation.html#image-snapshots`
```json
{
  "type": "at_timestamp",
  "timestamp": 1787176751000,
  "image_options": {
    "format": "jpeg",
    "resolution": { "width": 1920, "height": 1080 }
  },
  "components": [
    { "component_id": "1" }
  ]
}
```
*(Single-camera devices do not require `components`). Flow returns `303 See Other` with `Location` header to pre-signed binary URL.*

### [2026-10-13T13:30:00Z] Task A3 — Shared Ingest & Webhook Server (COMPLETE)

#### 1. Unit 1: Shared Ingest Pipeline (`src/threshold/ingest.py`)
- Created `ingest_event()` as the unified entry point for all event sources (`webhook`, `history`, `simulator`).
- Performs:
  1. Idempotent insertion into `events` table on `(source, external_id)`.
  2. If `event_type` is `device_online` or `device_offline`, calls `upsert_device_health()`.
  3. If event matches a registered routine (`device_id`, `expected_event_type`, and matching `sub_type`), advances `routines.last_satisfied_at` if the event is newer.
- Tested in `tests/test_ingest.py` (5 unit tests covering duplicate delivery, health flip, last_satisfied advancement, older event preservation, non-matching routines).

#### 2. Unit 2: Webhook Server (`src/threshold/server.py`) & Sender CLI (`scripts/send_signed_event.py`)
- `src/threshold/server.py`:
  - Standard library only: `http.server.ThreadingHTTPServer`, `threading`, `json`, `os`, `sys`, `urllib.parse`.
  - Binds to `127.0.0.1` (configurable via `THRESHOLD_PORT`, default 8080).
  - Enforces `MAX_BODY_BYTES = 1 MB` limit; drains and rejects oversized payloads with HTTP 413.
  - Verifies HMAC-SHA256 signature (`X-Signature: sha256=<hex>`) **BEFORE** JSON parsing on raw request body bytes.
  - Fast response time (< 5 ms, well under the 5-second constraint).
  - Storage failures are NOT swallowed: returns HTTP 500 on database or storage failure.
  - Endpoints: `POST /webhooks/ring` (ingest), `GET /healthz` (`{"status": "ok"}`).
- `scripts/send_signed_event.py`:
  - CLI utility to test/simulate Ring events with HMAC signature computed from environment variable (`RING_WEBHOOK_SECRET`).
  - Never accepts secret as a CLI flag.

#### 3. Test Suite Verification (`tests/test_server.py`)
Ran full integration suite on ephemeral port 0 with live background server:
1. `test_valid_signature_200_and_one_row` -> 200 OK, exactly 1 row in `events`.
2. `test_corrupted_signature_401_zero_rows` -> 401 Unauthorized, 0 rows in `events`.
3. `test_missing_signature_header_401` -> 401 Unauthorized.
4. `test_replay_attack_duplicate_delivery_one_row` -> 200 OK, exactly 1 row (idempotent).
5. `test_malformed_json_with_valid_signature_400` -> 400 Bad Request, HMAC verified first.
6. `test_body_too_large_413` -> 413 Payload Too Large.
7. `test_forced_storage_failure_returns_500` -> 500 Internal Server Error (verifies errors are never swallowed).
8. `test_measured_response_time_under_five_seconds` -> Measured response time: 1.75 ms (< 5000 ms limit).
9. `test_device_offline_event_updates_device_health` -> `device_health` updated to `online=0`.
10. `test_matching_motion_event_advances_last_satisfied_at` -> routine `last_satisfied_at` advanced.
11. `test_healthz_endpoint` -> 200 OK `{"status": "ok"}`.

**Full Test Suite Run:**
`python3 -m unittest discover tests -v`
**72 tests in 5.671s — all passing** (56 baseline + 5 ingest + 11 server).

#### 4. Manual curl Transcript (Verbatim Output)
```text
Starting Threshold Webhook Server on 127.0.0.1:8899

=== 1. CURL WITH VALID SIGNATURE ===
HTTP/1.0 200 OK
Server: BaseHTTP/0.6 Python/3.14.7
Date: Tue, 13 Oct 2026 07:47:53 GMT
Content-Type: application/json
Content-Length: 23

{"status": "received"}

=== SQLITE3 ROW COUNT AFTER VALID CURL ===
1|webhook|demo_req_001|front_door|motion_detected

=== 2. CURL WITH CORRUPTED SIGNATURE ===
HTTP/1.0 401 Unauthorized
Server: BaseHTTP/0.6 Python/3.14.7
Date: Tue, 13 Oct 2026 07:47:53 GMT
Content-Type: application/json
Content-Length: 82

{"error": "Unauthorized: Signature mismatch — request may not be from Ring"}

=== SQLITE3 ROW COUNT AFTER CORRUPTED CURL (SHOULD STILL BE 1) ===
1
```

### [2026-10-13T13:40:00Z] Step 0 — Verbatim Ring Docs Excerpts & Evidence Verification

#### 1. Verbatim Allowed `sub_type` Values from Official Docs
Source file: `content.md` lines 7227–7277:
```html
7227: <p>The values <code>sub_type</code> can carry depend on whether the device has <strong>Smart Alerts</strong> enabled. Smart Alerts is the Ring-side detection that classifies what triggered a motion event; customers turn it on per device in the Ring app.</p>
7228: 
7229: <p><strong>Smart Alerts disabled.</strong> No classification is performed, and every motion event from the device carries one value:</p>
7230: 
7231: <table>
7232:   <thead>
7233:     <tr>
7234:       <th>Sub Type</th>
7235:       <th>Description</th>
7236:     </tr>
7237:   </thead>
7238:   <tbody>
7239:     <tr>
7240:       <td>motion</td>
7241:       <td>General motion detected</td>
7242:     </tr>
7243:   </tbody>
7244: </table>
7245: 
7246: <p><strong>Smart Alerts enabled.</strong> Ring classifies the trigger, and <code>sub_type</code> carries one of:</p>
7247: 
7248: <table>
7249:   <thead>
7250:     <tr>
7251:       <th>Sub Type</th>
7252:       <th>Description</th>
7253:     </tr>
7254:   </thead>
7255:   <tbody>
7256:     <tr>
7257:       <td>human</td>
7258:       <td>Human detected</td>
7259:     </tr>
7260:     <tr>
7261:       <td>vehicle</td>
7262:       <td>Vehicle detected</td>
7263:     </tr>
7264:     <tr>
7265:       <td>package_delivery</td>
7266:       <td>Package delivery detected</td>
7267:     </tr>
7268:     <tr>
7269:       <td>other_motion</td>
7270:       <td>Unable to categorize motion</td>
7271:     </tr>
7272:   </tbody>
7273: </table>
7274: 
7275: <div class="alert alert-danger" role="alert"><i class="fa fa-exclamation-triangle"></i> <b>Important:</b> <code>sub_type</code> is returned <strong>inside</strong> <code>attributes</code> — read it as <code>data.attributes.sub_type</code>, not <code>data.subType</code>.</div>
7276: 
7277: <div class="alert alert-info" role="alert"><i class="fa fa-info-circle"></i> <b>Note:</b> No API exposes whether Smart Alerts is enabled on a device, so your app cannot check the setting before deciding how to read <code>sub_type</code>. Treat the field as an open-ended vocabulary: handle the values you act on, and pass anything else through rather than dropping it. See <a href="ux-design-guide.html#refer-to-ring-app-for-device-configuration-settings">Refer to Ring app for device configuration settings</a>.</div>
```

**Verification:** The docs DO explicitly list `vehicle` and `package_delivery` alongside `human`, `motion`, and `other_motion`.

#### 2. Verbatim Image Snapshots Section from Official Docs
Source file: `content.md` lines 11431–11581, 11633–11656, 11707–11760:
```html
11431: <h2 id="image-snapshots">Image Snapshots</h2>
11432: 
11433: <p>Ring provides an endpoint to download image snapshots in JPEG or PNG formats by a specified timestamp of the last snapshot in a time range.</p>
11434: 
11435: <h3 id="overview-9">Overview</h3>
11436: 
11437: <ul>
11438:   <li><strong>JPEG / PNG</strong>: The download can be in JPEG or PNG format as specified in the <code>image_options.format</code></li>
11439:   <li><strong>Image Resolution</strong>: Image width and height as specified (optional) in <code>image_options.resolution.width</code> and <code>image_options.resolution.height</code></li>
11440:   <li><strong>Snapshot Type</strong>: The sanpshot at the specified timestamp or the last snapshot in a timestamp range</li>
11441: </ul>
11442: 
11443: <h3 id="download-flow">Download Flow</h3>
11444: 
11445: <p>Image downloads follow a <strong>two-step flow</strong>:</p>
11446: 
11447: <ol>
11448:   <li><strong>POST</strong> to the media endpoint with your request parameters → returns a <code>303 See Other</code> redirect with a pre-signed <code>Location</code> URL</li>
11449:   <li><strong>GET</strong> the pre-signed URL from the <code>Location</code> header → returns the binary media content</li>
11450: </ol>
...
11499: <table>
11500:   <thead>
11501:     <tr>
11502:       <th>Field</th>
11503:       <th>Type</th>
11504:       <th>Required</th>
11505:       <th>Description</th>
11506:       <th>Constraints</th>
11507:     </tr>
11508:   </thead>
11509:   <tbody>
11510:     <tr>
11511:       <td><code>type</code></td>
11512:       <td>string</td>
11513:       <td>Yes</td>
11514:       <td>Search mode</td>
11515:       <td><code>"at_timestamp"</code> or <code>"latest_in_range"</code></td>
11516:     </tr>
11517:     <tr>
11518:       <td><code>timestamp</code></td>
11519:       <td>integer</td>
11520:       <td>For <code>at_timestamp</code></td>
11521:       <td>Target time in epoch milliseconds</td>
11522:       <td>Within last 180 days, ≤ now</td>
11523:     </tr>
11524:     <tr>
11525:       <td><code>start_timestamp</code></td>
11526:       <td>integer</td>
11527:       <td>For <code>latest_in_range</code></td>
11528:       <td>Window start in epoch milliseconds</td>
11529:       <td>Within last 180 days, ≤ now</td>
11530:     </tr>
11531:     <tr>
11532:       <td><code>end_timestamp</code></td>
11533:       <td>integer</td>
11534:       <td>No</td>
11535:       <td>Window end in epoch milliseconds</td>
11536:       <td>Defaults to now. Window ≤ 24 hours</td>
11537:     </tr>
11538:     <tr>
11539:       <td><code>image_options</code></td>
11540:       <td>object</td>
11541:       <td>No</td>
11542:       <td>Image format options</td>
11543:       <td>&nbsp;</td>
11544:     </tr>
11545:     <tr>
11546:       <td><code>image_options.format</code></td>
11547:       <td>string</td>
11548:       <td>No</td>
11549:       <td>Output format</td>
11550:       <td><code>"jpeg"</code> or <code>"png"</code></td>
11551:     </tr>
11552:     <tr>
11553:       <td><code>image_options.resolution.width</code></td>
11554:       <td>integer</td>
11555:       <td>No</td>
11556:       <td>Width in pixels.</td>
11557:       <td>1–1920</td>
11558:     </tr>
11559:     <tr>
11560:       <td><code>image_options.resolution.height</code></td>
11561:       <td>integer</td>
11562:       <td>No</td>
11563:       <td>Height in pixels.</td>
11564:       <td>1–1080</td>
11565:     </tr>
11566:     <tr>
11567:       <td><code>components</code></td>
11568:       <td>array</td>
11569:       <td>No</td>
11570:       <td>Camera module to capture from. Required for a predictable result on a multi-camera device.</td>
11571:       <td>Exactly one object — multiple modules per request are not allowed</td>
11572:     </tr>
11573:     <tr>
11574:       <td><code>components[].component_id</code></td>
11575:       <td>string</td>
11576:       <td>With <code>components</code></td>
11577:       <td>Camera module identifier from <code>components.items[]</code> in capabilities</td>
11578:       <td>String, not an integer</td>
11579:     </tr>
11580:   </tbody>
11581: </table>
...
11633: <h5 id="redirect-response">Redirect Response</h5>
11634: 
11635: <div class="language-http highlighter-rouge"><div class="highlight"><pre class="highlight"><code><span class="k">HTTP</span><span class="o">/</span><span class="m">1.1</span> <span class="m">303</span> <span class="ne">See Other</span>
11636: <span class="na">Location</span><span class="p">:</span> <span class="s">https://download-{region}.{domain}/v1/download?security_token=...&amp;req_type=image_download&amp;req_id=...&amp;X-Amz-Date=...&amp;X-Amz-Expires=...</span>
11637: <span class="na">X-Request-ID</span><span class="p">:</span> <span class="s">&lt;request_id&gt;</span>
11638: </code></pre></div></div>
...
11707: <table>
11708:   <thead>
11709:     <tr>
11710:       <th>HTTP Status</th>
11711:       <th>Code</th>
11712:       <th>Detail</th>
11713:       <th>When</th>
11714:     </tr>
11715:   </thead>
11716:   <tbody>
11717:     <tr>
11718:       <td>400</td>
11719:       <td><code>INVALID_PAYLOAD</code></td>
11720:       <td>Validation message</td>
11721:       <td>Invalid or missing request parameters</td>
11722:     </tr>
11723:     <tr>
11724:       <td>401</td>
11725:       <td><code>UNAUTHORIZED</code></td>
11726:       <td>Authentication credentials are missing or invalid</td>
11727:       <td>Bad or expired token</td>
11728:     </tr>
11729:     <tr>
11730:       <td>403</td>
11731:       <td><code>REQUEST_FORBIDDEN</code></td>
11732:       <td>Client is not authorized for the device</td>
11733:       <td>No access to device</td>
11734:     </tr>
11735:     <tr>
11736:       <td>404</td>
11737:       <td><code>DEVICE_NOT_FOUND</code></td>
11738:       <td>Unknown device provided: {id}</td>
11739:       <td>Invalid device ID</td>
11740:     </tr>
11741:     <tr>
11742:       <td>416</td>
11743:       <td><code>MEDIA_NOT_FOUND</code></td>
11744:       <td>No media found in time range</td>
11745:       <td>No recordings exist for the requested time</td>
11746:     </tr>
11747:     <tr>
11748:       <td>422</td>
11749:       <td><code>CORRUPT_RECORDING</code></td>
11750:       <td>Provided timestamp is within a corrupt recording</td>
11751:       <td>Damaged recording data</td>
11752:     </tr>
11753:     <tr>
11754:       <td>425</td>
11755:       <td><code>RECORDING_NOT_READY</code></td>
11756:       <td>The request was made before any recording was available</td>
11757:       <td>Recording not yet available</td>
11758:     </tr>
11759:     <tr>
11760:       <td>500</td>
11761:       <td><code>INTERNAL_ERROR</code></td>
11762:       <td>Internal server error</td>
11763:       <td>Ring service failure</td>
11764:     </tr>
11765:   </tbody>
11766: </table>
```

### [2026-10-13T13:50:00Z] Task A4 — Scheduler + Absence Engine (COMPLETE)

#### 1. Implementation Architecture (`src/threshold/scheduler.py`)
- Standard library only: `abc`, `datetime`, `json`, `logging`, `os`, `sqlite3`, `threading`, `time`, `traceback`, `typing`, `zoneinfo`.
- **Clock Interfaces:**
  - `Clock` (ABC)
  - `RealClock`: Returns UTC wall-clock time (`datetime.now(timezone.utc)`).
  - `FakeClock`: Controllable via `set_time()` and `advance()`.
  - `AcceleratedClock`: N simulated seconds per 1 real second for demo mode.
- **Absence Engine (`run_scheduler_tick`):**
  - Evaluates each routine via existing deterministic `derive_status()` in `status.py`.
  - `missed` status: creates escalation once per `(routine_id, local_date)` using `INSERT OR IGNORE`.
  - `unknown` (stale/offline device), `snoozed`, or `observed`: never escalates.
  - Late event after a miss: status transitions to `observed`, existing escalation row is preserved (no duplicate).
- **Delivery with Retry:**
  - `escalations.notified_at` column tracks delivery status (NULL until delivered).
  - Each tick processes pending unnotified escalations via `Notifier` interface (`RecordingNotifier`).
  - If notification delivery raises, `notified_at` remains NULL and retries next tick. After delivery, never resends.
- **Background Scheduler (`Scheduler`):**
  - Background daemon thread controlled by `threading.Event` (`start()`, `stop()`).
  - Tick exceptions are caught and logged with full traceback without killing the thread (`last_tick_at`, `last_error`).
- **Timezone & Window Handling (`src/threshold/status.py`):**
  - Added support for overnight windows crossing midnight (e.g. 22:00 to 06:00).
  - Verified across US fall-back DST boundary (2026-11-01), US spring-forward DST boundary (2026-03-08), and non-DST zone (`Asia/Kolkata`).

#### 2. Test Suite Verification (`tests/test_scheduler.py`)
Ran unit and integration suite:
1. `test_five_consecutive_ticks_past_miss_exactly_one_escalation_row` -> ok
2. `test_simulated_restart_on_same_db_still_one_escalation_row` -> ok
3. `test_stale_device_returns_unknown_and_zero_escalations` -> ok
4. `test_offline_device_returns_unknown_and_zero_escalations` -> ok
5. `test_snoozed_routine_returns_zero_escalations` -> ok
6. `test_late_event_after_miss_status_observed_still_one_escalation` -> ok
7. `test_notifier_fails_once_retries_and_delivers_once` -> ok
8. `test_tick_exception_does_not_kill_thread` -> ok
9. `test_timezone_window_crossing_midnight` -> ok
10. `test_us_fallback_dst_boundary` -> ok
11. `test_us_spring_forward_dst_boundary` -> ok
12. `test_non_dst_zone_asia_kolkata` -> ok
13. `test_clocks_mechanics` -> ok

**Full Test Suite Run:**
`python3 -m unittest discover tests -v`
**86 tests in 6.501s — all passing** (56 baseline + 5 ingest + 12 server + 13 scheduler).

#### 3. Demo Timeline Output (`scripts/demo_ticks.py`)
```text
=========================================================================================================
                    THRESHOLD DETERMINISTIC ABSENCE DETECTION ENGINE — SIMULATED DAY                     
=========================================================================================================
| Tick (UTC)   | Local (CDT)  | Routine Status  | Escalations | Notified   | Context Description                    |
|--------------|--------------|-----------------|-------------|------------|----------------------------------------|
| 11:45 UTC    | 06:45 CDT    | watching        | 0           | No         | Before window starts (07:00)           |
| 12:30 UTC    | 07:30 CDT    | watching        | 0           | No         | Inside active window (07:00-09:00)     |
| 14:15 UTC    | 09:15 CDT    | watching        | 0           | No         | Past window_end, inside 30m delay      |
| 14:35 UTC    | 09:35 CDT    | missed          | 1           | Yes        | Past deadline (09:30 CDT) -> Miss detected! |
| 14:45 UTC    | 09:45 CDT    | missed          | 1           | Yes        | Consecutive tick past miss (deduped)   |
| 15:00 UTC    | 10:00 CDT    | observed        | 1           | Preserved  | Late event arrives satisfying window   |
=========================================================================================================

=== SQLITE3 ESCALATIONS TABLE AFTER DEMO ===
rt_morning_door|2026-10-12|2026-10-12T14:35:00Z||2026-10-12T14:35:00Z
```







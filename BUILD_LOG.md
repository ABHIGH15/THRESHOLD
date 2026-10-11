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

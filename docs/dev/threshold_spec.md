# Threshold — Consolidated Spec

> **Status:** Brainstorming complete. All load-bearing design decisions locked. Implementation blocked on Ring Partner API portal access and three deferred verification items.
>
> **Hackathon:** Build, Ship, Shape — Amazon Developer Hackathon. Ring track. Deadline: October 23, 2026, 12:00pm PDT. Prizes: 1st ($12,000), 2nd ($8,000). Mini-challenges stacked: AWS Builder + Open Source (opportunistic).

---

## Problem Statement

Ring's Shared Users feature lets a remote caretaker see everything a resident sees — but "everything" is the problem. A stream of individual events is the wrong interface for someone who isn't there. A caretaker needs to know the **state** of a property ("everything's normal" / "something's off" / "something's missing"), not a feed of moments. And the most critical state — the absence of expected activity — is invisible to any event-triggered system, including Ring's newest AI, because there's no event to trigger on.

### What Ring already ships (the competitive landscape)

| Feature | What it does | What it doesn't do |
|---|---|---|
| AI Single Event Alert (Nov 2025) | Collapses related motion events into one notification | Doesn't triage for a remote observer |
| Familiar Faces (Jan 2026, Ring AI Pro) | Recognizes frequent visitors | Doesn't adapt context for a proxy |
| Unusual Event Alert (CES 2026) | Learns property norms, flags deviations | Doesn't detect absence of expected activity |
| Active Warnings | Real-time verbal warnings on person detection | Resident-facing, not caretaker-facing |
| Alexa+ Greetings / Video Descriptions | AI-generated event descriptions, visitor interaction | Consumer-app feature, not exposed via Partner API |

**The gap:** Nobody tells the person who isn't there what's missing, or triages a property's event stream for someone without the resident's ambient context. Nobody detects the routine that silently didn't happen.

---

## The Idea

**Threshold** — a state-aware triage agent for remote property monitoring.

### Positioning line

> "Ring tells the person in the house what happened. Threshold tells the person who isn't there what's missing — and calls someone who can check."

Tagline (marketing language, not a claim of priority over unnamed competitors):
> **"Ring tells you what happened. Threshold tells you when something you expected didn't happen."**

Alternate framing (more technical register — test in rehearsal):
> "Events tell us what happened. Expectations tell us what should have happened. Threshold connects the two."

### Hero feature

**Absence detection.** A scheduled check against persistent state that fires when an expected routine didn't happen — structurally impossible for any reactive, webhook-triggered system. This is the lead demo beat and the primary differentiator.

### Three-tier verdict

| Tier | Trigger | Action |
|---|---|---|
| **Routine** | Event matches expected patterns | Log silently |
| **Needs a look** | Event doesn't match known routines or threats; agent investigated but escalation gate not cleared | Notify caretaker with reasoning + snapshot attached |
| **Urgent** | Absence detected (scheduler) OR agent recommends urgent + deterministic gate clears | Escalate outward: SMS → voice call to caretaker |

**UI-facing labels:** Internal tier names stay `Routine / Needs a look / Urgent`. Caretaker-facing UI copy becomes **`Routine / Check Needed / Escalation`** — avoids "Urgent" reading as an emergency-detection claim we've explicitly scoped out.

---

## Architecture

### Two entry points, one state store, one reasoning engine

```
┌─────────────────┐         ┌──────────────────────┐
│  Ring Webhooks   │────────▶│                      │
│  (event-driven)  │         │   Property State     │
└─────────────────┘         │   Store (DynamoDB)   │
                            │                      │
┌─────────────────┐         │  Per-device:          │
│  Scheduler       │────────▶│   last_event_ts      │
│  (timer-driven)  │         │   event_type         │
└─────────────────┘         │   sub_type            │
                            │                      │
                            │  Per-routine:         │
                            │   config fields       │
                            │   last_satisfied_at   │
                            │   snoozed_until       │
                            └──────────┬───────────┘
                                       │
                                       ▼
                            ┌──────────────────────┐
                            │  Triage Engine        │
                            │  (same logic for both │
                            │   entry points)       │
                            └──────────┬───────────┘
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                      Routine    Needs a look    Urgent
                      (log)      (notify+snap)   (escalate)
```

### AWS Stack

| Component | Role |
|---|---|
| **Lambda (webhook handler)** | Receives Ring events, HMAC-SHA256 verifies, writes to state store, runs triage engine |
| **Lambda (scheduler handler)** | Triggered by EventBridge Scheduler. Derives routine status. Edge-detects `watching → missed` transitions. Fires triage engine with absence signal. |
| **DynamoDB** | Shared persistent state: routine configs, `last_satisfied_at`, `snoozed_until`, per-device recent event log, correction history |
| **EventBridge Scheduler** | Timer for absence-check path. Configurable interval — tight for demo (≤1 min), wider for production. |
| **Bedrock (Claude/Nova)** | Reasoning layer for bounded-authority agent on ambiguous events |
| **Strands SDK / AgentCore** | Agent framework — tool-using agent with defined tool set |
| **SNS** | SMS delivery to caretaker ($0.00645/msg US, paid from $150 AWS credit) |
| **Amazon Connect** | Voice call escalation (more setup than Twilio, but no trial clock, uses AWS credit, keeps stack AWS-native) |
| **Image Snapshots API** | `api.amazonvision.com` — JPEG/PNG snapshots, OAuth bearer auth. Used for "Needs a look" attachment. |

> [!TIP]
> DynamoDB + EventBridge Scheduler + Lambda + Bedrock + SNS + Connect is a coherent, idiomatic serverless AWS stack end-to-end — a second, independent signal of real cloud engineering on top of "we called an LLM," strengthening Tech Implementation beyond just the AWS Builder box-check.

---

## Routine Schema (Locked — Derived Status, Three-State Absence Model)

```python
# Stored in DynamoDB — routine configuration + minimal mutable state
Routine:
    property_id: str
    name: str                        # e.g., "Morning door open"
    device_id: str
    expected_event_type: str         # e.g., "motion_detected", "button_press"
    window_start: str                # "07:00"
    window_end: str                  # "09:00"
    timezone: str                    # "America/Chicago"
    days: list[str]                  # ["mon", "tue", ..., "sun"]
    escalation_delay_minutes: int    # how long past window_end before firing
    last_satisfied_at: timestamp | None   # written by webhook path
    snoozed_until: timestamp | None       # written by caretaker action


# Status is DERIVED on every scheduler tick, never stored:
def status(routine, device_health, now):
    if routine.snoozed_until and now < routine.snoozed_until:
        return "snoozed"
    if routine.last_satisfied_at falls within today's window:
        return "observed"
    if now > today's window_end + escalation_delay:
        if device_health.is_stale(routine.device_id, now):   # freshness check, not a bare boolean flag
            return "unknown"     # do not escalate — data isn't trustworthy enough to conclude absence
        return "missed"          # fires Urgent — this is the only case that escalates
    return "watching"
```

`device_health.is_stale` checks recency of the device's last known status, not just whether the last webhook said "online" — a stale timestamp is itself informative, independent of whether Ring's `device_offline` event fired promptly.

> [!IMPORTANT]
> Status is never stored as mutable state — it's computed fresh each tick. This eliminates stale-state bugs (routines stuck in "missed" forever, repeated escalations, forgotten resets) and cleanly separates system-computed state from caretaker actions (`snoozed_until` is its own field with its own expiry).
>
> The `"unknown"` state prevents a specific dangerous failure: escalating "Dad didn't open the door" when in fact the camera was offline and couldn't have reported the event. This stays fully deterministic — no agent involvement in the absence path.

**Edge detection for "fire once":** The scheduler compares current derived status against last-tick status. Urgent fires only on the `watching → missed` transition, not on every subsequent tick where it's still `missed`.

---

## Triage Engine — Bounded-Authority Agent

### Design principle

> Rules gate anything safety-critical. The LLM adds judgment and explanation on top. A probabilistic model is never the sole trigger on an action that calls someone's phone about their parent.

### Rules-determined paths (no agent involvement)

- **Event matches a known routine** → Routine. Log silently. Update `last_satisfied_at`.
- **Scheduler derives `watching → missed`** → Urgent. Escalate. The absence itself is the evidence; no LLM needed.
- **Scheduler derives `watching → unknown`** (device health stale) → **Needs a look.** Do not escalate — the data isn't trustworthy enough to conclude absence. Do not silently swallow — the caretaker should know the device may be offline. This is a deterministic rules path, not an agent decision.

### Agent-investigated path (ambiguous events)

Events that don't match any known routine trigger the bounded-authority agent.

> [!NOTE]
> **Before the agent exists (Phase 1–4):** any event that doesn't cleanly match a routine or a clear absence defaults conservatively to **Needs a look** as a placeholder — consistent with "don't auto-clear, don't auto-escalate." This is an explicit architectural choice, not a stub: the conservative default ensures no event is silently dropped or incorrectly escalated while the agent layer is being built.

**Full investigative tool authority — three evidence layers:**

| Tool | Answers |
|---|---|
| `get_recent_ring_events(device_id, time_range)` | What did Ring's own Event History API report? |
| `pull_snapshot(device_id)` | What does the latest image show? (Handle TAKE/E2EE-restricted response as a first-class case, not an error to crash on — see TAKE/E2EE graceful degradation below.) |
| `check_routine_status(property_id)` | What does Threshold currently expect? |
| `get_threshold_history(property_id)` | What has Threshold itself previously concluded about similar situations? |
| `check_household_memory(device_id, event_type, time_pattern)` | Has the caretaker previously marked this pattern a false alarm, or defined an exception for it? |

**Agent output — evidence, not confidence scores:**
- Recommended tier (Routine / Needs a look / Urgent)
- Plain-language reasoning
- Evidence list (checkable facts, not a bare percentage that implies calibration this system doesn't have)

Example:
```
Evidence
✓ Kitchen activity at 08:17
✗ Expected front-door event
✓ Similar delay occurred 3 times previously, 2 marked false alarm by caretaker

Recommendation: Needs a look
```

**Deterministic escalation gate:**
- Agent recommends Urgent **AND** event is outside all configured routine windows **AND** evidence supports the recommendation → **Escalate**
- Anything that doesn't clear the gate → **Needs a look** (caretaker gets reasoning + evidence + snapshot if available, not silently downgraded, not auto-escalated)
- Timeout (agent doesn't resolve in N seconds) → **Conservative fallback** (escalate with "could not determine — please check")

> [!NOTE]
> The `check_household_memory` tool is the "gets better over time" answer for judges. The one-tap false-alarm reply already captures correction data; this tool makes the agent reason over it. Not a learning system — a memory system. Cheap to build, strong to demo.

### TAKE/E2EE Graceful Degradation

**Event received ≠ media available.** `pull_snapshot` and any future video calls must handle an encrypted/unavailable response as a distinct, expected outcome — the verdict still gets produced (from event metadata and history alone), just without a visual attached, with that omission stated plainly to the caretaker rather than silently absent.

---

## Escalation Mechanics

**Outbound channel:** AWS SNS (SMS) + Amazon Connect (voice call)
- No 30-day trial clock (eliminates Twilio calendar risk)
- Paid from $150 AWS hackathon credit
- Keeps entire stack AWS-native

**Escalation ladder:**
1. SMS with verdict + reasoning + snapshot link
2. If no response within configured window → voice call via Connect

**False alarm reply:** SMS keyword (e.g., reply `FALSE ALARM`). Captured via SNS/Connect inbound handling. Writes a correction record to DynamoDB that the agent's `check_household_memory` tool reads on future events.

**Stretch (if time):** Tap-a-link into dashboard, deep-linked to the specific verdict. Build only after core loop is solid.

---

## Consent Model

**MVP: setup-time mutual consent.** When a caretaker configures monitoring for a property, both parties (caretaker and resident) complete an explicit consent step, logged with a timestamp. No live physical indicator in v1 — we don't have confirmed Partner API access to control anything on the resident's device UI.

**Verified and ruled out:** Alexa Proactive Events API. It requires pre-defined schema templates (not arbitrary text), plays a chime (doesn't announce out loud), and requires building and certifying a separate Alexa Skill. Wrong tool for this job.

**Pitch-time answer for "does the resident know":** "In production, this would integrate with Ring's existing Alexa announcement path for motion/doorbell events." Honest framing — name the gap, name the path, don't claim you built it.

---

## Scope Fences (State Explicitly)

- **Not a security system.** Ring's own features and 911 are the right tools for acute threats.
- **Not a real-time emergency detector.** This is a pattern-absence detector — it notices when the morning didn't happen, not when someone falls.
- **Not learning from history in v1.** Manual routine configuration. The `check_past_corrections` tool is memory, not learning.
- **Complement to Ring's AI, not a competitor.** "Ring's Unusual Event Alert learns what's *normal* and flags what's *abnormal*. Threshold learns what's *expected* and flags what's *missing*. One for commission, one for omission."

---

## Demo Script (Sub-3 Minutes)

### Beat 1 — Absence detection (hero moment)
Open cold. The dashboard shows: "Dad opens the front door every morning between 7:00 and 8:30. It's 9:15. No event." The scheduler fires, derives `missed`, the caretaker's phone buzzes with an SMS, then rings with a voice call. **"Your father hasn't opened the front door this morning."**

### Beat 2 — Agent investigation trace
An ambiguous motion event arrives at an unexpected time. Show the agent's tool-use trace at demo speed (pre-run, faithful replay — not a live Bedrock call): pulls event history, checks past corrections, retrieves snapshot, produces reasoning and recommendation. The deterministic gate evaluates. Caretaker gets a "Needs a look" notification with the reasoning and snapshot attached.

### Beat 3 — Correction loop
Caretaker replies `FALSE ALARM` via SMS. Show the correction record being written. Mention that the agent's `check_past_corrections` tool will factor this in next time — the system has memory.

> [!IMPORTANT]
> Pre-run the agent investigation sequence for beat 2. Showing the tool calls and reasoning is the point — proving it's not cached is not. A live model call that hangs for 3 seconds on stage is a worse trade than a fast, faithful replay of a real run.

**Scheduler interval for demo:** ≤1 minute or on-demand trigger. State in pitch that production default would be wider (5–15 min). Don't make the room wait.

---

## Ring Partner API — Verified Ground Truth

| Item | Status | Source |
|---|---|---|
| OAuth 2.0 account linking | ✅ Confirmed | developer.amazon.com/docs/ring |
| Webhooks: HMAC-SHA256 `X-Signature`, 5s response, JSON:API | ✅ Confirmed | developer.amazon.com/docs/ring |
| Full webhook event list: `motion_detected`, `button_press`, `device_added`, `device_removed`, `device_online`, `device_offline`, `app_integration_added`, `app_integration_removed`, `subscription_activated`, `subscription_deactivated` | ✅ Confirmed | GA release notes, April 21, 2025 |
| `subType` field: camelCase, under `data`; only `"human"` confirmed as a value | ✅ Confirmed (partial) | motion-detection.html |
| Image Snapshots (JPEG/PNG) | ✅ Confirmed | api-documentation.html |
| Live Video Streaming (WebRTC/WHEP, video-only, receive-only, 30s battery / 60s line-powered session limits) | ✅ Confirmed | live-video.html |
| RTSP live video | ✅ Confirmed, available since GA (not a later addition) | release notes, April 21, 2025 |
| Media Clips (historical MP4 download) | ✅ Confirmed | release notes, April 21, 2025 |
| **Event History API** (historical device events) | ✅ Confirmed to exist; exact response schema/pagination unverified | release notes, April 21, 2025 — GA |
| Watermarking on all media (Live Video, Media Clips, Image Snapshots — Ring logo, Device ID, App Name, timestamp; server-side, non-removable) | ✅ Confirmed | release notes, June 8, 2026 |
| Developers Playground (30-min one-click OAuth token, API explorer, event simulation for **Package, Vehicle, and Motion**) | ✅ Confirmed | release notes, May 28, 2026 |
| **TAKE encryption** — becoming default for all Ring customers worldwide, phased rollout starting September 2026 | ✅ Confirmed real and imminent | Amazon's own announcement + technical whitepaper |
| TAKE's effect on Partner API media access specifically | ❌ **Not confirmed either way** | No primary developer-facing source found — verify directly in Phase 0 |
| E2EE — blocks even the resident's own Shared Users from clip access when enabled | ✅ Confirmed | Ring support documentation |
| Starter repo `ring-api-helloworld`, $150 AWS credit, official Ring Appstore Knowledge MCP (docs-lookup only) | ✅ Confirmed | prior verification, unchanged |

> [!NOTE]
> The **Ring Appstore Knowledge MCP Server** is a remote MCP server backed by a Bedrock Knowledge Base with exactly two tools — `search_docs` and `get_doc` — for documentation lookup inside your IDE (Kiro, Cursor, Cline, Claude Desktop). It is a **dev-productivity aid for building faster**, not a runtime tool for Threshold's own agent. This is a genuinely different thing from the unofficial, personal-credential "jpcors" Ring MCP Server that was correctly ruled out earlier — that one uses `ring-client-api`, requires personal Ring account credentials and 2FA, and explicitly disclaims Amazon affiliation.

### Deferred to portal access

| Item | Why deferred |
|---|---|
| Full `subType` enum beyond `"human"` | Only `"human"` confirmed from primary source. Playground simulates Package, Vehicle, and Motion — test what actually arrives on the wire. |
| Video Descriptions in partner webhook payload | Not confirmed present or absent. Consumer-app feature; likely not in partner payload, but not verified. |
| TAKE's effect on Partner API media endpoints | Confirmed as real and imminent (September 2026 rollout), but no developer-facing documentation on how it affects partner snapshot/video/clip access. **Verify in Phase 0 before any media-dependent feature is treated as stable.** |

> [!CAUTION]
> Do not build against the `attributes.sub_type` (snake_case, nested under `attributes`) schema that appears in some search results and the `ring-sandbox` PyPI package. That structure was not confirmed from `developer.amazon.com` primary sources. The user-verified structure is `subType` (camelCase) directly under `data`.

---

## Mini-Challenge Alignment

### AWS Builder ✅
Bedrock (reasoning), Strands SDK / AgentCore (agent framework), DynamoDB, Lambda, EventBridge Scheduler, SNS, Amazon Connect. Optionally: Ring Appstore Knowledge MCP Server (Bedrock-backed) for doc lookups during development. Building with Kiro Crew qualifies on its own.

### Open Source (opportunistic)
A genuine PR to `AmazonAppdev/ring-api-helloworld` or another public repo during the hackathon window, if a real gap surfaces. A real contribution, not just making your own repo public.

---

## Scope — Updated After Second-Opinion Integration

- **Package and vehicle detection:** moved from "dropped" to **Phase 0 verification candidates**. The confirmed Developers Playground simulates Package, Vehicle, and Motion events — test what actually arrives on the wire before committing either way, rather than assuming.
- **Vision (Bedrock on snapshots):** promoted from "rushed stretch" to **properly-built feature**, including TAKE/E2EE graceful degradation. Still logically separate from the two deterministic paths, but no longer scoped as an afterthought. If media is unavailable due to encryption, the verdict still fires from metadata + history alone, with the omission stated plainly.
- **Dashboard specifics:** one-page, shows Routine / Check Needed / Escalation with reasons. Design during build.

### General scope principle

Anything that was scoped down purely to protect a tight timeline (DynamoDB entity granularity, vision as stretch, package/vehicle conservatism) can now be built to its fuller, better-designed version. Anything that was a safety or correctness principle — the deterministic escalation gate, the agent never touching the absence-detection path, verify-before-build discipline — does not change regardless of available time, and more time should go toward doing those more thoroughly, not loosening them.

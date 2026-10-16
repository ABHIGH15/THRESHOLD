# Threshold — Hackathon Demo Video Script & Presentation Storyboard

> **Project:** Threshold — Pattern-Absence Detection for Elderly Care  
> **Tagline:** *"Ring tells you when something happens. Threshold tells you when something doesn't."*  
> **Video Target Length:** Under 2:00 hackathon ceiling  
> **Format:** 1080p 60fps (16:9), crisp voiceover, two-segment structure: Segment A (REAL) and Segment B (SIMULATED)  

---

## 1. Segment Overview: REAL vs SIMULATED

The demonstration video consists of two clearly demarcated segments:

- **Segment A — LIVE Real Ring Sandbox (Labelled REAL):**
  - Live execution against the official Ring Partner API sandbox (Developer Playground).
  - Diagnostic output of `python3 -m src.threshold ring-check` verifying live token, device discovery, health polling, and event history.
  - Live camera snapshot retrieved directly from the Ring sandbox camera (parking lot camera feed with `X-Media-Timestamp`).
  - No synthetic data; authentic API responses only.

- **Segment B — Simulated Household Demo (Labelled SIMULATED):**
  - Zero-secret, zero-network end-to-end demonstration (`python3 -m src.threshold demo --no-color`).
  - Simulated household in Dallas, TX (America/Chicago) with 3 fictional camera routines.
  - All routines use camera motion detection (no Contact Sensor, Motion Sensor, or Doorbell hardware claims).
  - Real core pipelines: signature verification, idempotency, ingest, status derivation, fire-once escalation, notifier formatting, agent tools, evidence derivation, deterministic gate, and read-only dashboard.
  - Agent reasoning is in replay mode (`mode: replay (scripted)` using committed traces, not a live LLM). The system never claims to know what an image visually depicts.

---

## 2. Shot-by-Shot Storyboard & Script

### Segment A: LIVE Real Ring Partner Sandbox (REAL) (0:00 – 0:35)

| Timestamp | Visual Cue | On-Screen Action | Audio Narration (Verbatim) |
| :--- | :--- | :--- | :--- |
| **0:00 – 0:10** | Title card fades in from black. | Title: **THRESHOLD**<br>*"Pattern-Absence Detection for Elderly Care"*<br>**Segment A: Live Ring Sandbox (REAL)** | *"Fifty-three million Americans care for an aging family member. Ring tells you when something happens. Threshold listens for when something doesn't."* |
| **0:10 – 0:22** | Terminal view running `python3 -m src.threshold ring-check`. | Diagnostic check completes against Ring Partner API: token redacted, devices listed, device health confirmed, recent history queried. | *"Here is Threshold running live against the real Ring Partner API sandbox. It polls authenticated device health, monitors motion event streams, and checks connectivity without third-party cloud intermediaries."* |
| **0:22 – 0:35** | Real snapshot retrieved from Ring sandbox camera. | Image displayed with honest caption: `Most recent camera image, taken [timestamp] ([N] min ago). Not live.` | *"When an alert triggers, Threshold requests the latest camera snapshot directly from Ring. The image and capture timestamp are delivered to the caregiver for human review."* |

---

### Segment B: Simulated Household Demo (SIMULATED) (0:35 – 1:55)

| Timestamp | Visual Cue | On-Screen Action | Audio Narration (Verbatim) |
| :--- | :--- | :--- | :--- |
| **0:35 – 0:50** | Terminal runs `python3 -m src.threshold demo`. Banner displayed: `SIMULATED HOUSEHOLD (fictional)`. | **Scene 1: Calm Morning.** Routine 'Morning movement' (07:00–09:00 CDT, +30m grace) on living room camera. Signed motion at 07:42 $\rightarrow$ Status **🟢 OBSERVED**. Tick at 09:31 $\rightarrow$ 0 escalations. | *"In our simulated household, Scene 1 shows a calm morning. Movement in the living room at 7:42 is signed and accepted. The routine derives as Observed, and the 9:31 deadline passes with zero false alerts."* |
| **0:50 – 1:02** | Terminal shows Scene 2: Trust Boundary. | **Scene 2: Trust Boundary.** Bad HMAC signature $\rightarrow$ HTTP 401 Unauthorized, 0 rows stored. Duplicate request $\rightarrow$ HTTP 200 idempotent, event count unchanged. | *"Scene 2 demonstrates the trust boundary: unauthorized webhooks are rejected with HTTP 401 before parsing, and duplicate deliveries are safely ignored by database idempotency."* |
| **0:52 – 1:18** | Terminal shows Scene 3: Missed Routine & Fire-Once Escalation. | **Scene 3: Missed Routine.** 'Evening check-in' (18:00–20:00 CDT, +30m delay) on front door camera. Clock reaches 20:31 CDT. Status transitions to **🔴 MISSED**. Exactly 1 escalation dispatched. Subsequent tick at 20:46 fires 0 alerts. | *"In Scene 3, the evening routine window and 30-minute grace period pass in complete silence. Threshold's deterministic scheduler transitions status to Missed, fires exactly one escalation alert, and enforces fire-once deduplication on subsequent ticks."* |
| **1:18 – 1:30** | Terminal shows Scene 4: UNKNOWN Offline Device Silenced. | **Scene 4: UNKNOWN Not Alarm.** Hallway camera poll is > 120m old. Status derives as **🟡 UNKNOWN**. Display: 'Unknown — device offline, not escalated'. Tick past deadline $\rightarrow$ 0 escalations. | *"In Scene 4, a device offline for more than two hours transitions to Unknown, not Missed. Threshold silences the absence alert, ensuring offline hardware never triggers a false emergency alarm."* |
| **1:30 – 1:44** | Terminal shows Scene 5: Check Needed & Gate Evaluation. | **Scene 5: Check Needed.** Event 5a: unexpected quiet-hours motion at 02:10 CDT $\rightarrow$ agent tick in replay mode $\rightarrow$ gate cleared $\rightarrow$ CHECK NEEDED notification dispatched with ✓/✗ evidence lines. Event 5b: same device < 30m deduped. Event 5c: 1 tool $\rightarrow$ gate downgrades to check_needed (insufficient_evidence) $\rightarrow$ 0 messages. | *"Scene 5 exercises bounded investigation: unexpected 2:10 AM motion triggers scripted investigation. When two distinct tools succeed and the gate clears, a Check Needed message dispatches with evidence lines. When evidence is insufficient, the gate strictly downgrades without alerting."* |
| **1:44 – 1:55** | Terminal shows Scene 6: Household Memory & Closing Comparison Table. | **Scene 6: Household Memory.** Caregiver taps 'False alarm' via real callback pathway. Later 03:40 quiet-hours motion queries real household memory $\rightarrow$ 3 false alarms in last 30 days $\rightarrow$ alert safely suppressed. Closing table: REAL vs SIMULATED breakdown. | *"Finally, Scene 6 executes the real callback resolution path. When quiet-hours motion recurs, household memory reveals three recorded false alarms, safely suppressing duplicate panic. Real core logic, transparently demonstrated."* |

---

## 3. Judge Presentation Talking Points & 45-Second Pitch

> *"Ring tells you when something happens — motion detected. But nobody tells you when something **didn't** happen. If an elderly parent usually moves around the house between 7:00 and 9:00 AM, and one morning there is only silence — that absence is the most critical signal of the day.
> 
> *Threshold monitors daily routines using existing Ring cameras. When expected activity is missing, it edge-triggers a deterministic escalation with fire-once safety, device health verification, and caregiver callback resolution.
> 
> *It runs entirely offline with zero AWS, zero external cloud dependencies, and zero pip packages — pure Python standard library and SQLite. We've verified it live against the real Ring Partner API sandbox and demonstrated it across a fully simulated household."*

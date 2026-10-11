# Threshold — Execution Plan & Agent Rule Book

This document governs how implementation happens, not what gets built — `threshold_spec.md` is the source of truth for the product. This file exists because the coding agent doing the work tends to report tasks complete before they're actually verified. Every rule here is aimed at that specific failure mode.

**Load this file into every agent session before work begins. Re-read Section 1 at the start of every session, not just the first one.**

---

## 1. Non-negotiable rules

These apply to every task, in every phase, with no exceptions the agent grants itself.

1. **"Done" requires evidence, not description.** "This implements X" is not a completion report. "I ran `<exact command>`, got `<exact output>`, and it matches `<expected result>`" is a completion report. If a task can't produce that sentence, it isn't done.
2. **Every claim of working software must show the actual output.** Terminal output, HTTP response body and status code, DynamoDB item read back via `get-item`, test runner output — pasted verbatim, not paraphrased, not summarized as "it worked."
3. **Nothing gets invented.** If a field, value, endpoint, or behavior isn't in `threshold_spec.md`'s confirmed rows or a real portal/API response you've personally seen, it does not exist yet. Mark it `TODO-VERIFY` and stop — do not guess a plausible value and continue.
4. **One unit of work at a time.** A "unit" is one function, one handler, one DynamoDB access pattern, one tool definition — never "build the webhook handler" as a single unclosed task. Smaller units make skipped verification visible.
5. **No task is marked complete without its Definition of Done (Section 2) satisfied in full.** Partial completion is reported as partial, explicitly, with the remaining item named.
6. **The build log (Section 4) is updated after every unit, not at the end of a session.** If the log wasn't updated, the work isn't done, regardless of what the code looks like.
7. **The deterministic escalation gate is never weakened, bypassed, or "simplified" without explicit human sign-off.** This is the one piece of the architecture that exists specifically so a model is never the sole trigger on calling someone about their parent. An agent optimizing for "cleaner code" is not authorized to touch this logic unsupervised.

---

## 2. Definition of Done — universal checklist

Copy this checklist into every task. Every box must be true, with evidence, before the task is reported complete.

```
[ ] Code written
[ ] Code actually executed — exact command shown
[ ] Actual output captured verbatim (not described)
[ ] Output explicitly compared against the task's stated expected result — match or mismatch stated plainly
[ ] At least one failure/edge case tried on purpose (bad input, missing field, expired window, wrong signature)
    and confirmed to fail the *correct* way, not just "fail"
[ ] Relevant section of threshold_spec.md re-read and cross-checked immediately before marking done
[ ] Build log updated with timestamp, what was attempted, evidence, pass/fail
```

A task with an unchecked box is not done. State which box is unchecked and why, rather than rounding up.

---

## 3. Stop-and-ask triggers

The agent halts and asks the human instead of proceeding on assumption when:

- A real API response doesn't match what `threshold_spec.md` says is confirmed.
- A required credential, token, or config value is missing or expired.
- The same test has failed twice in a row after fix attempts.
- Any change would touch the deterministic escalation gate logic (Rule 7 above).
- A task can't be completed without inventing a field or behavior not in the spec.
- More than one plausible interpretation of a spec item exists and the choice affects downstream work.

Guessing past any of these and reporting success anyway is the exact failure mode this document exists to prevent.

---

## 4. Build log

Maintain `BUILD_LOG.md` at the repo root. Append, never rewrite history. One entry per unit of work:

```
### [timestamp] <unit of work>
Attempted: <what was built/changed>
Command run: <exact command>
Output: <verbatim, or path to a saved log file if long>
Expected: <what the task said should happen>
Result: PASS | FAIL | PARTIAL — <one line on which>
Spec cross-check: <section of threshold_spec.md re-checked>
```

This is the artifact a human reviews to spot-check progress without re-deriving it. If the log and the code disagree, the log is what gets questioned first.

---

## 5. Phase 0 — Environment & Access Bootstrap

**Goal:** Every credential, tool, and account decision closed before any product logic is written. Nothing in Phase 1+ should require a new account signup or an undecided tool choice mid-task.

Tasks:
1. Create Ring developer account, register the Partner App, obtain OAuth credentials. Request the $150 AWS credit.
2. Get into the Ring sandbox/simulator. Confirm, directly from the portal — not from search — the three deferred items: full `subType` enum, whether Video Descriptions appear in the partner payload, full webhook payload field list. Update `threshold_spec.md`'s ground-truth table with real citations (portal screenshots or saved response bodies), replacing the "deferred" rows.
3. Decide and record the IaC tool. Recommendation: **AWS SAM** — least boilerplate for this scale, `sam local invoke` gives a fast offline test loop, native support for the exact stack (Lambda, DynamoDB, EventBridge Scheduler). Confirm or override, then record the decision in this file.
4. Pin the agent framework integration shape. Recommendation: **Strands SDK running inside the existing webhook/scheduler Lambdas**, not a separate AgentCore Runtime deployment — fewer new moving parts for a ~4-week build. AgentCore stays a stretch upgrade if time allows. Confirm or override, then record.
5. Enable Bedrock model access for the target model(s) in your AWS region via the console, before any code calls it. Confirm access is active (a trivial `invoke-model` smoke test), not just requested.
6. **Confirm the exact Bedrock model ID live — do not hardcode from memory, docs, or search results.** Run `aws bedrock list-foundation-models --by-provider anthropic --region <your-region>` (or check the Bedrock console's Model Catalog) and use the exact returned model ID. Note whether it requires a cross-region inference profile prefix (`us.` / `global.`), since that changes the literal string your code calls. Record the confirmed ID in Section 12.
7. Decide secrets handling: **AWS Secrets Manager or SSM Parameter Store for all tokens (Ring OAuth, SNS/Connect access) — never in code, never in a committed `.env` file.** Add a `.gitignore` entry before the first credential is ever written to disk locally.
8. Build the local synthetic-data harness: a fixture file of ~30 days of synthetic events (normal day, missed pickup, 3am door-open, silent morning) in the exact confirmed payload shape from step 2 — not the earlier assumed shape. This is what Phase 1 tests against, and what every later phase falls back to when live services are unavailable.
9. **Get a Developers Playground token** (one-click, 30-minute OAuth) as the fast first checkpoint, before full production app registration — confirmed real, lowers Phase 0 friction significantly.
10. **Re-test Package and Vehicle simulation in the Playground specifically.** Record exact payload shape received. This decides whether package/vehicle detection is in scope, not assumption.
11. **Verify TAKE's effect on Partner API media endpoints, specifically and early — not as a stretch-feature detail.** Given TAKE is becoming the default for the entire Ring install base starting September 2026, this affects the baseline "attach a snapshot" feature, not just the vision stretch. Test snapshot/live-video/clip retrieval against a Playground or real test account and record actual behavior.
12. **Confirm the Event History API's actual response shape** (pagination, fields, rate limits) — its existence is confirmed, its behavior isn't.

**Definition of Done for Phase 0:**
- Ring OAuth credentials obtained and stored per step 7, not hardcoded anywhere — grep the repo for the literal token value to confirm.
- `threshold_spec.md`'s deferred rows are replaced with portal-sourced confirmations, each citing where it was checked.
- IaC tool and agent framework decisions are written down in this file (Section 12), not left implicit.
- A Bedrock `invoke-model` call succeeds and its raw response is pasted into the build log.
- Synthetic fixture file exists, matches the confirmed schema, and covers all four scenarios named above.
- TAKE/E2EE behavior against at least one test device is observed and logged with actual response evidence — not assumed from documentation, since no documentation confirms it either way.

> [!IMPORTANT]
> **General note on scope, all phases:** Anything that was scoped down purely to protect a tight timeline (DynamoDB entity granularity, vision as stretch, package/vehicle conservatism) can now be built to its fuller, better-designed version. Anything that was a safety or correctness principle — the deterministic escalation gate, the agent never touching the absence-detection path, verify-before-build discipline — does not change regardless of available time, and more time should go toward doing those more thoroughly, not loosening them.

---

## 6. Phase 1 — Rules-only triage engine (no AWS)

**Goal:** The deterministic paths — Routine and Urgent-via-absence (three-state model: watching/observed/unknown/missed) — working correctly against the synthetic fixtures, entirely locally, before any cloud deployment.

Tasks: implement routine matching, derived-status computation with device-health check (Section on locked schema in `threshold_spec.md`), edge-detected `watching → missed` firing.

**Definition of Done:**
- Every synthetic fixture scenario produces the tier the scenario was designed to produce — shown as a pass/fail table, not a verbal claim.
- The "fire once, not on every tick" edge-detection is tested explicitly: run the derived-status function across multiple consecutive ticks past a miss and confirm only one Urgent fires.
- The `unknown` state is tested explicitly: a routine past its window with a stale device health timestamp must NOT escalate — it must return `unknown` instead of `missed`.
- No AWS resource has been touched yet.

---

## 7. Phase 2 — AWS skeleton deploy

**Goal:** Prove the deploy pipeline end-to-end with dummy logic before adding real behavior — isolates infrastructure problems from logic problems.

Tasks: deploy both Lambdas (stub handlers), the DynamoDB table, the EventBridge schedule, wired together, via the IaC tool from Phase 0.

**Definition of Done:**
- A deploy command's real output (resource ARNs) is pasted into the build log.
- A manually-sent test event reaches the webhook Lambda and a corresponding item appears in DynamoDB, confirmed via `aws dynamodb get-item`, output pasted.
- The scheduler Lambda fires on schedule at least once, confirmed via CloudWatch Logs, output pasted.

---

## 8. Phase 3 — Real Ring webhook wiring

**Goal:** Replace the stub with real HMAC verification and real payload parsing, against the sandbox/simulator first.

**Definition of Done:**
- HMAC-SHA256 verification tested against both a valid and a deliberately corrupted signature — the corrupted one must be rejected, shown in the log.
- A real sandbox/simulator event produces a correct DynamoDB state update, with the raw received payload pasted (this is also your evidence the confirmed schema from Phase 0 was right).
- Response time confirmed under the 5-second requirement.

---

## 9. Phase 4 — Scheduler / absence detection against real state

**Goal:** Phase 1's logic, now reading real DynamoDB state instead of fixtures.

**Definition of Done:** A real "missed" transition, triggered by genuinely not sending a satisfying event within a test window, produces one Urgent verdict — observed via logs, not assumed from the code.

---

## 10. Phase 5 — Agent layer (bounded authority)

**Goal:** The five tools (`get_recent_ring_events`, `pull_snapshot`, `check_routine_status`, `get_threshold_history`, `check_household_memory`), the investigation trace with evidence-based output, and the deterministic gate — exactly as locked in the spec, not a simplified version.

**Definition of Done:**
- Each tool individually tested with a real call and real return value pasted, before wiring into the agent loop.
- `pull_snapshot` specifically tested against both an available and a TAKE/E2EE-restricted device — the restricted case must produce a graceful "media unavailable" response, not crash.
- A full ambiguous-event trace, run for real, shows the agent using at least one tool and producing tier + reasoning + evidence list.
- The gate is tested on both sides deliberately: one case engineered to clear it (confirm it escalates) and one engineered to just miss it (confirm it lands as Needs a look, not silently dropped or auto-escalated).
- Timeout fallback tested by forcing a timeout and confirming the conservative "could not determine" path fires.

---

## 11. Phase 6 — Escalation + correction loop

**Goal:** SNS + Connect wired for real, false-alarm reply writing a real correction record.

**Definition of Done:**
- A real SMS received on a real phone, screenshot or delivery-status log pasted.
- A real voice call placed after a simulated non-response window.
- A real `FALSE ALARM` reply produces a DynamoDB correction record, confirmed via read-back, and `check_household_memory` returns it on a matching follow-up query.

---

## 12. Decisions log (fill in during Phase 0, don't leave implicit)

- IaC tool: **AWS SAM**
- Agent framework integration shape: **Strands SDK inside Lambdas** (AgentCore as stretch upgrade if time allows)
- Bedrock model(s): **Claude, single model for narration + tool-calling + vision (stretch).** Exact model ID confirmed live via CLI in Phase 0 — not hardcoded from docs or search results.
- Secrets storage: **AWS Secrets Manager or SSM Parameter Store** — never in code, never in a committed `.env` file
- Device health staleness threshold: **2 hours (120 minutes).** A device with no events in >2h is considered stale, which means the absence model returns `unknown` instead of `missed` for routines on that device. This directly trades off false `unknown`s (threshold too short → healthy devices look stale) against missed escalations going undetected (threshold too long → genuinely offline devices look healthy). 2h is a reasonable starting default for a home monitoring scenario — configurable per-device via `staleness_threshold_minutes`.
- `unknown` status → verdict tier: **Needs a look.** Don't escalate (data isn't trustworthy), don't silently swallow (caretaker should know device may be offline). Deterministic rules path, not an agent decision. Pinned in `threshold_spec.md`.
- Pre-agent ambiguous event default: **Needs a look** until Phase 5 adds the bounded-authority agent. Conservative default: no event silently dropped or incorrectly escalated. Pinned in `threshold_spec.md`.
- Test toolchain: **`unittest` (stdlib).** `pytest` is not installed; `unittest` runs without dependencies. Sufficient for Phase 1; revisit if test complexity warrants `pytest` later.

---

## 13. Phase 7 — Dashboard, Phase 8 — Demo rehearsal, Phase 9 — Submission packaging

Lower-risk, less novel phases — standard Definition of Done (Section 2) applies, no special rules beyond it. Phase 8 specifically: pre-run and save the agent investigation trace used in the demo; do not rely on a live model call during the actual pitch.

---

## Appendix — the one rule that overrides convenience

If at any point completing a task the "easy way" would mean the deterministic gate stops being deterministic, or a verdict gets reported without the evidence this document requires, stop and say so. A visibly incomplete build with an honest build log is a better position, one week out, than a build that looks finished and isn't.

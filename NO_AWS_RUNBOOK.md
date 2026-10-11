# Threshold — No-AWS Runbook (Oct 11 → Oct 23, 2026)

**What this file is:** the single, ordered answer to "what do I do by hand, what do I tell the agent, what can go wrong."
**Put it in the repo root next to `threshold_spec.md` and `EXECUTION_PLAN.md`.** Where this file conflicts with those two, **this file wins** (see Part F).

**Deadline:** Fri Oct 23, 2026, 12:00pm PDT = 19:00 UTC (in India that is 12:30 AM Sat Oct 24 — convert it for your own timezone now and write it down).
**Your personal deadline:** submit by **Thu Oct 22 evening**. Oct 23 is for checking the submission page only. Do not touch code on Oct 23.

**Spend rule:** nothing in this plan costs money. If any step asks for a card, a paid plan, or a "confirm" on billing → stop, don't proceed, ask me.

### Legend (how sure am I about each claim)
- ✅ **Verified** from official Ring docs / starter repo during this project.
- ⚠️ **Unverified** — I could not confirm it from a primary source. Treat as a hypothesis; the plan has a fallback.
- 🔒 **Safety rule** — never relax, regardless of time.

---

## 0. The plan in 7 lines

1. Build everything **locally**: Python + SQLite + Telegram. **No AWS account needed.** (Ring track allows it; AWS Builder mini-prize is forfeited — see Part F.)
2. The first thing we prove is that **our own code makes a real authenticated call to Ring** (Stage One pass/fail depends on it). Everything else is built after that gate.
3. The app runs in two modes: **Live** (real Ring token, real Telegram) and **Demo** (zero secrets, signed event simulator, accelerated clock). Judges can run Demo in one command.
4. **You** do accounts, ID verification, phone tests, video, and submission. **The agent** writes code. **You verify** with commands (listed per task) — you never accept "done" without output.
5. Feature freeze **Mon Oct 19**. Video **Oct 21**. Submit **Oct 22**.
6. Anything not proven (e.g. webhooks from the Playground) is **stated honestly** in the README and video. Honest + working beats impressive + fake.
7. 🔒 The deterministic escalation gate and "agent never decides absence" never change.

---

# PART A — MANUAL STEPS (only you can do these)

Do them in this order. Time estimates are rough.

| # | What you do | Where | Time | Save / done-when |
|---|---|---|---|---|
| **M0** | **Close the AWS signup tab. Do NOT click Confirm / pick a paid plan / retry with another email or card.** | browser | 1 min | No AWS account in progress. We don't need one. |
| **M1** | **Confirm you're registered** for the hackathon on Devpost and the **Ring track** is the one you're entering. Read the Rules page once more for: required submission fields, and anything about AWS being required for the Ring track (my earlier read: not required — if the page says otherwise, tell me). | Devpost | 15 min | Screenshot of your registration. |
| **M2** | **Create the GitHub repo** (`threshold`), set **Public**, add **MIT License** at creation. (Public + OSS license satisfies the repo rule; private-shared-with-reviewers is the alternative.) Don't push secrets — `.env` is git-ignored (A0 verifies). | github.com | 5 min | Repo URL. Tell the agent the URL in A0. |
| **M3** | **Ring Developer Console:** sign in at `developer.amazon.com/ring/console` ✅. Complete **government photo-ID verification** ✅ (required before you can submit apps). Register a Partner App; **Client ID, Client Secret, and the webhook HMAC key are shown only once ✅ — copy them straight into your local `.env`**, never into chat, never into git. In Configure, there are separate **Staging/Production** tabs for Account Link / Redirect / Token Exchange / **Webhook URL** ✅ (HTTPS required ✅). | developer.amazon.com | 20–60 min + unknown ID-verification wait ⚠️ | `.env` has `RING_CLIENT_ID`, `RING_CLIENT_SECRET`, `RING_WEBHOOK_SECRET`. **Start this TODAY** — the ID-verification wait is the one thing outside your control. |
| **M4** | **Developers Playground session** ✅ (released May 28, 2026): one-click OAuth token valid **30 minutes** ✅; API explorer; simulates **Package, Vehicle, Motion** ✅. **Do this only when A1's `probe_ring.py` is already written and waiting** (the token expires in 30 min). Paste the token into `.env` as `RING_ACCESS_TOKEN`, run the probe, then in the Playground UI trigger each simulated event (Motion, Package, Vehicle) and **save the raw JSON / screenshot** of what you see. Note: *where* the simulated event shows up (only in the UI? delivered to a URL?) ⚠️ — that's the single most important thing to learn. | Playground + terminal | 45 min | `evidence/ring/*.json` + screenshots in `evidence/ring/playground/`. **Send me the redacted files** — I'll tell you whether the plan holds. |
| **M5** | **Telegram bot:** open Telegram → talk to **@BotFather** → `/newbot` → copy the bot token into `.env` as `TELEGRAM_BOT_TOKEN`. Send any message to your new bot. (Your chat id is fetched later in A5 by `scripts/telegram_whoami.py` → `TELEGRAM_CHAT_ID`.) Free, no card. ⚠️ Check Telegram works on your network/country; if it doesn't, tell me — fallback is ntfy.sh or Gmail SMTP (Part C). | Telegram | 10 min | Bot token saved locally. The real "message arrives on my phone" proof happens in A5. |
| **M6** | **Tunnel — ONLY if M4 shows Ring can push webhooks to your own URL** ⚠️. Ring needs a public **HTTPS** webhook URL ✅; a free tunnel (e.g. Cloudflare quick tunnel, ngrok free, localtunnel) gives you one. ⚠️ I have **not** verified current free-tier terms or stability of any of them — check the tool's own site before relying on it, and don't enter a card. The URL changes each run → re-paste it in the Console's Staging webhook URL each time. | terminal | 20 min | A public HTTPS URL that reaches your local `/healthz`. If M4 shows no push → **skip M6 entirely**. |
| **M7** | **Free LLM for the live agent (optional, do after A7a):** options ⚠️ (limits/availability unverified — check each site yourself): a free-tier API key (e.g. Google AI Studio / Gemini), or a **local model via Ollama** (free, uses your laptop). If none works → **Mock/replay mode is the shipped default** and the video says so. | browser/terminal | 20 min | `LLM_API_KEY` or local model name in `.env`. **Never** attach a billing account to get a key. |
| **M8** | **Record the video** (<3:00, aim 2:40) → upload **public** to YouTube or Vimeo. Shot list in Part E. Record **Live-mode Ring calls during a fresh 30-min Playground token window** (so the real Ring call is on camera). | screen recorder | 3–4 h incl. retakes | Public URL; open it in an incognito window to confirm. |
| **M9** | **Submit on Devpost** (checklist in Part E). Submit **Oct 22**. Re-open the submission page on Oct 23 to confirm status. | Devpost | 1–2 h | Screenshot of the submitted page. |
| **M10** | **Questions to organizers** (email / Devpost discussion / Office Hours — I noted an Oct 19 Office Hours earlier; **re-check the date on the Devpost page**). Ask: **(a)** Can the Playground deliver events to my own webhook URL, or is it UI-only? **(b)** Is a physical Ring device required for the Ring track given the Playground? (the get-started page lists "at least one Ring device for testing"; the FAQ says a device is not required — they conflict). **(c)** For the Open Source mini-challenge: does a new public repo with an OSS license count? **(d)** Does a project without any AWS usage remain fully eligible for the Ring track prizes? | email/Devpost | 15 min | Written answers. Save them. |

### Where secrets live
Only in a local `.env` (git-ignored). Variable names:
`RING_ACCESS_TOKEN`, `RING_CLIENT_ID`, `RING_CLIENT_SECRET`, `RING_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `LLM_API_KEY`.
**Never paste any of these into chat with me or the agent.** If you leak one: regenerate it immediately.

### What you send me (and when)
- After **A1/M4**: the redacted `evidence/ring/*.json` + screenshots → I tell you if the plan holds.
- After **A7a**: one agent trace JSON → I check it reads well.
- After **A10**: README + Devpost draft → I review before you submit.

---

# PART B — WHAT TO TELL THE AGENT (ordered tasks)

## How to use this part
1. **Start every new agent session** by pasting the **Session Preamble** below. Every time. (This is what stops "done!" with no proof.)
2. Then paste **one** task prompt. Never two.
3. The agent must reply with: the exact commands run, verbatim output, expected-vs-actual, and a BUILD_LOG entry. If it doesn't, reply: *"Not done. Show the exact command and verbatim output."*
4. **Run the "You verify" commands yourself** (takes ~2–5 min each). If your output differs from what the agent reported, the agent was wrong — paste your output back to it.
5. Only then move to the next task.

### 🚩 Red flags that the agent is bluffing
"should work", "verified by inspection", "looks correct", test counts without the output, the same "ready for handoff" message repeated, files "created" that `ls` doesn't show, JS/HTML "verified" without a browser having opened it. Response: demand the command + output.

## SESSION PREAMBLE (paste at the start of EVERY session)

```
Read these fully before doing anything: EXECUTION_PLAN.md (Section 1 rules), threshold_spec.md,
NO_AWS_RUNBOOK.md (Part F overrides any conflicting line in the older docs), and the last 3 entries of BUILD_LOG.md.

Session rules:
1. Work on ONE task only — the one I paste below. Do not start the next one.
2. "Done" = the exact command you ran + its verbatim output + a statement of expected vs actual.
   No evidence = not done. Say "PARTIAL" and name what is missing rather than rounding up.
3. Never invent Ring fields, endpoints, or enum values. If it is not in the official docs, the
   ring-api-helloworld starter repo, or a real response saved under evidence/, write TODO-VERIFY and stop.
4. Never modify src/threshold/gate.py or any escalation-gate logic without my explicit written sign-off.
5. Never print, log, or commit secrets. .env is git-ignored. Redact tokens in every saved file.
6. Include at least one deliberate failure case per unit and show it fails the CORRECT way.
7. If the same test fails twice, STOP and report the failure verbatim. Do not keep patching.
8. If you are about to hit a quota/limit, first write your current state to BUILD_LOG.md.
9. When finished: append a BUILD_LOG.md entry, give ONE report, then STOP and wait for me.
   Do not repeat "ready for next task".
```

---

## A0 — Pivot cleanup & repo baseline
## A1 — Ring probe (🚪 GATE 1)
## A2 — SQLite store
## A3 — Webhook server
## A4 — Scheduler + absence engine (🔒)
## A5 — Escalation (Telegram) + corrections
## A6 — Real `ring_client.py`
## A7a — Agent layer: tools, mock provider, gate (🔒)
## A7b — Live LLM provider
## A8 — Dashboard wired to real state
## A9 — Demo mode + signed simulator
## A10 — Packaging, docs, honesty pass
## A11 — Submission drafts

(Full task prompts in the original runbook document.)

---

# PART C — HURDLES, HOW YOU'LL NOTICE, WHAT WE DO

| # | Hurdle | How you'll notice | What we do |
|---|---|---|---|
| 1 | **Ring ID verification is slow/rejected** ⚠️ | Console won't let you submit/register | Start M3 today. Meanwhile use the Playground (it works without an app ✅). Ask organizers (M10). Log it in the friction log. |
| 2 | **Playground token lasts 30 min** ✅ | 401 from probe | Write the script first, then get the token. Re-generate as needed. |
| 3 | **Playground can't push webhooks to your URL** ⚠️ | M4 shows events only inside the UI | **Poll mode**: Event History via the same ingest path + signed local simulator. Say so in the video. |
| 4 | **"Needs a physical Ring device"** ⚠️ | Console/Playground refuses without a device | Ask organizers (M10b). Playground ✅ doesn't require app/linking/subscription. |
| 5 | **TAKE encryption → snapshots unreadable** ✅ | Snapshot returns encrypted/empty bytes | Design already treats "event ≠ media available" as normal; verdict continues without image; UI says so. |
| 6 | **Watermark on all media** ✅ | Logo/text on images | Cosmetic for us; mention in README. |
| 7 | **Webhook payload differs from the spec** | A1/A3 evidence mismatch | Agent stops (rule 3), we update the spec from real payloads. |
| 8 | **Free tunnel flaky / URL changes** ⚠️ | Ring says webhook unreachable | Only matters if Ring can push. Demo mode doesn't use it. |
| 9 | **Free LLM unavailable/limited** ⚠️ | 429 or no key | Mock/replay is the default; one real recorded live trace is enough. |
| 10 | **Telegram blocked** ⚠️ | M5 hello message never arrives | Swap for NtfyNotifier or Gmail SMTP. Same Notifier interface. |
| 11 | **Agent claims "done" without proof** | Red flags list in Part B | Demand command + output; run "You verify" yourself. |
| 12 | **Local agent hits its quota** | Agent stops mid-task | Preamble rule 8 + BUILD_LOG keeps state. |
| 13 | **Python 3.14 + third-party libs** ⚠️ | `pip install` fails | Stdlib-only by design. |
| 14 | **Timezone/DST bugs in routine windows** | Escalation fires an hour early/late | A4 tests a DST boundary and a midnight-crossing window explicitly. |
| 15 | **Secret leaks into git** | Secret-grep finds something | Rotate immediately; rewrite history before repo is public. |
| 16 | **Old handlers swallowed DB errors and returned 200** | (design bug) | A3 forbids it: storage failure → 5xx + log. |
| 17 | **Dashboard JS never really ran** | Console errors / blank page | A8 requires a real browser run; you check DevTools yourself. |
| 18 | **Demo fails live during recording** | — | Demo mode is deterministic and offline; record in takes. |
| 19 | **Scope creep** | New ideas after Oct 19 | Feature freeze Oct 19. After that: bug fixes, README, video only. |
| 20 | **Fighting the clock at the end** | — | Personal deadline Oct 22. Oct 23: look, don't touch. |
| 21 | **Judges only read the description + video** | — | Description and video must stand alone; README must state REAL vs SIMULATED honestly. |
| 22 | **AWS Builder mini-prize** | — | Forfeited under this plan. Don't chase it. |

---

# PART D — DAY-BY-DAY SCHEDULE

| Date | You (manual) | Agent task | Checkpoint |
|---|---|---|---|
| **Sun Oct 11** (today) | M0, M1, M2, **start M3**, M5 | **A0** | Repo clean, 30 tests green, secret-grep clean. |
| **Mon Oct 12** | **M4** (A1 script ready first), M10 emails | **A1** | 🚪 **Gate 1**: send me redacted evidence. |
| **Tue Oct 13** | verify A2/A3 | **A2 → A3** | Signed event → 200 → row; bad signature → 401. |
| **Wed Oct 14** | re-check M3 status; M10 follow-up if blocked | **A4** | Fire-once + restart-safe proven. |
| **Thu Oct 15** | **M5 live phone test** | **A5 → A6 (start)** | Telegram message + correction row. |
| **Fri Oct 16** | M6 only if Gate 1 said push works | **A6 (finish)** | 🚪 **Hard decision** on the Ring path. Live `ring-check` works. |
| **Sat Oct 17** | read `gate.py` | **A7a** | Gate tested both sides; one trace to me. |
| **Sun Oct 18** | **M7** | **A7b → A8** | Dashboard shows 0 console errors. |
| **Mon Oct 19** | Office Hours if scheduled | **A9** | **FEATURE FREEZE end of day.** Fresh-clone demo works with no secrets. |
| **Tue Oct 20** | rehearse the demo twice | **A10** | README honest; secret-scan clean; fresh-clone passes. |
| **Wed Oct 21** | **M8 record + upload video** | **A11** | Public video link opens in incognito. |
| **Thu Oct 22** | **M9 submit** | bug fixes only | Submission page shows "Submitted". |
| **Fri Oct 23** | Check the page. **Do not change code.** | — | Deadline 12:00 PDT. |

---

# PART E — SUBMISSION CHECKLIST & VIDEO SHOT LIST

### Video shot list (target 2:40, hard cap 2:55)
| Time | Beat |
|---|---|
| 0:00–0:15 | Hook: "Ring tells you what happened. Threshold tells you when something you expected didn't." |
| 0:15–0:35 | Problem: a remote caretaker; a stream of events is the wrong interface; the silent state is the one that matters. |
| 0:35–1:15 | **Hero:** routine window passes, no event, device healthy → *missed* → **Escalation** on a **real phone** (Telegram) → tap **False alarm** → correction recorded. |
| 1:15–1:40 | **Honesty beat:** device offline → **Unknown, not escalated** ("no event ≠ no data"). |
| 1:40–2:10 | Ambiguous event → agent investigates (trace) → **evidence list** → **Check Needed**; the deterministic gate decides escalation, not the model. |
| 2:10–2:30 | **Real Ring:** our code calling the Ring API (devices / event history / snapshot) — on screen — plus one line on TAKE/watermark handling. |
| 2:30–2:50 | Scope fences (not a security system, not a fall detector) → impact → what's next. |

---

# PART F — OVERRIDES (this file wins over `threshold_spec.md` / `EXECUTION_PLAN.md`)

| Old document says | Now |
|---|---|
| Lambda ×2 | One Python process: HTTP server (webhook path) + scheduler thread (scheduler path). |
| DynamoDB single-table | SQLite file (A2 writes the mapping into BUILD_LOG). |
| EventBridge `rate(1 minute)` | In-process tick with injectable clock; fire-once persisted via UNIQUE constraint. |
| SNS + Amazon Connect, "SMS then voice call" | Telegram message + reminder ladder. **No phone call** — say so. |
| Bedrock + Strands | Provider-agnostic agent loop; MockProvider replay default; optional free live provider. |
| Secrets Manager / SSM | Local `.env`, git-ignored. |
| SAM / CloudFormation / `samconfig.toml` | Removed (A0). |
| $150 AWS credit, Bedrock smoke test (Phase 0 tasks 1, 5) | Removed. |
| Phase 2 "AWS skeleton deploy" | Replaced by A3 + A4 locally. |
| Agent "confidence clears bar" / numeric confidence | **Evidence list.** Gate uses structured facts only. |
| Tier name "Urgent" in UI | UI: **Routine / Check Needed / Escalation** (internal names may stay). |
| `device_online/offline` as sole health source | Health = last status **+ timestamp freshness** (stale → `unknown`). |
| "First system built to notice when nothing happened" | "Ring tells you what happened. Threshold tells you when something you expected didn't happen." |

**Unchanged 🔒:** deterministic escalation gate; agent never in the absence path; derived (not stored) status; `unknown` never escalates; fire-once edge detection; household memory = recorded corrections (not "learning"); scope fences; evidence-before-"done".

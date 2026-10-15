# Threshold — Hackathon Demo Video Script & Presentation Storyboard

> **Project:** Threshold — Pattern-Absence Detection for Elderly Care  
> **Tagline:** *"Ring tells you when something happens. Threshold tells you when something doesn't."*  
> **Video Target Length:** 1 minute 58 seconds (Strictly under 2:00 hackathon ceiling)  
> **Format:** 1080p 60fps (16:9), crisp voiceover, dual-screen display (Dashboard + Terminal + Phone PIP)  

---

## 1. Production Setup & Screen Layout

### Screen Layout (OBS Studio or Screenflow)
```
┌────────────────────────────────────────────────────────────────────────┐
│                              TOP BAR                                   │
│            THRESHOLD: Zero Cloud Pattern Absence Detection             │
├──────────────────────────────────────┬─────────────────────────────────┤
│                                      │                                 │
│         LEFT 55%: WEB DASHBOARD      │      RIGHT 45%: TERMINAL        │
│          http://localhost:8080       │    python3 scripts/run_demo.py  │
│                                      │                                 │
│  - Routine cards (green/yellow/red)  │  - Interactive 3-beat runner    │
│  - Emergency pulsing top banner      │  - Step-by-step timeline table  │
│  - Ring Partner API sandbox status   │  - Clean formatted ASCII boxes  │
│  - Today's activity & audit log      │                                 │
│                                      ├─────────────────────────────────┤
│                                      │   LOWER RIGHT (1:15–1:40 PIP)   │
│                                      │      PHONE SCREEN MIRROR        │
│                                      │  - Real Telegram Photo Alert    │
│                                      │  - [False alarm] [Got it] tap   │
└──────────────────────────────────────┴─────────────────────────────────┘
```

---

## 2. Shot-by-Shot Storyboard & Script (0:00 – 1:58)

### Beat 1: The Human Problem & Baseline (0:00 – 0:32)

| Timestamp | Visual Cue | On-Screen Action | Audio Narration (Verbatim) |
| :--- | :--- | :--- | :--- |
| **0:00 – 0:10** | Title card fades in from black. Warm dark aesthetic. | Title: **THRESHOLD**<br>*"Pattern-Absence Detection for Elderly Care"* | *"Fifty-three million Americans care for an aging family member. Most live hundreds of miles away. Every day, families live with a quiet anxiety: is Grandma okay today?"* |
| **0:10 – 0:22** | Cut to dual screen: Terminal + Dashboard. | Terminal runs `python3 scripts/run_demo.py --interactive`. ASCII banner appears. Dashboard opens at `localhost:8080`. | *"Threshold monitors daily routines using the Ring sensors already installed in her home. No new gadgets, no wearable trackers she forgets to charge, and zero cloud bills."* |
| **0:22 – 0:32** | Dashboard shows 2 routine cards: Front Door & Medicine Cabinet. | 07:30 CDT: Margaret opens the front door for the newspaper. Event arrives $\rightarrow$ Front Door card turns **🟢 OBSERVED**. | *"On a normal morning, Margaret steps outside at 7:30 for the paper. Threshold registers the motion, marks her morning walk Observed, and the family knows her day has started safely."* |

---

### Beat 2: The Silence & Pattern-Absence Detection (0:32 – 1:15)

| Timestamp | Visual Cue | On-Screen Action | Audio Narration (Verbatim) |
| :--- | :--- | :--- | :--- |
| **0:32 – 0:45** | Focus on Medicine Cabinet card: **🟡 WATCHING** (Window: 07:00–09:00). | Terminal clock advances past 09:00. Status remains Watching due to the 30-minute grace buffer. | *"Every morning between 7:00 and 9:00, Margaret opens her medicine cabinet. But today... the clock strikes 9:00. The window closes. Threshold doesn't panic — it waits for a thirty-minute grace buffer, during which any activity still satisfies the routine."* |
| **0:45 – 0:58** | Clock reaches 09:31 CDT. The room remains silent. | Edge transition occurs! Medicine Cabinet card turns **🔴 MISSED**. High-contrast **MISSED ROUTINE** banner pulses at top of dashboard. | *"At 9:31, the grace buffer expires. Silence. Ring tells you when a door opens. Nobody tells you when it didn't. Threshold's deterministic engine detects the absence and edge-triggers an immediate escalation."* |
| **0:58 – 1:15** | Phone Screen PIP appears over lower right terminal. | Phone vibrates. Real Telegram notification pops up with high-res camera photo and buttons: `[False alarm]` `[Got it]`. | *"The caregiver doesn't get a cold, jarring phone call. Her daughter Sarah gets a quiet photo notification right inside Telegram — the most recent camera image from the sandbox camera. In one glance, Sarah can see if Mom is in bed, sitting in her armchair, or in need of help."* |

---

### Beat 3: Resolution & Peace of Mind (1:15 – 1:42)

| Timestamp | Visual Cue | On-Screen Action | Audio Narration (Verbatim) |
| :--- | :--- | :--- | :--- |
| **1:15 – 1:28** | Finger taps `[False alarm]` on phone screen. | Callback processes instantly. SQLite records correction. Dashboard emergency banner dismisses; badge updates to **🟢 Acknowledged (False alarm)**. | *"Sarah sees Mom peacefully reading on the sofa — she simply slept in. Sarah taps 'False alarm.' Instantly, the dashboard updates, the emergency alert dismisses, and the system records the correction without false panic."* |
| **1:28 – 1:42** | At 10:00 CDT, delayed Ring event arrives. | Late event arrives: `motion_detected` at 08:30 CDT backfilled. Routine updates to **🟢 OBSERVED**. Audit trail preserved. | *"And when Margaret's delayed morning activity syncs from Ring event history, Threshold gracefully reconciles it into Observed, preserving the complete audit trail for family peace of mind."* |

---

### Beat 4: Architectural Differentiators & Close (1:42 – 1:58)

| Timestamp | Visual Cue | On-Screen Action | Audio Narration (Verbatim) |
| :--- | :--- | :--- | :--- |
| **1:42 – 1:52** | Terminal runs `python3 -m unittest discover tests -v`. Green output scrolls: **automated tests passing**. | Architecture slide / scorecard: Zero AWS, Zero Pip dependencies. Runs on your own machine; no Threshold-operated cloud. Data goes only to Ring (the source) and Telegram (alerts). | *"Zero cloud. Zero AWS bills. Zero third-party dependencies — built entirely on Python's standard library and SQLite. Runs on a twenty-dollar Raspberry Pi. Comprehensive automated test suite."* |
| **1:52 – 1:58** | Closing Title Card: THRESHOLD + GitHub URL. | Subtitle: *"Because the most critical signal of the day is the one that's missing."* | *"Threshold — because the most important signal of the day is the silence. Thank you."* |

---

## 3. Judge Presentation Talking Points & 60-Second Pitch

If pitching live at a hackathon booth, deliver this exact 45-second pitch:

> *"Ring tells you when something happens — the door opened, motion detected. But nobody tells you when something **didn't** happen. If your 82-year-old grandmother opens her medicine cabinet every morning at 7:30, and one morning she doesn't — that silence is the most critical signal of her day.
> 
> *Threshold monitors daily routines using existing Ring sensors already in the home. When expected activity doesn't occur, it edge-triggers an escalation and sends the caregiver a snapshot photo from the nearest Ring camera right inside Telegram with two simple buttons: 'False alarm' or 'Got it.'
> 
> *It runs on a Raspberry Pi on the local home network. Zero AWS, zero cloud subscriptions, zero pip dependencies. Runs on your own machine; no Threshold-operated cloud. Data goes only to Ring (the source) and Telegram (alerts). We've verified it live against Ring Partner API sandbox (Developer Playground), captured camera snapshots, and backed it with passing automated tests.
> 
> *The sensors were already there. The cameras were already recording. All that was missing was someone listening for the silence."*

---

## 4. Judge Q&A Defense & Objection Cheat Sheet

### Q1: "Why did you build this with Zero AWS / Zero Cloud?"
- **Answer:** *"Elderly care data is deeply intimate: sleep schedules, bathroom visits, medication timing. Families do not want their parent's daily behavioral rhythms uploaded to third-party cloud data lakes or LLM providers. Runs on your own machine; no Threshold-operated cloud. Data goes only to Ring (the source) and Telegram (alerts). Families never pay monthly cloud hosting bills."*

### Q2: "Why standard library only (zero pip dependencies)?"
- **Answer:** *"A non-technical family member or volunteer setting up an edge device for Grandma cannot debug pip virtualenvs, broken C-extensions, or broken wheels on ARM processors. Threshold runs directly on any machine with Python 3.10+ installed. It simplifies deployment and maintenance."*

### Q3: "Why a deterministic rule engine instead of Machine Learning or LLMs?"
- **Answer:** *"In life safety and elderly care, determinism and explainability are paramount. If an alert fires, a caregiver needs to know exactly why: 'The window was 7:00 to 9:00, the 30-minute grace buffer expired, and no cabinet event was received.' If a model hallucinates a 68% anomaly probability, nobody knows what that means. Deterministic edge detection provides one alert per missed routine per day."*

### Q4: "What happens if the home internet or Ring camera goes offline?"
- **Answer:** *"Threshold implements a strict three-state absence model: if Ring device health stops reporting or becomes stale (> 2 hours old), the routine transitions to `UNKNOWN`, NOT `MISSED`. Threshold will NEVER sound a false alarm because Grandma's Wi-Fi router rebooted. When connectivity restores, Threshold backfills event history and catches up automatically."*

### Q5: "What if the camera snapshot fails to download?"
- **Answer:** *"Threshold enforces a strict 4.0-second timeout on snapshot capture. If a camera is waking from deep sleep or Ring returns a 425 Media Unavailable, Threshold instantly falls back to a clean text-only Telegram alert with the inline buttons. We never delay an emergency alert waiting for an image."*

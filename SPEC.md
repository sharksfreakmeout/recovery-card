# Recovery Card — Spec

## Architecture

A three-stage local pipeline, time as the axis.

### Stage 1 — Capture (`capture.py`)
- Screenshot the main display every 10 seconds via `screencapture`.
- Skip frames near-identical to the previous one (hash or pixel diff).
- Retain the newest 20 distinct frames in `captures/`.
- Alongside each frame, log the frontmost app name and window title via `osascript` into a paired metadata file.

### Idle watcher (in `capture.py` or `trigger.py`)
- Read macOS idle time (`ioreg` `HIDIdleTime`).
- When idle exceeds `IDLE_THRESHOLD` seconds (default 60, configurable via env var for demos), run card generation once automatically, so the card exists before the user returns.

### Stage 2 — Inference (`card.py`)
- Send the 3 newest **distinct** frames plus recent window titles to local Ollama, **images before text** (Gemma 4's documented preference).
- Output strict JSON:
  `{goal, reasoning, next_action, open_loops (max 3), confidence ("high"|"medium"|"low"), evidence (one sentence citing what on screen supports this)}`
- If a park note exists, treat it as user-confirmed truth, weighted above inference, and include it.
- Save cards to `cards/`, timestamped.
- Malformed JSON: retry once, then produce a fail-closed card.

### Stage 3 — Interface (`app.py`)
- Flask at `localhost:5000`.
- Preflight on startup: Ollama reachable, model present, test screenshot contains real content. Plain-English fixes if not.
- One large calm **"I'm back"** button revealing the latest card instantly. Only generate on demand if no card exists in 10 minutes, with a visible "reconstructing" state.
- Card sections: **"Pick up here"** (goal + reasoning), **"Next step"**, **"Open loops"**, plus quiet confidence and evidence lines, and **"You said:"** when a park note exists.
- A **"Park it"** one-line text box for stepping away (macOS dictation gives voice for free).
- Status strip: model name, network state (must clearly show offline when network is down), last capture time, last card time.
- History of past cards below.
- Calm, minimal, zero cognitive load.

### `eval.py`
- Park notes double as ground truth.
- Show each card/truth pair, let the user mark correct or incorrect, print a running tally like "8/10".

## Model policy (test-informed, 2026-07-18)

Both models were tested on an identical real work screenshot (a Notion PRD, a browser on a pricing page, an editor) with the question *"What task is this person working on, and what were they about to do next?"*

- **`gemma4:12b-it-qat` — primary.** Correctly identified the document, extracted its problem statement, and connected two separate windows into one coherent work narrative. ~22s per card. This is genuine cross-window inference, not OCR.
- **`gemma4:e2b-it-qat` — emergency only.** Identified zero applications correctly, missed the browser window entirely, and **fabricated a Maven build step (`mvn`) that appears nowhere on screen.** It pattern-matched a dark UI to "IDE" and invented supporting detail.

Therefore:

1. **e2b is used solely when 12B is absent.** It is never a silent fallback.
2. **Every card produced by e2b is forced to `confidence: "low"`**, regardless of what the model reports.
3. **The UI must visibly display "reduced model" on those cards.** The user must never mistake an e2b card for a 12B card.

## The evidence field is load-bearing

`evidence` is not decoration and not a UI nicety. It is the **hallucination tripwire**.

The e2b failure above was confidently worded and internally consistent; the only thing that exposed it was checking its claims against what was actually on screen. The `evidence` field forces the model to cite the specific on-screen thing supporting its inference, which makes a fabrication visible to the user at a glance instead of persuasive.

Consequences: `evidence` is never optional, never empty, and never generic. A card whose evidence does not name something concrete and on-screen is treated as a failed generation and falls back to the fail-closed card.

## Invariants

- **Local-first.** No cloud API, hosted model, or network call at runtime.
- **Fail-closed.** Thin signal produces "not enough signal to reconstruct" plus what was seen. Never fabrication.
- **Source-attributed.** Every card carries evidence.
- **Capture liberally, infer deliberately.** Inference runs on interruption boundaries, not timers.
- **Everything timestamped and replayable.**

## Out of scope

Integrations, cloud calendars, DOM scraping, accessibility APIs, multi-monitor, packaging, auth, team features.

**Nothing enters this list.**

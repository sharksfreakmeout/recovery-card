# Recovery Card — Spec

## Core reframe: the unit is a THREAD, not a project

The app holds every thread of attention the user might return to: declared
work (Phossil, PLite), emergent errands (a "why is my speaker broken → Mac
repair" detour), and communication threads (an email they started answering).
Its job is to keep each thread's **return-point** so nothing gets lost.

- Threads are **held, never discarded**.
- A mere glance (LinkedIn, an email preview, no action taken) stays
  **ambient/unfiled**. Starting to DO something — typing a reply, researching
  repairs — forms a thread.
- Thread states: `active` (attention is here now), `parked` (held, with a
  return-point), `ambient` (seen, not engaged, not filed).
- Threads are `declared` (named by the user, at onboarding or later) or
  `emergent` (detected from sustained engagement, offered to the user to
  name — human-in-the-loop, continuously, not just at onboarding).

## Governing design law (every surface)

W3C COGA, WCAG 2.1/2.2 cognitive criteria, inclusive neurodivergent UX:

- One primary action per screen. Plain language, sentence case, no jargon.
- Calm sensory field: dark canvas, generous space, slow intentional motion,
  nothing flashing.
- Patient pacing: nothing auto-advances or expires on screen; no time
  pressure.
- Consistent, predictable layout: the same kind of thing always in the same
  place across sessions, so the eye learns the board.
- Recognition over recall: the user understands their situation at a glance,
  never by remembering prior state.
- Errors explain plainly, never blame. A visible, consistent way back
  everywhere.

The native macOS window (`window.py`) is THE interface. The browser URL is an
emergency fallback only.

## Architecture

A three-stage local pipeline, time as the axis, with a thread graph
underneath. Two local models: `embeddinggemma` (768-dim) for continuous cheap
classification, `gemma4:12b-it-qat` reserved for card generation at
interruption boundaries. Both via Ollama; nothing leaves the machine.

### Stage 1 — Capture (`capture.py`)
- Screenshot the main display via `screencapture`, interaction-aware:
  capture when the user lands and engages (typing, clicking, intentional
  scroll); skip rapid app-switching with no engagement (thrashing).
- Skip frames near-identical to the previous one (pixel diff via sips).
- Retain the newest 20 distinct frames in `captures/`.
- **Engagement metadata per frame**: what the user was doing ("Cursor,
  typing" vs "Chrome, idle 8s"), from per-event-type Quartz timing (keys,
  clicks, scroll — timing only, never content). Feeds card.py so it can tell
  a forming thread from a glance.
- **Accessibility-tree capture, opportunistic, never required**: for the
  frontmost app, pull structured data where available — window title, focused
  element, selected text, active browser tab/URL — to sharpen evidence (exact
  PR number, exact selected cell). Degrades silently to screenshot+title.
  Never blocks or slows capture.
- **Clipboard watcher**: copied text snippets logged into frame metadata as
  high-intent signal. HARD RULE, consent by design: the standard concealed/
  transient pasteboard flags (password managers) are honored — anything so
  marked is never captured. Clipboard data stays local and git-ignored like
  all capture metadata.
- **Recent-file activity**: files modified in the last few minutes (Spotlight
  metadata) logged as thread fingerprints in frame metadata.
- **Refused, by principle: microphone / ambient audio capture.** Attention
  reconstruction does not require listening to the room, so this app never
  will.

### Idle watcher (in `capture.py` or `trigger.py`)
- Read macOS idle time (`ioreg` `HIDIdleTime`).
- When idle exceeds `IDLE_THRESHOLD` seconds (default 60, configurable via env var for demos), run card generation once automatically, so the card exists before the user returns.

### Stage 2 — Inference (`card.py`), thread-aware
- On return, first CLASSIFY recent frames into threads (window titles +
  engagement metadata + accessibility data matched against known threads —
  see Thread Intelligence addendum for exactly how).
- The card reconstructs the ACTIVE thread in depth — goal, reasoning,
  next_action as a direct instruction, open_loops ordered by consequence,
  confidence, evidence — AND surfaces the parked threads with their
  return-points.
- If recent activity forms a NEW thread, note it and offer to keep it. If it
  is a mere glance, leave it ambient. Never force ambiguous activity into a
  thread; "a new thread?" and "ambient" are both honest options.
- Cards are generated with the thread's memory (prior cards, park notes,
  return-points retrieved by embedding similarity), not just the last 3
  frames.
- Send frames **images before text** (Gemma 4's documented preference).
- Park notes and corrections outrank inference. Direction never exceeds
  evidence. Fail-closed unchanged: thin signal produces "not enough signal"
  plus what was seen, never fabrication.
- Save cards to `cards/`, timestamped, with the trigger stamped.
- Malformed JSON: retry once, then produce a fail-closed card.

### The thread graph (`graph.json`)
- Threads are top-level entities: `{id, name, status (active|parked|ambient),
  origin (declared|emergent), return_point, last_seen, salience, anchors,
  centroid}`.
- Nodes (`document | person | decision | blocker | task`) attach to a thread;
  edges connect nodes.
- Corrections re-parent nodes/threads and persist.
- The return-point is the single most important field: what the user needs to
  pick the thread back up.

### Stage 3 — Interface (`app.py`)

`app.py` is both the user surface and the live demo control room: one Flask
page at `localhost:5000`, calm and minimal, dark-friendly.

**Top — status strip.** Model name, network state (clearly shows OFFLINE when
down), frames captured, last capture time, last card time.

**Control.** A Start/Stop capture button that launches and stops `capture.py`
as a subprocess, so the whole demo runs from the browser without terminal
juggling.

**Live state — the centerpiece.** A live-updating panel (2-second polling is
fine, no websockets) showing the current mode:
- `ACTIVE` — user present, capturing. Subtle heartbeat with frames kept.
- `AWAY` — counter of seconds since last input, ticking live.
- `RECONSTRUCTING` — idle threshold crossed, card generation running, elapsed
  seconds shown.
- `CARD READY`.

`capture.py` writes its state to a small status file that `app.py` reads.

**Card.** When ready, the newest card displayed large: **"Pick up here"**
(goal + reasoning), **"Next step"**, **"Open loops"**, quiet confidence and
evidence lines, and **"You said:"** when a park note exists.

**Park it.** One-line text box saving a timestamped park note (macOS dictation
supplies voice).

**History.** Past cards below, timestamped.

**Preflight on startup.** Ollama reachable, model present, test screenshot
contains real content. Plain-English fixes if not.

**Port.** `localhost:5001` by default, pinned so the demo URL is
deterministic (macOS AirPlay Receiver owns 5000 and will answer requests
there with a 403 even when Flask appears to have started). `PORT` overrides.
If the port is busy the app steps to the next free one and prints a loud
warning naming the port actually in use.

### Stage 3b — Native macOS surface

Recovery Card is an ambient background utility, so its primary surface is
the OS, not a browser tab. The Flask service above becomes the local
backend; the native layer is a thin client over its tested API and adds no
capture, inference, or state logic of its own.

- **`menubar.py`** — menu bar app (`rumps`). A glyph shows state at a glance
  (`●` watching, `◐ 42s` away, `◍` reconstructing, `◉` card ready). The menu
  carries the state line, Start/Stop capture, Park it…, the current card's
  goal and next step, and Open card window. A native notification fires when
  a card lands.
- **`window.py`** — the full card in a real macOS window (`pywebview`,
  WKWebView, no browser chrome, own Dock icon). Runs as its own process
  because the menu bar and the window cannot share the macOS main thread.
- **Fallback.** `app.py` alone in a browser remains fully functional and is
  never removed. If the native layer fails, the demo still runs.

### Card provenance

Every card records `trigger`: `idle` (the automatic watcher fired), `im_back`
(the button), `menubar`, or `cli`. Without it there is no way to prove after
the fact that the automatic chain actually ran, which is the single most
important claim this project makes.

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

## Addendum — Thread Intelligence

How classification actually works. Two models, both local via Ollama:
`embeddinggemma` (768-dim, verified) runs continuously and cheaply;
`gemma4:12b-it-qat` runs only at interruption boundaries.

**Tiered classification, cheapest first, escalate only when needed:**
1. **Explicit hints** — park notes and user corrections always win.
2. **Metadata anchors** — app name, window title, URL, active file matched
   directly against a thread's anchors settles it.
3. **Semantic similarity** — embed the frame's text context (titles,
   accessibility text, clipboard snippet) and compare by cosine similarity
   against each thread's running centroid (mean of recent embeddings). Clear
   winner above a threshold → that thread.
4. **Ambiguous** → provisionally untagged, for retrospective healing.

**Momentum-based affinity (the glance-vs-thread rule).** Each thread carries
an affinity score that builds with sustained engagement (consecutive engaged
frames matching it) and decays otherwise. Switching the active thread, or
proposing a NEW emergent thread, requires momentum over multiple frames —
never a single frame. A glance (one or two low-engagement frames) moves
nothing and stays ambient. Thrashing (rapid switching, no engagement) builds
no affinity anywhere.

**Thread memory as retrieval.** At card time, the active thread's own history
(prior cards, park notes, return-points) is retrieved by embedding similarity
and the most relevant items included in the prompt.

**Retrospective healing.** When a card is generated, or an emergent thread is
confirmed and named, recent provisionally-untagged frames are re-examined
with that hindsight and folded into the right thread. Ambiguous frames are
never forced at capture time.

**Cost honesty.** Tiers 1–3 and all capture stay lightweight and continuous.
The 12B runs only at interruption boundaries.

## Future work (NOT built, listed so scope stays honest)

- Calendar federation (Google multi-account + Notion) as the next context
  layer, with explicit consent.
- Full universal accessibility-tree reading beyond the active app.
- A local-only Mac calendar read (no network) as a possible later stretch.

## Invariants

- **Local-first.** No cloud API, hosted model, or network call at runtime.
- **Fail-closed.** Thin signal produces "not enough signal to reconstruct" plus what was seen. Never fabrication.
- **Source-attributed.** Every card carries evidence.
- **Capture liberally, infer deliberately.** Inference runs on interruption boundaries, not timers.
- **Everything timestamped and replayable.**

## Out of scope

Integrations, cloud calendars, DOM scraping, accessibility APIs, multi-monitor, packaging, auth, team features.

**Nothing enters this list.**

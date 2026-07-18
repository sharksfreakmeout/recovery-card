# RESUME — read this first

If the user says "resume", read this file, then CLAUDE.md, then SPEC.md, then
continue from **START HERE TOMORROW** below.

Last session ended 2026-07-18, ~01:45. Deadline: 2026-07-18, 3:00 PM.

## What this project is

Recovery Card — a 100% local macOS tool for the Build with Gemma hackathon
(On-Device track). It passively screenshots the user's work and, after an
interruption, uses a local Gemma 4 model via Ollama to reconstruct what they
were doing, why, and what comes next. Full rules in CLAUDE.md, architecture in
SPEC.md.

## User context

- **Non-technical.** Explain in plain English with exact commands.
- **Do not relitigate decisions or repeat time/scope warnings.** He said
  directly: "Build everything and stop telling me about the time and if I'm
  sure." Flag a genuine blocker once, then build. Surfacing a real bug or a
  false claim is still required; second-guessing his priorities is not.
- He finds bugs by *using* the app. Screenshots and process checks miss the
  class of bug that matters (blinking UI, a key that does nothing). Hand him
  interactive surfaces to test in short loops.

## Environment (verified, do NOT redo)

- Ollama v0.32.1 running at localhost:11434.
- `gemma4:12b-it-qat` (7.2 GB, vision) — primary. ~22s for 1 frame, ~44s for 3.
- `gemma4:e2b-it-qat` (4.3 GB, vision) — emergency only. **It fabricates.**
  In testing it invented a Maven build step that was nowhere on screen.
- Screen Recording + Accessibility permissions granted to Cursor.
- Flask, rumps, pywebview, pyobjc installed in project `.venv`.
- Public repo: https://github.com/sharksfreakmeout/recovery-card

## What is built and working

| File | What it does | State |
|---|---|---|
| `capture.py` | 10s screenshots, sips-based frame dedup, window titles, idle watcher, status file | tested |
| `card.py` | 3 newest frames → Ollama → strict JSON card; park notes as truth; fail-closed; evidence tripwire | tested |
| `app.py` | Flask backend + full UI at :5001, control room, summon, overlay page, scoring panel | tested |
| `menubar.py` | 🦴 menu bar app, ambient state, notifications OFF by default | runs |
| `window.py` | native macOS card window (pywebview) | confirmed |
| `overlay.py` | full-screen blurred focus overlay, native Escape | fixed, needs user retest |
| `eval.py` | per-field scoring (goal/reasoning/next_action/open_loops) + in-app panel | tested |

## How to start everything

    cd "/Users/ericespinel/Gemma Hackathon" && IDLE_THRESHOLD=25 .venv/bin/python menubar.py

That starts the Flask backend too. UI at **http://localhost:5001** (port 5001
is pinned deliberately; macOS AirPlay owns 5000).

Emergency: `pkill -f overlay.py` — never let the user be trapped by the overlay.

## START HERE TOMORROW

**1. Prove the idle → card chain. Still never run end-to-end.**
This is the project's central claim and the biggest open risk. Everything
needed exists; the automatic path connecting them is unverified. Start capture,
work ~1 min, hands off 25s. A card stamped `"trigger": "idle"` is the proof.
Check with:

    python3 -c "import json,glob; print(json.load(open(sorted(glob.glob('cards/card_*.json'))[-1]))['trigger'])"

**2. Get 5–10 real eval marks.** The harness works; zero real data exists. Park
a note before each break so the card can be scored. This number is the
strongest evidence for judges.

**3. Then build, in this order** (all approved by the user):
- Hotkey binding via macOS Shortcuts → `curl -X POST localhost:5001/api/summon`
- **Tier 1** activity narrative: classify each frame authoring / reading /
  navigating / watching / away from per-event-type Quartz timing
  (`CGEventSourceSecondsSinceLastEventType` — verified, needs NO permissions)
  plus the existing screen-diff number. Feed a behavioral narrative to Gemma
  instead of a list of window titles. Add scroll-as-presence so motionless
  reading stops looking like an empty chair.
- **Tier 2** smart frame selection: `card.py` currently takes the 3 *newest*
  frames, so 20 minutes in a PRD followed by 90 seconds on a pricing page
  produces a card about pricing. Pick the last frame from each distinct
  context, weighted to recency. Biggest single card-quality win.
- **Tier 3** adaptive capture: dedup threshold and interval as functions of
  mode. Today's dedup is arguably backwards — typing changes few pixels and
  gets skipped, idle scrolling changes many and gets kept.

## Known rough edges

- 🦴 may be invisible: his menu bar is full and macOS hides overflow behind the
  notch on a 16" MacBook Pro. Fix by freeing menu bar space, not by code.
- App menu reads "Python" because it is unbundled. Needs py2app to fix properly.
- Overlay blink + trapped-Escape are fixed but **he has not retested since**.

## macOS landmines already hit (do not rediscover)

1. `screencapture` exits 0 even on failure; refuses dot-prefixed filenames.
2. AirPlay Receiver owns port 5000 and binds the IPv6 wildcard — Flask binds
   127.0.0.1:5000 fine while every localhost:5000 request 403s from AirPlay.
3. `fullscreen=True` in pywebview uses native fullscreen → own Space → wrong
   for an overlay.
4. An ordinary on_top window will not draw over another app's fullscreen Space.
   Needs raised window level + fullScreenAuxiliary + canJoinAllSpaces.
5. Window level must be set BEFORE the frame, or macOS constrains it below the
   menu bar.
6. AppKit calls from a background thread kill the process natively with no
   traceback. Use `PyObjCTools.AppHelper.callAfter`.

## Invariants — do not break these

- **100% local.** Only network call is localhost:11434.
- **Fail-closed.** Thin signal produces "not enough signal to reconstruct" plus
  what was seen. Never fabrication.
- **Evidence is a tripwire.** Empty/short/generic evidence = failed generation.
- **e2b is never trusted.** Forced to low confidence, flagged in the UI.
- **Never push real screenshots.** `captures/`, `cards/`, `eval/` contents are
  git-ignored, plus all image formats repo-wide.

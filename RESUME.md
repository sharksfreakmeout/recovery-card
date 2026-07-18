# RESUME — read this first

If the user says "resume", read this file, then read CLAUDE.md, then continue from **NEXT STEP** below.

## What this project is
Recovery Card — a 100% local macOS tool for the Build with Gemma hackathon (On-Device track).
Screenshots the user's work, and on return uses a local Gemma model via Ollama to generate a
"Recovery Card" (what they were doing, why, and the next action). Full rules in CLAUDE.md.

## User context
- Non-technical. Explain every step in plain English + exact commands. One milestone at a time,
  stop and wait for confirmation. Commit after each working milestone.
- On macOS 26.3. Working inside Cursor's integrated terminal (Cursor.app is the TCC-responsible app).

## Environment already verified (do NOT re-download)
- Ollama installed (v0.32.1) and running at http://localhost:11434 — responds HTTP 200.
- `gemma4:e2b-it-qat` (4.3 GB) — fully downloaded. (fallback model)
- `gemma4:12b-it-qat` (7.2 GB) — ALREADY fully downloaded. (primary model — no pull needed)

## Setup checklist status
1. Ollama installed & running .............. DONE
2. gemma4:e2b-it-qat downloaded ............ DONE
3. Screenshot -> Gemma understanding test .. BLOCKED (see below)
4. Pull gemma4:12b-it-qat ................... DONE (already present)

## Why item 3 is blocked
`screencapture -x` fails with "could not create image from display" = macOS Screen Recording
permission is not active for Cursor. On macOS 26 the checkbox alone isn't enough: Cursor must be
FULLY quit (Cmd+Q) and relaunched AFTER the toggle is on. User is restarting Cursor now to fix this.

## NEXT STEP (do this when user returns and says "resume")
1. Re-run the screenshot test:
   cd "/Users/ericespinel/Gemma Hackathon" && screencapture -x captures/test_screen.png && open captures/test_screen.png
   - If it still fails: the permission still didn't take. Re-diagnose (toggle OFF/ON, Cmd+Q, reopen).
   - If it succeeds: ask the user to LOOK at the opened image and confirm it shows real windows,
     not a blank/black desktop.
2. Once the user confirms it's a real screenshot, send that image to `gemma4:e2b-it-qat` via the
   Ollama API (POST http://localhost:11434/api/generate with the base64 image in "images") and ask:
   "What task is this person working on, and what were they about to do next?"
   Then give an HONEST assessment: real screen understanding vs vague/hallucinated.
3. Report a one-line status for all 4 checklist items.

## After the checklist passes
Begin Milestone 1 of the actual build (simple Python 3 + one Flask page, no DB, all local).
Stop after each milestone and wait for confirmation. Commit each working milestone.

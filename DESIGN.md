# PLite Design System

This file is law, like CLAUDE.md. Read it before any UI work. Every surface
ships against it; violations are logged, not shipped silently.

## Tokens

### Canvas & surfaces
| token | value | use |
|---|---|---|
| `--bg` | `#0f1115` | the canvas. Dark, warm-neutral, never pure black |
| `--panel` | `#171a21` | cards, rows, panels |
| `--line` | `#252a34` | hairline borders, dividers |

### Text hierarchy
| token | value | use |
|---|---|---|
| `--text` | `#e6e8ec` | primary — what the person came to read |
| `--text-2` | `#9aa3b2` | secondary — reasoning, descriptions |
| `--dim` | `#8b93a1` | muted — labels, timestamps, hints |

### The three accent roles
| token | value | role |
|---|---|---|
| `--teal` | `#4fd6be` | **active / positive**: the active thread, watching state, correct, resolved |
| `--amber` | `#e0af68` | **next step**: the one thing to do now; away-time notices |
| `--coral` | `#f7768e` | **blocker / attention**: blockers, wrong, offline, destructive actions |

Accents are roles, not decoration. Never use an accent outside its role;
never introduce a fourth accent. Links and focus rings borrow teal.

### Spacing, radii, type
- Spacing scale: `4 / 8 / 12 / 16 / 22 / 28 / 40` px. Nothing off-scale.
- Radii: panels/cards `14px` · buttons/inputs `9px` · chips/tags `6px`.
- Type: system stack (`-apple-system, SF Pro Text`). Scale:
  `29px` card goal · `20px` headline · `15.5px` body · `13.5px` secondary ·
  `12.5px` meta · `11px` section labels (letter-spaced, never bold-shouty).
- **Minimum text size: 12px.** Nothing readable is smaller, ever.

## Voice

- **Second person, always.** "You were drafting…" — never "the user", never
  "they" when meaning the person.
- **No hedging in goal or next_action.** Uncertainty lives in the confidence
  field. "Possibly", "it seems", "may have been" are banned there.
- **Plain words.** Sentence case. No jargon.
- **Never engine-speak in user-facing text.** Banned: "Frame 1/Frame 2",
  file names of internal stores (graph.json, status.json), tier numbers,
  model names — except the one quiet status line, which may name the model.
- Evidence speaks in human terms: *"the terminal showing frame-timing logs
  and your Claude window"* — never *"Frame 2 shows…"*.

## Surface laws

1. **One primary action per screen.** Everything else is quiet.
2. **Recognition over recall.** The person understands their situation at a
   glance; they never need to remember prior state.
3. **Nothing flashes.** Motion is slow and intentional (fade/rise ≤ 400ms,
   pulse periods ≥ 2s). Nothing blinks to get attention.
4. **Nothing expires on screen.** No auto-advance, no time pressure.
5. **Consistent placement across sessions.** The same kind of thing lives in
   the same place, so the eye learns the layout.
6. **Every surface dismissable/escapable at all times** — visible ways out
   (Esc, ×, back), never dependent on a single mechanism.
7. **Errors explain plainly and never blame.** Every error names the fix.

## Component specs

### Return Card (the moment of return — overlay)
Order, top to bottom, never rearranged:
1. Headline (plain sentence, `--text-2`, 15px)
2. Away line when present (amber): "You were away 25 minutes (your Mac was
   asleep)."
3. Flags only when true: REDUCED MODEL (amber) · NOT ENOUGH SIGNAL (coral)
4. Agent line when present: "While you were away: …"
5. **PICK UP HERE** — goal (29px) + reasoning (`--text-2`)
6. **NEXT STEP** — amber label, direct instruction
7. **OPEN LOOPS** — active thread only, consequence-ordered
8. You-said / corrections (teal-bordered quote blocks)
9. **ALSO HOLDING** — parked threads, name + return-point, one line each
10. Quiet meta: confidence · model · trigger · evidence
Per-section ✓/✗ taps (faint until hover; ✓ teal, ✗ coral). One dismiss (×)
plus Esc plus click-outside. Background: vibrancy blur + scrim.

### Board thread rows
- Headline sentence at top (20px), then one quiet status line.
- Active thread: teal left-accented card, name 18px, return-point visible.
- Held threads: panel rows — HELD tag (11px, letter-spaced), name 15.5px,
  return-point or "No return-point yet — one appears after the first card."
- Emergent affordance: dashed amber border, name field + Keep / Leave it.
- Park box beneath the controls. Footer links quiet (`--dim`).
- Every row is clickable → that thread's map. No other action on the row.

### Thread map
- Top: back link ("‹ Back to your threads"), thread name + status tag,
  headline including return-point + parked duration.
- Map: thread chip centered; ≤ 6 nodes around it chosen by pin > blocker >
  salience/recency; blocker nodes coral with a coral edge; "show more" for
  the rest. Node labels are plain words, never IDs.
- Node tap → short menu in plain words (rename · move · detach · not a
  thing · resolve · pin · why is this here?). No drag, no zoom.
- Beside/below: recent cards timeline — relative time + one-line goal;
  tap opens that card read-only on the Return surface.
- One primary action: **Resume this thread** (teal). The restore offer is a
  quiet secondary: "I can bring back: […]. The rest is on the card."

## Compliance log

Violations found and not yet fixed are listed here, dated, so they are
visible instead of silently shipped. (none currently)

### Excluded-state copy (private apps)
Calm and plain, never alarm-styled: menu bar "Paused — a private app is in
front" (quiet ⏸ beside the bone, no coral, no flash); board quiet line
"paused — a private app is in front". The app is never named in any log,
status file, or UI string. Dashboard copy: "PLite never captures anything
while these are in front."

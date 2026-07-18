# PLite — Recovery Card

**Built for the Build with Gemma hackathon, On-Device track.**

PLite holds your threads of attention. It quietly watches your screen while
you work — locally, on this Mac, with nothing uploaded anywhere — and when
you're pulled away (a meeting, a lid-close, a child, a phone call), it uses a
local Gemma 4 model to reconstruct where you were: what you were doing, why,
your exact next step, and every other thread you're holding with its
return-point. When you come back, the answer is already waiting. The unit of
the product is a *thread*, not an app or a project: declared work, emergent
errands, the email you started answering — held, never lost.

## Quickstart

```bash
# the app experience
open /Applications/PLite.app          # starts watching, bone in the menu bar

# or from a terminal
./plite start        # engine + menu bar + native window
./plite stop         # tears everything down
./plite doctor       # pre-demo health checks, plain English
./plite bench        # demo settings (model kept warm, 20s idle trigger)
```

Summon your card any time with **⌃⌥⌘R**, the menu bar bone, or the board's
**Show my card**. Park a one-line note before stepping away — it becomes
truth on your next card.

Requirements: macOS, [Ollama](https://ollama.com) with `gemma4:12b-it-qat`
and `embeddinggemma` pulled. Everything runs at `localhost`; the only network
call the app makes is to Ollama on this machine.

## Where things are

- **[SPEC.md](SPEC.md)** — architecture: the thread graph, tiered
  classification, momentum affinity, chat-aware capture, sleep/wake handling,
  consent-by-design capture rules, and the invariants (fail-closed,
  evidence-attributed, 100% local).
- **[DESIGN.md](DESIGN.md)** — the design system: tokens, voice, surface
  laws, component specs. It is law for every UI change.
- `tests/gate.sh` — the pre-commit gate (state machine, liveness, template
  sanity, classification truth, end-to-end smoke). `doctor.py` — pre-demo
  checks. `rehearse.py` — a seeded synthetic user that drives the real
  pipeline and grades the cards against declared answer keys.

## For judges: try it with the network off

This app's claim is that it is 100% local, so the best demo is the hostile
one: **turn off Wi-Fi and use it.** The status line will read
"offline — fully on-device" and everything keeps working — capture,
thread classification, card generation, the lot. The trust dashboard
("what it sees") shows every mechanism with a live off switch, the actual
data on disk, real deletion, and the standing refusals: concealed clipboard
content is never captured, excluded private apps never exist on disk, and
there is no microphone, ever.

Every card carries its own evidence, its coverage window ("From your
screen, 1:32–1:34 PM"), its trigger, and per-section ✓/✗ so you can score
it against what you were actually doing. The accuracy tally is computed
only from ground truth the user wrote down *before* the model saw anything.

#!/usr/bin/env python3
"""Recovery Card - Stage 2: Inference.

Takes the newest distinct frames plus the window titles captured alongside
them, sends them to a local Gemma 4 model through Ollama, and writes a
Recovery Card as strict JSON.

Everything is local. The only network call is to localhost:11434.

Run:  python3 card.py
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
CARDS = ROOT / "cards"
PARK_NOTE = CARDS / "park_note.json"
CORRECTIONS = CARDS / "corrections.json"

# How many recent corrections carry forward as session memory.
CORRECTION_MEMORY = int(os.environ.get("CORRECTION_MEMORY", 3))

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Model policy is set in SPEC.md and is deliberately not a free choice.
PRIMARY_MODEL = "gemma4:12b-it-qat"
EMERGENCY_MODEL = "gemma4:e2b-it-qat"

FRAMES_PER_CARD = int(os.environ.get("FRAMES_PER_CARD", 3))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", 300))

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "reasoning": {"type": "string"},
        "next_action": {"type": "string"},
        "open_loops": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "string"},
    },
    "required": ["goal", "reasoning", "next_action", "open_loops",
                 "confidence", "evidence"],
}

REQUIRED_FIELDS = list(CARD_SCHEMA["properties"])

# Evidence that says nothing. See "the evidence field is load-bearing" in
# SPEC.md: this is the hallucination tripwire, so it is checked, not trusted.
GENERIC_EVIDENCE = {
    "the screen", "the screenshot", "the image", "the user's screen",
    "various windows", "the windows", "the desktop", "screen content",
    "the visible content", "n/a", "none", "unknown",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- Ollama ---------------------------------------------------------------

def ollama_json(path: str, payload=None, timeout=10):
    url = f"{OLLAMA}{path}"
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def pick_model():
    """Primary model if present, emergency model only if it is not.

    Returns (model_name, is_reduced). See SPEC.md model policy: the
    emergency model fabricated on-screen detail during testing, so cards it
    produces are forced to low confidence and flagged in the UI.
    """
    try:
        tags = ollama_json("/api/tags")
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA}. Is it running? "
            f"Start it with: ollama serve   ({e})")

    available = {m["name"] for m in tags.get("models", [])}
    if PRIMARY_MODEL in available:
        return PRIMARY_MODEL, False
    if EMERGENCY_MODEL in available:
        log(f"WARNING: {PRIMARY_MODEL} is missing. Falling back to "
            f"{EMERGENCY_MODEL}, which is known to fabricate detail.")
        log("         This card will be marked reduced model and forced to "
            "low confidence.")
        return EMERGENCY_MODEL, True
    raise RuntimeError(
        f"Neither {PRIMARY_MODEL} nor {EMERGENCY_MODEL} is installed. "
        f"Install one with: ollama pull {PRIMARY_MODEL}")


# --- Inputs ---------------------------------------------------------------

def newest_frames(n):
    """The n newest distinct frames, oldest first, with their metadata."""
    frames = sorted(CAPTURES.glob("frame_*.png"), key=lambda p: p.name)[-n:]
    out = []
    for f in frames:
        meta = {}
        mp = f.with_suffix(".json")
        if mp.exists():
            try:
                meta = json.loads(mp.read_text())
            except Exception:
                pass
        out.append((f, meta))
    return out


def read_park_note():
    """The user's own words, if they parked before stepping away."""
    if not PARK_NOTE.exists():
        return None
    try:
        note = json.loads(PARK_NOTE.read_text())
        return note if note.get("text", "").strip() else None
    except Exception:
        return None


def read_corrections():
    """Recent corrections the person typed after marking a card wrong.

    These are their own words about what they were actually doing, so they
    carry the same authority as a park note. Keeping the last few gives the
    next card a little session memory: if it misread the work once and was
    told so, it should not make the same mistake again.
    """
    if not CORRECTIONS.exists():
        return []
    try:
        log = json.loads(CORRECTIONS.read_text())
    except Exception:
        return []
    return [c for c in log if c.get("text", "").strip()][-CORRECTION_MEMORY:]


def thread_context():
    """Everything the thread graph knows that should shape this card.

    Returns (graph, active_thread_dict_or_None, parked_list, emergent, memory).
    Import is guarded: if thread intelligence fails, cards still generate
    the old way. Healing runs here because card time is when hindsight
    exists.
    """
    try:
        import threads as T
    except Exception:
        return None, None, [], None, []
    g = T.load()
    healed = T.heal(g)
    if healed:
        log(f"healed {healed} previously-ambiguous frame(s) into threads")

    active_tid = g["meta"].get("active_thread")
    active = g["threads"].get(active_tid)
    parked = [
        {"name": t["name"], "return_point": t.get("return_point", ""),
         "last_seen": t.get("last_seen", "")}
        for tid, t in g["threads"].items()
        if tid != active_tid and t.get("status") != "ambient"
    ]
    emergent = T.emergent_candidate(g)
    memory = []
    if active:
        memory = T.retrieve(g, active_tid,
                            active.get("return_point") or active["name"], k=4)
    T.save(g)
    return g, active, parked, emergent, memory


def build_prompt(frames, park, corrections=None, active=None, parked=None,
                 memory=None):
    lines = []

    if active:
        lines += [
            f'THE ACTIVE THREAD OF WORK IS: "{active["name"]}"',
            (f'  Its last known return-point: {active["return_point"]}'
             if active.get("return_point") else ""),
            "",
        ]
        if memory:
            lines.append("WHAT THIS THREAD'S OWN HISTORY SAYS (most relevant "
                         "first):")
            for m in memory:
                lines.append(f'  - ({m["kind"]}) {m["text"]}')
            lines.append("")
        lines = [l for l in lines if l != ""] + [""]

    if parked:
        lines.append("OTHER THREADS THE PERSON IS HOLDING (do not confuse "
                     "them with the active one):")
        for p in parked[:6]:
            rp = f' - return-point: {p["return_point"]}' if p["return_point"] else ""
            lines.append(f'  - {p["name"]}{rp}')
        lines.append("")

    if corrections:
        lines.append("THE PERSON HAS ALREADY CORRECTED EARLIER CARDS:")
        for c in corrections:
            lines.append(f'  on {c.get("field", "a field")}: '
                         f'"{c["text"].strip()}"')
        lines += [
            "",
            "These are their own words, stated after seeing a card that was "
            "wrong. Treat them as confirmed truth, exactly as you would a "
            "park note, and do not repeat the mistake they describe.",
            "",
        ]

    if park:
        lines += [
            "BEFORE STEPPING AWAY, THE PERSON WROTE THIS NOTE:",
            f'  "{park["text"].strip()}"',
            "",
            "This note is confirmed truth, stated by the person themselves.",
            "Weight it above anything you infer from the screenshots. If the "
            "screenshots seem to disagree with the note, the note wins.",
            "",
        ]

    lines += [
        "You are looking at screenshots of one person's screen, taken a few "
        "seconds apart, oldest first. They were interrupted and have now "
        "come back. Reconstruct what they were doing so they can resume.",
        "",
        "The frontmost window at each moment was:",
    ]
    for i, (f, meta) in enumerate(frames, 1):
        app = meta.get("app", "unknown")
        titles = meta.get("window_titles") or (
            [meta["window_title"]] if meta.get("window_title") else [])
        lines.append(f"  Frame {i} ({meta.get('timestamp', '?')}): {app}")
        for t in titles[:4]:
            lines.append(f"      window: {t}")

    lines += [
        "",
        "Reply with JSON only. No prose outside the JSON.",
        "",
        "  goal        - what they were trying to accomplish, one sentence",
        "  reasoning   - why they were doing it, what the thinking was",
        "  next_action - a DIRECT INSTRUCTION for the very next step, as if "
        "telling them what to do ('Reply to Marcus about the infra cost'), "
        "never a description of options",
        "  open_loops  - up to 3 unfinished items, ORDERED BY CONSEQUENCE: "
        "the one that costs most if forgotten comes first",
        "  confidence  - high, medium, or low",
        "  evidence    - one sentence naming the specific things ON SCREEN "
        "that support this. Name the actual document, window, or content "
        "you saw. Do not write vague phrases like 'the screen shows work'.",
        "",
        "Rules that matter:",
        "- Describe only what you can actually see. Never invent a tool, "
        "file, or command that is not visible.",
        "- If the screenshots are too thin to tell, say so plainly in goal "
        "and set confidence to low. An honest 'not enough signal' is far "
        "more useful than a confident guess.",
    ]
    return "\n".join(lines)


# --- Validation -----------------------------------------------------------

def validate(card):
    """Return (ok, reason). Enforces the evidence tripwire from SPEC.md."""
    missing = [f for f in REQUIRED_FIELDS if f not in card]
    if missing:
        return False, f"missing fields: {', '.join(missing)}"

    for f in ("goal", "next_action", "evidence"):
        if not isinstance(card.get(f), str) or not card[f].strip():
            return False, f"empty {f}"

    if card.get("confidence") not in ("high", "medium", "low"):
        return False, f"bad confidence value: {card.get('confidence')!r}"

    if not isinstance(card.get("open_loops"), list):
        return False, "open_loops is not a list"

    ev = card["evidence"].strip()
    if len(ev) < 25:
        return False, f"evidence too thin to verify: {ev!r}"
    if ev.lower().rstrip(".") in GENERIC_EVIDENCE:
        return False, f"evidence is generic, cites nothing specific: {ev!r}"

    return True, ""


def fail_closed(reason, frames, model, reduced):
    """A card that admits it has nothing, rather than inventing something."""
    seen = []
    for f, meta in frames:
        app = meta.get("app", "unknown")
        title = meta.get("window_title", "")
        seen.append(f"{app}: {title}" if title else app)

    return {
        "goal": "Not enough signal to reconstruct what you were doing.",
        "reasoning": (
            "The screenshots and window titles did not give a clear enough "
            "picture, so nothing is being guessed here. What was on screen "
            "is listed below so you can pick up the thread yourself."),
        "next_action": "Have a look at what was on screen and take it from there.",
        "open_loops": seen[:3],
        "confidence": "low",
        "evidence": ("No card was generated. " + reason) if reason else
                    "No card was generated.",
        "fail_closed": True,
        "fail_reason": reason,
        "model": model,
        "reduced_model": reduced,
    }


# --- Generation -----------------------------------------------------------

def ask_model(model, prompt, images):
    payload = {
        "model": model,
        "images": images,          # images before text, Gemma 4's preference
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": CARD_SCHEMA,     # Ollama constrains output to this shape
        "options": {"temperature": 0.2},
    }
    out = ollama_json("/api/generate", payload, timeout=REQUEST_TIMEOUT)
    return out.get("response", "")


def generate():
    CARDS.mkdir(exist_ok=True)

    frames = newest_frames(FRAMES_PER_CARD)
    park = read_park_note()
    corrections = read_corrections()
    graph, active, parked, emergent, memory = thread_context()

    model, reduced = pick_model()
    log(f"model: {model}" + ("  (REDUCED - emergency fallback)" if reduced else ""))

    if not frames:
        log("No frames captured yet. Is capture.py running?")
        return fail_closed("No screenshots had been captured yet.",
                           [], model, reduced)

    log(f"frames: {len(frames)}  " +
        ("park note: yes" if park else "park note: none") +
        (f"  corrections carried: {len(corrections)}" if corrections else ""))

    images = [base64.b64encode(f.read_bytes()).decode() for f, _ in frames]
    prompt = build_prompt(frames, park, corrections,
                          active=active, parked=parked, memory=memory)

    card = None
    for attempt in (1, 2):
        t0 = time.time()
        try:
            raw = ask_model(model, prompt, images)
        except urllib.error.URLError as e:
            log(f"attempt {attempt}: could not reach the model ({e})")
            continue
        except Exception as e:
            log(f"attempt {attempt}: request failed ({e})")
            continue
        log(f"attempt {attempt}: model replied in {time.time() - t0:.1f}s")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log(f"attempt {attempt}: reply was not valid JSON")
            continue

        ok, reason = validate(parsed)
        if ok:
            card = parsed
            break
        log(f"attempt {attempt}: rejected - {reason}")

    if card is None:
        log("Both attempts failed validation. Writing a fail-closed card.")
        card = fail_closed("The model did not return a usable, "
                           "evidence-backed answer.", frames, model, reduced)
    else:
        card["fail_closed"] = False
        card["model"] = model
        card["reduced_model"] = reduced
        card["open_loops"] = [str(x) for x in card["open_loops"]][:3]

    # SPEC.md model policy: emergency model output is never trusted at face value.
    if reduced:
        card["confidence"] = "low"

    if park:
        card["park_note"] = park.get("text", "").strip()
        card["park_note_at"] = park.get("timestamp")

    if corrections:
        card["corrections_used"] = [c["text"] for c in corrections]

    # Thread-awareness on the card itself. The parked list and emergent
    # proposal come from the graph, never from the model: the model
    # reconstructs the active thread, the graph holds the rest.
    if active:
        card["thread"] = active["name"]
    card["parked"] = parked
    if emergent:
        card["emergent_proposal"] = {
            "sample_text": emergent["sample_text"],
            "coherence": emergent["coherence"],
            "frames": emergent["frames"],
        }

    if graph is not None and active and not card.get("fail_closed"):
        try:
            import threads as T
            T.add_history(graph, active["id"], "card",
                          f"{card['goal']} → {card['next_action']}")
            T.touch(graph, active["id"], return_point=card["next_action"])
            T.save(graph)
        except Exception:
            pass

    card["generated_at"] = datetime.now().isoformat(timespec="seconds")
    card["frames_used"] = [f.name for f, _ in frames]
    # Who asked for this card: the idle watcher, the "I'm back" button, the
    # menu bar, or a bare command line. Without this you cannot tell after
    # the fact whether the automatic chain actually fired.
    card["trigger"] = os.environ.get("RECOVERY_TRIGGER", "cli")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = CARDS / f"card_{stamp}.json"
    path.write_text(json.dumps(card, indent=2))
    log(f"wrote {path.relative_to(ROOT)}")
    return card


def show(card):
    print()
    print("=" * 68)
    if card.get("reduced_model"):
        print("  [REDUCED MODEL - emergency fallback, treat with suspicion]")
    if card.get("fail_closed"):
        print("  [FAIL-CLOSED - not enough signal]")
    print("PICK UP HERE")
    print(f"  {card['goal']}")
    print(f"  {card['reasoning']}")
    print()
    print("NEXT STEP")
    print(f"  {card['next_action']}")
    if card.get("open_loops"):
        print()
        print("OPEN LOOPS")
        for loop in card["open_loops"]:
            print(f"  - {loop}")
    if card.get("park_note"):
        print()
        print(f'YOU SAID: "{card["park_note"]}"')
    print()
    print(f"confidence: {card['confidence']}   model: {card.get('model', '?')}")
    print(f"evidence:   {card['evidence']}")
    print("=" * 68)


if __name__ == "__main__":
    try:
        show(generate())
    except RuntimeError as e:
        log(f"ERROR: {e}")
        sys.exit(1)

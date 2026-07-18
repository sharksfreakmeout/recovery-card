#!/usr/bin/env python3
"""Recovery Card - Stage 1: Capture.

Screenshots the main display on an interval, throws away frames that look
near-identical to the one before, keeps the newest N distinct frames, and
records what app and window were in front at the time.

Also watches macOS idle time. When you step away for longer than
IDLE_THRESHOLD, it triggers card generation once, so the card already
exists by the time you come back.

Everything is local. No network calls.

Run:  python3 capture.py
Stop: Ctrl-C
"""

import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
# app.py reads this to drive the live state panel.
STATUS = CAPTURES / "status.json"

# --- Tunables (override with environment variables) -----------------------
CAPTURE_INTERVAL = float(os.environ.get("CAPTURE_INTERVAL", 10))
IDLE_THRESHOLD = float(os.environ.get("IDLE_THRESHOLD", 60))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", 20))
# Mean per-pixel brightness difference (0-255) below which two frames are
# considered "the same screen". Raise it to skip more, lower it to keep more.
DIFF_THRESHOLD = float(os.environ.get("DIFF_THRESHOLD", 1.5))

THUMB_PX = 32  # fingerprint width; small enough that a blinking cursor vanishes

# Seconds of no input before we call it AWAY rather than ACTIVE.
AWAY_AFTER = float(os.environ.get("AWAY_AFTER", 5))

# Only files matching this are ever pruned. Nothing else is touched, ever.
FRAME_RE = re.compile(r"^frame_\d{8}_\d{6}\.(png|json)$")

# Live state, mirrored to STATUS for app.py.
STATE = {
    "mode": "STARTING",
    "pid": os.getpid(),
    "frames_kept": 0,
    "frames_skipped": 0,
    "idle_seconds": 0.0,
    "last_capture": None,
    "last_frame": None,
    "card_started_at": None,
    "idle_threshold": IDLE_THRESHOLD,
    "capture_interval": CAPTURE_INTERVAL,
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def write_status(mode=None, **fields):
    """Mirror current state to captures/status.json for the UI."""
    if mode:
        STATE["mode"] = mode
    STATE.update(fields)
    STATE["updated_at"] = time.time()
    try:
        CAPTURES.mkdir(exist_ok=True)
        tmp = STATUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(STATE, indent=2))
        tmp.replace(STATUS)  # atomic, so app.py never reads a half-written file
    except Exception:
        pass  # the UI is a nicety; never let it kill capture


# --- Screen capture -------------------------------------------------------

def grab_screen(dest: Path) -> bool:
    """Screenshot the main display. Returns True on success.

    Two macOS quirks worth knowing:
      - screencapture exits 0 even when it fails, so the only reliable
        success test is whether the file actually appeared.
      - it silently refuses to write to any filename starting with a dot,
        which is why the temp frames are named pending_* and not .pending_*.
    """
    for args in (["-x", "-t", "png", "-D", "1"], ["-x", "-t", "png"]):
        subprocess.run(["screencapture", *args, str(dest)], capture_output=True)
        if dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def fingerprint(png: Path):
    """Downscale to a tiny BMP and return a list of grayscale values.

    Uses macOS's built-in `sips`, so there is nothing to pip install.
    """
    tmp = png.with_suffix(".fp.bmp")
    try:
        r = subprocess.run(
            ["sips", "-Z", str(THUMB_PX), "-s", "format", "bmp",
             str(png), "--out", str(tmp)],
            capture_output=True,
        )
        if r.returncode != 0 or not tmp.exists():
            return None

        data = tmp.read_bytes()
        if len(data) < 34 or data[:2] != b"BM":
            return None

        offset = struct.unpack("<I", data[10:14])[0]
        width = struct.unpack("<i", data[18:22])[0]
        height = abs(struct.unpack("<i", data[22:26])[0])
        bpp = struct.unpack("<H", data[28:30])[0]
        if bpp != 32:
            return None

        px = data[offset:offset + width * height * 4]
        if len(px) < width * height * 4:
            return None

        # Average the three colour channels into one brightness number.
        return [(px[i] + px[i + 1] + px[i + 2]) // 3
                for i in range(0, len(px), 4)]
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def frame_distance(a, b) -> float:
    """Mean absolute brightness difference between two fingerprints."""
    if a is None or b is None or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# --- Window context -------------------------------------------------------

# --- Engagement (timing only, never content) -------------------------------

try:
    from Quartz import (CGEventSourceSecondsSinceLastEventType,
                        kCGEventSourceStateHIDSystemState)
    _QUARTZ = True
except Exception:
    _QUARTZ = False

# Event type constants (avoid importing each symbol; values are stable).
_EV_KEY, _EV_CLICK, _EV_SCROLL, _EV_MOVE = 10, 1, 22, 5


def engagement_snapshot():
    """Seconds since the last key / click / scroll / mouse move.

    Pure timing from Quartz - what kind of input happened and when, never
    what was typed. This is how a forming thread (typing, clicking) is told
    apart from a glance (window open, hands still).
    """
    if not _QUARTZ:
        return {}
    s = kCGEventSourceStateHIDSystemState
    def ago(t):
        try:
            return round(CGEventSourceSecondsSinceLastEventType(s, t), 1)
        except Exception:
            return 999.0
    snap = {"keys_ago": ago(_EV_KEY), "click_ago": ago(_EV_CLICK),
            "scroll_ago": ago(_EV_SCROLL), "move_ago": ago(_EV_MOVE)}
    if snap["keys_ago"] < 10:
        snap["doing"] = "typing"
    elif snap["click_ago"] < 8:
        snap["doing"] = "clicking"
    elif snap["scroll_ago"] < 8:
        snap["doing"] = "scrolling"
    elif snap["move_ago"] < 8:
        snap["doing"] = "mousing"
    else:
        snap["doing"] = "idle"
    return snap


def is_engaged(snap):
    return snap.get("doing") in ("typing", "clicking", "scrolling")


# --- Clipboard (consent by design) -----------------------------------------

try:
    from AppKit import NSPasteboard
    _PASTEBOARD = True
except Exception:
    _PASTEBOARD = False

_last_clip_count = -1


def clipboard_snippet():
    """The current copied text, as a high-intent signal.

    HARD RULE: the standard concealed/transient pasteboard flags that
    password managers set are honored - anything so marked is never read,
    let alone stored. Only NEW copies are logged (change count), and only
    a short snippet.
    """
    global _last_clip_count
    if not _PASTEBOARD:
        return None
    try:
        pb = NSPasteboard.generalPasteboard()
        count = pb.changeCount()
        if count == _last_clip_count:
            return None  # nothing new since last frame
        _last_clip_count = count
        types = [str(t) for t in (pb.types() or [])]
        if any("ConcealedType" in t or "TransientType" in t
               or "AutoGeneratedType" in t for t in types):
            return None  # password-manager content: never captured
        s = pb.stringForType_("public.utf8-plain-text")
        if s and s.strip():
            return " ".join(str(s).split())[:300]
    except Exception:
        pass
    return None


# --- Recent files (Spotlight) ----------------------------------------------

_EXCLUDE_RE = re.compile(
    r"Library/|\.git/|node_modules|__pycache__|/captures/|/cards/|/logs/"
    r"|\.venv/|\.Trash|DerivedData")


def recent_files(minutes=5, limit=5):
    """Files the user touched recently - fingerprints of the live thread."""
    try:
        r = subprocess.run(
            ["mdfind", "-onlyin", str(Path.home()),
             f"kMDItemFSContentChangeDate >= $time.now(-{minutes * 60})"],
            capture_output=True, text=True, timeout=4)
        out = []
        for line in r.stdout.splitlines():
            if _EXCLUDE_RE.search(line):
                continue
            out.append(Path(line).name)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


# --- Accessibility enrichment (opportunistic, never required) ---------------

def ax_context(app_name):
    """Structured detail about the frontmost app, where macOS offers it.

    Browser tab URL and title, focused element, selected text. Every call is
    short-timeout and failure-silent: this sharpens evidence when available
    and costs nothing when not. It must never block or slow capture.
    """
    ax = {}
    try:
        if app_name in ("Google Chrome", "Arc", "Brave Browser"):
            url = osa(f'tell application "{app_name}" to get URL of active '
                      'tab of front window')
            tab = osa(f'tell application "{app_name}" to get title of active '
                      'tab of front window')
            if url:
                ax["url"] = url[:300]
            if tab:
                ax["tab"] = tab[:200]
        elif app_name == "Safari":
            url = osa('tell application "Safari" to get URL of front document')
            if url:
                ax["url"] = url[:300]

        focused = osa(
            'tell application "System Events" to tell (first application '
            'process whose frontmost is true) to get name of value of '
            'attribute "AXFocusedUIElement" of it')
        if focused and focused != "missing value":
            ax["focused"] = focused[:200]

        selected = osa(
            'tell application "System Events" to tell (first application '
            'process whose frontmost is true) to get value of attribute '
            '"AXSelectedText" of value of attribute "AXFocusedUIElement" of it')
        if selected and selected != "missing value" and selected.strip():
            ax["selected"] = " ".join(selected.split())[:300]
    except Exception:
        pass
    return ax


def osa(script: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def composed_value():
    """The text of the focused input element - the user's draft.

    Called ONLY after a frame is recognized as a chat surface, so an
    editor's whole buffer is never slurped by accident. Capped short.
    """
    v = osa('tell application "System Events" to tell (first application '
            'process whose frontmost is true) to get value of attribute '
            '"AXValue" of value of attribute "AXFocusedUIElement" of it')
    if v and v != "missing value":
        return " ".join(v.split())[:500]
    return ""


def window_context():
    """Frontmost app plus its window titles.

    The frontmost window is often untitled (palettes, dialogs), so we keep
    every non-empty title and use the first as the headline.
    """
    app = osa('tell application "System Events" to get name of first '
              'application process whose frontmost is true') or "unknown"

    raw = osa('tell application "System Events" to tell (first application '
              'process whose frontmost is true) to get name of every window')
    titles = [t.strip() for t in raw.split(",") if t.strip()]

    return {
        "app": app,
        "window_title": titles[0] if titles else "",
        "window_titles": titles[:5],
    }


# --- Sleep / wake awareness -------------------------------------------------
# Two detectors, by design:
#
#   1. NSWorkspace notifications (best-effort): stamp the moment sleep
#      begins. We may lose the race to suspension - that must cost nothing.
#   2. Wall-clock jump (reliable): during sleep this process is suspended,
#      so when a loop cycle takes far longer than the interval, the machine
#      slept. This works even when the notification never arrived.
#
# The away duration is computed from the wall clock, so sleep time counts
# as away time. What happened during sleep is honestly unobserved.

import threading

_slept_at = {"t": None}          # stamped by the notification, best-effort
_woke_event = threading.Event()  # set by DidWake, best-effort

SLEEP_JUMP = float(os.environ.get("SLEEP_JUMP", 30))  # extra secs = slept


def _sleep_watcher():
    """Background listener for macOS sleep/wake. Entirely best-effort."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSObject, NSRunLoop

        class _Obs(NSObject):
            def willSleep_(self, _note):
                _slept_at["t"] = time.time()
                try:
                    write_status("SUSPENDED", slept_at=_slept_at["t"])
                except Exception:
                    pass
                log("Mac is going to sleep - state flushed.")

            def didWake_(self, _note):
                _woke_event.set()

        obs = _Obs.alloc().init()
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.addObserver_selector_name_object_(
            obs, b"willSleep:", "NSWorkspaceWillSleepNotification", None)
        nc.addObserver_selector_name_object_(
            obs, b"didWake:", "NSWorkspaceDidWakeNotification", None)
        NSRunLoop.currentRunLoop().run()
    except Exception:
        pass  # clock-jump detection carries the load alone


def away_summary_text(seconds, asleep):
    mins = int(round(seconds / 60))
    span = (f"{mins} minute" + ("s" if mins != 1 else "")) if mins >= 1 \
        else f"{int(seconds)} seconds"
    return (f"You were away {span} (your Mac was asleep)." if asleep
            else f"You were away {span}.")


# --- Idle watching --------------------------------------------------------

def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input."""
    try:
        r = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
        return int(m.group(1)) / 1_000_000_000 if m else 0.0
    except Exception:
        return 0.0


def generate_card(reason: str, trigger="idle", away=None):
    """Hand off to Stage 2. Safe to call before card.py exists."""
    card = ROOT / "card.py"
    if not card.exists():
        log(f"  -> would generate a card now ({reason}), "
            "but card.py is not built yet. Skipping.")
        return
    log(f"  -> generating card ({reason})...")
    try:
        env = dict(os.environ)
        env["RECOVERY_TRIGGER"] = trigger  # stamped into the card as provenance
        if away:
            env["RECOVERY_AWAY_MODE"] = away.get("mode", "")
            env["RECOVERY_AWAY_SECONDS"] = str(int(away.get("seconds", 0)))
        r = subprocess.run([sys.executable, str(card)],
                           capture_output=True, text=True, timeout=300,
                           env=env)
        if r.returncode == 0:
            log("  -> card generated.")
        else:
            log(f"  -> card generation failed: {r.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        log("  -> card generation timed out after 5 minutes.")


# --- Retention ------------------------------------------------------------

def prune():
    """Keep only the newest MAX_FRAMES frames.

    Deletes nothing except frame_YYYYMMDD_HHMMSS.png/.json files inside
    captures/ that this script created.
    """
    frames = sorted(p for p in CAPTURES.glob("frame_*.png")
                    if FRAME_RE.match(p.name))
    for old in frames[:-MAX_FRAMES] if len(frames) > MAX_FRAMES else []:
        meta = old.with_suffix(".json")
        old.unlink(missing_ok=True)
        if FRAME_RE.match(meta.name):
            meta.unlink(missing_ok=True)
        log(f"  pruned {old.name} (keeping newest {MAX_FRAMES})")


# --- Main loop ------------------------------------------------------------

def main():
    CAPTURES.mkdir(exist_ok=True)

    log("Recovery Card capture started.")
    log(f"  interval={CAPTURE_INTERVAL}s  idle_threshold={IDLE_THRESHOLD}s  "
        f"keep={MAX_FRAMES}  diff_threshold={DIFF_THRESHOLD}")
    log("  Press Ctrl-C to stop.")

    # Thread intelligence is optional at capture time: if it fails, capture
    # must keep running. Frames are still classifiable later by healing.
    try:
        import threads as T
        graph = T.load()
    except Exception:
        T, graph = None, None

    threading.Thread(target=_sleep_watcher, daemon=True).start()

    prev_fp = None
    prev_app = None
    landed_waiting = False   # switched app but not yet engaged there
    kept = skipped = 0
    card_fired = False  # so one absence triggers exactly one card
    last_cycle = time.time()

    while True:
        cycle_start = time.time()

        # --- Wake detection. The return is certain: no idle threshold. ---
        gap = cycle_start - last_cycle
        woke = _woke_event.is_set() or gap > CAPTURE_INTERVAL + SLEEP_JUMP
        if woke:
            _woke_event.clear()
            # Sleep time counts as away time, from the wall clock. Prefer
            # the notification's stamp when we won that race; the loop gap
            # is the honest fallback when we lost it.
            slept_at = _slept_at["t"]
            away_secs = (cycle_start - slept_at) if slept_at else gap
            _slept_at["t"] = None
            summary = away_summary_text(away_secs, asleep=True)
            log(f"Woke from sleep. {summary}")
            write_status("RECONSTRUCTING", card_started_at=time.time(),
                         away_summary=summary)

            # Generate as soon as the model is reachable (Ollama also just
            # woke up; give it a moment rather than failing the one card
            # that matters most).
            for _ in range(30):
                try:
                    urllib.request.urlopen(
                        "http://localhost:11434/api/tags", timeout=2)
                    break
                except Exception:
                    time.sleep(1)
            generate_card(f"wake after {summary}", trigger="wake",
                          away={"mode": "asleep", "seconds": away_secs})
            card_fired = True
            prev_fp = None       # the screen has certainly changed
            landed_waiting = False
            write_status("CARD_READY", card_started_at=None)
        # Measured AFTER any card generation: a 30-second generation must
        # not read as another sleep on the next cycle (it did, in testing -
        # one wake fired three cards).
        last_cycle = time.time()

        idle = idle_seconds()
        STATE["idle_seconds"] = round(idle, 1)

        if idle >= IDLE_THRESHOLD and not card_fired:
            log(f"Idle for {idle:.0f}s - you stepped away.")
            write_status("RECONSTRUCTING", card_started_at=time.time(),
                         away_summary=away_summary_text(idle, asleep=False))
            generate_card(f"idle {idle:.0f}s", trigger="idle",
                          away={"mode": "awake", "seconds": idle})
            card_fired = True
            write_status("CARD_READY", card_started_at=None)
            last_cycle = time.time()  # generation time is not a sleep gap
        elif idle < AWAY_AFTER and card_fired:
            log("Welcome back. Re-arming the idle trigger.")
            card_fired = False
            write_status("ACTIVE")
        elif card_fired:
            write_status("CARD_READY")
        else:
            write_status("AWAY" if idle >= AWAY_AFTER else "ACTIVE")

        # Trust switches are read live every cycle: flipping one off in the
        # dashboard stops that mechanism within one interval, no restart.
        # The frames check comes BEFORE the screenshot: off means the screen
        # is never photographed at all, not photographed-then-discarded.
        try:
            import trust
            switches = trust.read_settings()
        except Exception:
            switches = {}

        if not switches.get("frames", True):
            write_status("PAUSED")
            log("frames switch is off - not capturing")
            time.sleep(max(0.0, CAPTURE_INTERVAL - (time.time() - cycle_start)))
            continue

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp = CAPTURES / f"pending_{stamp}.png"

        if not grab_screen(tmp):
            log("Screenshot failed. Check Screen Recording permission for "
                "your terminal in System Settings > Privacy & Security.")
            tmp.unlink(missing_ok=True)
            time.sleep(CAPTURE_INTERVAL)
            continue

        fp = fingerprint(tmp)
        dist = frame_distance(prev_fp, fp)
        snap = engagement_snapshot()
        ctx = (window_context() if switches.get("titles", True)
               else {"app": "hidden", "window_title": "", "window_titles": []})
        app_switched = prev_app is not None and ctx["app"] != prev_app
        prev_app = ctx["app"]

        # Interaction-aware keeping. Two ways to skip:
        #  - the screen has not changed (the old rule), UNLESS the user is
        #    actively typing: authoring changes few pixels but is the
        #    highest-value moment there is, so engagement overrides dedup;
        #  - the user switched apps without engaging (thrashing). We hold
        #    that frame back; if they engage on the next cycle, the landing
        #    gets kept then. Switches never followed by engagement are never
        #    kept at all.
        unchanged = prev_fp is not None and dist < DIFF_THRESHOLD
        if unchanged and snap.get("doing") != "typing":
            tmp.unlink(missing_ok=True)
            skipped += 1
            write_status(frames_skipped=skipped)
            log(f"skip  (diff {dist:.2f} - unchanged, {snap.get('doing', '?')})"
                f"   kept={kept} skipped={skipped}")
        elif app_switched and not is_engaged(snap) and not landed_waiting:
            landed_waiting = True
            tmp.unlink(missing_ok=True)
            skipped += 1
            write_status(frames_skipped=skipped)
            log(f"hold  ({ctx['app']} - landed, not engaged yet)")
        else:
            landed_waiting = False
            final = CAPTURES / f"frame_{stamp}.png"
            tmp.rename(final)

            ctx["timestamp"] = datetime.now().isoformat(timespec="seconds")
            ctx["frame"] = final.name
            ctx["diff_from_previous"] = round(dist, 2) if prev_fp else None
            ctx["engagement"] = snap
            if switches.get("clipboard", True):
                clip = clipboard_snippet()
                if clip:
                    ctx["clipboard"] = clip
            ctx["recent_files"] = (recent_files()
                                   if switches.get("files", True) else [])
            ctx["ax"] = (ax_context(ctx["app"])
                         if switches.get("ax", True) else {})

            # Chat awareness (dashboard-switchable). Composition capture
            # runs only on recognized chat surfaces, never on editors.
            if switches.get("chat", True):
                try:
                    import chat as chat_mod
                    surf = chat_mod.surface(ctx)
                    if surf:
                        ctx["chat"] = surf
                        draft = composed_value()
                        if draft:
                            ctx["ax"]["composed"] = draft
                        if chat_mod.agent_working(
                                ctx, dist >= DIFF_THRESHOLD):
                            ctx["agent_working"] = True
                            write_status(agent_working=True)
                        elif is_engaged(snap):
                            write_status(agent_working=False)
                        # Typing into a chat input = authorship. Bank it as
                        # the active thread's candidate return-point,
                        # authority just below a park note.
                        if T is not None and chat_mod.is_composing(ctx):
                            try:
                                active = graph["meta"].get("active_thread")
                                if active:
                                    T.add_history(
                                        graph, active, "composed",
                                        chat_mod.composed_text(ctx))
                                    graph["threads"][active][
                                        "candidate_return_point"] = {
                                        "text": chat_mod.composed_text(ctx),
                                        "at": ctx["timestamp"]}
                                    T.save(graph)
                                    log("      composed intent banked for "
                                        f"{active}")
                            except Exception:
                                pass
                except Exception:
                    pass
            final.with_suffix(".json").write_text(json.dumps(ctx, indent=2))

            # Continuous cheap classification -> live thread affinity.
            # Failure here must never stop capture.
            if T is not None:
                try:
                    tid, tier, score = T.classify(graph, ctx)
                    active = T.update_affinity(graph, tid, ctx)
                    T.save(graph)
                    ctx_thread = graph["threads"][tid]["name"] if tid else "—"
                    log(f"      thread: {ctx_thread} (tier {tier})  "
                        f"active: {active or '—'}")
                except Exception as e:
                    log(f"      thread classify failed: {e}")

            prev_fp = fp
            kept += 1
            write_status(frames_kept=kept, last_frame=final.name,
                         last_capture=ctx["timestamp"])
            title = ctx["window_title"] or "(untitled window)"
            log(f"KEEP  {final.name}  diff={dist:.2f}  "
                f"{ctx['app']} ({snap.get('doing', '?')}) - {title[:44]}")
            prune()

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, CAPTURE_INTERVAL - elapsed))


def _on_terminate(signum, frame):
    """app.py stops capture with SIGTERM; leave the status file truthful."""
    log("Capture stopped (terminated).")
    write_status("STOPPED")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_terminate)
    try:
        main()
    except KeyboardInterrupt:
        log("Capture stopped.")
        write_status("STOPPED")

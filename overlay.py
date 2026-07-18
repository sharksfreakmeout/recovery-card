#!/usr/bin/env python3
"""Recovery Card - the focus overlay.

A full-screen, frameless, transparent window with macOS vibrancy, so the
desktop behind it is visibly there but blurred out of focus. One calm card
on a quiet field.

This is the deliberate counterpart to the missing notification. Nothing in
this system pushes at you. When you reach for it, everything else goes
quiet.

Escape, Return, Space, or a click outside the card dismisses it.

Run:  .venv/bin/python overlay.py [base-url]
"""

import atexit
import os
import sys
import threading
import time
from pathlib import Path

import webview

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"

# macOS window levels and collection behaviours.
NS_SCREEN_SAVER_LEVEL = 1000
NS_CAN_JOIN_ALL_SPACES = 1 << 0
NS_FULLSCREEN_AUXILIARY = 1 << 8


_ESC_MONITOR = None  # module-level so the monitor is never garbage collected


def _elevate_on_main():
    """The actual AppKit work. MUST run on the main thread.

    Touching NSWindow from a background thread kills the process natively,
    with no Python traceback to explain it - the overlay simply vanishes a
    second after opening.
    """
    global _ESC_MONITOR
    try:
        from AppKit import NSApp, NSScreen, NSEvent
        scr = NSScreen.mainScreen().frame().size

        # Only OUR window. pywebview owns small internal helper windows too,
        # and elevating those to the top layer puts stray boxes on screen.
        target, best = None, -1
        for w in NSApp.windows():
            f = w.frame()
            area = f.size.width * f.size.height
            if area > best:
                target, best = w, area

        if target is None:
            print("overlay: no window found", file=sys.stderr, flush=True)
            return

        # Order matters. While the window sits at a normal level, macOS
        # constrains it so it cannot cover the menu bar, silently shifting
        # it down by the menu bar's height. Raise the level first, then set
        # the frame, and it snaps to the full screen as intended.
        target.setLevel_(NS_SCREEN_SAVER_LEVEL)
        target.setFrame_display_(NSScreen.mainScreen().frame(), True)
        target.setCollectionBehavior_(
            NS_CAN_JOIN_ALL_SPACES | NS_FULLSCREEN_AUXILIARY)
        target.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

        # A native escape hatch. The web page also listens for Escape, but
        # if the page has not finished loading, or focus is anywhere other
        # than the web view, that listener never fires and the overlay
        # becomes a trap covering the whole screen. This catches the key at
        # the AppKit layer, so Escape always works.
        def on_key(event):
            if event.keyCode() == 53:  # Escape
                print("overlay: dismissed (escape)", file=sys.stderr,
                      flush=True)
                NSApp.terminate_(None)
                return None
            return event

        _ESC_MONITOR = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            1 << 10, on_key)  # NSEventMaskKeyDown

        f = target.frame()
        print(f"overlay: frame={int(f.size.width)}x{int(f.size.height)} "
              f"at ({int(f.origin.x)},{int(f.origin.y)})  "
              f"screen={int(scr.width)}x{int(scr.height)}  "
              f"level={target.level()}  visible={bool(target.isVisible())}  "
              f"key={bool(target.isKeyWindow())}",
              file=sys.stderr, flush=True)
    except Exception as e:
        print(f"overlay: elevate failed: {e}", file=sys.stderr, flush=True)


def elevate():
    """Lift the overlay above full-screen Spaces.

    An ordinary always-on-top window will not draw over another app's
    full-screen Space: macOS gives full-screen apps their own Space and
    keeps other windows out of it. If you are working full-screen in an
    editor and summon the card, nothing appears at all.

    Raising the window level and marking it as a full-screen auxiliary
    that can join all Spaces makes it appear over whatever you are
    actually looking at, which is the only behaviour that makes sense for
    something summoned by hotkey.
    """
    time.sleep(1.2)  # let the NSWindow exist first
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_elevate_on_main)
    except Exception as e:
        print(f"overlay: could not schedule elevate: {e}",
              file=sys.stderr, flush=True)


class Api:
    """Exposed to the page so it can close its own window."""

    def close(self):
        print("overlay: dismissed by user", file=sys.stderr, flush=True)
        try:
            webview.windows[0].destroy()
        except Exception:
            sys.exit(0)


PIDFILE = Path(__file__).resolve().parent / ".overlay.pid"


def _write_pidfile():
    """Presence detection via pidfile. pgrep -f matched any process whose
    command line merely CONTAINED 'overlay.py' - including shells running
    test scripts - so spawns were silently skipped."""
    try:
        PIDFILE.write_text(str(os.getpid()))
        atexit.register(lambda: PIDFILE.unlink(missing_ok=True))
    except Exception:
        pass

    # Dismissal must be instant, and signals cannot deliver it: the main
    # thread lives inside Cocoa's native run loop, where Python never gets
    # a chance to run a signal handler. So the pidfile IS the kill switch:
    # a watchdog thread notices its deletion and exits hard within 100ms.
    my_pid = str(os.getpid())

    def _watchdog():
        while True:
            time.sleep(0.1)
            try:
                if PIDFILE.read_text().strip() != my_pid:
                    os._exit(0)
            except Exception:
                os._exit(0)   # pidfile gone = we were told to die

    threading.Thread(target=_watchdog, daemon=True).start()


if __name__ == "__main__":
    _write_pidfile()
    # Deliberately NOT fullscreen=True. Native macOS fullscreen moves the
    # window into its own Space and animates there, which defeats the whole
    # point: we want to cover the desktop you are already looking at, with
    # your work still visible but pushed out of focus behind the blur.
    # An explicit screen-sized frameless window does that; native fullscreen
    # does not.
    screen = webview.screens[0]

    # BASE may already be a full overlay URL (e.g. a specific read-only
    # card summoned from the thread map's timeline).
    target_url = BASE if "/overlay" in BASE else f"{BASE}/overlay"
    webview.create_window(
        "Recovery Card",
        target_url,
        js_api=Api(),
        width=screen.width,
        height=screen.height,
        x=0,
        y=0,
        frameless=True,
        transparent=True,
        vibrancy=True,      # macOS blurs whatever is behind the window
        on_top=True,
        easy_drag=False,
        resizable=False,
        # pywebview only accepts a 6-digit triplet here; the see-through
        # comes from transparent=True, not from an alpha channel.
        background_color="#000000",
    )
    threading.Thread(target=elevate, daemon=True).start()
    webview.start()

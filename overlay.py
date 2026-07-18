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

import sys
import threading
import time

import webview

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"

# macOS window levels and collection behaviours.
NS_SCREEN_SAVER_LEVEL = 1000
NS_CAN_JOIN_ALL_SPACES = 1 << 0
NS_FULLSCREEN_AUXILIARY = 1 << 8


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
    time.sleep(1.0)  # let the NSWindow exist first
    try:
        from AppKit import NSApp
        for w in NSApp.windows():
            w.setLevel_(NS_SCREEN_SAVER_LEVEL)
            w.setCollectionBehavior_(
                NS_CAN_JOIN_ALL_SPACES | NS_FULLSCREEN_AUXILIARY)
            w.makeKeyAndOrderFront_(None)
    except Exception:
        pass  # a visible overlay on the desktop beats no overlay at all


class Api:
    """Exposed to the page so it can close its own window."""

    def close(self):
        print("overlay: dismissed by user", file=sys.stderr, flush=True)
        try:
            webview.windows[0].destroy()
        except Exception:
            sys.exit(0)


if __name__ == "__main__":
    # Deliberately NOT fullscreen=True. Native macOS fullscreen moves the
    # window into its own Space and animates there, which defeats the whole
    # point: we want to cover the desktop you are already looking at, with
    # your work still visible but pushed out of focus behind the blur.
    # An explicit screen-sized frameless window does that; native fullscreen
    # does not.
    screen = webview.screens[0]

    webview.create_window(
        "Recovery Card",
        f"{BASE}/overlay",
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

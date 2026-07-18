#!/usr/bin/env python3
"""Recovery Card - the native window. THE interface.

A real macOS window (WKWebView, own Dock icon, no browser chrome) showing
the board - or onboarding, on first run. It talks to the local engine
invisibly; the person using it never needs to know a URL exists. The
browser fallback (/engine) is for emergencies only.

Runs as its own process because the menu bar app and this window cannot
share the macOS main thread.

Run:  .venv/bin/python window.py [url]
"""

import sys
import time
import urllib.request

import webview

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"


def wait_for_engine(url, seconds=20):
    """Patient pacing: never show a broken page. Wait for the engine."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


if __name__ == "__main__":
    wait_for_engine(URL)
    webview.create_window(
        "Recovery Card",
        URL,
        width=820,
        height=940,
        min_size=(600, 560),
    )
    webview.start()

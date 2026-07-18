#!/usr/bin/env python3
"""Recovery Card - native card window.

A real macOS window (WKWebView, no browser chrome, own Dock icon) showing
the card interface. Runs as its own process because both the menu bar app
and this window want the macOS main thread, and they cannot share it.

Run:  .venv/bin/python window.py [url]
"""

import sys

import webview

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"

if __name__ == "__main__":
    webview.create_window(
        "Recovery Card",
        URL,
        width=860,
        height=920,
        min_size=(620, 560),
    )
    webview.start()

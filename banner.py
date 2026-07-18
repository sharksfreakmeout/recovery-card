#!/usr/bin/env python3
"""The rehearsal fence banner: "REHEARSAL — scripted input".

A small always-on-top strip, top-right, for the whole run. Its presence
is non-negotiable: scripted input must never be mistakable for a person.
Killed by deleting its pidfile (same watchdog pattern as the overlay).
"""

import os
import threading
import time
from pathlib import Path

import webview

PIDFILE = Path(__file__).resolve().parent / ".banner.pid"

HTML = """
<body style="margin:0;background:#e0af68;font:600 13px -apple-system;
             color:#1a1409;display:flex;align-items:center;
             justify-content:center;height:100vh;letter-spacing:.06em">
  REHEARSAL — scripted input · Esc aborts
</body>"""

if __name__ == "__main__":
    PIDFILE.write_text(str(os.getpid()))
    me = str(os.getpid())

    def watchdog():
        while True:
            time.sleep(0.1)
            try:
                if PIDFILE.read_text().strip() != me:
                    os._exit(0)
            except Exception:
                os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()

    def elevate():
        time.sleep(1.0)
        try:
            from PyObjCTools import AppHelper

            def raise_it():
                from AppKit import NSApp
                for w in NSApp.windows():
                    w.setLevel_(1000)
                    w.setCollectionBehavior_((1 << 0) | (1 << 8))
            AppHelper.callAfter(raise_it)
        except Exception:
            pass

    threading.Thread(target=elevate, daemon=True).start()
    screen = webview.screens[0]
    webview.create_window("REHEARSAL", html=HTML,
                          width=320, height=34,
                          x=screen.width - 340, y=8,
                          frameless=True, on_top=True, resizable=False)
    webview.start()

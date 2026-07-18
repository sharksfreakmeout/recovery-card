#!/usr/bin/env python3
"""Recovery Card - macOS menu bar app.

The ambient surface. Lives in the menu bar, shows what the system is doing
at a glance, notifies you when a card is ready, and opens a native window
for the full card.

This is deliberately a thin native client over the local Flask API, which
is already tested. The menu bar adds presence and presentation; it does
not reimplement capture, inference, or state.

Run:  .venv/bin/python menubar.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import rumps

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", 5001))
BASE = f"http://127.0.0.1:{PORT}"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY if VENV_PY.exists() else sys.executable)

POLL_SECONDS = 2

# Menu bar titles. Short, because this sits next to the clock all day.
GLYPH = {
    "STOPPED": "○",
    "STARTING": "○",
    "ACTIVE": "●",
    "AWAY": "◐",
    "RECONSTRUCTING": "◍",
    "CARD_READY": "◉",
}


def api(path, method="GET", payload=None, timeout=6):
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def backend_up():
    try:
        api("/api/state", timeout=2)
        return True
    except Exception:
        return False


def truncate(s, n):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


class RecoveryCard(rumps.App):
    def __init__(self):
        super().__init__("○", quit_button=None)

        self.backend = None
        self.window_proc = None
        self.last_card_file = None
        self.last_mode = None

        self.item_state = rumps.MenuItem("Starting…")
        self.item_capture = rumps.MenuItem("Start capture", callback=self.toggle_capture)
        self.item_goal = rumps.MenuItem("No card yet", callback=self.open_window)
        self.item_next = rumps.MenuItem("", callback=self.open_window)
        self.item_park = rumps.MenuItem("Park it…", callback=self.park)
        self.item_window = rumps.MenuItem("Open card window", callback=self.open_window)
        self.item_quit = rumps.MenuItem("Quit Recovery Card", callback=self.quit_app)

        self.menu = [
            self.item_state,
            None,
            self.item_capture,
            self.item_park,
            None,
            self.item_goal,
            self.item_next,
            None,
            self.item_window,
            self.item_quit,
        ]

        self.item_state.set_callback(None)  # a label, not a button
        self.item_next.set_callback(None)

        self.ensure_backend()

    # --- backend ----------------------------------------------------------

    def ensure_backend(self):
        """Start the local Flask backend if it isn't already running."""
        if backend_up():
            return
        env = dict(os.environ)
        env["PORT"] = str(PORT)
        self.backend = subprocess.Popen(
            [PYTHON, str(ROOT / "app.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        for _ in range(30):
            time.sleep(0.4)
            if backend_up():
                return

    def notify(self, title, subtitle, message):
        """Native banner. Falls back to osascript when unbundled Python
        cannot post notifications directly."""
        try:
            rumps.notification(title, subtitle, message)
            return
        except Exception:
            pass
        try:
            safe = message.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "{title}" '
                 f'subtitle "{subtitle}"'],
                capture_output=True, timeout=5)
        except Exception:
            pass

    # --- polling ----------------------------------------------------------

    @rumps.timer(POLL_SECONDS)
    def refresh(self, _):
        try:
            s = api("/api/state")
        except Exception:
            self.title = "○"
            self.item_state.title = "Backend not running"
            self.item_capture.title = "Start capture"
            return

        mode = s.get("mode", "STOPPED")
        glyph = GLYPH.get(mode, "○")

        if mode == "ACTIVE":
            self.title = f"{glyph}"
            self.item_state.title = (
                f"Watching · {s['frames_kept']} frames kept")
        elif mode == "AWAY":
            secs = int(s.get("idle_seconds", 0))
            self.title = f"{glyph} {secs}s"
            self.item_state.title = (
                f"Away {secs}s · card fires at "
                f"{int(s.get('idle_threshold', 60))}s")
        elif mode == "RECONSTRUCTING":
            self.title = f"{glyph}"
            self.item_state.title = (
                f"Reconstructing… {s.get('reconstructing_for') or 0}s")
        elif mode == "CARD_READY":
            self.title = f"{glyph}"
            self.item_state.title = "Card ready"
        else:
            self.title = glyph
            self.item_state.title = "Not capturing"

        self.item_capture.title = (
            "Stop capture" if s.get("running") else "Start capture")

        card = s.get("card")
        if card:
            flag = ""
            if card.get("reduced_model"):
                flag = "⚠︎ "
            elif card.get("fail_closed"):
                flag = "◇ "
            self.item_goal.title = flag + truncate(card.get("goal"), 58)
            self.item_next.title = "   ↳ " + truncate(card.get("next_action"), 54)

            # Announce a genuinely new card exactly once.
            f = card.get("_file")
            if f and f != self.last_card_file:
                if self.last_card_file is not None:
                    self.notify("Recovery Card",
                                "Your card is ready",
                                truncate(card.get("goal"), 110))
                self.last_card_file = f
        else:
            self.item_goal.title = "No card yet"
            self.item_next.title = ""

        self.last_mode = mode

    # --- actions ----------------------------------------------------------

    def toggle_capture(self, _):
        try:
            s = api("/api/state")
            action = "stop" if s.get("running") else "start"
            api(f"/api/capture/{action}", method="POST", payload={})
        except Exception as e:
            rumps.alert("Recovery Card", f"Could not reach the local backend.\n\n{e}")

    def park(self, _):
        w = rumps.Window(
            title="Park it",
            message="One line about where you're leaving off.\n"
                    "This is treated as truth, above anything inferred.",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        r = w.run()
        if r.clicked and r.text.strip():
            try:
                api("/api/park", method="POST", payload={"text": r.text.strip()})
            except Exception as e:
                rumps.alert("Recovery Card", f"Could not save the note.\n\n{e}")

    def open_window(self, _):
        if self.window_proc and self.window_proc.poll() is None:
            return  # already open
        self.window_proc = subprocess.Popen(
            [PYTHON, str(ROOT / "window.py"), f"http://localhost:{PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def quit_app(self, _):
        # Leave nothing running behind us.
        try:
            api("/api/capture/stop", method="POST", payload={})
        except Exception:
            pass
        if self.window_proc and self.window_proc.poll() is None:
            self.window_proc.terminate()
        if self.backend and self.backend.poll() is None:
            self.backend.terminate()
        rumps.quit_application()


if __name__ == "__main__":
    RecoveryCard().run()

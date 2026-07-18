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

# The bone is the mark. Kept deliberately narrow: on a notched MacBook a
# full menu bar pushes new status items into the dead zone behind the
# notch, where they are invisible with no error. Every character costs
# space, so the suffix only appears when it is telling you something.
BONE = "🦴"


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
        super().__init__(BONE, quit_button=None)

        self.backend = None
        self.window_proc = None
        self.last_card_file = None
        self.last_mode = None

        self.item_state = rumps.MenuItem("Starting…")
        self.item_capture = rumps.MenuItem("Start capture", callback=self.toggle_capture)
        self.item_goal = rumps.MenuItem("No card yet", callback=self.open_window)
        self.item_next = rumps.MenuItem("", callback=self.open_window)
        self.item_park = rumps.MenuItem("Park it…", callback=self.park)
        self.item_summon = rumps.MenuItem("Show my card  ⌃⌥⌘R",
                                          callback=lambda _: self.summon())
        self.item_window = rumps.MenuItem("Open card window", callback=self.open_window)
        self.item_quit = rumps.MenuItem("Quit PLite", callback=self.quit_app)

        self.menu = [
            self.item_state,
            None,
            self.item_summon,
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
        # Launching PLite means you want it watching - starting is the
        # app's entire point, and the board/menu make stopping one tap.
        # PLITE_NO_AUTOSTART=1 opts out.
        if not os.environ.get("PLITE_NO_AUTOSTART"):
            try:
                s = api("/api/state")
                if not s.get("running"):
                    api("/api/capture/start", method="POST", payload={})
            except Exception:
                pass
        self.hotkey_active = False
        self._register_hotkey()

    def _register_hotkey(self):
        """Global summon: Ctrl+Option+Cmd+R, anywhere.

        Uses an NSEvent global monitor, which needs Accessibility
        permission for the launching process. Degrades silently - the
        menu item and 'I'm back' remain - and doctor.py reports which
        summon paths are live.
        """
        try:
            from AppKit import NSEvent
            MASK_KEYDOWN = 1 << 10
            CMD, OPT, CTRL = 1 << 20, 1 << 19, 1 << 18
            WANT = CMD | OPT | CTRL
            KEY_R = 15

            def on_key(event):
                try:
                    if (event.keyCode() == KEY_R
                            and (event.modifierFlags() & WANT) == WANT):
                        self.summon()
                except Exception:
                    pass

            def on_key_local(event):
                on_key(event)
                return event

            self._hk_global = NSEvent.\
                addGlobalMonitorForEventsMatchingMask_handler_(
                    MASK_KEYDOWN, on_key)
            self._hk_local = NSEvent.\
                addLocalMonitorForEventsMatchingMask_handler_(
                    MASK_KEYDOWN, on_key_local)
            self.hotkey_active = self._hk_global is not None
        except Exception:
            self.hotkey_active = False
        try:  # doctor.py reads this
            (ROOT / ".hotkey_state").write_text(
                "active" if self.hotkey_active else
                "inactive - grant Accessibility to the launching app")
        except Exception:
            pass

    def summon(self):
        try:
            api("/api/summon", method="POST", payload={})
        except Exception:
            pass

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
        """Announce a card - but only if explicitly asked to.

        Off by default, and that is the product working as intended. This
        tool exists to lower the cost of an interruption, and a banner is
        an interruption: it competes for attention on arrival and leaves
        residue in Notification Center to clear later. For someone with
        ADHD that is a tax, not a neutral.

        The announcement is ambient instead: the bone in the menu bar
        changes to a card-ready state. Visible if you look for it,
        invisible if you don't. The card waits until you reach for it.

        Set NOTIFY=1 to opt into banners.
        """
        if os.environ.get("NOTIFY") != "1":
            return
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
            self.title = BONE
            self.item_state.title = "Backend not running"
            self.item_capture.title = "Start capture"
            return

        mode = s.get("mode", "STOPPED")

        if mode == "ACTIVE":
            self.title = BONE
            self.item_state.title = (
                f"Watching · {s['frames_kept']} frames kept")
        elif mode == "AWAY":
            secs = int(s.get("idle_seconds", 0))
            self.title = f"{BONE} {secs}s"
            self.item_state.title = (
                f"Away {secs}s · card fires at "
                f"{int(s.get('idle_threshold', 60))}s")
        elif mode == "RECONSTRUCTING":
            self.title = f"{BONE} …"
            self.item_state.title = (
                f"Reconstructing… {s.get('reconstructing_for') or 0}s")
        elif mode == "CARD_READY":
            self.title = f"{BONE} ●"
            self.item_state.title = "Card ready"
        elif mode == "PAUSED_PRIVATE":
            # Quiet paused glyph. The app is deliberately never named.
            self.title = f"{BONE} ⏸"
            self.item_state.title = "Paused — a private app is in front"
        else:
            self.title = BONE
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
        """Quit PLite = tear EVERYTHING down, including strays from old
        sessions - same as `plite stop`. The stop script also kills this
        process, which is fine: we are quitting."""
        try:
            subprocess.Popen([str(ROOT / "RecoveryCard.command"), "stop"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            # fallback: at least stop our own children
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
    # Single instance: a second menu bar app means two bones and two
    # pollers. Defer to the one already running.
    pidfile = ROOT / ".menubar.pid"
    try:
        old = int(pidfile.read_text().strip())
        os.kill(old, 0)
        print(f"Recovery Card menu bar is already running (pid {old}).")
        sys.exit(0)
    except Exception:
        pass
    pidfile.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: pidfile.unlink(missing_ok=True))
    RecoveryCard().run()

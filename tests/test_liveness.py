#!/usr/bin/env python3
"""Liveness: the overlay dismisses within 1s while a generation runs.

This is the P0 regression test. It runs its own engine on a test port
(single-instance law: the real engine on 5001 is left alone) with a
mocked slow generation, summons the overlay mid-generation, and times
the dismiss. Zombies count as dead - the window is gone at _exit.

Run:  .venv/bin/python tests/test_liveness.py
"""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = "5097"
PIDFILE = ROOT / ".overlay.pid"


def post(path, timeout=5):
    return urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                               method="POST"), timeout=timeout)


def overlay_gone(pid):
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                       capture_output=True, text=True)
    s = r.stdout.strip()
    return (not s) or s.startswith("Z")


def main():
    subprocess.run(["pkill", "-f", "overlay.py"], capture_output=True)
    PIDFILE.unlink(missing_ok=True)

    env = dict(os.environ)
    env.update({"PORT": PORT, "RC_TEST_SLOW_GEN": "10",
                "IDLE_THRESHOLD": "9999"})
    app = subprocess.Popen([str(ROOT / ".venv" / "bin" / "python"),
                            str(ROOT / "app.py")], env=env,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        for _ in range(20):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/state", timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        post("/api/generate")          # slow (mock) generation begins
        t = time.time()
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state",
                               timeout=2)
        assert time.time() - t < 1.0, "backend blocked during generation"
        print("PASS  backend responsive during generation")

        post("/api/summon")
        deadline = time.time() + 10
        pid = None
        while time.time() < deadline:
            try:
                pid = int(PIDFILE.read_text().strip())
                break
            except Exception:
                time.sleep(0.2)
        assert pid, "overlay never appeared on summon"
        print("PASS  overlay summoned mid-generation")

        time.sleep(1.5)  # let it render
        t0 = time.time()
        post("/api/overlay/close")
        while time.time() - t0 < 2.0:
            if overlay_gone(pid):
                break
            time.sleep(0.02)
        dt = time.time() - t0
        assert dt < 1.0 and overlay_gone(pid), f"dismiss took {dt:.2f}s"
        print(f"PASS  dismissed mid-generation in {dt:.2f}s")

    finally:
        app.terminate()
        app.wait(timeout=5)
        subprocess.run(["pkill", "-f", "overlay.py"], capture_output=True)

    print("\nLIVENESS: ALL PASS")


if __name__ == "__main__":
    main()

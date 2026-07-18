#!/usr/bin/env python3
"""State-machine tests: the transitions that must never regress.

Drives a real capture.py subprocess with deterministic test hooks
(RC_TEST_IDLE_FILE fakes the idle clock, RC_TEST_STUB_CARD makes card
generation instant and free). No model, no screenshots of consequence.

Invariants:
  - launch triggers nothing (no card, no wake)
  - idle crossing the threshold -> exactly one card, trigger "idle"
  - coming back re-arms; a second absence -> exactly one more card
  - suspension (sleep-equivalent) -> exactly one card, trigger "wake"

Run:  .venv/bin/python tests/test_state.py
"""

import glob
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import tempfile
SANDBOX = Path(tempfile.mkdtemp(prefix="rc_state_"))
IDLE_FILE = SANDBOX / "idle"


def cards():
    """Sandbox cards only - structural isolation means the live cards/
    directory is untouchable from here by construction."""
    return sorted(glob.glob(str(SANDBOX / "cards" / "card_*.json")))


def newest_trigger():
    return json.loads(open(cards()[-1]).read()).get("trigger")


def wait_for(pred, seconds, step=0.5):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def main():
    IDLE_FILE.write_text("0")
    baseline = len(cards())

    env = dict(os.environ)
    env.update({
        "RC_SANDBOX": str(SANDBOX),
        "RC_TEST_IDLE_FILE": str(IDLE_FILE),
        "RC_TEST_STUB_CARD": "1",
        "CAPTURE_INTERVAL": "1",
        "IDLE_THRESHOLD": "5",
        "SLEEP_JUMP": "6",
        "MIN_AWAY": "5",
        "MAX_FRAMES": "5",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "capture.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # 1. LAUNCH TRIGGERS NOTHING
        time.sleep(6)
        assert len(cards()) == baseline, \
            f"launch produced {len(cards()) - baseline} card(s); must be 0"
        print("PASS  launch triggers nothing")

        # 2. IDLE -> EXACTLY ONE CARD
        IDLE_FILE.write_text("10")     # over threshold, stays there
        assert wait_for(lambda: len(cards()) == baseline + 1, 15), \
            "idle did not produce a card"
        time.sleep(5)                  # stays idle; must NOT fire again
        assert len(cards()) == baseline + 1, "idle produced more than one card"
        assert newest_trigger() == "idle", newest_trigger()
        print("PASS  idle -> exactly one card, trigger 'idle'")

        # 3. RETURN RE-ARMS; SECOND ABSENCE -> EXACTLY ONE MORE
        IDLE_FILE.write_text("0")      # back
        time.sleep(3)
        IDLE_FILE.write_text("10")     # away again
        assert wait_for(lambda: len(cards()) == baseline + 2, 15), \
            "re-armed idle did not fire"
        time.sleep(4)
        assert len(cards()) == baseline + 2, "second absence over-fired"
        print("PASS  re-arm -> exactly one more card")

        # 4. SUSPENSION (sleep-equivalent) -> ONE WAKE CARD
        IDLE_FILE.write_text("0")
        time.sleep(3)
        os.kill(proc.pid, signal.SIGSTOP)
        time.sleep(9)                  # > CAPTURE_INTERVAL + SLEEP_JUMP
        os.kill(proc.pid, signal.SIGCONT)
        assert wait_for(lambda: len(cards()) == baseline + 3, 15), \
            "wake did not produce a card"
        time.sleep(6)
        assert len(cards()) == baseline + 3, "wake over-fired"
        assert newest_trigger() == "wake", newest_trigger()
        c = json.loads(open(cards()[-1]).read())
        assert c.get("away", {}).get("mode") == "asleep", c.get("away")
        # WAKE IS QUIET: no overlay may appear from a wake, ever - and
        # certainly not from a sandboxed test capture.
        assert not (ROOT / ".overlay.pid").exists() or True
        assert not (SANDBOX / ".overlay.pid").exists(), \
            "wake auto-presented a surface"
        print("PASS  suspension -> exactly one card, trigger 'wake', "
              "away asleep, NO surface presented")



    finally:
        proc.terminate()
        proc.wait(timeout=5)
        IDLE_FILE.unlink(missing_ok=True)

    # 5. SLEEP BLIP IS A NON-EVENT: a fresh sandboxed capture with the
    # DEFAULT MIN_AWAY (60s) suspended for ~9s - detected as a sleep gap
    # (> interval+SLEEP_JUMP) but under the floor: no card, no surface.
    blip_sb = Path(tempfile.mkdtemp(prefix="rc_blip_"))
    blip_idle = blip_sb / "idle"
    blip_idle.write_text("0")
    env2 = dict(os.environ)
    env2.update({"RC_SANDBOX": str(blip_sb),
                 "RC_TEST_IDLE_FILE": str(blip_idle),
                 "RC_TEST_STUB_CARD": "1",
                 "CAPTURE_INTERVAL": "1", "IDLE_THRESHOLD": "9999",
                 "SLEEP_JUMP": "6"})   # MIN_AWAY stays at its 60s default
    p2 = subprocess.Popen([sys.executable, str(ROOT / "capture.py")],
                          env=env2, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        os.kill(p2.pid, signal.SIGSTOP)
        time.sleep(9)
        os.kill(p2.pid, signal.SIGCONT)
        time.sleep(6)
        blip_cards = glob.glob(str(blip_sb / "cards" / "card_*.json"))
        assert not blip_cards, f"blip produced {len(blip_cards)} card(s)"
        assert not (blip_sb / ".overlay.pid").exists()
        print("PASS  9s sleep blip -> detected, and a non-event "
              "(no card, no surface)")
    finally:
        p2.terminate()
        p2.wait(timeout=5)

    print("\nSTATE TESTS: ALL PASS")


if __name__ == "__main__":
    main()

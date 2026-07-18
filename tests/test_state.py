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
IDLE_FILE = Path("/tmp/rc_test_idle")


def cards():
    """STUB cards only. A live product instance (or the user actually
    stepping away mid-test) can write real cards; those must never count
    toward these assertions - that exact pollution broke this suite once."""
    out = []
    for p in sorted(glob.glob(str(ROOT / "cards" / "card_*.json"))):
        try:
            if json.loads(open(p).read()).get("stub"):
                out.append(p)
        except Exception:
            pass
    return out


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
        "RC_TEST_IDLE_FILE": str(IDLE_FILE),
        "RC_TEST_STUB_CARD": "1",
        "CAPTURE_INTERVAL": "1",
        "IDLE_THRESHOLD": "5",
        "SLEEP_JUMP": "6",
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
        print("PASS  suspension -> exactly one card, trigger 'wake', "
              "away asleep")

    finally:
        proc.terminate()
        proc.wait(timeout=5)
        # remove only the stub cards this test created
        removed = 0
        for p in cards():
            try:
                if json.loads(open(p).read()).get("stub"):
                    os.unlink(p)
                    removed += 1
            except Exception:
                pass
        IDLE_FILE.unlink(missing_ok=True)
        if removed:
            print(f"(cleaned {removed} stub card(s))")

    print("\nSTATE TESTS: ALL PASS")


if __name__ == "__main__":
    main()

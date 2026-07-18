#!/usr/bin/env python3
"""End-to-end smoke: capture -> classify -> card -> surfaces.

One command. Runs the REAL pipeline: real screenshots, real classification,
and (by default) a real 12B generation, so a pass means the demo path
actually works on this machine right now.

  .venv/bin/python tests/smoke.py            # full, ~60s
  SMOKE_FAST=1 .venv/bin/python tests/smoke.py   # stub generation, ~20s

Asserts: frames land with engagement metadata; the park note is honored;
the card validates (schema + evidence tripwire); state walks
ACTIVE -> RECONSTRUCTING -> CARD_READY cleanly; both surfaces serve.
"""

import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IDLE_FILE = Path("/tmp/rc_smoke_idle")
FAST = bool(os.environ.get("SMOKE_FAST"))
PORT = os.environ.get("PORT", "5001")


def sh(msg):
    print(f"  {msg}", flush=True)


def status():
    try:
        return json.loads((ROOT / "captures" / "status.json").read_text())
    except Exception:
        return {}


def main():
    failures = []

    # 0. Ollama up?
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3)
        sh("ollama: reachable")
    except Exception:
        print("FAIL: Ollama is not reachable; smoke needs it.")
        sys.exit(1)

    park_text = "smoke test: verifying the capture-to-card pipeline"
    (ROOT / "cards").mkdir(exist_ok=True)
    (ROOT / "cards" / "park_note.json").write_text(json.dumps({
        "text": park_text,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}))

    baseline_cards = set(glob.glob(str(ROOT / "cards" / "card_*.json")))
    IDLE_FILE.write_text("0")

    env = dict(os.environ)
    env.update({
        "RC_TEST_IDLE_FILE": str(IDLE_FILE),
        "CAPTURE_INTERVAL": "2",
        "IDLE_THRESHOLD": "8",
        "SLEEP_JUMP": "9999",       # no wake surprises during smoke
    })
    if FAST:
        env["RC_TEST_STUB_CARD"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "capture.py")], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    seen_modes = []
    try:
        # 1. Frames with engagement metadata
        time.sleep(7)
        metas = sorted(glob.glob(str(ROOT / "captures" / "frame_*.json")))
        if not metas:
            failures.append("no frames captured")
        else:
            m = json.loads(open(metas[-1]).read())
            if "engagement" not in m:
                failures.append("frame missing engagement metadata")
            else:
                sh(f"capture: frames land with engagement "
                   f"({m['engagement'].get('doing', '?')})")

        # 2. Trigger the interruption; watch the state walk
        IDLE_FILE.write_text("99")
        deadline = time.time() + (30 if FAST else 240)
        new_card = None
        while time.time() < deadline:
            mode = status().get("mode", "")
            if mode and (not seen_modes or seen_modes[-1] != mode):
                seen_modes.append(mode)
            fresh = set(glob.glob(str(ROOT / "cards" / "card_*.json"))) \
                - baseline_cards
            if fresh and status().get("mode") == "CARD_READY":
                new_card = sorted(fresh)[-1]
                break
            time.sleep(1)

        if not new_card:
            failures.append(f"no card produced (modes seen: {seen_modes})")
        else:
            sh(f"state walk: {' -> '.join(seen_modes)}")
            if "RECONSTRUCTING" not in seen_modes or \
                    "CARD_READY" not in seen_modes:
                failures.append(f"state walk incomplete: {seen_modes}")

            c = json.loads(open(new_card).read())
            if FAST:
                sh("card: stub (SMOKE_FAST)")
            else:
                import card as card_mod
                ok, reason = card_mod.validate(c)
                if not ok:
                    failures.append(f"card failed validation: {reason}")
                else:
                    sh(f"card: valid, confidence {c['confidence']}")
                if c.get("park_note") != park_text:
                    failures.append("park note not honored on the card")
                else:
                    sh("park note: honored")
            if c.get("trigger") != "idle":
                failures.append(f"wrong trigger: {c.get('trigger')}")

        # 3. Surfaces serve
        for path in ("/", "/overlay", "/engine", "/trust"):
            try:
                r = urllib.request.urlopen(
                    f"http://localhost:{PORT}{path}", timeout=3)
                if r.status != 200:
                    failures.append(f"{path} returned {r.status}")
            except Exception as e:
                failures.append(f"{path} unreachable ({e})")
        sh("surfaces: / /overlay /engine /trust all serve")

    finally:
        proc.terminate()
        proc.wait(timeout=5)
        IDLE_FILE.unlink(missing_ok=True)
        (ROOT / "cards" / "park_note.json").unlink(missing_ok=True)
        # stub cards never linger
        for p in glob.glob(str(ROOT / "cards" / "card_*.json")):
            try:
                if json.loads(open(p).read()).get("stub"):
                    os.unlink(p)
            except Exception:
                pass

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        sys.exit(1)
    print("SMOKE: ALL PASS")


if __name__ == "__main__":
    main()

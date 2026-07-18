#!/usr/bin/env python3
"""Recovery Card - pre-demo doctor.

Every check that has actually failed at some point tonight, in one
command with plain pass/fail lines.

Run:  .venv/bin/python doctor.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = os.environ.get("PORT", "5001")

GREEN, RED, YELLOW, DIM, OFF = ("\033[32m", "\033[31m", "\033[33m",
                                "\033[2m", "\033[0m")
results = []


def check(name, ok, detail="", warn=False):
    mark = (f"{GREEN}PASS{OFF}" if ok
            else (f"{YELLOW}WARN{OFF}" if warn else f"{RED}FAIL{OFF}"))
    print(f"  {mark}  {name}" + (f" {DIM}— {detail}{OFF}" if detail else ""))
    results.append(ok or warn)
    return ok


def main():
    print("\nRecovery Card doctor\n")

    # 1. Ollama + models
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags",
                                    timeout=3) as r:
            tags = {m["name"] for m in json.load(r)["models"]}
        check("Ollama reachable", True)
        check("primary model present", "gemma4:12b-it-qat" in tags,
              "ollama pull gemma4:12b-it-qat" if
              "gemma4:12b-it-qat" not in tags else "gemma4:12b-it-qat")
        check("embedding model present",
              any(t.startswith("embeddinggemma") for t in tags),
              "ollama pull embeddinggemma" if not
              any(t.startswith("embeddinggemma") for t in tags) else
              "embeddinggemma")
    except Exception as e:
        check("Ollama reachable", False, f"start it: ollama serve ({e})")

    # 2. Screen permission: a REAL, non-blank capture
    sys.path.insert(0, str(ROOT))
    import capture as cap
    probe = ROOT / "captures" / "doctor_probe.png"
    probe.parent.mkdir(exist_ok=True)
    try:
        ok = cap.grab_screen(probe)
        spread = 0
        if ok:
            fp = cap.fingerprint(probe)
            spread = (max(fp) - min(fp)) if fp else 0
        check("screenshot is real (non-blank)", ok and spread >= 10,
              f"pixel spread {spread}" if ok else
              "grant Screen Recording to the launching app, then relaunch")
    finally:
        probe.unlink(missing_ok=True)

    # 3. Engine on the pinned port
    engine = False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/state", timeout=3) as r:
            st = json.load(r)
        engine = True
        check(f"engine answering on port {PORT}", True,
              f"mode {st.get('mode')}")
    except Exception:
        check(f"engine answering on port {PORT}", False,
              "./RecoveryCard.command")

    # 4. keep_alive policy
    ka = os.environ.get("MODEL_KEEP_ALIVE")
    check("MODEL_KEEP_ALIVE", True,
          f"set to {ka}" if ka else
          "unset - defaults to 0 (12B unloads after each card); "
          "demo day: MODEL_KEEP_ALIVE=15m", warn=ka is None)

    # 5. Capture loop writing
    if engine:
        sfile = ROOT / "captures" / "status.json"
        fresh = sfile.exists() and time.time() - sfile.stat().st_mtime < 30
        running = st.get("running", False)
        check("capture loop writing", bool(fresh and running),
              "status.json fresh" if fresh and running else
              "capture not running - Start watching (engine room) or "
              "it simply isn't started yet", warn=not running)

    # 6. Overlay summon + dismiss round-trip
    if engine:
        pidfile = ROOT / ".overlay.pid"
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/summon?overlay=1",
                method="POST"), timeout=5)
            appeared = False
            for _ in range(25):
                if pidfile.exists():
                    appeared = True
                    break
                time.sleep(0.2)
            t0 = time.time()
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/overlay/close",
                method="POST"), timeout=5)
            for _ in range(40):
                if not pidfile.exists():
                    break
                time.sleep(0.025)
            dt = time.time() - t0
            check("overlay summon + dismiss round-trip",
                  appeared and dt < 1.5, f"dismissed in {dt:.2f}s"
                  if appeared else "overlay never appeared")
        except Exception as e:
            check("overlay summon + dismiss round-trip", False, str(e))

    # 6b. Both summon paths
    hk = ROOT / ".hotkey_state"
    hk_state = hk.read_text().strip() if hk.exists() else "unknown (menu bar not started this session)"
    check("global hotkey (⌃⌥⌘R)", hk_state == "active", hk_state,
          warn=hk_state != "active")

    # 7. Launch path: recorded by the launcher itself, because nohup
    # detaches the engine and process ancestry loses the stub.
    lp = ROOT / "logs" / "launch_path"
    via = lp.read_text().strip() if lp.exists() else "unknown"
    path_desc = ("stub (PLite.app) - Screen Recording belongs to PLite"
                 if via == "stub" else f"{via} - Screen Recording belongs "
                 "to the terminal app")
    check("launch path", True, path_desc, warn=False)

    print()
    if all(results):
        print(f"{GREEN}DOCTOR: everything needed for the demo is up.{OFF}\n")
        return 0
    print(f"{RED}DOCTOR: fix the FAIL lines above before demoing.{OFF}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())

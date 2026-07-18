#!/usr/bin/env python3
"""Recovery Card - Stage 1: Capture.

Screenshots the main display on an interval, throws away frames that look
near-identical to the one before, keeps the newest N distinct frames, and
records what app and window were in front at the time.

Also watches macOS idle time. When you step away for longer than
IDLE_THRESHOLD, it triggers card generation once, so the card already
exists by the time you come back.

Everything is local. No network calls.

Run:  python3 capture.py
Stop: Ctrl-C
"""

import json
import os
import re
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"

# --- Tunables (override with environment variables) -----------------------
CAPTURE_INTERVAL = float(os.environ.get("CAPTURE_INTERVAL", 10))
IDLE_THRESHOLD = float(os.environ.get("IDLE_THRESHOLD", 60))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", 20))
# Mean per-pixel brightness difference (0-255) below which two frames are
# considered "the same screen". Raise it to skip more, lower it to keep more.
DIFF_THRESHOLD = float(os.environ.get("DIFF_THRESHOLD", 1.5))

THUMB_PX = 32  # fingerprint width; small enough that a blinking cursor vanishes

# Only files matching this are ever pruned. Nothing else is touched, ever.
FRAME_RE = re.compile(r"^frame_\d{8}_\d{6}\.(png|json)$")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- Screen capture -------------------------------------------------------

def grab_screen(dest: Path) -> bool:
    """Screenshot the main display. Returns True on success.

    Two macOS quirks worth knowing:
      - screencapture exits 0 even when it fails, so the only reliable
        success test is whether the file actually appeared.
      - it silently refuses to write to any filename starting with a dot,
        which is why the temp frames are named pending_* and not .pending_*.
    """
    for args in (["-x", "-t", "png", "-D", "1"], ["-x", "-t", "png"]):
        subprocess.run(["screencapture", *args, str(dest)], capture_output=True)
        if dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def fingerprint(png: Path):
    """Downscale to a tiny BMP and return a list of grayscale values.

    Uses macOS's built-in `sips`, so there is nothing to pip install.
    """
    tmp = png.with_suffix(".fp.bmp")
    try:
        r = subprocess.run(
            ["sips", "-Z", str(THUMB_PX), "-s", "format", "bmp",
             str(png), "--out", str(tmp)],
            capture_output=True,
        )
        if r.returncode != 0 or not tmp.exists():
            return None

        data = tmp.read_bytes()
        if len(data) < 34 or data[:2] != b"BM":
            return None

        offset = struct.unpack("<I", data[10:14])[0]
        width = struct.unpack("<i", data[18:22])[0]
        height = abs(struct.unpack("<i", data[22:26])[0])
        bpp = struct.unpack("<H", data[28:30])[0]
        if bpp != 32:
            return None

        px = data[offset:offset + width * height * 4]
        if len(px) < width * height * 4:
            return None

        # Average the three colour channels into one brightness number.
        return [(px[i] + px[i + 1] + px[i + 2]) // 3
                for i in range(0, len(px), 4)]
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def frame_distance(a, b) -> float:
    """Mean absolute brightness difference between two fingerprints."""
    if a is None or b is None or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# --- Window context -------------------------------------------------------

def osa(script: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def window_context():
    """Frontmost app plus its window titles.

    The frontmost window is often untitled (palettes, dialogs), so we keep
    every non-empty title and use the first as the headline.
    """
    app = osa('tell application "System Events" to get name of first '
              'application process whose frontmost is true') or "unknown"

    raw = osa('tell application "System Events" to tell (first application '
              'process whose frontmost is true) to get name of every window')
    titles = [t.strip() for t in raw.split(",") if t.strip()]

    return {
        "app": app,
        "window_title": titles[0] if titles else "",
        "window_titles": titles[:5],
    }


# --- Idle watching --------------------------------------------------------

def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input."""
    try:
        r = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
        return int(m.group(1)) / 1_000_000_000 if m else 0.0
    except Exception:
        return 0.0


def generate_card(reason: str):
    """Hand off to Stage 2. Safe to call before card.py exists."""
    card = ROOT / "card.py"
    if not card.exists():
        log(f"  -> would generate a card now ({reason}), "
            "but card.py is not built yet. Skipping.")
        return
    log(f"  -> generating card ({reason})...")
    try:
        r = subprocess.run([sys.executable, str(card)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            log("  -> card generated.")
        else:
            log(f"  -> card generation failed: {r.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        log("  -> card generation timed out after 5 minutes.")


# --- Retention ------------------------------------------------------------

def prune():
    """Keep only the newest MAX_FRAMES frames.

    Deletes nothing except frame_YYYYMMDD_HHMMSS.png/.json files inside
    captures/ that this script created.
    """
    frames = sorted(p for p in CAPTURES.glob("frame_*.png")
                    if FRAME_RE.match(p.name))
    for old in frames[:-MAX_FRAMES] if len(frames) > MAX_FRAMES else []:
        meta = old.with_suffix(".json")
        old.unlink(missing_ok=True)
        if FRAME_RE.match(meta.name):
            meta.unlink(missing_ok=True)
        log(f"  pruned {old.name} (keeping newest {MAX_FRAMES})")


# --- Main loop ------------------------------------------------------------

def main():
    CAPTURES.mkdir(exist_ok=True)

    log("Recovery Card capture started.")
    log(f"  interval={CAPTURE_INTERVAL}s  idle_threshold={IDLE_THRESHOLD}s  "
        f"keep={MAX_FRAMES}  diff_threshold={DIFF_THRESHOLD}")
    log("  Press Ctrl-C to stop.")

    prev_fp = None
    kept = skipped = 0
    card_fired = False  # so one absence triggers exactly one card

    while True:
        cycle_start = time.time()

        idle = idle_seconds()
        if idle >= IDLE_THRESHOLD and not card_fired:
            log(f"Idle for {idle:.0f}s - you stepped away.")
            generate_card(f"idle {idle:.0f}s")
            card_fired = True
        elif idle < 5 and card_fired:
            log("Welcome back. Re-arming the idle trigger.")
            card_fired = False

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp = CAPTURES / f"pending_{stamp}.png"

        if not grab_screen(tmp):
            log("Screenshot failed. Check Screen Recording permission for "
                "your terminal in System Settings > Privacy & Security.")
            tmp.unlink(missing_ok=True)
            time.sleep(CAPTURE_INTERVAL)
            continue

        fp = fingerprint(tmp)
        dist = frame_distance(prev_fp, fp)

        if prev_fp is not None and dist < DIFF_THRESHOLD:
            tmp.unlink(missing_ok=True)
            skipped += 1
            log(f"skip  (diff {dist:.2f} - screen unchanged)   "
                f"kept={kept} skipped={skipped}")
        else:
            final = CAPTURES / f"frame_{stamp}.png"
            tmp.rename(final)

            ctx = window_context()
            ctx["timestamp"] = datetime.now().isoformat(timespec="seconds")
            ctx["frame"] = final.name
            ctx["diff_from_previous"] = round(dist, 2) if prev_fp else None
            final.with_suffix(".json").write_text(json.dumps(ctx, indent=2))

            prev_fp = fp
            kept += 1
            title = ctx["window_title"] or "(untitled window)"
            log(f"KEEP  {final.name}  diff={dist:.2f}  "
                f"{ctx['app']} - {title[:50]}")
            prune()

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, CAPTURE_INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Capture stopped.")

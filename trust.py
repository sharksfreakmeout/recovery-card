#!/usr/bin/env python3
"""Recovery Card - trust layer.

The dashboard's contract: everything it shows is read live from the real
files on disk, and every control is real. No cached claims, no soft
deletes, no "takes effect after restart".

Switches live in trust_settings.json and are read by capture.py on every
cycle, so flipping one off stops that mechanism within one capture
interval, persistently, with no restart.
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / "trust_settings.json"
CAPTURES = ROOT / "captures"
CARDS = ROOT / "cards"
EVAL = ROOT / "eval"
GRAPH = ROOT / "graph.json"

# Every capture mechanism, its plain-English name, and what OFF means.
SWITCHES = {
    "frames":    "Screen frames",
    "titles":    "Window titles",
    "clipboard": "Clipboard snippets",
    "files":     "File activity",
    "ax":        "Accessibility text",
    "chat":      "Chat awareness",
}

DEFAULTS = {k: True for k in SWITCHES}


def read_settings():
    """Live read, every time. The file IS the truth."""
    try:
        s = json.loads(SETTINGS.read_text())
        return {**DEFAULTS, **{k: bool(s.get(k, True)) for k in SWITCHES}}
    except Exception:
        return dict(DEFAULTS)


def set_switch(name, value):
    if name not in SWITCHES:
        raise ValueError(f"unknown switch: {name}")
    s = read_settings()
    s[name] = bool(value)
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(SETTINGS)
    return s


def enabled(name):
    return read_settings().get(name, True)


# --- Private apps (exclusion zones) -----------------------------------------
# Enforced AT CAPTURE, not at inference: while an excluded app is frontmost
# no frame is written, no metadata logged, no clipboard read. The gap is
# total - excluded moments never exist on disk.

PRIVATE = ROOT / "private_apps.json"

# Pre-seeded candidates, but only ones actually present on this Mac, and
# always VISIBLE on the dashboard - a promise the user can see and edit,
# never a hidden default.
_STARTER_CANDIDATES = [
    "Messages", "FaceTime", "Keychain Access", "1Password", "Bitwarden",
    "KeePassXC", "Dashlane", "Signal", "WhatsApp",
]


def _installed(app):
    from pathlib import Path as _P
    return any((_P(base) / f"{app}.app").exists() for base in
               ("/Applications", "/System/Applications",
                "/System/Applications/Utilities"))


def read_private():
    if PRIVATE.exists():
        try:
            d = json.loads(PRIVATE.read_text())
            return {"apps": sorted(set(d.get("apps", []))),
                    "domains": sorted(set(d.get("domains", [])))}
        except Exception:
            pass
    # first run: seed with what is actually installed, visibly
    seeded = {"apps": sorted(a for a in _STARTER_CANDIDATES
                             if _installed(a)),
              "domains": []}
    write_private(seeded)
    return seeded


def write_private(d):
    tmp = PRIVATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"apps": sorted(set(d.get("apps", []))),
         "domains": sorted(set(d.get("domains", [])))}, indent=2))
    tmp.replace(PRIVATE)


def is_private(app, url=""):
    d = read_private()
    if app and app in d["apps"]:
        return True
    if url:
        low = url.lower()
        if any(dom and dom.lower() in low for dom in d["domains"]):
            return True
    return False


# --- Data on hand (live, from disk) ----------------------------------------

def data_on_hand():
    frames = sorted(CAPTURES.glob("frame_*.png")) if CAPTURES.exists() else []
    metas = sorted(CAPTURES.glob("frame_*.json")) if CAPTURES.exists() else []
    cards = sorted(CARDS.glob("card_*.json")) if CARDS.exists() else []
    threads = {}
    if GRAPH.exists():
        try:
            threads = json.loads(GRAPH.read_text()).get("threads", {})
        except Exception:
            pass
    clip_frames = 0
    for m in metas:
        try:
            if json.loads(m.read_text()).get("clipboard"):
                clip_frames += 1
        except Exception:
            pass
    return {
        "frames": len(frames),
        "frame_files": [f.name for f in frames],
        "cards": len(cards),
        "card_files": [c.name for c in cards],
        "threads": len(threads),
        "thread_list": [{"id": t["id"], "name": t["name"]}
                        for t in threads.values()],
        "clipboard_frames": clip_frames,
    }


# --- Deletion (real and immediate) -----------------------------------------

def forget_thread(tid):
    """Remove one thread and everything it holds. Gone means gone."""
    if not GRAPH.exists():
        return False
    try:
        g = json.loads(GRAPH.read_text())
    except Exception:
        return False
    if tid not in g.get("threads", {}):
        return False
    del g["threads"][tid]
    g["nodes"] = [n for n in g.get("nodes", []) if n.get("thread") != tid]
    if g.get("meta", {}).get("active_thread") == tid:
        g["meta"]["active_thread"] = None
    g.get("affinity", {}).pop(tid, None)
    GRAPH.write_text(json.dumps(g, indent=1))
    return True


def delete_captures():
    """Every screenshot and its metadata. The rolling window, emptied."""
    n = 0
    if CAPTURES.exists():
        for p in list(CAPTURES.glob("frame_*")) + \
                 list(CAPTURES.glob("pending_*")):
            p.unlink(missing_ok=True)
            n += 1
        (CAPTURES / "status.json").unlink(missing_ok=True)
    return n


def delete_everything():
    """Captures, cards, threads, corrections, eval marks. All of it.

    The trust settings themselves survive: if the user turned something
    off, deleting their data must not silently turn it back on.
    """
    counts = {"captures": delete_captures(), "cards": 0, "eval": 0}
    if CARDS.exists():
        for p in CARDS.glob("*.json"):
            p.unlink(missing_ok=True)
            counts["cards"] += 1
    if EVAL.exists():
        for p in EVAL.glob("*.json"):
            p.unlink(missing_ok=True)
            counts["eval"] += 1
    if GRAPH.exists():
        GRAPH.unlink()
        counts["graph"] = 1
    return counts

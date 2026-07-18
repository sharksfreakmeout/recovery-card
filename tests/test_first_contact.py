#!/usr/bin/env python3
"""First-contact regressions: the mirror bug, exclusion zones, summon speed.

Run:  .venv/bin/python tests/test_first_contact.py   (engine must be up)
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = os.environ.get("PORT", "5001")


def test_self_detection():
    import capture
    yes = [
        {"window_title": "Recovery Card"},
        {"window_title": "PLite — thread"},
        {"window_title": "", "ax": {"tab": "Recovery Card"}},
        {"window_title": "REHEARSAL"},
    ]
    no = [
        {"window_title": "Gemma Hackathon"},              # our repo in an editor
        {"window_title": "card.py — Gemma Hackathon"},    # editing our code
        {"window_title": "Atlas Mobile — Q3 Launch PRD"},
        {"window_title": "", "ax": {"tab": "Recovering deleted files"}},
    ]
    for m in yes:
        assert capture.is_self_surface(m), f"should be self: {m}"
    for m in no:
        assert not capture.is_self_surface(m), f"should NOT be self: {m}"
    print("PASS  self-surface detection (incl. our-repo-in-editor negative)")


def test_mirror_exclusion():
    """A fixture set including a PLite-surface frame: the self frame must
    never reach inference, even when it is the newest."""
    import card
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_captures = card.CAPTURES
        card.CAPTURES = tmp
        try:
            for name, self_flag in (
                    ("frame_20260718_140000", False),
                    ("frame_20260718_140010", False),
                    ("frame_20260718_140020", True)):   # newest is ours
                (tmp / f"{name}.png").write_bytes(b"png")
                (tmp / f"{name}.json").write_text(json.dumps(
                    {"frame": f"{name}.png", "app": "X",
                     **({"self": True} if self_flag else {})}))
            frames = card.newest_frames(3)
            names = [f.name for f, _ in frames]
            assert "frame_20260718_140020.png" not in names, names
            assert len(names) == 2, names
        finally:
            card.CAPTURES = old_captures
    print("PASS  mirror exclusion: self frame never reaches inference")


def test_exclusion_total_gap():
    """Excluded app frontmost -> zero frames, no metadata, app never named
    in status; menu-bar state is the quiet paused one."""
    sb = Path(tempfile.mkdtemp(prefix="rc_excl_"))
    env = dict(os.environ)
    env.update({"RC_TEST_FRONT_APP": "Messages", "RC_SANDBOX": str(sb),
                "CAPTURE_INTERVAL": "1", "IDLE_THRESHOLD": "9999"})
    # ensure Messages is on the exclusion list for the test
    import trust
    d = trust.read_private()
    added = "Messages" not in d["apps"]
    if added:
        d["apps"].append("Messages")
        trust.write_private(d)
    p = subprocess.Popen([sys.executable, str(ROOT / "capture.py")],
                         env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        st = json.loads((sb / "captures" / "status.json").read_text())
        leaked = glob.glob(str(sb / "captures" / "frame_*"))
        assert not leaked, f"{len(leaked)} frame(s) leaked"
        assert st.get("mode") == "PAUSED_PRIVATE", st.get("mode")
        assert "Messages" not in json.dumps(st), "status names the app"
    finally:
        p.terminate()
        p.wait(timeout=5)
        if added:
            d = trust.read_private()
            d["apps"].remove("Messages")
            trust.write_private(d)
    print("PASS  exclusion: total gap, quiet status, app never named")


def test_summon_speed():
    """With any existing card, summon must surface it in under 1s."""
    cards = sorted(glob.glob(str(ROOT / "cards" / "card_*.json")))
    if not cards:
        print("SKIP  summon speed (no card exists yet)")
        return
    t0 = time.time()
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/summon?overlay=0", method="POST"),
        timeout=5)
    with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/api/state", timeout=5) as r:
        st = json.load(r)
    dt = time.time() - t0
    assert dt < 1.0, f"summon+state took {dt:.2f}s"
    assert st.get("card"), "no card in state after summon"
    print(f"PASS  summon: card data served {dt:.2f}s after summon")


if __name__ == "__main__":
    test_self_detection()
    test_mirror_exclusion()
    test_exclusion_total_gap()
    test_summon_speed()
    print("\nFIRST-CONTACT TESTS: ALL PASS")

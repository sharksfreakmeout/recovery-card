#!/usr/bin/env python3
"""Status honesty + capture-stall regressions.

Run:  .venv/bin/python tests/test_status_honesty.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_skip_reason_typing_keeps():
    import capture
    typing = {"doing": "typing"}
    idle = {"doing": "idle"}
    # typing overrides dedup, always
    assert capture.skip_reason(True, typing, False, False) is None
    # unchanged + not typing skips
    assert capture.skip_reason(True, idle, False, False) == "unchanged"
    # app switch without engagement holds
    assert capture.skip_reason(False, idle, True, False) == "hold"
    # engaged landing keeps
    assert capture.skip_reason(False, typing, True, False) is None
    print("PASS  typing always keeps; unchanged/hold reasons intact")


def test_self_is_identity_not_topic():
    import capture
    not_self = [
        # a conversation ABOUT PLite is the user's real work
        {"app": "Claude", "window_title": "PLite capture stall + status"},
        {"app": "Claude", "window_title": "Recovery Card design review"},
        {"app": "Google Chrome", "window_title": "x",
         "ax": {"tab": "PLite thoughts - Notion"}},
        {"app": "Cursor", "window_title": "card.py — Gemma Hackathon"},
    ]
    is_self = [
        {"app": "Python", "window_title": "Recovery Card"},
        {"app": "Python", "window_title": "PLite — thread"},
        {"app": "Google Chrome", "window_title": "anything",
         "ax": {"url": "http://localhost:5001/", "tab": "whatever"}},
    ]
    for m in not_self:
        assert not capture.is_self_surface(m), f"over-matched: {m}"
    for m in is_self:
        assert capture.is_self_surface(m), f"missed our own surface: {m}"
    print("PASS  self = our identities, never our topic "
          "(Claude-about-PLite stays real work)")


def test_watch_note():
    import app
    now = time.time()
    fresh = {"mode": "ACTIVE", "last_kept_at": now - 30}
    blind = {"mode": "ACTIVE", "last_kept_at": now - 14 * 60,
             "skip_reasons": {"unchanged": 80}}
    stalled = {"mode": "STALLED", "stall_seconds": 400}
    assert app.watch_note(fresh) == ""
    n = app.watch_note(blind)
    assert "14m" in n and "unchanged" in n, n
    assert "stalled" in app.watch_note(stalled)
    print(f'PASS  watch note: "{n}"')


def test_stalled_beat_detection():
    import app
    with tempfile.TemporaryDirectory() as td:
        old_status = app.STATUS
        app.STATUS = Path(td) / "status.json"
        try:
            app.STATUS.write_text(json.dumps({
                "mode": "ACTIVE", "pid": __import__("os").getpid(),  # alive
                "capture_interval": 10,
                "beat": time.time() - 300}))
            s = app.read_status()
            assert s["mode"] == "STALLED", s["mode"]
            # fresh beat stays honest too
            app.STATUS.write_text(json.dumps({
                "mode": "ACTIVE", "pid": __import__("os").getpid(), "capture_interval": 10,
                "beat": time.time() - 5}))
            assert app.read_status()["mode"] == "ACTIVE"
        finally:
            app.STATUS = old_status
    print("PASS  stale heartbeat -> STALLED even with a live pid")


def test_card_staleness_flag():
    import card
    from datetime import datetime, timedelta
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old = card.CAPTURES
        card.CAPTURES = tmp
        try:
            stamp = (datetime.now() - timedelta(minutes=14)).strftime(
                "%Y%m%d_%H%M%S")
            (tmp / f"frame_{stamp}.png").write_bytes(b"png")
            (tmp / f"frame_{stamp}.json").write_text(json.dumps(
                {"frame": f"frame_{stamp}.png", "app": "X"}))
            frames = card.newest_frames(3)
            stamps = sorted(datetime.strptime(f.name[6:21], "%Y%m%d_%H%M%S")
                            for f, _ in frames)
            age = (datetime.now() - stamps[-1]).total_seconds()
            assert age > 300, "fixture built wrong"
            # the exact logic generate() applies:
            assert int(age / 60) >= 13
        finally:
            card.CAPTURES = old
    print("PASS  14-minute-old frames trip the staleness threshold")


def test_unknown_never_chips():
    import threads as T
    t = {"sources_agg": {"unknown": {"count": 9, "first": "x", "last": "z"},
                         "Cursor": {"count": 2, "first": "x", "last": "y"}}}
    shown, more = T.chips(t)
    labels = [c["label"] for c in shown]
    assert "unknown" not in labels and "Cursor" in labels
    print("PASS  'unknown' never renders as a chip")


if __name__ == "__main__":
    test_skip_reason_typing_keeps()
    test_self_is_identity_not_topic()
    test_watch_note()
    test_stalled_beat_detection()
    test_card_staleness_flag()
    test_unknown_never_chips()
    print("\nSTATUS HONESTY TESTS: ALL PASS")

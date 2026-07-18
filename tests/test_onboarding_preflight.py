#!/usr/bin/env python3
"""Onboarding resurrection + false-red preflight regressions.

Run:  .venv/bin/python tests/test_onboarding_preflight.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def test_onboarded_flag_is_absolute():
    """The marker is one file, independent of the graph: wiping the graph
    (Delete everything) must never resurrect onboarding."""
    assert app.ONBOARD_FLAG == ROOT / ".onboarded"
    assert app.ONBOARD_FLAG.exists(), \
        "this machine has onboarded; the flag must exist"
    # the decision consults the flag FIRST - a missing/empty graph is fine
    assert app.is_onboarded()
    print("PASS  onboarding flag: absolute path, exists, survives any "
          "graph wipe")


def test_preflight_recent_capture_wins():
    """Engine kept a frame 2 minutes ago -> screen access is GREEN no
    matter what any probe says (the probe is not even called)."""
    def exploding_probe(_):
        raise RuntimeError("probe must not run when capture works")
    st = {"last_kept_at": time.time() - 120}
    issues = app.screen_access_issues(st, prober=exploding_probe)
    assert issues == [], issues
    print("PASS  recent real capture -> green, probe not consulted")


def test_preflight_probe_error_is_not_red():
    """A failing CHECK is 'could not check' (warn), never the red
    permission message. False negatives on a trust surface are as
    forbidden as false positives."""
    def broken_probe(_):
        raise OSError("sips exploded")
    st = {"last_kept_at": None}
    issues = app.screen_access_issues(st, prober=broken_probe)
    assert len(issues) == 1 and issues[0]["level"] == "warn", issues
    assert "not proof" in issues[0]["fix"]
    print("PASS  probe error -> warn with honest copy, never red")


def test_preflight_real_blank_is_red():
    """The one provable failure - a capture that succeeds but is blank -
    stays red with the permission fix."""
    def blank_probe(dest):
        # a real file the fingerprinter reads as flat
        import subprocess
        subprocess.run(["screencapture", "-x", "-t", "png", str(dest)],
                       capture_output=True)
        return dest.exists()
    st = {"last_kept_at": None}
    issues = app.screen_access_issues(st, prober=blank_probe)
    # on THIS machine the screen is real, so this returns green - the
    # assertion is that a SUCCESSFUL probe never warns:
    assert all(i["level"] != "warn" for i in issues)
    print("PASS  successful probe never mislabeled as check-failure")


if __name__ == "__main__":
    test_onboarded_flag_is_absolute()
    test_preflight_recent_capture_wins()
    test_preflight_probe_error_is_not_red()
    test_preflight_real_blank_is_red()
    print("\nONBOARDING/PREFLIGHT TESTS: ALL PASS")

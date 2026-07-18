#!/usr/bin/env python3
"""Classification truth: no absorption, and editors disambiguate by folder.

Run:  .venv/bin/python tests/test_classification.py   (needs embeddinggemma)
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import threads as T  # noqa: E402

ENGAGED = {"doing": "typing", "keys_ago": 1, "click_ago": 2, "scroll_ago": 2}


def fresh_graph():
    T.GRAPH = Path(tempfile.mkdtemp()) / "graph.json"
    g = T.load()
    T.new_thread(g, "PLite build",
                 anchors=["plite", "gemma hackathon", "recovery card"])
    T.new_thread(g, "Phossil production app",
                 anchors=["phossil", "phossil-production-app"])
    return g


def frame(app, title, titles=None, url=""):
    return {"frame": f"f_{title[:8]}", "app": app, "window_title": title,
            "window_titles": titles or [title], "engagement": ENGAGED,
            "ax": {"url": url} if url else {}}


def test_no_absorption():
    """Shopping while a work thread is active must NOT join it; sustained
    shopping must surface as emergent instead."""
    g = fresh_graph()
    # make PLite the active thread with real momentum
    work = frame("Cursor", "capture.py — Gemma Hackathon")
    for _ in range(4):
        tid, _, _ = T.classify(g, work)
        T.update_affinity(g, tid, work)
    assert g["meta"]["active_thread"] == "plite-build"

    shop_titles = [
        "Skateboard Pads Adult Knee and Elbow — Amazon.com",
        "Wrist guards size guide — Amazon.com",
        "Skateboard protective gear reviews - Google Search",
    ]
    for st in shop_titles:
        tid, tier, _ = T.classify(
            g, frame("Google Chrome", st, url="https://amazon.com/x"))
        assert tid != "plite-build", \
            f"shopping frame absorbed into active work thread (tier {tier})"
        T.update_affinity(g, tid, frame("Google Chrome", st))
    assert g["meta"]["active_thread"] == "plite-build", \
        "shopping displaced the active thread without earning it"
    prop = T.emergent_candidate(g)
    assert prop, "sustained shopping did not surface as emergent"
    print("PASS  no absorption: shopping stayed out of the work thread "
          "and surfaced as emergent")


def test_editor_disambiguation():
    """Two Cursor windows, two projects: the FRONTMOST folder decides,
    even though both titles appear in the frame's full text."""
    g = fresh_graph()
    both = ["capture.py — Gemma Hackathon",
            "Gate_Contract_Crosswalk_Report.md — phossil-production-app"]

    f1 = frame("Cursor", both[0], titles=both)
    tid1, tier1, _ = T.classify(g, f1)
    assert (tid1, tier1) == ("plite-build", 2), (tid1, tier1)

    f2 = frame("Cursor", both[1], titles=both)
    tid2, tier2, _ = T.classify(g, f2)
    assert (tid2, tier2) == ("phossil-production-app", 2), (tid2, tier2)
    print("PASS  editor anchors: frontmost folder token disambiguates two "
          "Cursor windows listing both projects")


def test_folder_token():
    assert T.editor_folder_token(
        {"app": "Cursor", "window_title": "a.py — My Repo"}) == "my repo"
    assert T.editor_folder_token(
        {"app": "Google Chrome", "window_title": "x — y"}) == ""
    print("PASS  folder token extraction (editors only)")


def test_live_fixture_amazon_absorption():
    """The exact live regression: Phossil active, background Cursor window
    titled with the project name, sustained Amazon shopping frontmost.
    Real URLs from the incident as fixture data. Must never absorb; must
    surface as emergent; Phossil's restore list must stay clean."""
    g = fresh_graph()
    work = frame("Cursor",
                 "Gate_Contract_Crosswalk_Report.md — phossil-production-app")
    for _ in range(4):
        tid, _, _ = T.classify(g, work)
        T.update_affinity(g, tid, work)
    assert g["meta"]["active_thread"] == "phossil-production-app"

    background = ["Skateboard Pads Adult - 4 Stars & Up — Amazon.com",
                  "Gate_Contract_Crosswalk_Report.md — phossil-production-app"]
    shop = [
        ("Skateboard Pads Adult - 4 Stars & Up — Amazon.com",
         "https://www.amazon.com/s?k=skateboard+pads+adult"),
        ("JBM Knee Pads Elbow Pads Wrist Guards — Amazon.com",
         "https://www.amazon.com/JBM-Adult-Knee-Elbow-Wrist/dp/B01GST70FK"),
        ("Skateboard Pads Adult - 4 Stars & Up — Amazon.com",
         "https://www.amazon.com/s?k=skateboard+pads+adult&rh=p_72"),
    ]
    for title, url in shop:
        f = frame("Google Chrome", title, titles=background, url=url)
        tid, tier, _ = T.classify(g, f)
        assert tid != "phossil-production-app", \
            f"ABSORBED via tier {tier} (background-title anchor leak)"
        T.update_affinity(g, tid, f)

    phossil = g["threads"]["phossil-production-app"]
    assert not any("amazon" in u for u in phossil.get("recent_urls", [])), \
        f"amazon URLs in Phossil restore list: {phossil['recent_urls']}"
    assert T.emergent_candidate(g), "sustained shopping not offered as emergent"
    print("PASS  live fixture: Amazon shopping never absorbed, restore "
          "list clean, emergent offered")


def test_chips_require_fresh_engagement():
    """A brief frontmost appearance with carried-over engagement must not
    produce a chip. keys_ago=8 = typed in the PREVIOUS app."""
    g = fresh_graph()
    stale = {"doing": "typing", "keys_ago": 8, "click_ago": 60,
             "scroll_ago": 60}
    f = {"frame": "c1", "app": "Claude", "window_title": "phossil notes",
         "window_titles": ["phossil notes"], "engagement": stale, "ax": {}}
    tid, _, _ = T.classify(g, f)     # anchors match "phossil" frontmost
    T.update_affinity(g, tid, f)
    t = g["threads"]["phossil-production-app"]
    assert "Claude" not in (t.get("sources_agg") or {}), \
        "passing frontmost with stale engagement earned a chip"
    fresh = {"doing": "typing", "keys_ago": 1, "click_ago": 60,
             "scroll_ago": 60}
    f["engagement"] = fresh
    tid, _, _ = T.classify(g, f)
    T.update_affinity(g, tid, f)
    assert "Claude" in (t.get("sources_agg") or {}), \
        "fresh engagement should earn the chip"
    print("PASS  chips require fresh engagement (stale carryover ignored)")


if __name__ == "__main__":
    test_folder_token()
    test_editor_disambiguation()
    test_no_absorption()
    test_live_fixture_amazon_absorption()
    test_chips_require_fresh_engagement()
    print("\nCLASSIFICATION TESTS: ALL PASS")

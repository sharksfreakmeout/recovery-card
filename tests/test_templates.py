#!/usr/bin/env python3
"""Template sanity: no served page may ever show its own JS as text.

The regression this guards: a script block that terminates early (an
unclosed or misplaced <script> during template assembly) prints the rest
of the page's JavaScript as visible text, the data fetch never runs, and
every surface shows the same corpse skeleton.

Checks, per surface:
  - <script> and </script> counts match exactly
  - after removing script/style blocks, the visible text contains zero
    JS signatures (addEventListener, setInterval, function, =>, fetch()
  - the board page carries the headline element AND in-script code that
    populates it; /api/board serves a non-empty headline for it

Run:  .venv/bin/python tests/test_templates.py   (engine must be up)
"""

import re
import sys
import urllib.request

PORT = "5001"
PAGES = ["/", "/overlay", "/engine", "/trust"]
JS_SIGNS = ["addEventListener", "setInterval(", "async function",
            "=> {", "document.getElementById", "fetch("]


def fetch(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}",
                                timeout=5) as r:
        return r.read().decode()


def visible_text(html):
    html = re.sub(r"<script\b.*?</script>", "", html,
                  flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def main():
    failures = []
    for path in PAGES:
        try:
            html = fetch(path)
        except Exception as e:
            failures.append(f"{path}: unreachable ({e})")
            continue

        opens = len(re.findall(r"<script\b", html, re.I))
        closes = len(re.findall(r"</script>", html, re.I))
        if opens != closes:
            failures.append(f"{path}: {opens} <script> vs {closes} "
                            f"</script> - a block is broken")

        vis = visible_text(html)
        leaked = [s for s in JS_SIGNS if s in vis]
        if leaked:
            failures.append(f"{path}: JS visible as text: {leaked}")

        if not failures or all(not f.startswith(path) for f in failures):
            print(f"  PASS  {path}  ({opens} script blocks, no JS leakage)")

    # The board must be wired to populate its headline.
    board = fetch("/")
    if 'id="headline"' not in board:
        failures.append("board: headline element missing")
    if "tickBoard" not in board:
        failures.append("board: headline population code missing")
    try:
        import json
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/board", timeout=5) as r:
            h = json.load(r).get("headline", "")
        if not h.strip():
            failures.append("/api/board: empty headline")
        else:
            print(f"  PASS  headline feed: \"{h[:60]}\"")
    except Exception as e:
        failures.append(f"/api/board unreachable ({e})")

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        sys.exit(1)
    print("TEMPLATES: ALL PASS")


if __name__ == "__main__":
    main()
